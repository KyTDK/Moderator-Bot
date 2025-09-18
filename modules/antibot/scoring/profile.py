from __future__ import annotations

from .config import PROFILE_WEIGHTS
from .context import ScoreContext

__all__ = ["apply"]

def apply(ctx: ScoreContext) -> None:
    """Apply profile related scoring rules."""
    _score_avatars(ctx)
    _score_banner(ctx)
    _score_accent(ctx)
    _score_nickname(ctx)
    _score_avatar_animation(ctx)
    _score_global_profile_features(ctx)

def _score_avatars(ctx: ScoreContext) -> None:
    member = ctx.member
    user = ctx.user

    try:
        avatar_obj = getattr(member, "display_avatar", None)
        is_default = avatar_obj.is_default() if avatar_obj else not bool(getattr(user, "avatar", None))
    except Exception:
        is_default = not bool(getattr(user, "avatar", None))

    has_avatar = not is_default
    ctx.set_detail("has_avatar", has_avatar)
    ctx.set_detail("default_avatar", is_default)

    if has_avatar:
        ctx.add("avatar_present", PROFILE_WEIGHTS["avatar_present"])
    else:
        ctx.add("avatar_missing", PROFILE_WEIGHTS["avatar_missing"])

    if getattr(member, "guild_avatar", None) is not None:
        ctx.add("server_avatar", PROFILE_WEIGHTS["server_avatar"])

def _score_banner(ctx: ScoreContext) -> None:
    user = ctx.user
    banner_asset = getattr(user, "banner", None)
    has_banner = banner_asset is not None
    ctx.set_detail("has_banner", has_banner)

    if has_banner:
        ctx.add("banner_present", PROFILE_WEIGHTS["banner_present"])
        banner_url = None
        try:
            banner_url = banner_asset.url  # type: ignore[attr-defined]
        except Exception:
            try:
                banner_url = str(banner_asset)
            except Exception:
                banner_url = None
        if banner_url:
            ctx.set_detail("banner_url", banner_url)

def _score_accent(ctx: ScoreContext) -> None:
    user = ctx.user
    accent = getattr(user, "accent_color", None)
    has_accent = accent is not None
    ctx.set_detail("has_accent_color", has_accent)
    if has_accent:
        ctx.add("accent_color", PROFILE_WEIGHTS["accent_color"])
        try:
            ctx.set_detail("accent_color_value", getattr(accent, "value", None))
        except Exception:
            pass

def _score_nickname(ctx: ScoreContext) -> None:
    if getattr(ctx.member, "nick", None):
        ctx.add("nickname_set", PROFILE_WEIGHTS["nickname_set"])

def _score_avatar_animation(ctx: ScoreContext) -> None:
    try:
        avatar = getattr(ctx.member, "display_avatar", None)
        if avatar and avatar.is_animated():
            ctx.add("animated_avatar", PROFILE_WEIGHTS["animated_avatar"])
    except Exception:
        pass

def _score_global_profile_features(ctx: ScoreContext) -> None:
    user = ctx.user
    try:
        if getattr(user, "global_name", None):
            ctx.add("global_name", PROFILE_WEIGHTS["global_name"])
    except Exception:
        pass

    has_decoration = False
    try:
        decoration = getattr(user, "avatar_decoration", None)
        has_decoration = bool(decoration)
    except Exception:
        has_decoration = False
    ctx.set_detail("has_avatar_decoration", has_decoration)
    if has_decoration:
        ctx.add("avatar_decoration", PROFILE_WEIGHTS["avatar_decoration"])
