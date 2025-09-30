from __future__ import annotations

from collections.abc import Iterable

from modules.utils.localization import TranslateFn, localize_message

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


def _localize_plan_name(
    plan: str,
    translator: TranslateFn | None,
    translator_kwargs: dict | None,
) -> str:
    return localize_message(
        translator,
        "modules.config.premium_plans.plan_names",
        plan,
        fallback=PLAN_DISPLAY_NAMES[plan],
        **(translator_kwargs or {}),
    )


def describe_plan_requirements(
    plans: Iterable[str],
    *,
    translator: TranslateFn | None = None,
    **translator_kwargs: object,
) -> str:
    """Produce a human-readable description of plan requirements."""

    ordered = order_plans(plans)
    if not ordered or len(ordered) == len(PREMIUM_PLANS):
        return localize_message(
            translator,
            "modules.config.premium_plans.requirements",
            "any",
            fallback="an active premium plan",
            **translator_kwargs,
        )

    fallback_names = [PLAN_DISPLAY_NAMES[p] for p in ordered]
    active_names = fallback_names
    if translator is not None:
        active_names = [
            _localize_plan_name(plan, translator, translator_kwargs)
            for plan in ordered
        ]

    if len(ordered) == 1:
        return localize_message(
            translator,
            "modules.config.premium_plans.requirements",
            "single",
            placeholders={"plan": active_names[0]},
            fallback=f"an active {fallback_names[0]} plan",
            **translator_kwargs,
        )

    if len(ordered) == 2:
        return localize_message(
            translator,
            "modules.config.premium_plans.requirements",
            "double",
            placeholders={
                "plan_a": active_names[0],
                "plan_b": active_names[1],
            },
            fallback=(
                f"an active {fallback_names[0]} or {fallback_names[1]} plan"
            ),
            **translator_kwargs,
        )

    joined_fallback = ", ".join(fallback_names[:-1])
    joined_active = ", ".join(active_names[:-1])
    return localize_message(
        translator,
        "modules.config.premium_plans.requirements",
        "list",
        placeholders={
            "plans": joined_active,
            "last": active_names[-1],
        },
        fallback=f"an active {joined_fallback}, or {fallback_names[-1]} plan",
        **translator_kwargs,
    )

