from __future__ import annotations

from collections.abc import Iterable

PLAN_FREE = "free"
PLAN_CORE = "core"
PLAN_PRO = "pro"
PLAN_ULTRA = "ultra"

PREMIUM_PLANS: tuple[str, ...] = (PLAN_CORE, PLAN_PRO, PLAN_ULTRA)
PREMIUM_PLAN_SET = set(PREMIUM_PLANS)
PLAN_ORDER_INDEX = {plan: index for index, plan in enumerate(PREMIUM_PLANS)}

PLAN_DISPLAY_NAMES = {
    PLAN_FREE: "Free",
    PLAN_CORE: "Core",
    PLAN_PRO: "Pro",
    PLAN_ULTRA: "Ultra",
}

def plans_at_or_above(plan: str) -> set[str]:
    """Return all plans at or above the provided plan tier."""
    normalized = normalize_plan_name(plan, allow_free=False)
    if normalized is None:
        raise ValueError(f"Unknown plan name '{plan}'.")
    start_index = PLAN_ORDER_INDEX[normalized]
    return {p for p in PREMIUM_PLANS if PLAN_ORDER_INDEX[p] >= start_index}

def resolve_required_plans(plans: str | Iterable[str]) -> set[str]:
    """Expand a plan requirement into all permitted tiers."""
    if isinstance(plans, str):
        return plans_at_or_above(plans)
    normalized = normalize_plan_collection(plans)
    min_index = min(PLAN_ORDER_INDEX[p] for p in normalized)
    return {p for p in PREMIUM_PLANS if PLAN_ORDER_INDEX[p] >= min_index}



_PLAN_ALIASES = {
    PLAN_FREE: PLAN_FREE,
    "free": PLAN_FREE,
    "core": PLAN_CORE,
    "accelerated": PLAN_CORE,
    "accelerated_core": PLAN_CORE,
    "pro": PLAN_PRO,
    "accelerated_pro": PLAN_PRO,
    "ultra": PLAN_ULTRA,
    "accelerated_ultra": PLAN_ULTRA,
}


def normalize_plan_name(
    raw: str | None,
    *,
    allow_free: bool = True,
    default: str | None = None,
) -> str | None:
    """Return a normalised plan name or raise if invalid."""
    if raw is None:
        return default
    normalized = _PLAN_ALIASES.get(raw.strip().lower())
    if normalized is None:
        if default is not None:
            return default
        raise ValueError(f"Unknown plan name '{raw}'.")
    if not allow_free and normalized == PLAN_FREE:
        if default is not None:
            return default
        raise ValueError("Free plan is not valid in this context.")
    return normalized


def normalize_plan_collection(plans: Iterable[str]) -> set[str]:
    """Normalise a collection of plan names, rejecting invalid entries."""
    normalized = {normalize_plan_name(plan, allow_free=False) for plan in plans}
    normalized.discard(None)
    if not normalized:
        raise ValueError("At least one premium plan must be provided.")
    return normalized


def tier_to_plan(tier: str | None, *, default: str | None = None) -> str | None:
    """Map a database tier identifier to a dashboard plan name."""
    if tier is None:
        return default
    return normalize_plan_name(tier, allow_free=False, default=default)


def order_plans(plans: Iterable[str]) -> list[str]:
    """Return plans in their display order (Core -> Pro -> Ultra)."""
    normalized = normalize_plan_collection(plans)
    return [plan for plan in PREMIUM_PLANS if plan in normalized]


def describe_plan_requirements(plans: Iterable[str]) -> str:
    """Produce a human-readable description of plan requirements."""
    ordered = order_plans(plans)
    if not ordered:
        return "an active premium plan"
    if len(ordered) == len(PREMIUM_PLANS):
        return "an active premium plan"
    if len(ordered) == 1:
        return f"an active {PLAN_DISPLAY_NAMES[ordered[0]]} plan"
    if len(ordered) == 2:
        return f"an active {PLAN_DISPLAY_NAMES[ordered[0]]} or {PLAN_DISPLAY_NAMES[ordered[1]]} plan"
    joined = ", ".join(PLAN_DISPLAY_NAMES[p] for p in ordered[:-1])
    return f"an active {joined}, or {PLAN_DISPLAY_NAMES[ordered[-1]]} plan"

