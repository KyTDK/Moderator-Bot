from __future__ import annotations

import asyncio

from modules.verification.actions import apply_role_actions, parse_role_actions, RoleAction


class DummyRole:
    def __init__(self, role_id: int) -> None:
        self.id = role_id


class DummyGuild:
    def __init__(self, guild_id: int, roles: list[int]) -> None:
        self.id = guild_id
        self._roles = {role_id: DummyRole(role_id) for role_id in roles}

    def get_role(self, role_id: int) -> DummyRole | None:
        return self._roles.get(role_id)


class DummyMember:
    def __init__(self, guild: DummyGuild, role_ids: list[int]) -> None:
        self.guild = guild
        self.id = 999
        self.roles = [guild.get_role(role_id) for role_id in role_ids if guild.get_role(role_id) is not None]

    async def add_roles(self, *roles: DummyRole, reason: str | None = None) -> None:
        for role in roles:
            if role not in self.roles:
                self.roles.append(role)

    async def remove_roles(self, *roles: DummyRole, reason: str | None = None) -> None:
        for role in roles:
            if role in self.roles:
                self.roles.remove(role)


def test_parse_role_actions_normalizes_inputs() -> None:
    raw = [
        "give_role:1",
        {"action": "take_role", "role_id": "2"},
        ("give_role", "<@&3>"),
        RoleAction("give_role", 4),
        {"give_role": "5"},
        "invalid",
        "give_role:",
    ]
    actions = parse_role_actions(raw)
    assert {(action.operation, action.role_id) for action in actions} == {
        ("give_role", 1),
        ("take_role", 2),
        ("give_role", 3),
        ("give_role", 4),
        ("give_role", 5),
    }


def test_apply_role_actions_adjusts_roles() -> None:
    guild = DummyGuild(100, [1, 2, 3])
    member = DummyMember(guild, [2])
    actions = parse_role_actions(["give_role:1", "take_role:2", "give_role:3", "take_role:4"])

    async def run() -> list[str]:
        return await apply_role_actions(member, actions, reason="test")

    executed = asyncio.run(run())
    assert set(executed) == {"give_role:1", "take_role:2", "give_role:3"}
    assert sorted(role.id for role in member.roles) == [1, 3]
