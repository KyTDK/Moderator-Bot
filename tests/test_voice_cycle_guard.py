from datetime import timedelta
from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Provide lightweight stubs for optional voice deps so imports succeed without extras.
voice_recv_stub = types.ModuleType("discord.ext.voice_recv")


class _DummyVoiceRecvClient:  # pragma: no cover - only used as a stub
    pass


class _DummyAudioSink:  # pragma: no cover - only used as a stub
    def __init__(self, *_, **__):
        pass

    @staticmethod
    def listener():
        def _decorator(func):
            return func

        return _decorator


voice_recv_stub.VoiceRecvClient = _DummyVoiceRecvClient
voice_recv_stub.AudioSink = _DummyAudioSink
sys.modules.setdefault("discord.ext.voice_recv", voice_recv_stub)

whisper_stub = types.ModuleType("faster_whisper")


class _DummyWhisperModel:  # pragma: no cover - only used as a stub
    def __init__(self, *_, **__):
        pass

    def transcribe(self, *_, **__):
        return [], types.SimpleNamespace()


whisper_stub.WhisperModel = _DummyWhisperModel
sys.modules.setdefault("faster_whisper", whisper_stub)

from cogs.voice_moderation.voice_moderator import (  # noqa: E402
    _cycle_timeout_seconds,
    _failure_delay_seconds,
)


def test_failure_delay_scales_and_caps():
    idle_seconds = 20.0

    first = _failure_delay_seconds(1, idle_seconds)
    second = _failure_delay_seconds(2, idle_seconds)
    third = _failure_delay_seconds(5, idle_seconds)

    assert first == idle_seconds
    assert second > first
    assert third >= second
    assert third <= 90.0


def test_cycle_timeout_sets_floor_and_margin():
    listen = timedelta(minutes=2)
    idle = timedelta(seconds=30)

    timeout = _cycle_timeout_seconds(listen, idle)
    assert timeout >= (listen + idle).total_seconds()
    assert timeout >= 180.0

    short_timeout = _cycle_timeout_seconds(timedelta(seconds=10), timedelta(seconds=0))
    assert short_timeout >= 90.0
