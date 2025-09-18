from __future__ import annotations

import discord

from ..conditions import MEMBER_FLAG_CHOICES, PUBLIC_FLAG_CHOICES

MIN_SCORE = 0
MAX_SCORE = 100

ACCOUNT_AGE_BONUSES = (
    {"label": "account_age>=1095d", "min_days": 1_095, "score": 40},
    {"label": "account_age>=730d", "min_days": 730, "score": 34},
    {"label": "account_age>=365d", "min_days": 365, "score": 28},
    {"label": "account_age>=180d", "min_days": 180, "score": 20},
    {"label": "account_age>=90d", "min_days": 90, "score": 14},
    {"label": "account_age>=30d", "min_days": 30, "score": 9},
    {"label": "account_age>=14d", "min_days": 14, "score": 6},
    {"label": "account_age>=7d", "min_days": 7, "score": 3},
)

ACCOUNT_AGE_PENALTIES = (
    {"label": "account_age<=3d", "max_days": 3, "score": -5},
    {"label": "account_age<=7d", "max_days": 7, "score": -2},
)

GUILD_TENURE_BONUSES = (
    {"label": "guild_tenure>=730d", "min_days": 730, "score": 12},
    {"label": "guild_tenure>=365d", "min_days": 365, "score": 10},
    {"label": "guild_tenure>=180d", "min_days": 180, "score": 8},
    {"label": "guild_tenure>=90d", "min_days": 90, "score": 6},
    {"label": "guild_tenure>=30d", "min_days": 30, "score": 4},
    {"label": "guild_tenure>=14d", "min_days": 14, "score": 3},
    {"label": "guild_tenure>=7d", "min_days": 7, "score": 2},
)

GUILD_TENURE_PENALTIES = ()

MEMBERSHIP_PENDING_PENALTY = {"label": "membership_screening_pending", "score": -6}

CREATION_TO_JOIN_BONUSES = (
    {"label": "join_after_180d", "min_minutes": 259_200, "score": 15},
    {"label": "join_after_90d", "min_minutes": 129_600, "score": 12},
    {"label": "join_after_30d", "min_minutes": 43_200, "score": 9},
    {"label": "join_after_14d", "min_minutes": 20_160, "score": 6},
    {"label": "join_after_7d", "min_minutes": 10_080, "score": 4},
)

CREATION_TO_JOIN_PENALTIES = (
    {"label": "join_soon_after_creation", "max_minutes": 10, "score": -5},
    {"label": "join_within_1h", "max_minutes": 60, "score": -3},
    {"label": "join_within_1d", "max_minutes": 1_440, "score": -2},
)

PROFILE_WEIGHTS = {
    "avatar_present": 12,
    "avatar_missing": -4,
    "server_avatar": 4,
    "banner_present": 3,
    "accent_color": 2,
    "nickname_set": 4,
    "animated_avatar": 3,
    "global_name": 3,
    "avatar_decoration": 2,
}

COLLECTIBLE_DETAIL_LIMIT = 10
BADGE_DETAIL_LIMIT = 10

STATUS_BONUS = {"label": "status!=offline", "score": 8}
ACTIVITY_BASE_BONUS = {"label": "has_activity", "score": 10}
ACTIVITY_TYPE_WEIGHTS = {
    discord.ActivityType.playing: {"label": "playing", "score": 3},
    discord.ActivityType.listening: {"label": "listening", "score": 2},
    discord.ActivityType.streaming: {"label": "streaming", "score": 5},
    discord.ActivityType.custom: {"label": "custom_status", "score": 3},
}
ACTIVITY_MANY_BONUS = {"label": "many_activities", "min_count": 3, "score": 3}
PLATFORM_PRESENCE_BONUS = {"label": "multi_platform_online", "min_platforms": 2, "score": 2}

PUBLIC_FLAG_WEIGHT_MAP = {
    "staff": 10,
    "partner": 8,
    "bug_hunter_level_2": 7,
    "bug_hunter": 5,
    "early_supporter": 5,
    "active_developer": 5,
    "discord_certified_moderator": 6,
    "moderator_programs_alumni": 5,
    "hypesquad": 3,
    "hypesquad_bravery": 3,
    "hypesquad_brilliance": 3,
    "hypesquad_balance": 3,
    "verified_bot": 0,
    "verified_bot_developer": 5,
    "early_verified_developer": 5,
}

UNKNOWN_PUBLIC_FLAG_RULES = (
    ("moderator", 6),
    ("founder", 4),
    ("subscriber", 4),
    ("member_since", 4),
    ("quest", 3),
    ("contributor", 3),
    ("contrib", 3),
    ("beta", 2),
    ("alpha", 2),
)
UNKNOWN_PUBLIC_DEFAULT_WEIGHT = 2

EXTRA_BADGE_BONUS_CAP = 4
COLLECTIBLE_BONUS_CAP = 5
PRIMARY_GUILD_BONUS = {"label": "primary_guild", "score": 3}

MEMBER_FLAG_WEIGHT_MAP = {
    "completed_onboarding": 4,
    "completed_home_actions": 3,
    "started_onboarding": 2,
    "started_home_actions": 2,
    "did_rejoin": 2,
    "dm_settings_upsell_acknowledged": 2,
    "bypasses_verification": 2,
    "automod_quarantined_username": -5,
    "automod_quarantined_guild_tag": -4,
    "guest": 0,
}

NITRO_BOOST_BONUS = {"label": "boosting", "score": 6}

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
    "score": -4,
}

ALLOWED_MEMBER_FLAGS = frozenset(MEMBER_FLAG_CHOICES)
ALLOWED_PUBLIC_FLAGS = frozenset(PUBLIC_FLAG_CHOICES)

BOT_ACCOUNT_LABEL = "bot_account"
