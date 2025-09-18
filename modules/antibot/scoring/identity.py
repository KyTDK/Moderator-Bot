from __future__ import annotations

from .config import (
    BOT_ACCOUNT_LABEL,
    USERNAME_DIGIT_RULE,
    USERNAME_ENTROPY_RULE,
)
from .context import ScoreContext
from ..utils import digits_ratio, longest_digit_run, shannon_entropy

__all__ = ["apply"]

def apply(ctx: ScoreContext) -> None:
    """Apply identity centric rules (bot detection and username heuristics)."""
    is_bot = _score_bot_account(ctx)
    _score_username(ctx, is_bot)

def _score_bot_account(ctx: ScoreContext) -> bool:
    try:
        is_bot = bool(getattr(ctx.user, "bot", False))
    except Exception:
        is_bot = False
    ctx.set_detail("is_bot_account", is_bot)

    if is_bot:
        ctx.contributions[BOT_ACCOUNT_LABEL] = -ctx.score if ctx.score > 0 else 0
        ctx.score = min(ctx.score, 0)

    return is_bot

def _score_username(ctx: ScoreContext, is_bot: bool) -> None:
    try:
        uname = (
            getattr(ctx.user, "global_name", None)
            or getattr(ctx.user, "name", "")
            or ""
        ).strip()
    except Exception:
        uname = ""

    uname_lower = uname.lower()
    entropy = shannon_entropy(uname_lower)
    ratio = digits_ratio(uname_lower)
    longest_run = longest_digit_run(uname_lower)

    ctx.set_detail("name_entropy", round(entropy, 2))
    ctx.set_detail("name_digits_ratio", round(ratio, 2))
    ctx.set_detail("name_longest_digit_run", longest_run)

    digit_rule = USERNAME_DIGIT_RULE
    if (
        len(uname_lower) >= digit_rule["min_length"]
        and ratio >= digit_rule["min_ratio"]
        and longest_run >= digit_rule["min_run"]
    ):
        ctx.add(digit_rule["label"], digit_rule["score"])

    entropy_rule = USERNAME_ENTROPY_RULE
    if len(uname_lower) >= entropy_rule["min_length"] and entropy <= entropy_rule["max_entropy"]:
        ctx.add(entropy_rule["label"], entropy_rule["score"])
