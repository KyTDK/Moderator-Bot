import asyncio
import base64
import os
import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta

import pytest

sys.path.append(str(Path(__file__).resolve().parents[1]))

os.environ.setdefault(
    "FERNET_SECRET_KEY", base64.urlsafe_b64encode(b"0" * 32).decode()
)

from cogs.voice_moderation.transcript_utils import (
    LiveTranscriptEmitter,
    TranscriptFormatter,
)


class _FakeMember:
    def __init__(self, user_id: int, name: str) -> None:
        self.id = user_id
        self.display_name = name
        self.mention = f"<@{user_id}>"


class _FakeChannel:
    def __init__(self, channel_id: int, name: str) -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"#{name}"


class _FakeBot:
    async def translate(self, *args, **kwargs):
        return {}


_TRANSCRIPT_TEXTS = {
    "author_label": "AUTHOR",
    "utterance_label": "UTTERANCE",
    "divider": "---",
    "unknown_speaker": "Unknown speaker",
    "unknown_prefix": "Unknown",
    "user_fallback": "<@{id}> (id = {id})",
    "line": "[{timestamp}] {prefix} ({name}): {text}",
    "title_single": "VC Transcript",
    "title_part": "VC Transcript (part {index}/{total})",
    "field_channel": "Channel",
    "field_utterances": "Utterances",
    "footer_high": "High accuracy transcript",
    "footer_normal": "Normal accuracy transcript",
}


def run_async(coro):
    return asyncio.run(coro)


async def _test_formatter_builds_transcript_block_and_lines():
    members = {
        1: _FakeMember(1, "Alice"),
        2: _FakeMember(2, "Bob"),
    }

    async def _resolver(uid: int):
        return members.get(uid)

    formatter = TranscriptFormatter(
        guild=None,  # guild not used
        transcript_texts=_TRANSCRIPT_TEXTS,
        member_resolver=_resolver,
    )

    now = datetime.now(timezone.utc)
    chunk = [
        (1, "hello world", now),
        (2, "hi there", now + timedelta(seconds=1)),
    ]
    block = await formatter.build_transcript_block(chunk)
    assert "Alice" in block
    assert "Bob" in block
    lines = await formatter.build_embed_lines(chunk)
    assert len(lines) == 2
    assert "hello world" in lines[0]
    assert "hi there" in lines[1]


async def _test_live_transcript_emitter_flush(monkeypatch):
    sent_embeds = []

    async def fake_log_to_channel(embed, channel_id, bot, file=None):
        sent_embeds.append((embed, channel_id))

    monkeypatch.setattr(
        "modules.utils.mod_logging.log_to_channel", fake_log_to_channel
    )

    members = {1: _FakeMember(1, "Alice")}

    async def _resolver(uid: int):
        return members.get(uid)

    formatter = TranscriptFormatter(
        guild=None,
        transcript_texts=_TRANSCRIPT_TEXTS,
        member_resolver=_resolver,
    )
    emitter = LiveTranscriptEmitter(
        formatter=formatter,
        bot=_FakeBot(),
        channel=_FakeChannel(10, "general"),
        transcript_channel_id=20,
        high_quality=False,
        min_utterances=2,
        max_utterances=5,
        min_interval=0.0,
        max_latency=30.0,
    )

    now = datetime.now(timezone.utc)
    await emitter.add_chunk([(1, "first", now)])
    assert sent_embeds == []

    await emitter.add_chunk([(1, "second", now + timedelta(seconds=1))])
    assert len(sent_embeds) == 1
    embed, channel_id = sent_embeds[0]
    assert channel_id == 20
    assert "first" in embed.description
    assert "second" in embed.description

    await emitter.add_chunk([(1, "third", now + timedelta(seconds=2))])
    await emitter.flush(force=True)
    assert len(sent_embeds) == 2
    assert "third" in sent_embeds[1][0].description


def test_formatter_builds_transcript_block_and_lines():
    run_async(_test_formatter_builds_transcript_block_and_lines())


def test_live_transcript_emitter_flush(monkeypatch):
    run_async(_test_live_transcript_emitter_flush(monkeypatch))
