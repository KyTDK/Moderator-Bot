from __future__ import annotations

DISCIPLINARY_TEXTS_FALLBACK: dict[str, str] = {
    "no_action": "No action taken.",
    "bulk_delete": "{deleted}/{total} messages bulk deleted.",
    "delete_summary": "{deleted}/{total} message(s) deleted.",
    "delete_missing": "Delete requested, but no message was provided.",
    "strike_issued": "Strike issued.",
    "strike_issued_with_expiry": "Strike issued with expiry.",
    "user_kicked": "User kicked.",
    "user_banned": "User banned.",
    "timeout_missing": "No timeout duration provided.",
    "timeout_invalid": "Invalid timeout duration: '{value}'.",
    "timeout_applied": "User timed out until <t:{timestamp}:R>.",
    "give_role_missing": "No role specified to give.",
    "give_role_not_found": "Role '{role}' not found.",
    "give_role_success": "Role '{role}' given.",
    "remove_role_missing": "No role specified to remove.",
    "remove_role_not_found": "Role '{role}' not found.",
    "remove_role_success": "Role '{role}' removed.",
    "warn_dm": "User warned via DM.",
    "warn_channel": "User warned via channel (DM failed).",
    "warn_failed": "Warning failed (couldn't send DM or channel message).",
    "broadcast_missing": "No broadcast message provided.",
    "broadcast_sent": "Broadcast message sent.",
    "broadcast_failed": "Broadcast failed.",
    "broadcast_no_channel": "No valid channel found for broadcast.",
    "unknown_action": "Unknown action: '{action}'.",
    "action_failed": "Action failed: {action}.",
}


STRIKE_TEXTS_FALLBACK: dict[str, str] = {
    "default_reason": "No reason provided",
    "embed_title_user": "You have received a strike",
    "embed_title_public": "{name} received a strike",
    "actions_heading": "**Actions Taken:**",
    "action_none": "**Action Taken:** No action applied",
    "action_skipped": "**Action Taken:** Punishments were skipped.",
    "action_item": "- {action}",
    "action_timeout": "Timeout (ends <t:{timestamp}:R>)",
    "action_ban": "Ban",
    "action_kick": "Kick",
    "action_delete": "Delete Message",
    "action_give_role": "Give Role {role}",
    "action_remove_role": "Remove Role {role}",
    "action_warn": "Warn: {message}",
    "action_broadcast": "Broadcast to {channel}: {message}",
    "action_strike": "Strike",
    "strike_count": "**Strike Count:** {count} strike(s).",
    "strike_until_ban": "{remaining} more strike(s) before a permanent ban.",
    "reason": "**Reason:** {reason}",
    "expires": "**Expires:** {expiry}",
    "issued_by": "Issued By",
    "expiry_never": "Never",
    "footer": "Server: {server}",
}


STRIKE_ERRORS_FALLBACK: dict[str, str] = {
    "too_many_strikes": "You cannot give the same player more than 100 strikes. Use `/strikes clear <user>` to reset their strikes.",
}


WARN_EMBED_FALLBACK: dict[str, str] = {
    "title": "⚠️ You Have Been Warned",
    "description": "{mention}, {message}\n\n{reason_block}{reminder}",
    "reason_block": "**Reason:** {reason}\n\n",
    "reminder": "Please follow the server rules to avoid further action such as timeouts, strikes, or bans.",
    "footer": "Server: {server}",
}


__all__ = [
    "DISCIPLINARY_TEXTS_FALLBACK",
    "STRIKE_TEXTS_FALLBACK",
    "STRIKE_ERRORS_FALLBACK",
    "WARN_EMBED_FALLBACK",
]
