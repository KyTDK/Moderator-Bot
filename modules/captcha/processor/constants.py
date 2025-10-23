from __future__ import annotations

from typing import Any

PROCESSOR_BASE_KEY = "modules.captcha.processor"

SUCCESS_TEXTS_FALLBACK: dict[str, Any] = {
    "reason": "Captcha verification successful",
    "vpn_post_reason": "Applying VPN post-verification role adjustments.",
    "embed": {
        "title": "Captcha Verification Passed",
        "description": "{mention} ({user_id}) passed captcha verification.",
        "fields": {
            "actions": "Actions Applied",
            "attempts": "Attempts",
            "provider": "Provider",
            "challenge": "Challenge",
            "review": "Review",
        },
    },
}

FAILURE_TEXTS_FALLBACK: dict[str, Any] = {
    "reason_default": "Failed captcha verification.",
    "notes": {
        "deferred_generic": "Failure actions deferred; captcha attempts remain.",
        "deferred_remaining": "Failure actions deferred; {remaining} {attempts_word} remaining.",
        "member_left": "Member is no longer in the guild; configured failure actions were skipped.",
        "policy_duplicate": "Policy actions already applied for this captcha attempt.",
        "policy_challenge": "Secondary verification required ({escalation}).",
        "policy_challenge_generic": "Secondary verification required.",
        "vpn_cached_stale": (
            "VPN intel served from a stale cache; consider a softer response before taking "
            "irreversible actions."
        ),
        "policy_actions_fallback": "VPN actions enforced using guild-configured defaults.",
    },
    "attempts": {
        "unlimited": "Unlimited attempts remain.",
        "additional": "Additional attempts remain.",
        "remaining": "{count} {attempts_word} remaining.",
        "word_one": "attempt",
        "word_other": "attempts",
    },
    "embed": {
        "title_failed": "Captcha Verification Failed",
        "title_attempt": "Captcha Attempt Failed",
        "description_failed": "{mention} ({user_id}) failed captcha verification.",
        "description_attempt": "{mention} ({user_id}) failed a captcha attempt.",
        "fields": {
            "actions": "Actions Applied",
            "notes": "Notes",
            "attempts": "Attempts",
            "remaining": "Attempts Remaining",
            "challenge": "Challenge",
            "reason": "Reason",
            "review": "Review",
            "policy": "VPN Screening",
            "risk": "VPN Risk",
            "providers": "Providers",
            "behavior": "Behaviour Signals",
            "cached_state": "Intel Cache",
            "hard_signals": "Hard Signals",
            "policy_actions": "Policy Actions",
            "flagged_at": "Flagged At",
        },
    },
}

ERROR_TEXTS_FALLBACK: dict[str, str] = {
    "role_assignment_failed": "Failed to apply captcha success actions due to a Discord API error.",
    "unknown_token": "No pending captcha verification found for this user.",
    "token_mismatch": "Captcha token does not match the pending verification.",
    "missing_permissions": "Bot is missing permissions to apply captcha failure actions.",
    "action_failed": "Failed to apply captcha failure actions due to a Discord API error.",
    "guild_not_found": "Guild {guild_id} is not available to this bot.",
    "guild_fetch_failed": "Failed to fetch guild {guild_id} from Discord.",
    "member_not_found": "Member {user_id} not found in guild {guild_id}.",
    "member_fetch_failed": "Failed to fetch member {user_id} from guild {guild_id}.",
}

__all__ = [
    "PROCESSOR_BASE_KEY",
    "SUCCESS_TEXTS_FALLBACK",
    "FAILURE_TEXTS_FALLBACK",
    "ERROR_TEXTS_FALLBACK",
]
