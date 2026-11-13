"""A local GGUF model run by llama.cpp (llama-cpp-python).

The model is loaded through `models.ModelManager`, so it never shares the GPU
with Whisper: loading it unloads the speech model first, and vice versa.
"""

from __future__ import annotations

import logging

from minutemind import hardware, models
from minutemind.llm.base import ProgressCallback

log = logging.getLogger(__name__)

# Qwen 2.5 3B has 36 layers. On a 2-4 GB card, put roughly half on the GPU.
SMALL_GPU_LAYERS = 18


def gpu_layers_for(profile: hardware.Profile) -> int:
    """How many of the model's layers go on the GPU (-1 means all)."""
    if profile.device != "cuda":
        return 0
    if profile.name == "small_gpu":
        return SMALL_GPU_LAYERS
    return -1


class LlamaCppModel:
    local = True

    def __init__(self, manager: models.ModelManager, spec: models.LlmSpec | None = None):
        self.manager = manager
        self.spec = spec or models.minutes_llm()
        self.name = self.spec.name
        self.context_tokens = self.spec.context_tokens

    def ensure_model(self, on_progress: ProgressCallback) -> None:
        models.download_llm(self.spec, on_progress)

    def complete_json(
        self,
        system: str,
        user: str,
        schema: dict,
        profile: hardware.Profile,
        max_tokens: int,
    ) -> str:
        llama = self.manager.llama(self.spec, gpu_layers_for(profile))
        result = llama.create_chat_completion(
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # llama.cpp turns the schema into a grammar, so the output always parses.
            response_format={"type": "json_object", "schema": schema},
            temperature=0.2,
            top_p=0.9,
            repeat_penalty=1.05,
            max_tokens=max_tokens,
        )
        choice = result["choices"][0]
        if choice.get("finish_reason") == "length":
            log.warning("The model ran out of room before finishing its answer")
        return choice["message"]["content"] or ""

    def release(self) -> None:
        self.manager.unload()
