import os
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

sys.modules.setdefault("pillow_avif", types.ModuleType("pillow_avif"))
_apnggif = types.ModuleType("apnggif")
setattr(_apnggif, "apnggif", lambda *_, **__: None)
sys.modules.setdefault("apnggif", _apnggif)

actions_stub = types.ModuleType("modules.nsfw_scanner.actions")

async def _noop_handle_nsfw_content(*_, **__):
    return None

actions_stub.handle_nsfw_content = _noop_handle_nsfw_content
sys.modules.setdefault("modules.nsfw_scanner.actions", actions_stub)

os.environ[
    "FERNET_SECRET_KEY"
] = "MDEyMzQ1Njc4OWFiY2RlZjAxMjM0NTY3ODlhYmNkZWY="


class _FakeQueue:
    def __init__(self, *, name=None, **_):
        self.name = name
        self._name = name
        self.running = True
        self.added = []

    async def add_task(self, coro):
        self.added.append(coro)
        close = getattr(coro, "close", None)
        if callable(close):
            close()


def _make_dummy_task():
    async def _dummy():
        return None

    return _dummy()


@pytest.fixture()
def fake_queues(monkeypatch):
    created: dict[str, _FakeQueue] = {}

    def _factory(*_, name=None, **kwargs):
        queue = _FakeQueue(name=name, **kwargs)
        created[name] = queue
        return queue

    monkeypatch.setattr("cogs.aggregated_moderation.cog.WorkerQueue", _factory)
    return created


async def _build_cog(monkeypatch, fake_queues, *, accelerated: bool):
    async def _fake_is_accelerated(guild_id: int):
        return accelerated

    monkeypatch.setattr("modules.utils.mysql.is_accelerated", _fake_is_accelerated)
    monkeypatch.setattr(
        "cogs.aggregated_moderation.cog.AggregatedModerationCog._is_new_guild",
        lambda self, guild_id: False,
    )
    bot = types.SimpleNamespace()
    from cogs.aggregated_moderation.cog import AggregatedModerationCog

    return AggregatedModerationCog(bot)


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.mark.anyio
async def test_video_tasks_routed_to_accelerated_video_queue(monkeypatch, fake_queues):
    cog = await _build_cog(monkeypatch, fake_queues, accelerated=True)
    token = _make_dummy_task()

    await cog.add_to_queue(token, guild_id=1, task_kind="video")

    assert len(fake_queues["accelerated_video"].added) == 1
    assert fake_queues["accelerated"].added == []
    assert fake_queues["free"].added == []
    token.close()


@pytest.mark.anyio
async def test_non_accelerated_video_tasks_use_free_queue(monkeypatch, fake_queues):
    cog = await _build_cog(monkeypatch, fake_queues, accelerated=False)
    token = _make_dummy_task()

    await cog.add_to_queue(token, guild_id=1, task_kind="video")

    assert len(fake_queues["free"].added) == 1
    assert fake_queues["accelerated_video"].added == []
    assert fake_queues["accelerated"].added == []
    token.close()


@pytest.mark.anyio
async def test_text_tasks_routed_to_accelerated_text_queue(monkeypatch, fake_queues):
    cog = await _build_cog(monkeypatch, fake_queues, accelerated=True)
    token = _make_dummy_task()

    await cog.add_to_queue(token, guild_id=5, task_kind="text")

    assert len(fake_queues["accelerated_text"].added) == 1
    assert fake_queues["accelerated"].added == []
    assert fake_queues["free"].added == []
    token.close()


@pytest.mark.anyio
async def test_non_accelerated_text_tasks_use_free_queue(monkeypatch, fake_queues):
    cog = await _build_cog(monkeypatch, fake_queues, accelerated=False)
    token = _make_dummy_task()

    await cog.add_to_queue(token, guild_id=7, task_kind="text")

    assert len(fake_queues["free"].added) == 1
    assert fake_queues["accelerated_text"].added == []
    assert fake_queues["accelerated"].added == []
    token.close()
