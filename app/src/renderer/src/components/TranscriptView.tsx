import { useEffect, useRef, useState } from 'react'
import type { Meeting, Note, Speaker, Transcript, TranscriptSegment } from '@shared/models'
import { useBackend, useTranscriptionJobs } from '../lib/contexts'
import { formatClock } from '../lib/format'

/** Display name for a spoken line: "You", a detected person, or "Others". */
function speakerName(seg: TranscriptSegment, names: Map<string, string>): string {
  if (seg.source === 'mic') return 'You'
  return (seg.speaker && names.get(seg.speaker)) || 'Others'
}

interface TranscriptViewProps {
  meeting: Meeting
  /** Playback position, to highlight the line being heard. */
  positionMs: number
  onSeek: (ms: number) => void
  /** Called when a transcript starts, so the meeting list can refresh. */
  onRequested: () => void
}

export function TranscriptView({
  meeting,
  positionMs,
  onSeek,
  onRequested
}: TranscriptViewProps): React.JSX.Element {
  const { client } = useBackend()
  const jobs = useTranscriptionJobs()
  const [transcript, setTranscript] = useState<Transcript | null>(null)
  const [notes, setNotes] = useState<Note[]>([])
  const [error, setError] = useState<string | null>(null)

  const job = jobs.current?.meeting_id === meeting.id ? jobs.current : null
  // Writing minutes happens after the transcript is saved, so it stays on screen.
  const writingMinutes = job !== null && job.stage.startsWith('minutes')
  // Detecting speakers again keeps the transcript on screen too.
  const redetecting = job?.kind === 'speakers'
  const keepShowing = redetecting || writingMinutes
  const queuedAt = jobs.queued.indexOf(meeting.id)
  const queuedForMinutes =
    queuedAt >= 0 && meeting.transcript_status === 'done' && meeting.minutes_status === 'queued'
  // Refetch whenever this meeting's job starts, moves on or finishes.
  const jobKey = writingMinutes
    ? 'minutes'
    : job
      ? `running-${job.kind}`
      : queuedAt >= 0 && !queuedForMinutes
        ? 'queued'
        : `${meeting.transcript_status}-${meeting.speakers_status}`

  useEffect(() => {
    if (!client) return
    let active = true
    client.get<Transcript>(`/meetings/${meeting.id}/transcript`).then(
      (t) => {
        if (active) setTranscript(t)
      },
      (err: Error) => {
        if (active) setError(err.message)
      }
    )
    return () => {
      active = false
    }
  }, [client, meeting.id, jobKey])

  useEffect(() => {
    if (!client) return
    let active = true
    client.get<Note[]>(`/meetings/${meeting.id}/notes`).then(
      (list) => {
        if (active) setNotes(list)
      },
      (err: Error) => {
        if (active) setError(err.message)
      }
    )
    return () => {
      active = false
    }
  }, [client, meeting.id])

  const request = (): void => {
    if (!client) return
    setError(null)
    client
      .post(`/meetings/${meeting.id}/transcribe`)
      .then(onRequested, (err: Error) => setError(err.message))
  }

  const notesApi: NotesApi = {
    add: (text, tsMs) => {
      if (!client) return
      client.post<Note>(`/meetings/${meeting.id}/notes`, { text, ts_ms: tsMs }).then(
        (note) => setNotes((list) => sortNotes([...list, note])),
        (err: Error) => setError(err.message)
      )
    },
    edit: (id, text) => {
      if (!client) return
      client.patch<Note>(`/notes/${id}`, { text }).then(
        (note) => setNotes((list) => list.map((n) => (n.id === id ? note : n))),
        (err: Error) => setError(err.message)
      )
    },
    remove: (id) => {
      if (!client) return
      client.delete(`/notes/${id}`).then(
        () => setNotes((list) => list.filter((n) => n.id !== id)),
        (err: Error) => setError(err.message)
      )
    }
  }

  const speakersApi: SpeakersApi = {
    rename: (label, name) => {
      if (!client) return
      client
        .patch<Speaker[]>(`/meetings/${meeting.id}/speakers/${label}`, { display_name: name })
        .then(
          (speakers) => setTranscript((t) => (t ? { ...t, speakers } : t)),
          (err: Error) => setError(err.message)
        )
    },
    detect: (count) => {
      if (!client) return
      setError(null)
      client
        .post(`/meetings/${meeting.id}/speakers/detect`, { num_speakers: count })
        .then(onRequested, (err: Error) => setError(err.message))
    }
  }

  const status =
    job && !keepShowing
      ? 'running'
      : queuedAt >= 0 && !keepShowing && !queuedForMinutes
        ? 'queued'
        : transcript?.status
  const segments = status === 'done' && transcript ? transcript.segments : []
  const names = new Map((transcript?.speakers ?? []).map((sp) => [sp.label, sp.display_name]))

  return (
    <section aria-label="Transcript" className="space-y-4 pt-2">
      <div className="flex items-baseline justify-between gap-4">
        <p className="eyebrow">Transcript and notes</p>
        {status === 'done' && transcript && (
          <p className="font-mono text-[10px] uppercase tracking-wider text-paper-700">
            {transcript.model} · {transcript.device}
          </p>
        )}
      </div>

      {job && (status === 'running' || redetecting) && (
        <JobProgress stage={job.stage} progress={job.progress} />
      )}

      {status === 'queued' && (
        <p className="text-sm text-paper-500">
          {jobs.paused
            ? 'Waits until the recording in progress stops.'
            : `Waiting to transcribe${
                queuedAt > 0 ? `, ${queuedAt} ${queuedAt === 1 ? 'meeting' : 'meetings'} ahead` : ''
              }…`}
        </p>
      )}

      {(status === 'none' || status === undefined) && (
        <div className="flex items-center gap-4">
          <p className="text-sm text-paper-500">No transcript yet.</p>
          <ActionButton onClick={request}>Transcribe</ActionButton>
        </div>
      )}

      {status === 'failed' && (
        <div className="space-y-2">
          <p className="text-sm text-signal">
            Transcription failed{transcript?.error ? `: ${transcript.error}` : '.'}
          </p>
          <ActionButton onClick={request}>Try again</ActionButton>
        </div>
      )}

      {status === 'done' && segments.length === 0 && (
        <p className="text-sm text-paper-500">No speech was found in this recording.</p>
      )}

      {status === 'done' && transcript && segments.some((sg) => sg.source === 'system') && (
        <SpeakersBar
          transcript={transcript}
          busy={job !== null || queuedAt >= 0}
          api={speakersApi}
        />
      )}

      {(segments.length > 0 || notes.length > 0) && (
        <Lines
          segments={segments}
          names={names}
          notes={notes}
          positionMs={positionMs}
          onSeek={onSeek}
          notesApi={notesApi}
        />
      )}

      <AddNote positionMs={positionMs} onAdd={notesApi.add} />

      {status === 'done' && !job && queuedAt < 0 && (
        <ActionButton onClick={request}>Transcribe again</ActionButton>
      )}

      {error && <p className="text-xs text-signal">{error}</p>}
    </section>
  )
}

interface NotesApi {
  add: (text: string, tsMs: number) => void
  edit: (id: number, text: string) => void
  remove: (id: number) => void
}

function sortNotes(list: Note[]): Note[] {
  return [...list].sort((a, b) => a.ts_ms - b.ts_ms || a.id - b.id)
}

function JobProgress({ stage, progress }: { stage: string; progress: number }): React.JSX.Element {
  const label =
    stage === 'downloading'
      ? 'Downloading the speech model (one time only, about 1.5 GB)'
      : stage === 'transcribing'
        ? 'Transcribing'
        : stage === 'speakers'
          ? 'Telling the people on the call apart'
          : 'Preparing'
  const pct = Math.round(progress * 100)
  return (
    <div className="space-y-2" role="status">
      <div className="flex items-baseline justify-between text-sm">
        <span className="text-paper-300">{label}</span>
        <span className="font-mono text-xs tabular-nums text-paper-500">{pct}%</span>
      </div>
      <div className="h-0.5 w-full bg-line">
        <div className="h-full bg-paper-300 transition-[width]" style={{ width: `${pct}%` }} />
      </div>
    </div>
  )
}

type Item =
  | { kind: 'segment'; ms: number; key: string; segment: TranscriptSegment }
  | { kind: 'note'; ms: number; key: string; note: Note }

interface SpeakersApi {
  rename: (label: string, name: string) => void
  detect: (count: number | null) => void
}

/** People on the call: click a name to rename. Giving two the same name merges them. */
function SpeakersBar({
  transcript,
  busy,
  api
}: {
  transcript: Transcript
  busy: boolean
  api: SpeakersApi
}): React.JSX.Element {
  const [count, setCount] = useState('auto')

  return (
    <div className="space-y-3 rounded-md border border-line bg-ink-850/60 p-3">
      <div className="flex flex-wrap items-center gap-2">
        <span className="eyebrow mr-1">People</span>
        <span className="rounded-sm bg-ink-800 px-2 py-1 text-xs text-paper-300">You</span>
        {transcript.speakers.map((sp) => (
          <SpeakerChip
            key={sp.label}
            speaker={sp}
            onRename={(name) => api.rename(sp.label, name)}
          />
        ))}
        {transcript.speakers_status !== 'done' && (
          <span className="text-xs text-paper-500">
            {transcript.speakers_status === 'failed'
              ? `Couldn't tell people apart${transcript.speakers_error ? `: ${transcript.speakers_error}` : ''}.`
              : 'Not separated by person.'}
          </span>
        )}
      </div>
      <div className="flex flex-wrap items-center gap-2 text-xs text-paper-500">
        <span>Wrong?</span>
        <select
          value={count}
          onChange={(e) => setCount(e.target.value)}
          disabled={busy}
          aria-label="Number of other people on the call"
          className="rounded-sm border border-line-strong bg-ink-900 px-1.5 py-0.5 text-paper-100 focus:border-paper-500 focus:outline-none"
        >
          <option value="auto">Count automatically</option>
          {[1, 2, 3, 4, 5, 6, 7, 8].map((n) => (
            <option key={n} value={String(n)}>
              {n} other {n === 1 ? 'person' : 'people'}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={busy}
          onClick={() => api.detect(count === 'auto' ? null : Number(count))}
          className="font-mono text-[11px] uppercase tracking-wider text-paper-500 hover:text-paper-100 disabled:opacity-40"
        >
          {busy ? 'Working…' : 'Detect again'}
        </button>
        <span className="text-paper-700">Tip: give two speakers the same name to merge them.</span>
      </div>
    </div>
  )
}

function SpeakerChip({
  speaker,
  onRename
}: {
  speaker: Speaker
  onRename: (name: string) => void
}): React.JSX.Element {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(speaker.display_name)

  const save = (): void => {
    const name = draft.trim()
    setEditing(false)
    if (name && name !== speaker.display_name) onRename(name)
    else setDraft(speaker.display_name)
  }

  if (editing) {
    return (
      <input
        autoFocus
        value={draft}
        maxLength={80}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={save}
        onKeyDown={(e) => {
          if (e.key === 'Enter') save()
          if (e.key === 'Escape') {
            setDraft(speaker.display_name)
            setEditing(false)
          }
        }}
        aria-label={`Rename ${speaker.display_name}`}
        className="w-36 rounded-sm border border-paper-500 bg-ink-900 px-2 py-1 text-xs text-paper-50 focus:outline-none"
      />
    )
  }
  return (
    <button
      type="button"
      onClick={() => {
        setDraft(speaker.display_name)
        setEditing(true)
      }}
      title="Click to rename"
      className="rounded-sm border border-line-strong px-2 py-1 text-xs text-paper-100 hover:border-paper-500"
    >
      {speaker.display_name}
      <span className="ml-1.5 font-mono text-[10px] text-paper-700">{speaker.lines}</span>
    </button>
  )
}

interface LinesProps {
  segments: TranscriptSegment[]
  names: Map<string, string>
  notes: Note[]
  positionMs: number
  onSeek: (ms: number) => void
  notesApi: NotesApi
}

/** Spoken lines and notes, merged in meeting order. */
function Lines({
  segments,
  names,
  notes,
  positionMs,
  onSeek,
  notesApi
}: LinesProps): React.JSX.Element {
  const items: Item[] = [
    ...segments.map((s) => ({
      kind: 'segment' as const,
      ms: s.start_ms,
      key: `s${s.id}`,
      segment: s
    })),
    ...notes.map((n) => ({ kind: 'note' as const, ms: n.ts_ms, key: `n${n.id}`, note: n }))
  ].sort((a, b) => a.ms - b.ms || (a.kind === 'segment' ? -1 : 1))

  // The spoken line being heard: the last one that has started.
  let current = -1
  items.forEach((item, i) => {
    if (item.kind === 'segment' && item.ms <= positionMs) current = i
  })
  const currentRef = useRef<HTMLLIElement>(null)
  useEffect(() => {
    currentRef.current?.scrollIntoView({ block: 'nearest' })
  }, [current])

  // Label a spoken line with its speaker only where the speaker changes
  // (a note in between also starts a new run).
  const showSpeaker = items.map((item, i) => {
    if (item.kind !== 'segment') return false
    const prev = items[i - 1]
    return (
      !prev ||
      prev.kind !== 'segment' ||
      speakerName(prev.segment, names) !== speakerName(item.segment, names)
    )
  })

  return (
    <ol className="max-h-[28rem] overflow-y-auto border-y border-line">
      {items.map((item, i) => {
        if (item.kind === 'note') {
          return (
            <li key={item.key}>
              <NoteLine note={item.note} onSeek={onSeek} notesApi={notesApi} />
            </li>
          )
        }
        const seg = item.segment
        const active = i === current && positionMs > 0
        return (
          <li key={item.key} ref={i === current ? currentRef : undefined}>
            <button
              type="button"
              onClick={() => onSeek(seg.start_ms)}
              className={`grid w-full grid-cols-[52px_96px_1fr] gap-4 px-2 py-2 text-left text-sm leading-relaxed transition-colors ${
                active ? 'bg-ink-800 text-paper-50' : 'text-paper-300 hover:bg-ink-850'
              }`}
            >
              <span className="pt-0.5 font-mono text-[11px] tabular-nums text-paper-500">
                {formatClock(seg.start_ms).replace(/^00:/, '')}
              </span>
              <span
                className="truncate pt-0.5 font-mono text-[11px] uppercase tracking-wider text-paper-500"
                title={speakerName(seg, names)}
              >
                {showSpeaker[i] ? speakerName(seg, names) : ''}
              </span>
              <span>{seg.text}</span>
            </button>
          </li>
        )
      })}
    </ol>
  )
}

interface NoteLineProps {
  note: Note
  onSeek: (ms: number) => void
  notesApi: NotesApi
}

function NoteLine({ note, onSeek, notesApi }: NoteLineProps): React.JSX.Element {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState(note.text)

  const save = (): void => {
    const text = draft.trim()
    setEditing(false)
    if (text && text !== note.text) notesApi.edit(note.id, text)
    else setDraft(note.text)
  }

  return (
    <div className="group grid grid-cols-[52px_96px_1fr_auto] gap-4 border-l border-paper-500 bg-ink-850/60 px-2 py-2 text-sm">
      <button
        type="button"
        onClick={() => onSeek(note.ts_ms)}
        className="pt-0.5 text-left font-mono text-[11px] tabular-nums text-paper-500 hover:text-paper-100"
        aria-label={`Play from ${formatClock(note.ts_ms)}`}
      >
        {formatClock(note.ts_ms).replace(/^00:/, '')}
      </button>
      <span className="pt-0.5 font-mono text-[11px] uppercase tracking-wider text-paper-300">
        Note
      </span>
      {editing ? (
        <textarea
          autoFocus
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={save}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && !e.shiftKey) {
              e.preventDefault()
              save()
            }
            if (e.key === 'Escape') {
              setDraft(note.text)
              setEditing(false)
            }
          }}
          rows={2}
          aria-label="Edit note"
          className="w-full resize-none border-b border-paper-500 bg-transparent text-paper-50 focus:outline-none"
        />
      ) : (
        <span className="whitespace-pre-wrap leading-relaxed text-paper-100">{note.text}</span>
      )}
      <span className="flex gap-3 opacity-0 focus-within:opacity-100 group-hover:opacity-100">
        <button
          type="button"
          onClick={() => setEditing(true)}
          className="font-mono text-[10px] uppercase tracking-wider text-paper-500 hover:text-paper-100"
        >
          Edit
        </button>
        <button
          type="button"
          onClick={() => notesApi.remove(note.id)}
          className="font-mono text-[10px] uppercase tracking-wider text-paper-500 hover:text-signal"
        >
          Delete
        </button>
      </span>
    </div>
  )
}

function AddNote({
  positionMs,
  onAdd
}: {
  positionMs: number
  onAdd: (text: string, tsMs: number) => void
}): React.JSX.Element {
  const [draft, setDraft] = useState('')
  const at = Math.floor(positionMs)

  const add = (): void => {
    const text = draft.trim()
    if (!text) return
    onAdd(text, at)
    setDraft('')
  }

  return (
    <div className="flex items-center gap-3">
      <span className="w-[52px] shrink-0 font-mono text-[11px] tabular-nums text-paper-500">
        {formatClock(at).replace(/^00:/, '')}
      </span>
      <input
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') add()
        }}
        maxLength={5000}
        placeholder="Add a note at the player's position, press Enter"
        aria-label="Add a note"
        className="flex-1 border-b border-line-strong bg-transparent pb-1 text-sm text-paper-100 placeholder:text-paper-700 focus:border-paper-500 focus:outline-none"
      />
    </div>
  )
}

function ActionButton({
  onClick,
  children
}: {
  onClick: () => void
  children: React.ReactNode
}): React.JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      className="font-mono text-[11px] uppercase tracking-wider text-paper-500 hover:text-paper-100"
    >
      {children}
    </button>
  )
}
