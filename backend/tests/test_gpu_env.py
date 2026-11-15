from __future__ import annotations

import pytest

from minutemind import gpu_env


@pytest.fixture
def fresh(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(gpu_env, "_done", False)
    monkeypatch.setattr(gpu_env, "nvidia_bin_dirs", lambda: [])
    monkeypatch.delenv("CUDA_CACHE_MAXSIZE", raising=False)


def test_prepare_enlarges_the_cuda_kernel_cache(fresh) -> None:
    import os

    gpu_env.prepare()
    assert os.environ["CUDA_CACHE_MAXSIZE"] == str(4 * 1024**3)


def test_prepare_keeps_a_cache_size_the_user_set(fresh, monkeypatch: pytest.MonkeyPatch) -> None:
    import os

    monkeypatch.setenv("CUDA_CACHE_MAXSIZE", "1000")
    gpu_env.prepare()
    assert os.environ["CUDA_CACHE_MAXSIZE"] == "1000"
