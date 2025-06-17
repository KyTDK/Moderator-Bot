import pytest
import sys
import asyncio
from types import SimpleNamespace
from pathlib import Path

# Provide a minimal stub for the discord module so modules.utils.strike can be
# imported without having the real dependency installed.
sys.modules.setdefault(
    "discord",
    SimpleNamespace(Role=object, Interaction=object),
)

# Ensure the project root is on the import path
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
            duration="spamming",
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
    assert "You must provide a warning message" in inter.followup.messages[0]
