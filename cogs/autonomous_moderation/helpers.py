import re
import discord
from datetime import timedelta
from modules.utils.discord_utils import safe_get_member
from modules.utils import mod_logging
from modules.moderation import strike
from typing import Iterable, Tuple, TYPE_CHECKING, Any
from modules.ai.token_utils import estimate_tokens as _estimate_tokens

if TYPE_CHECKING:
    from modules.core.moderator_bot import ModeratorBot

IMAGE_EXT = re.compile(r"\.(?:png|jpe?g|webp|bmp|tiff?)$", re.I)
GIF_EXT = re.compile(r"\.(?:gif|apng)$", re.I)
TENOR_RE = re.compile(r"(?:tenor\.com|giphy\.com)", re.I)
VIDEO_EXT = re.compile(r"\.(?:mp4|m4v|webm|mov|avi|mkv|gifv)$", re.I)

def collapse_media(url: str) -> str:
    if TENOR_RE.search(url):
        return "[gif]"
    if GIF_EXT.search(url):
        return "[gif]"
    if IMAGE_EXT.search(url):
        return "[image]"
    if VIDEO_EXT.search(url):
        return "[video]"
    return url

def estimate_tokens(text: str) -> int:
    # Delegate to shared token estimator for consistency
    return _estimate_tokens(text)

async def format_event(
    msg: discord.Message,
    content: str,
    tag: str,
    delta: timedelta | None,
    new_member_threshold: timedelta,
) -> str | None:
    author = await safe_get_member(msg.guild, msg.author.id)
    if not author:
        return None

    tokens = [collapse_media(w) if w.startswith("http") else w for w in content.split()]
    content = " ".join(tokens)

    if delta is None:
        time_since = "First message in batch."
    else:
        mins, secs = divmod(int(delta.total_seconds()), 60)
        time_since = f"{mins} min {secs}s after previous." if mins else f"{secs}s after previous."

    joined_at = getattr(author, "joined_at", None)
    new_member = ""
    if joined_at:
        age = msg.created_at - joined_at
        if age < new_member_threshold:
            m_mins, m_secs = divmod(int(age.total_seconds()), 60)
            m_hours, m_mins = divmod(m_mins, 60)
            parts = [f"{m_hours}h" if m_hours else "", f"{m_mins}m" if m_mins else "", f"{m_secs}s" if not m_hours and not m_mins else ""]
            pretty_age = " ".join(p for p in parts if p)
            new_member = f"\nNOTE: joined server {pretty_age} ago."

    return (
        f"[{time_since}]{new_member}\n"
        f"{tag.upper()}\n"
        f"AUTHOR: {author.display_name} (id = {author.id})\n"
        f"MESSAGE ID: {msg.id}\n"
        f"MESSAGE: {content}\n"
        "---"
    )

async def build_transcript(
    batch: list[tuple[str, str, discord.Message]],
    max_tokens: int,
    current_total_tokens: int,
    new_member_threshold: timedelta,
):
    lines: list[str] = []
    tokens: list[int] = []
    trimmed_batch = batch[:]
    prev_time = None

    for tag, text, msg in trimmed_batch:
        timestamp = msg.created_at
        delta = timestamp - prev_time if prev_time else None
        prev_time = timestamp

        line = await format_event(msg, text, tag, delta, new_member_threshold)
        if line:
            tok = estimate_tokens(line)
            lines.append(line)
            tokens.append(tok)

    total_tokens = current_total_tokens + sum(tokens)

    while trimmed_batch and total_tokens > max_tokens:
        total_tokens -= tokens.pop(0)
        trimmed_batch.pop(0)
        lines.pop(0)

    transcript = "\n".join(lines)
    return transcript, total_tokens, trimmed_batch

HELPERS_BASE = "cogs.autonomous_moderation.helpers"


def _translate_helper(
    bot: "ModeratorBot",
    guild: discord.Guild | None,
    suffix: str,
    *,
    placeholders: dict[str, Any] | None = None,
    fallback: str,
) -> str:
    locale = bot._guild_locales.resolve(guild) if guild else None
    key = f"{HELPERS_BASE}.{suffix}" if suffix else HELPERS_BASE
    return bot.translate(
        key,
        locale=locale,
        placeholders=placeholders,
        fallback=fallback,
    )


async def apply_actions_and_log(
    *,
    bot: "ModeratorBot",
    member: discord.Member,
    configured_actions: list[str],
    reason: str,
    rule: str,
    messages: list[discord.Message],
    aimod_debug: bool,
    ai_channel_id: int | None,
    monitor_channel_id: int | None,
    ai_actions: list[str] | None = None,
    fanout: bool = False,
    violation_cache: dict | None = None,
) -> None:
    # Apply actions
    await strike.perform_disciplinary_action(
        bot=bot,
        user=member,
        action_string=configured_actions,
        reason=reason,
        source="batch_ai",
        message=messages,
    )

    # Record violation for history
    if violation_cache is not None:
        violation_cache[member.id].append((rule, ", ".join(configured_actions)))

    # Build embed
    guild = member.guild if hasattr(member, "guild") else None
    user_display = member.mention if member else getattr(member, "id", "Unknown")
    joined_actions = ", ".join(configured_actions)
    actions_none = _translate_helper(
        bot,
        guild,
        "embed.fields.applied_actions.none",
        fallback="None",
    )
    actions_display = joined_actions or actions_none
    embed = discord.Embed(
        title=_translate_helper(
            bot,
            guild,
            "embed.title",
            fallback="AI-Flagged Violation",
        ),
        description=_translate_helper(
            bot,
            guild,
            "embed.description",
            placeholders={
                "user": user_display,
                "rule": rule,
                "reason": reason,
                "actions": actions_display,
            },
            fallback=(
                f"User: {user_display}\n"
                f"Rule Broken: {rule}\n"
                f"Reason: {reason}\n"
                f"Actions: {actions_display}"
            ),
        ),
        colour=discord.Colour.red(),
    )

    if aimod_debug:
        if fanout:
            embed.add_field(
                name=_translate_helper(
                    bot,
                    guild,
                    "embed.fields.fanout.name",
                    fallback="Fan-out",
                ),
                value=_translate_helper(
                    bot,
                    guild,
                    "embed.fields.fanout.value",
                    fallback="Multiple authors detected; applied per-author actions.",
                ),
                inline=False,
            )
        try:
            ai_decision_value = ", ".join(ai_actions) if ai_actions else ""
        except Exception:
            ai_decision_value = ""
            ai_decision = _translate_helper(
                bot,
                guild,
                "embed.fields.ai_decision.unknown",
                fallback="Unknown",
            )
        else:
            ai_decision = (
                ai_decision_value
                if ai_decision_value
                else _translate_helper(
                    bot,
                    guild,
                    "embed.fields.ai_decision.none",
                    fallback="None",
                )
            )
        embed.add_field(
            name=_translate_helper(
                bot,
                guild,
                "embed.fields.ai_decision.name",
                fallback="AI Decision",
            ),
            value=ai_decision,
            inline=False,
        )
        embed.add_field(
            name=_translate_helper(
                bot,
                guild,
                "embed.fields.applied_actions.name",
                fallback="Applied Actions",
            ),
            value=actions_display,
            inline=False,
        )

        # Include flagged messages (content)
        if messages:
            def _trim(s: str, n: int = 300) -> str:
                s = s or ""
                return s if len(s) <= n else s[:n] + "…"

            flagged_lines = []
            entry_template = _translate_helper(
                bot,
                guild,
                "embed.fields.flagged_messages.entry",
                fallback="• ID {id}: {content}",
            )
            no_content = _translate_helper(
                bot,
                guild,
                "embed.fields.flagged_messages.no_content",
                fallback="[no text content]",
            )
            for m in messages:
                content = m.content or no_content
                flagged_lines.append(
                    entry_template.format(id=m.id, content=_trim(content))
                )
            flagged_blob = "\n".join(flagged_lines)
            embed.add_field(
                name=_translate_helper(
                    bot,
                    guild,
                    "embed.fields.flagged_messages.name",
                    fallback="Flagged Message(s)",
                ),
                value=flagged_blob[:1000],
                inline=False,
            )

    log_channel = ai_channel_id or monitor_channel_id
    if log_channel:
        await mod_logging.log_to_channel(embed, log_channel, bot)

def resolve_configured_actions(settings: dict, ai_actions: list[str], setting_key: str) -> list[str]:
    configured = settings.get(setting_key) or ["auto"]
    if "auto" in configured:
        return ai_actions
    return configured

def build_no_violations_embed(
    bot: "ModeratorBot", guild: discord.Guild | None, scanned_count: int, mode: str
) -> discord.Embed:
    embed = discord.Embed(
        title=_translate_helper(
            bot,
            guild,
            "no_violations.title",
            fallback="AI Moderation Scan (Debug)",
        ),
        description=_translate_helper(
            bot,
            guild,
            "no_violations.description",
            fallback="No violations were found in the latest scan.",
        ),
        colour=discord.Colour.dark_grey(),
    )
    embed.add_field(
        name=_translate_helper(
            bot,
            guild,
            "no_violations.fields.scanned_messages",
            fallback="Scanned Messages",
        ),
        value=str(scanned_count),
        inline=True,
    )
    embed.add_field(
        name=_translate_helper(
            bot,
            guild,
            "no_violations.fields.mode",
            fallback="Mode",
        ),
        value=mode,
        inline=True,
    )
    return embed

async def prepare_report_batch(trigger_msg: discord.Message) -> list[tuple[str, str, discord.Message]]:
    """Fetch recent channel history for report mode and format entries."""
    entries: list[tuple[str, str, discord.Message]] = []
    fetched = [msg async for msg in trigger_msg.channel.history(limit=50)]
    fetched.sort(key=lambda m: m.created_at)
    for msg in fetched:
        content = msg.content
        if content:
            if msg.reference:
                content = f"(response to message_id={msg.reference.message_id}) {content}"
            entries.append(("Message", content, msg))
    return entries

def build_violation_history(
    batch: list[tuple[str, str, discord.Message]],
    violation_cache: dict[int, list[Tuple[str, str]]],
) -> str:
    user_ids = {msg.author.id for _, _, msg in batch if hasattr(msg, "author")}
    violation_blocks: list[str] = []
    for uid in user_ids:
        history = violation_cache[uid]
        if history:
            lines = [
                f"{i+1}. {reason} — previously punished with {action}"
                for i, (reason, action) in enumerate(history)
            ]
            joined = "\n".join(lines)
            violation_blocks.append(
                f"User {uid} has {len(history)} recent violation(s):\n{joined}"
            )
    violation_history = "\n".join(violation_blocks) if violation_blocks else "No recent violations on record."
    return f"Violation history:\n{violation_history}\n\n"

def build_violation_history_for_users(
    user_ids: set[int] | list[int],
    violation_cache: dict[int, list[Tuple[str, str]]],
) -> str:
    """Build a violation history block for a set of user IDs.

    Mirrors build_violation_history but without needing a message batch.
    """
    violation_blocks: list[str] = []
    for uid in user_ids:
        history = violation_cache[uid]
        if history:
            lines = [
                f"{i+1}. {reason} → previously punished with {action}"
                for i, (reason, action) in enumerate(history)
            ]
            joined = "\n".join(lines)
            violation_blocks.append(
                f"User {uid} has {len(history)} recent violation(s):\n{joined}"
            )
    violation_history = (
        "\n".join(violation_blocks) if violation_blocks else "No recent violations on record."
    )
    return f"Violation history:\n{violation_history}\n\n"

def aggregate_violations(
    violations: Iterable,
    batch: list[tuple[str, str, discord.Message]],
) -> tuple[dict[int, dict], set[int]]:
    """Aggregate AI violations so there is at most one record per user.
    Returns: (aggregated_map, fanout_authors_set)
    """
    aggregated: dict[int, dict] = {}
    fanout_authors: set[int] = set()

    id_to_msg = {m.id: m for (_, _, m) in batch}

    for v in violations:
        actions = list(getattr(v, "actions", []) or [])
        rule = (getattr(v, "rule", "") or "").strip()
        reason = (getattr(v, "reason", "") or "").strip()
        raw_ids = getattr(v, "message_ids", None) or []
        msg_ids = {int(m) for m in raw_ids if str(m).isdigit()}

        if not actions or not rule or not msg_ids:
            continue

        # Ensure delete if messages present
        if msg_ids and "delete" not in actions:
            actions.append("delete")

        # Map to messages
        messages = [id_to_msg[mid] for mid in msg_ids if mid in id_to_msg]
        if not messages:
            continue

        by_author: dict[int, list[discord.Message]] = {}
        for m in messages:
            by_author.setdefault(m.author.id, []).append(m)
        if len(by_author) > 1:
            fanout_authors.update(by_author.keys())

        for author_id, msgs in by_author.items():
            agg = aggregated.setdefault(
                author_id,
                {"messages": [], "actions": set(), "reasons": [], "rules": set()},
            )
            existing_ids = {m.id for m in agg["messages"]}
            for m in msgs:
                if m.id not in existing_ids:
                    agg["messages"].append(m)
                    existing_ids.add(m.id)
            agg["actions"].update(actions)
            if reason:
                agg["reasons"].append(reason)
            if rule:
                agg["rules"].add(rule)

    return aggregated, fanout_authors

def summarize_reason_rule(
    bot: "ModeratorBot",
    guild: discord.Guild | None,
    reasons: list[str] | None,
    rules: set[str] | list[str] | None,
) -> tuple[str, str]:
    """Create user-facing reason and rule strings from collections.

    - Reasons: prefer single entry; otherwise combine with semicolons; default fallback.
    - Rules: prefer single entry; otherwise combine with commas; default fallback.
    """
    reasons = reasons or []
    rules_list = list(rules or [])

    if not reasons:
        out_reason = _translate_helper(
            bot,
            guild,
            "summary.reason_default",
            fallback="Violation detected",
        )
    elif len(reasons) == 1:
        out_reason = reasons[0]
    else:
        joined = "; ".join(reasons)
        out_reason = _translate_helper(
            bot,
            guild,
            "summary.reason_multiple",
            placeholders={"reasons": joined},
            fallback=f"Multiple violations: {joined}",
        )

    if not rules_list:
        out_rule = _translate_helper(
            bot,
            guild,
            "summary.rule_default",
            fallback="Rule violation",
        )
    elif len(rules_list) == 1:
        out_rule = rules_list[0]
    else:
        joined_rules = ", ".join(rules_list)
        out_rule = _translate_helper(
            bot,
            guild,
            "summary.rule_multiple",
            placeholders={"rules": joined_rules},
            fallback=f"Multiple rules: {joined_rules}",
        )

    return out_reason, out_rule
