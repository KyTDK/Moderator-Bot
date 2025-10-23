from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Literal, Mapping, Sequence

import discord

__all__ = ["RoleAction", "apply_role_actions", "parse_role_actions"]

_logger = logging.getLogger(__name__)

RoleOperation = Literal["give_role", "take_role"]
_SUPPORTED_OPERATIONS: set[str] = {"give_role", "take_role"}


@dataclass(frozen=True)
class RoleAction:
    """Structured representation of a role adjustment action."""

    operation: RoleOperation
    role_id: int

    def as_string(self) -> str:
        return f"{self.operation}:{self.role_id}"


def _coerce_role_id(value: Any) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.startswith("<@&") and text.endswith(">"):
            text = text[3:-1]
        if text.startswith("&"):
            text = text[1:]
        if text.isdigit():
            try:
                return int(text)
            except ValueError:
                return None
    return None


def _iter_entries(raw: Any) -> Iterator[Any]:
    if raw is None:
        return iter(())
    if isinstance(raw, RoleAction):
        return iter([raw])
    if isinstance(raw, Mapping):
        return iter([raw])
    if isinstance(raw, str):
        return iter([raw])
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes, bytearray)):
        return iter(raw)
    return iter([raw])


def parse_role_actions(raw: Any) -> list[RoleAction]:
    """Normalise *raw* VPN role actions into :class:`RoleAction` entries."""

    actions: list[RoleAction] = []
    seen: set[tuple[str, int]] = set()

    for entry in _iter_entries(raw):
        operation: str | None = None
        role_id: int | None = None

        if isinstance(entry, RoleAction):
            operation = entry.operation
            role_id = entry.role_id
        elif isinstance(entry, str):
            op, _, extra = entry.partition(":")
            op = op.strip().lower()
            if op in _SUPPORTED_OPERATIONS and extra:
                role_id = _coerce_role_id(extra)
                operation = op if role_id is not None else None
        elif isinstance(entry, Mapping):
            candidate = entry.get("action") or entry.get("operation") or entry.get("type")
            if isinstance(candidate, str):
                operation = candidate.strip().lower()
            if operation not in _SUPPORTED_OPERATIONS:
                operation = None
            target: Any | None = None
            if operation:
                target = entry.get("role") or entry.get("role_id") or entry.get("value")
                if target is None:
                    target = entry.get(operation)
            if not operation:
                for key, value in entry.items():
                    if isinstance(key, str) and key.strip().lower() in _SUPPORTED_OPERATIONS:
                        operation = key.strip().lower()
                        target = value
                        break
            if operation in _SUPPORTED_OPERATIONS:
                role_id = _coerce_role_id(target)
        elif isinstance(entry, Sequence) and len(entry) >= 2:
            first, second = entry[0], entry[1]
            if isinstance(first, str):
                operation = first.strip().lower()
                if operation in _SUPPORTED_OPERATIONS:
                    role_id = _coerce_role_id(second)
        if operation not in _SUPPORTED_OPERATIONS or role_id is None:
            continue
        key = (operation, role_id)
        if key in seen:
            continue
        seen.add(key)
        actions.append(RoleAction(operation=operation, role_id=role_id))

    return actions


def _prepare_role_actions(
    member: discord.Member,
    actions: Iterable[RoleAction],
    *,
    logger: logging.Logger,
) -> tuple[list[discord.Role], list[discord.Role], list[str], list[str]]:
    current_roles = {role.id for role in getattr(member, "roles", [])}
    to_add: list[discord.Role] = []
    to_remove: list[discord.Role] = []
    add_strings: list[str] = []
    remove_strings: list[str] = []

    for action in actions:
        role = member.guild.get_role(action.role_id)
        if role is None:
            logger.warning(
                "Role %s referenced by VPN action is not available in guild %s",
                action.role_id,
                member.guild.id,
            )
            continue
        if action.operation == "give_role":
            if role.id in current_roles:
                continue
            to_add.append(role)
            add_strings.append(action.as_string())
            current_roles.add(role.id)
        else:
            if role.id not in current_roles:
                continue
            to_remove.append(role)
            remove_strings.append(action.as_string())
            current_roles.remove(role.id)

    return to_add, to_remove, add_strings, remove_strings


async def apply_role_actions(
    member: discord.Member,
    actions: Iterable[RoleAction] | Any,
    *,
    reason: str | None = None,
    logger: logging.Logger | None = None,
) -> list[str]:
    """Apply role adjustments described by *actions* to *member*."""

    if logger is None:
        logger = _logger

    if isinstance(actions, Iterable) and all(isinstance(a, RoleAction) for a in actions):
        parsed_actions = list(actions)  # type: ignore[arg-type]
    else:
        parsed_actions = parse_role_actions(actions)

    if not parsed_actions:
        return []

    to_add, to_remove, add_strings, remove_strings = _prepare_role_actions(
        member,
        parsed_actions,
        logger=logger,
    )

    executed: list[str] = []

    if to_remove:
        try:
            await member.remove_roles(*to_remove, reason=reason)
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to remove roles %s in guild %s for user %s",
                [role.id for role in to_remove],
                member.guild.id,
                member.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to remove roles %s in guild %s for user %s",
                [role.id for role in to_remove],
                member.guild.id,
                member.id,
            )
        else:
            executed.extend(remove_strings)

    if to_add:
        try:
            await member.add_roles(*to_add, reason=reason)
        except discord.Forbidden:
            logger.warning(
                "Missing permissions to assign roles %s in guild %s for user %s",
                [role.id for role in to_add],
                member.guild.id,
                member.id,
            )
        except discord.HTTPException:
            logger.exception(
                "Failed to assign roles %s in guild %s for user %s",
                [role.id for role in to_add],
                member.guild.id,
                member.id,
            )
        else:
            executed.extend(add_strings)

    return executed
