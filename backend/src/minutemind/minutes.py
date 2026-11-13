"""Meeting minutes written by a language model from the transcript.

The model answers in JSON constrained by MINUTES_SCHEMA (llama.cpp turns the
schema into a grammar), and the answer is then checked with pydantic. Meetings
too long for one request are split into parts: each part gets its own minutes,
and those are combined in a final request (in rounds, if even they don't fit).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from pydantic import BaseModel, ValidationError

from minutemind import hardware, store
from minutemind.llm.base import LanguageModel

log = logging.getLogger(__name__)

# Rough English-text ratio for budgeting. Transcripts with timestamps tokenize
# worse than prose, so this errs on the safe side.
CHARS_PER_TOKEN = 3.0
ANSWER_TOKENS = 1500
PROMPT_OVERHEAD_TOKENS = 700
# Consecutive lines by one person within this gap are joined to save room.
JOIN_GAP_MS = 20_000
MAX_ITEMS = 20
ATTEMPTS = 2

YOU = "You"
OTHERS = "Others"

MINUTES_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "summary": {"type": "string"},
        "decisions": {"type": "array", "items": {"type": "string"}, "maxItems": MAX_ITEMS},
        "action_items": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "items": {
                "type": "object",
                "properties": {
                    "task": {"type": "string"},
                    "owner": {"type": "string"},
                    "due": {"type": "string"},
                },
                "required": ["task", "owner", "due"],
            },
        },
        "topics": {
            "type": "array",
            "maxItems": MAX_ITEMS,
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "at": {"type": "string"}},
                "required": ["title", "at"],
            },
        },
    },
    "required": ["summary", "decisions", "action_items", "topics"],
}

SYSTEM_PROMPT = (
    "You write accurate, concise meeting minutes from a transcript. "
    "Use only what the transcript says: never invent names, numbers, dates, "
    "decisions or tasks. Keep names, figures and dates exactly as spoken. "
    'The person called "You" recorded the meeting. '
    "Answer with JSON only."
)

FIELD_GUIDE = """Fill in:
- summary: 2 to 6 sentences on what the meeting covered and concluded.
- decisions: each thing the group agreed or decided, as one short sentence. Use [] if nothing was decided.
- action_items: each task someone committed to do. "task" says what will be done; "owner" is who will do it, written as in the transcript ("You" for the person who recorded), or "" if nobody was named; "due" is the deadline as said (for example "Friday" or "14 March"), or "" if none.
- topics: the main subjects in the order discussed, each with "at", the [mm:ss] time where it starts, copied from the transcript."""

COMBINE_GUIDE = """These are minutes for consecutive parts of one meeting. Combine them into minutes for the whole meeting:
- summary: one summary of the whole meeting, 3 to 6 sentences.
- decisions and action_items: keep every distinct one, merging duplicates. Keep owners and due dates.
- topics: merge into one list in time order, keeping the earliest "at" for each."""


class ActionItem(BaseModel):
    task: str
    owner: str = ""
    due: str = ""


class Topic(BaseModel):
    title: str
    at: str = ""


class RawMinutes(BaseModel):
    """The model's answer, as checked."""

    summary: str
    decisions: list[str]
    action_items: list[ActionItem]
    topics: list[Topic]


@dataclass(frozen=True)
class Line:
    start_ms: int
    end_ms: int
    who: str
    text: str


@dataclass
class MeetingContext:
    title: str
    created_at: str
    duration_ms: int
    lines: list[Line]
    notes: list[tuple[int, str]] = field(default_factory=list)  # (ts_ms, text)
    people: list[str] = field(default_factory=list)


def build_context(db: store.Store, meeting_id: int) -> MeetingContext:
    meeting = db.get_meeting(meeting_id)
    if meeting is None:
        raise ValueError(f"Meeting {meeting_id} doesn't exist")
    names = {s["label"]: s["display_name"] for s in db.list_speakers(meeting_id)}
    lines = [
        Line(s.start_ms, s.end_ms, _speaker_name(s, names), s.text.strip())
        for s in db.get_segments(meeting_id)
        if s.text.strip()
    ]
    people: list[str] = []
    for line in lines:
        if line.who not in people:
            people.append(line.who)
    return MeetingContext(
        title=meeting.title,
        created_at=meeting.created_at,
        duration_ms=meeting.duration_ms,
        lines=join_lines(lines),
        notes=[(n.ts_ms, n.text) for n in db.list_notes(meeting_id)],
        people=people,
    )


def _speaker_name(segment: store.Segment, names: dict[str, str]) -> str:
    if segment.source == "mic":
        return YOU
    if segment.speaker:
        return names.get(segment.speaker, f"Speaker {segment.speaker[1:]}")
    return OTHERS


def join_lines(lines: list[Line]) -> list[Line]:
    """Merge back-to-back lines by the same person, so the prompt carries
    fewer timestamps and the model sees whole thoughts."""
    joined: list[Line] = []
    for line in sorted(lines, key=lambda l: l.start_ms):
        last = joined[-1] if joined else None
        if last and last.who == line.who and line.start_ms - last.end_ms <= JOIN_GAP_MS:
            joined[-1] = Line(last.start_ms, max(last.end_ms, line.end_ms), last.who, f"{last.text} {line.text}")
        else:
            joined.append(line)
    return joined


def clock(ms: int) -> str:
    total = max(0, ms) // 1000
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def parse_clock(text: str) -> int | None:
    match = re.fullmatch(r"\[?\s*(?:(\d{1,2}):)?(\d{1,3}):(\d{2})\s*\]?", text.strip())
    if not match:
        return None
    hours, minutes, seconds = (int(g) if g else 0 for g in match.groups())
    if seconds >= 60:
        return None
    return ((hours * 60 + minutes) * 60 + seconds) * 1000


def format_line(line: Line) -> str:
    return f"[{clock(line.start_ms)}] {line.who}: {line.text}"


ProgressCallback = Callable[[float], None]


class MinutesWriter:
    def __init__(self, model: LanguageModel) -> None:
        self.model = model

    @property
    def model_name(self) -> str:
        return self.model.name

    def ensure_model(self, on_progress: ProgressCallback) -> None:
        self.model.ensure_model(on_progress)

    def release(self) -> None:
        self.model.release()

    def input_budget_chars(self) -> int:
        tokens = self.model.context_tokens - ANSWER_TOKENS - PROMPT_OVERHEAD_TOKENS
        return max(2000, int(tokens * CHARS_PER_TOKEN))

    def write(
        self,
        ctx: MeetingContext,
        profile: hardware.Profile,
        on_progress: ProgressCallback = lambda _p: None,
    ) -> dict:
        """Write minutes for a meeting. Returns the stored form (see `finish`)."""
        if not ctx.lines:
            raise ValueError("The transcript is empty, so there's nothing to write minutes from")
        parts = split_lines(ctx.lines, self.input_budget_chars())
        rounds_guess = len(parts) + (1 if len(parts) > 1 else 0)
        step = 0

        def advance() -> None:
            nonlocal step
            step += 1
            on_progress(min(0.99, step / max(1, rounds_guess)))

        on_progress(0.0)
        partials: list[RawMinutes] = []
        for index, part in enumerate(parts):
            prompt = self._part_prompt(ctx, part, index, len(parts))
            partials.append(self._ask(prompt, profile))
            advance()

        while len(partials) > 1:
            groups = group_partials(partials, self.input_budget_chars())
            if len(groups) == len(partials):
                # Not even two partial minutes fit together: merge without the model.
                partials = [merge_mechanically(partials)]
                break
            partials = [
                group[0] if len(group) == 1 else self._ask(self._combine_prompt(ctx, group), profile)
                for group in groups
            ]
            advance()

        on_progress(1.0)
        return finish(partials[0], ctx)

    # -- prompts -------------------------------------------------------------

    def _header(self, ctx: MeetingContext) -> str:
        people = ", ".join(ctx.people) or YOU
        return (
            f"Meeting: {ctx.title}\n"
            f"Date: {_pretty_date(ctx.created_at)}\n"
            f"Length: {clock(ctx.duration_ms)}\n"
            f"People speaking: {people}\n"
        )

    def _part_prompt(self, ctx: MeetingContext, part: list[Line], index: int, total: int) -> str:
        which = "" if total == 1 else f" (part {index + 1} of {total})"
        start, end = part[0].start_ms, part[-1].end_ms
        notes = [
            f"[{clock(ts)}] {text}"
            for ts, text in ctx.notes
            if total == 1 or start <= ts <= end or (index == total - 1 and ts > end)
        ]
        transcript = "\n".join(format_line(line) for line in part)
        notes_block = (
            "\nNotes You typed during the meeting:\n" + "\n".join(notes) + "\n" if notes else ""
        )
        return (
            f"{self._header(ctx)}\nTranscript{which}:\n{transcript}\n{notes_block}\n{FIELD_GUIDE}"
        )

    def _combine_prompt(self, ctx: MeetingContext, group: list[RawMinutes]) -> str:
        blocks = "\n\n".join(
            f"Part {i + 1}:\n{minutes.model_dump_json()}" for i, minutes in enumerate(group)
        )
        return f"{self._header(ctx)}\n{blocks}\n\n{COMBINE_GUIDE}"

    def _ask(self, prompt: str, profile: hardware.Profile) -> RawMinutes:
        last_error: Exception | None = None
        for attempt in range(ATTEMPTS):
            answer = self.model.complete_json(
                SYSTEM_PROMPT, prompt, MINUTES_SCHEMA, profile, ANSWER_TOKENS
            )
            try:
                return RawMinutes.model_validate(json.loads(answer))
            except (json.JSONDecodeError, ValidationError) as exc:
                log.warning("Minutes answer didn't match the schema (attempt %d): %s", attempt + 1, exc)
                last_error = exc
        raise ValueError(f"The model's answer wasn't valid minutes: {last_error}")


def split_lines(lines: list[Line], budget_chars: int) -> list[list[Line]]:
    """Split the transcript into parts that each fit the budget, cutting
    between lines (a line longer than the budget becomes its own part)."""
    parts: list[list[Line]] = [[]]
    size = 0
    for line in lines:
        length = len(format_line(line)) + 1
        if parts[-1] and size + length > budget_chars:
            parts.append([])
            size = 0
        parts[-1].append(line)
        size += length
    return parts


def group_partials(partials: list[RawMinutes], budget_chars: int) -> list[list[RawMinutes]]:
    groups: list[list[RawMinutes]] = [[]]
    size = 0
    for minutes in partials:
        length = len(minutes.model_dump_json()) + 12
        if groups[-1] and size + length > budget_chars:
            groups.append([])
            size = 0
        groups[-1].append(minutes)
        size += length
    return groups


def merge_mechanically(partials: list[RawMinutes]) -> RawMinutes:
    return RawMinutes(
        summary=" ".join(p.summary.strip() for p in partials if p.summary.strip()),
        decisions=[d for p in partials for d in p.decisions],
        action_items=[a for p in partials for a in p.action_items],
        topics=[t for p in partials for t in p.topics],
    )


def finish(raw: RawMinutes, ctx: MeetingContext) -> dict:
    """Tidy the model's answer into the stored form: trimmed text, duplicates
    removed, empty owners/dates as null, and topic times in milliseconds."""
    decisions = _unique(d.strip() for d in raw.decisions)
    seen: set[str] = set()
    actions = []
    for item in raw.action_items:
        task = item.task.strip()
        key = _key(task)
        if not task or key in seen:
            continue
        seen.add(key)
        actions.append(
            {"task": task, "owner": _blank_to_none(item.owner), "due": _blank_to_none(item.due)}
        )
    topics = []
    seen_topics: set[str] = set()
    for topic in raw.topics:
        title = topic.title.strip()
        if not title or _key(title) in seen_topics:
            continue
        seen_topics.add(_key(title))
        start_ms = parse_clock(topic.at) if topic.at else None
        if start_ms is not None and ctx.duration_ms and start_ms > ctx.duration_ms:
            start_ms = None  # the model made up a time past the end
        topics.append({"title": title, "start_ms": start_ms})
    return {
        "summary": raw.summary.strip(),
        "decisions": decisions,
        "action_items": actions,
        "topics": topics,
    }


_NONE_WORDS = {"", "none", "n/a", "na", "unknown", "unspecified", "not specified", "-", "null"}


def _blank_to_none(value: str) -> str | None:
    value = value.strip()
    return None if value.lower() in _NONE_WORDS else value


def _key(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", text.lower()).strip()


def _unique(items) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in items:
        if item and _key(item) not in seen:
            seen.add(_key(item))
            out.append(item)
    return out


def _pretty_date(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso).astimezone().strftime("%d %B %Y, %H:%M")
    except ValueError:
        return iso


def to_markdown(meeting: store.Meeting, minutes: dict) -> str:
    """The minutes as a Markdown document, for export."""
    out = [f"# {meeting.title}", ""]
    out.append(f"{_pretty_date(meeting.created_at)} · {clock(meeting.duration_ms)}")
    out += ["", "## Summary", "", minutes.get("summary") or "_No summary._", ""]

    out += ["## Decisions", ""]
    decisions = minutes.get("decisions") or []
    out += [f"- {d}" for d in decisions] or ["_No decisions were recorded._"]
    out.append("")

    out += ["## Action items", ""]
    actions = minutes.get("action_items") or []
    for item in actions:
        extra = []
        if item.get("owner"):
            extra.append(f"**{item['owner']}**")
        if item.get("due"):
            extra.append(f"due {item['due']}")
        suffix = f" ({', '.join(extra)})" if extra else ""
        out.append(f"- [ ] {item['task']}{suffix}")
    if not actions:
        out.append("_No action items._")
    out.append("")

    topics = minutes.get("topics") or []
    if topics:
        out += ["## Topics", ""]
        for topic in topics:
            at = f"`{clock(topic['start_ms'])}` " if topic.get("start_ms") is not None else ""
            out.append(f"- {at}{topic['title']}")
        out.append("")
    return "\n".join(out)
