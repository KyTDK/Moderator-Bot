import base64
import os
import sys
from pathlib import Path

import numpy as np
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.append(str(PROJECT_ROOT))

os.environ.setdefault("FERNET_SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode())

from modules.utils.vector_spaces import MilvusVectorSpace


def _embed_batch(items):
    count = len(items)
    if count == 0:
        return np.zeros((0, 4), dtype=np.float32)
    return np.zeros((count, 4), dtype=np.float32)


@pytest.fixture
def milvus_space():
    return MilvusVectorSpace(
        collection_name="test_collection",
        dim=4,
        embed_batch=_embed_batch,
        description="Test collection",
        host="test-host",
        port="19530",
    )


def test_is_available_triggers_initializer(monkeypatch, milvus_space):
    called = False

    def fake_ensure() -> None:
        nonlocal called
        called = True

    milvus_space._milvus_available = True
    milvus_space._collection_ready.clear()
    monkeypatch.setattr(milvus_space, "_ensure_collection_initializer_started", fake_ensure)

    _ = milvus_space.is_available()

    assert called is True


def test_get_debug_info_reports_state(monkeypatch, milvus_space):
    calls = 0

    def fake_ensure() -> None:
        nonlocal calls
        calls += 1

    milvus_space._milvus_available = True
    milvus_space._collection_ready.clear()
    milvus_space._collection_init_started.set()
    monkeypatch.setattr(milvus_space, "_ensure_collection_initializer_started", fake_ensure)

    with milvus_space._collection_state_lock:
        milvus_space._fallback_active = True
        milvus_space._collection_error = RuntimeError("boom")

    info = milvus_space.get_debug_info()

    assert calls == 1
    assert info["init_started"] is True
    assert info["fallback_active"] is True
    assert info["collection_ready"] is False
    assert info["last_error"] == "RuntimeError: boom"
    assert info["host"] == "test-host"
    assert info["port"] == "19530"
