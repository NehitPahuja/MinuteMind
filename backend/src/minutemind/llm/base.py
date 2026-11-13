"""What MinuteMind needs from a language model, whichever one runs it.

Phase 5 has one provider (llama.cpp running a local model). Phase 7 adds
Claude behind the same interface.
"""

from __future__ import annotations

from typing import Callable, Protocol

from minutemind import hardware

ProgressCallback = Callable[[float], None]


class LanguageModel(Protocol):
    """A chat model that can be made to answer with JSON matching a schema."""

    # Shown next to the minutes, e.g. "qwen2.5-3b-instruct".
    name: str
    # How much text fits in one request (prompt plus answer), in tokens.
    context_tokens: int
    # True when the model runs on this PC (no Cloud badge needed).
    local: bool

    def ensure_model(self, on_progress: ProgressCallback) -> None:
        """Download the model if needed."""
        ...

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict,
        profile: hardware.Profile,
        max_tokens: int,
    ) -> str:
        """Return the model's answer, constrained to JSON matching `schema`."""
        ...

    def release(self) -> None:
        """Free the model's memory (GPU or RAM)."""
        ...
