from __future__ import annotations

from typing import Dict

from .config import (
    ALLOWED_MEMBER_FLAGS,
    ALLOWED_PUBLIC_FLAGS,
    BADGE_DETAIL_LIMIT,
    COLLECTIBLE_BONUS_CAP,
    COLLECTIBLE_DETAIL_LIMIT,
    EXTRA_BADGE_BONUS_CAP,
    MEMBER_FLAG_WEIGHT_MAP,
    NITRO_BOOST_BONUS,
    PRIMARY_GUILD_BONUS,
    PUBLIC_FLAG_WEIGHT_MAP,
    UNKNOWN_PUBLIC_DEFAULT_WEIGHT,
    UNKNOWN_PUBLIC_FLAG_RULES,
)
from .context import ScoreContext

__all__ = ["apply"]

def apply(ctx: ScoreContext) -> None:
    """Apply reputation, badge, and membership scoring rules."""
    _score_public_flags(ctx)
    _score_additional_badges(ctx)
    _score_collectibles(ctx)
    _score_primary_guild(ctx)
    _score_member_flags(ctx)
    _score_boosting(ctx)

def _score_public_flags(ctx: ScoreContext) -> None:
    user = ctx.user
    pf = getattr(user, "public_flags", None)
    raw_flag_names: list[str] = []
    if pf:
        extracted: Dict[str, bool] | None = None
        try:
            extracted = pf.to_dict()  # type: ignore[attr-defined]
        except Exception:
            extracted = None
        if not extracted:
            extracted = {}
            for attr in dir(pf):
                if attr.startswith("_"):
                    continue
                try:
                    value = getattr(pf, attr)
                except Exception:
                    continue
                if isinstance(value, bool):
                    extracted[attr] = value
        raw_flag_names = [name for name, enabled in (extracted or {}).items() if enabled]

    normalized_flags: list[str] = []
    seen: set[str] = set()
    for name in raw_flag_names:
        lname = name.lower()
        if lname in seen:
            continue
        seen.add(lname)
        normalized_flags.append(lname)

    weight_total = 0
    for name in normalized_flags:
        if name in PUBLIC_FLAG_WEIGHT_MAP:
            weight_total += PUBLIC_FLAG_WEIGHT_MAP[name]
            continue
        matched_weight = None
        for substring, weight in UNKNOWN_PUBLIC_FLAG_RULES:
            if substring in name:
                matched_weight = weight
                break
        if matched_weight is None:
            matched_weight = UNKNOWN_PUBLIC_DEFAULT_WEIGHT
        weight_total += matched_weight

    if pf and hasattr(pf, "value"):
        try:
            ctx.set_detail("public_flags_value", pf.value)
        except Exception:
            pass

    filtered_flags = [name for name in normalized_flags if name in ALLOWED_PUBLIC_FLAGS]
    ctx.set_detail("public_flags_list", filtered_flags)
    ctx.set_detail("public_flags_count", len(filtered_flags))
    ctx.set_detail("public_flags", ", ".join(normalized_flags) if normalized_flags else "none")

    if weight_total:
        ctx.add("public_flags_weight", weight_total)

def _score_additional_badges(ctx: ScoreContext) -> None:
    user = ctx.user
    badges: list[str] = []
    for attribute in ("badges", "profile_badges", "user_badges"):
        container = getattr(user, attribute, None)
        if not container:
            continue
        try:
            iterator = list(container)
        except TypeError:
            iterator = [container]
        for item in iterator:
            try:
                badges.append(str(item))
            except Exception:
                continue
    if badges:
        ctx.set_detail("badges_extra", badges[:BADGE_DETAIL_LIMIT])
        bonus = min(EXTRA_BADGE_BONUS_CAP, len(badges))
        ctx.add("extra_badges", bonus)

def _score_collectibles(ctx: ScoreContext) -> None:
    user = ctx.user
    collectibles = getattr(user, "collectibles", None)
    labels: list[str] = []
    if collectibles:
        try:
            iterator = list(collectibles)
        except TypeError:
            iterator = [collectibles]
        for item in iterator:
            if item is None:
                continue
            label = getattr(item, "label", None) or getattr(item, "name", None) or getattr(item, "title", None)
            if not label:
                try:
                    label = str(item)
                except Exception:
                    label = None
            if label:
                labels.append(str(label))
    if labels:
        ctx.set_detail("collectibles", labels[:COLLECTIBLE_DETAIL_LIMIT])
        ctx.set_detail("collectibles_count", len(labels))
        bonus = min(COLLECTIBLE_BONUS_CAP, len(labels))
        ctx.add("collectibles", bonus)

def _score_primary_guild(ctx: ScoreContext) -> None:
    user = ctx.user
    primary = getattr(user, "primary_guild", None)
    if not primary:
        return

    label = None
    tag = getattr(primary, "tag", None)
    if tag:
        label = str(tag)
    ident = getattr(primary, "id", None)
    if label and ident:
        label = f"{label} ({ident})"

    if label:
        ctx.set_detail("primary_guild", label)
        ctx.add(PRIMARY_GUILD_BONUS["label"], PRIMARY_GUILD_BONUS["score"])

def _score_member_flags(ctx: ScoreContext) -> None:
    member_flags = getattr(ctx.member, "flags", None)
    flag_entries = []
    weight = 0

    if member_flags:
        try:
            flag_entries = list(member_flags)
        except TypeError:
            flag_entries = []

    collected: list[str] = []
    for entry in flag_entries:
        try:
            name, enabled = entry
        except (TypeError, ValueError):
            continue
        if not enabled or name not in ALLOWED_MEMBER_FLAGS:
            continue
        collected.append(name)
        weight += MEMBER_FLAG_WEIGHT_MAP.get(name, 1)

    if member_flags and hasattr(member_flags, "value"):
        ctx.set_detail("member_flags_value", getattr(member_flags, "value", None))

    ctx.set_detail("member_flags_list", collected)
    ctx.set_detail("member_flags", ", ".join(collected) if collected else "none")
    ctx.set_detail("member_flags_count", len(collected))

    if weight:
        ctx.add("member_flags", weight)

def _score_boosting(ctx: ScoreContext) -> None:
    try:
        if getattr(ctx.member, "premium_since", None):
            ctx.add(NITRO_BOOST_BONUS["label"], NITRO_BOOST_BONUS["score"])
    except Exception:
        pass
