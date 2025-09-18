from __future__ import annotations

import discord

from ..conditions import MEMBER_FLAG_CHOICES, PUBLIC_FLAG_CHOICES

MIN_SCORE = 0
MAX_SCORE = 100

ACCOUNT_AGE_BONUSES = (
    {"label": "account_age>=365d", "min_days": 365, "score": 24},
    {"label": "account_age>=180d", "min_days": 180, "score": 18},
    {"label": "account_age>=90d", "min_days": 90, "score": 12},
    {"label": "account_age>=30d", "min_days": 30, "score": 7},
    {"label": "account_age>=14d", "min_days": 14, "score": 4},
    {"label": "account_age>=7d", "min_days": 7, "score": 2},
)

ACCOUNT_AGE_PENALTIES = (
    {"label": "account_age<=3d", "max_days": 3, "score": -6},
    {"label": "account_age<=7d", "max_days": 7, "score": -2},
)

GUILD_TENURE_BONUSES = (
    {"label": "guild_tenure>=365d", "min_days": 365, "score": 12},
    {"label": "guild_tenure>=180d", "min_days": 180, "score": 9},
    {"label": "guild_tenure>=90d", "min_days": 90, "score": 6},
    {"label": "guild_tenure>=30d", "min_days": 30, "score": 4},
    {"label": "guild_tenure>=14d", "min_days": 14, "score": 3},
    {"label": "guild_tenure>=7d", "min_days": 7, "score": 2},
)

GUILD_TENURE_PENALTIES = ()

MEMBERSHIP_PENDING_PENALTY = {"label": "membership_screening_pending", "score": -6}

CREATION_TO_JOIN_PENALTIES = (
    {"label": "join_soon_after_creation", "max_minutes": 10, "score": -6},
    {"label": "join_within_1h", "max_minutes": 60, "score": -4},
    {"label": "join_within_1d", "max_minutes": 1_440, "score": -2},
)

PROFILE_WEIGHTS = {
    "avatar_present": 9,
    "avatar_missing": -5,
    "server_avatar": 3,
    "banner_present": 2,
    "accent_color": 1,
    "nickname_set": 3,
    "animated_avatar": 2,
    "global_name": 2,
    "avatar_decoration": 1,
}

COLLECTIBLE_DETAIL_LIMIT = 10
BADGE_DETAIL_LIMIT = 10

STATUS_BONUS = {"label": "status!=offline", "score": 6}
ACTIVITY_BASE_BONUS = {"label": "has_activity", "score": 8}
ACTIVITY_TYPE_WEIGHTS = {
    discord.ActivityType.playing: {"label": "playing", "score": 2},
    discord.ActivityType.listening: {"label": "listening", "score": 2},
    discord.ActivityType.streaming: {"label": "streaming", "score": 4},
    discord.ActivityType.custom: {"label": "custom_status", "score": 2},
}
ACTIVITY_MANY_BONUS = {"label": "many_activities", "min_count": 3, "score": 2}
PLATFORM_PRESENCE_BONUS = {"label": "multi_platform_online", "min_platforms": 2, "score": 1}

PUBLIC_FLAG_WEIGHT_MAP = {
    "staff": 6,
    "partner": 5,
    "bug_hunter_level_2": 5,
    "bug_hunter": 3,
    "early_supporter": 3,
    "active_developer": 3,
    "discord_certified_moderator": 4,
    "moderator_programs_alumni": 3,
    "hypesquad": 2,
    "hypesquad_bravery": 2,
    "hypesquad_brilliance": 2,
    "hypesquad_balance": 2,
    "verified_bot": 0,
    "verified_bot_developer": 4,
    "early_verified_developer": 4,
}

UNKNOWN_PUBLIC_FLAG_RULES = (
    ("moderator", 4),
    ("founder", 3),
    ("subscriber", 3),
    ("member_since", 3),
    ("quest", 2),
    ("contributor", 2),
    ("contrib", 2),
    ("beta", 1),
    ("alpha", 1),
)
UNKNOWN_PUBLIC_DEFAULT_WEIGHT = 1

EXTRA_BADGE_BONUS_CAP = 3
COLLECTIBLE_BONUS_CAP = 4
PRIMARY_GUILD_BONUS = {"label": "primary_guild", "score": 2}

MEMBER_FLAG_WEIGHT_MAP = {
    "completed_onboarding": 3,
    "completed_home_actions": 2,
    "started_onboarding": 1,
    "started_home_actions": 1,
    "did_rejoin": 1,
    "dm_settings_upsell_acknowledged": 1,
    "bypasses_verification": 1,
    "automod_quarantined_username": -4,
    "automod_quarantined_guild_tag": -3,
    "guest": 0,
}

NITRO_BOOST_BONUS = {"label": "boosting", "score": 5}

USERNAME_DIGIT_RULE = {
    "label": "many_digits",
    "min_ratio": 0.5,
    "min_run": 5,
    "min_length": 8,
    "score": -6,
}
USERNAME_ENTROPY_RULE = {
    "label": "low_entropy_name",
    "max_entropy": 2.2,
    "min_length": 6,
    "score": -3,
}

ALLOWED_MEMBER_FLAGS = frozenset(MEMBER_FLAG_CHOICES)
ALLOWED_PUBLIC_FLAGS = frozenset(PUBLIC_FLAG_CHOICES)

BOT_ACCOUNT_LABEL = "bot_account"
