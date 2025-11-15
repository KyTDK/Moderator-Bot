import importlib.machinery
import sys
import types


if "redis" not in sys.modules:
    dummy = types.ModuleType("redis")

    class _DummyRedisClient:
        @classmethod
        def from_url(cls, *_, **__):  # pragma: no cover - not used in these tests
            raise RuntimeError("redis client not available")

    dummy.Redis = _DummyRedisClient
    dummy.__spec__ = importlib.machinery.ModuleSpec("redis", loader=None)
    sys.modules["redis"] = dummy


import scripts.metrics_redis_tool as tool  # noqa: E402  (after stubbing redis)


def test_needs_reset_covers_frame_totals():
    assert tool.needs_reset("accelerated_total_frames_scanned")
    assert tool.needs_reset("non_accelerated_total_frames_target")
    assert tool.needs_reset("unknown_acceleration_total_frames_media")
    assert tool.needs_reset("total_frames_scanned")
    assert tool.needs_reset("total_frames_media")
    assert not tool.needs_reset("total_bytes")
