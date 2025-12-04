from __future__ import annotations

from pathlib import Path
import sys
import types

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

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

from cogs.voice_moderation import voice_io


def _make_guild(socket: object) -> object:
    ws = types.SimpleNamespace(socket=socket)
    state = types.SimpleNamespace(
        _get_websocket=lambda *_: ws,
        ws=ws,
    )
    return types.SimpleNamespace(_state=state, shard_id=0)


def test_gateway_guard_false_when_socket_closing():
    socket = types.SimpleNamespace(closed=False, _closing=True, close_code=None)
    guild = _make_guild(socket)
    assert voice_io._gateway_websocket_ready(guild) is False


def test_gateway_guard_true_when_socket_open():
    socket = types.SimpleNamespace(closed=False, _closing=False, close_code=None)
    guild = _make_guild(socket)
    assert voice_io._gateway_websocket_ready(guild) is True
