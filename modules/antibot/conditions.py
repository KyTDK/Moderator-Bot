from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional

import discord

SignalAccessor = Callable[[discord.Member, Dict[str, Any]], Any]


@dataclass(frozen=True)
class ConditionSignal:
    key: str
    name: str
    description: str
    value_type: str  # number, boolean, string, list
    operators: List[str]
    accessor: SignalAccessor
    parser: Optional[Callable[[str], Any]] = None
    formatter: Optional[Callable[[Any], str]] = None


def _bool_parser(raw: str) -> bool:
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    raise ValueError("Enter a boolean like true/false")


def _number_parser(raw: str) -> float:
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError("Enter a number") from exc


def _identity_formatter(value: Any) -> str:
    return str(value)


def _number_formatter(value: Any) -> str:
    try:
        if value is None:
            return "none"
        if float(value).is_integer():
            return str(int(float(value)))
        return f"{float(value):.2f}"
    except Exception:
        return str(value)


def _bool_formatter(value: Any) -> str:
    return "true" if bool(value) else "false"


_CONDITION_SIGNALS: Dict[str, ConditionSignal] = {}


def _register(signal: ConditionSignal) -> ConditionSignal:
    _CONDITION_SIGNALS[signal.key] = signal
    return signal


_register(ConditionSignal(
    key="final_score",
    name="Final Score",
    description="Final AntiBot score (0-100)",
    value_type="number",
    operators=[">", ">=", "<", "<=", "==", "!="],
    accessor=lambda _member, details: details.get("final_score"),
    parser=_number_parser,
    formatter=_number_formatter,
))

_register(ConditionSignal(
    key="account_age_days",
    name="Account Age (days)",
    description="Number of days since the account was created.",
    value_type="number",
    operators=[">", ">=", "<", "<=", "==", "!="],
    accessor=lambda _member, details: details.get("account_age_days"),
    parser=_number_parser,
    formatter=_number_formatter,
))

_register(ConditionSignal(
    key="guild_join_days",
    name="Guild Tenure (days)",
    description="Number of days since the member joined the guild.",
    value_type="number",
    operators=[">", ">=", "<", "<=", "==", "!="],
    accessor=lambda _member, details: details.get("guild_join_days"),
    parser=_number_parser,
    formatter=_number_formatter,
))

_register(ConditionSignal(
    key="collectibles_count",
    name="Collectibles Count",
    description="Number of collectibles on the account.",
    value_type="number",
    operators=[">=", ">", "<", "<=", "==", "!="],
    accessor=lambda _member, details: details.get("collectibles_count", 0),
    parser=_number_parser,
    formatter=_number_formatter,
))

_register(ConditionSignal(
    key="member_flags_count",
    name="Member Flags Count",
    description="Number of Discord member flags set on the user.",
    value_type="number",
    operators=[">=", ">", "<", "<=", "==", "!="],
    accessor=lambda _member, details: details.get('member_flags_count', 0),
    parser=_number_parser,
    formatter=_number_formatter,
))

_register(ConditionSignal(
    key="has_avatar",
    name="Has Avatar",
    description="Whether the member has a custom avatar.",
    value_type="boolean",
    operators=["==", "!="],
    accessor=lambda _member, details: bool(details.get("has_avatar")),
    parser=_bool_parser,
    formatter=_bool_formatter,
))

_register(ConditionSignal(
    key="has_banner",
    name="Has Banner",
    description="Whether the account has a profile banner.",
    value_type="boolean",
    operators=["==", "!="],
    accessor=lambda _member, details: bool(details.get("has_banner")),
    parser=_bool_parser,
    formatter=_bool_formatter,
))

_register(ConditionSignal(
    key="server_tag_present",
    name="Server Tag Present",
    description="Whether the user has a primary guild/server tag set.",
    value_type="boolean",
    operators=["==", "!="],
    accessor=lambda _member, details: bool(details.get("primary_guild")),
    parser=_bool_parser,
    formatter=_bool_formatter,
))

_register(ConditionSignal(
    key="member_flags_contains",
    name="Member Flags Contains",
    description="Checks if a specific member flag is present.",
    value_type="string",
    operators=["contains", "not_contains"],
    accessor=lambda member, details: details.get("member_flags_list") or [],
    formatter=_identity_formatter,
))


def get_signal(signal_key: str) -> Optional[ConditionSignal]:
    return _CONDITION_SIGNALS.get(signal_key)


def list_signals() -> List[ConditionSignal]:
    return list(_CONDITION_SIGNALS.values())


def parse_condition_value(signal: ConditionSignal, raw_value: Optional[str]) -> Any:
    if signal.value_type == "boolean":
        if raw_value is None:
            raise ValueError("This condition expects a boolean value.")
        return signal.parser(raw_value) if signal.parser else _bool_parser(raw_value)
    if signal.value_type == "number":
        if raw_value is None:
            raise ValueError("This condition expects a number.")
        parser = signal.parser or _number_parser
        return parser(raw_value)
    if signal.value_type == "string":
        if raw_value is None or not raw_value.strip():
            raise ValueError("Enter a value for this condition.")
        return raw_value.strip()
    if signal.value_type == "list":
        if raw_value is None:
            raise ValueError("Enter a value for this condition.")
        return raw_value.strip()
    return raw_value


@dataclass
class Condition:
    id: str
    signal: str
    operator: str
    value: Any
    label: Optional[str] = None

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "Condition":
        return cls(
            id=str(payload.get('id')),
            signal=payload['signal'],
            operator=payload['operator'],
            value=payload.get('value'),
            label=payload.get('label'),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            'id': self.id,
            'signal': self.signal,
            'operator': self.operator,
            'value': self.value,
            'label': self.label,
        }


@dataclass
class ConditionResult:
    condition: Condition
    passed: bool
    actual_value: Any


def make_condition(signal: ConditionSignal, operator: str, raw_value: Optional[str], label: Optional[str]) -> Condition:
    value = parse_condition_value(signal, raw_value)
    cond_id = uuid.uuid4().hex[:8]
    return Condition(id=cond_id, signal=signal.key, operator=operator, value=value, label=label)


def evaluate_condition(condition: Condition, member: discord.Member, details: Dict[str, Any]) -> ConditionResult:
    signal_meta = get_signal(condition.signal)
    value = None
    if signal_meta:
        try:
            value = signal_meta.accessor(member, details)
        except Exception:
            value = None
    passed = _compare(condition.operator, value, condition.value, signal_meta)
    return ConditionResult(condition=condition, passed=passed, actual_value=value)


def evaluate_conditions(conditions: Iterable[Condition], member: discord.Member, details: Dict[str, Any]) -> List[ConditionResult]:
    return [evaluate_condition(cond, member, details) for cond in conditions]


def _compare(operator: str, actual: Any, expected: Any, signal: Optional[ConditionSignal]) -> bool:
    if operator in {">", ">=", "<", "<=", "==", "!="}:
        try:
            if actual is None:
                return False
            actual_num = float(actual)
            expected_num = float(expected)
        except Exception:
            actual_num, expected_num = actual, expected
        if operator == ">":
            return actual_num > expected_num
        if operator == ">=":
            return actual_num >= expected_num
        if operator == "<":
            return actual_num < expected_num
        if operator == "<=":
            return actual_num <= expected_num
        if operator == "==":
            return actual_num == expected_num
        if operator == "!=":
            return actual_num != expected_num
    elif operator == "contains":
        if actual is None:
            return False
        return str(expected).lower() in [str(x).lower() for x in actual]
    elif operator == "not_contains":
        if actual is None:
            return True
        return str(expected).lower() not in [str(x).lower() for x in actual]
    return False


def format_actual(signal_key: str, value: Any) -> str:
    signal = get_signal(signal_key)
    if signal and signal.formatter:
        return signal.formatter(value)
    if isinstance(value, list):
        return ', '.join(str(v) for v in value[:5])
    return str(value)


def format_expected(condition: Condition) -> str:
    signal = get_signal(condition.signal)
    if signal and signal.formatter:
        return signal.formatter(condition.value)
    return str(condition.value)


__all__ = [
    'Condition',
    'ConditionResult',
    'ConditionSignal',
    'evaluate_conditions',
    'get_signal',
    'list_signals',
    'make_condition',
    'format_actual',
    'format_expected',
]

