import pytest
import sys
import asyncio
from types import SimpleNamespace
from pathlib import Path

sys.modules.setdefault(
    "discord",
    SimpleNamespace(Role=object, Interaction=object),
)

sys.path.append(str(Path(__file__).resolve().parents[1]))

from modules.utils.strike import validate_action

class DummyFollowup:
    def __init__(self):
        self.messages = []

    async def send(self, message, ephemeral=True):
        self.messages.append(message)

class DummyInteraction:
    def __init__(self):
        self.followup = DummyFollowup()


def test_invalid_action():
    inter = DummyInteraction()
    result = asyncio.run(validate_action(inter, action="fly", valid_actions=["kick", "ban"]))
    assert result is None
    assert inter.followup.messages == ["Action must be one of: kick, ban"]


def test_timeout_success():
    inter = DummyInteraction()
    result = asyncio.run(validate_action(inter, action="timeout", duration="10s", valid_actions=["timeout"]))
    assert result == "timeout:10s"
    assert inter.followup.messages == []


def test_timeout_missing_duration():
    inter = DummyInteraction()
    result = asyncio.run(validate_action(inter, action="timeout", valid_actions=["timeout"]))
    assert result is None
    assert "You must provide a duration" in inter.followup.messages[0]


def test_timeout_invalid_duration():
    inter = DummyInteraction()
    result = asyncio.run(validate_action(inter, action="timeout", duration="abc", valid_actions=["timeout"]))
    assert result is None
    assert "Invalid duration format" in inter.followup.messages[0]


def test_give_role_success():
    inter = DummyInteraction()
    role = SimpleNamespace(id=123)
    result = asyncio.run(validate_action(inter, action="give_role", role=role, valid_actions=["give_role"]))
    assert result == "give_role:123"
    assert inter.followup.messages == []


def test_give_role_missing_role():
    inter = DummyInteraction()
    result = asyncio.run(validate_action(inter, action="give_role", valid_actions=["give_role"]))
    assert result is None
    assert "You must specify a role" in inter.followup.messages[0]


def test_warn_success():
    inter = DummyInteraction()
    result = asyncio.run(
        validate_action(
            inter,
            action="warn",
            param="Stop",
            valid_actions=["warn"],
        )
    )
    assert result == "warn:Stop"
    assert inter.followup.messages == []


def test_warn_missing_message():
    inter = DummyInteraction()
    result = asyncio.run(
        validate_action(
            inter,
            action="warn",
            duration="reason",
            param=None,
            valid_actions=["warn"],
        )
    )
    assert result is None
    msg = inter.followup.messages[0]
    assert "You must provide a warning message" in msg
    assert "`warn` does not support a duration" in msg


def test_timeout_optional_duration_none():
    inter = DummyInteraction()
    result = asyncio.run(
        validate_action(
            inter,
            action="timeout",
            duration=None,
            valid_actions=["timeout"],
            timeout_required=False,
        )
    )
    assert result == "timeout:None"
    assert inter.followup.messages == []


def test_timeout_with_role_error():
    inter = DummyInteraction()
    role = SimpleNamespace(id=1)
    result = asyncio.run(
        validate_action(
            inter,
            action="timeout",
            duration="10s",
            role=role,
            valid_actions=["timeout"],
        )
    )
    assert result is None
    assert "You cannot attach a role to a timeout action." in inter.followup.messages[0]


def test_timeout_duration_disallowed():
    inter = DummyInteraction()
    result = asyncio.run(
        validate_action(
            inter,
            action="timeout",
            duration="10s",
            valid_actions=["timeout"],
            allow_duration=False,
        )
    )
    assert result is None
    assert "Duration is not allowed for this command." in inter.followup.messages[0]


def test_ban_with_extras():
    inter = DummyInteraction()
    role = SimpleNamespace(id=123)
    result = asyncio.run(
        validate_action(
            inter,
            action="ban",
            duration="10s",
            role=role,
            valid_actions=["ban"],
        )
    )
    assert result is None
    msg = inter.followup.messages[0]
    assert "`ban` does not support a duration." in msg
    assert "`ban` does not use a role." in msg


def test_ban_success():
    inter = DummyInteraction()
    result = asyncio.run(validate_action(inter, action="ban", valid_actions=["ban"]))
    assert result == "ban"
    assert inter.followup.messages == []


def test_warn_with_role_error():
    inter = DummyInteraction()
    role = SimpleNamespace(id=2)
    result = asyncio.run(
        validate_action(
            inter,
            action="warn",
            param="Be nice",
            role=role,
            valid_actions=["warn"],
        )
    )
    assert result is None
    assert "You cannot attach a role to a warn action." in inter.followup.messages[0]


def test_broadcast_requires_channel():
    inter = DummyInteraction()
    result = asyncio.run(
        validate_action(
            inter,
            action="broadcast",
            param="Hello",
            valid_actions=["broadcast"],
        )
    )
    assert result is None
    assert "You must specify a channel" in inter.followup.messages[0]


def test_broadcast_success():
    inter = DummyInteraction()
    channel = SimpleNamespace(id=555)
    result = asyncio.run(
        validate_action(
            inter,
            action="broadcast",
            param="Hello",
            channel=channel,
            valid_actions=["broadcast"],
        )
    )
    assert result == "broadcast:555|Hello"
    assert inter.followup.messages == []
