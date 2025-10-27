from __future__ import annotations

from typing import Any, Optional

import discord

from modules.utils import mod_logging
from modules.utils.localization import localize_message

from .utils.file_types import FILE_TYPE_LABELS
from .utils.latency import build_latency_fields

from .helpers.localization import (
    REPORT_BASE,
    SHARED_ROOT,
    localize_boolean,
    localize_category,
    localize_decision,
    localize_field_name,
    localize_reason,
    resolve_translator,
)


async def emit_verbose_report(
    scanner,
    *,
    message: discord.Message | None,
    author,
    guild_id: int | None,
    file_type: str | None,
    detected_mime: str | None,
    scan_result: dict[str, Any] | None,
    duration_ms: int,
) -> None:
    if message is None or guild_id is None or scan_result is None:
        return

    translator = resolve_translator(scanner)
    decision_key = "unknown"
    if scan_result.get("is_nsfw") is True:
        decision_key = "nsfw"
    elif scan_result.get("is_nsfw") is False:
        decision_key = "safe"

    decision_label = localize_decision(translator, decision_key, guild_id)
    normalized_file_type = (file_type or "unknown").lower()
    file_type_label = localize_message(
        translator,
        REPORT_BASE,
        f"file_types.{normalized_file_type}",
        fallback=FILE_TYPE_LABELS.get(normalized_file_type, detected_mime or normalized_file_type.title()),
        guild_id=guild_id,
    )

    actor = author or getattr(message, "author", None)
    actor_id = getattr(actor, "id", None)
    actor_mention = getattr(actor, "mention", None)
    if actor_mention is None and actor_id is not None:
        actor_mention = f"<@{actor_id}>"
    if actor_mention is None:
        actor_mention = localize_message(
            translator,
            REPORT_BASE,
            "description.unknown_user",
            fallback="Unknown user",
            guild_id=guild_id,
        )

    embed = discord.Embed(
        title=localize_message(
            translator,
            REPORT_BASE,
            "title",
            fallback="NSFW Scan Report",
            guild_id=guild_id,
        ),
        description="\n".join(
            [
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.user",
                    placeholders={"user": actor_mention},
                    fallback="**User:** {user}",
                    guild_id=guild_id,
                ),
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.type",
                    placeholders={"file_type": file_type_label},
                    fallback="**Type:** {file_type}",
                    guild_id=guild_id,
                ),
                localize_message(
                    translator,
                    REPORT_BASE,
                    "description.decision",
                    placeholders={"decision": decision_label},
                    fallback="**Decision:** {decision}",
                    guild_id=guild_id,
                ),
            ]
        ),
        color=discord.Color.orange() if scan_result.get("is_nsfw") else discord.Color.green(),
    )
    pipeline_metrics = scan_result.get("pipeline_metrics")
    for field in build_latency_fields(
        lambda key: localize_field_name(translator, key, guild_id),
        pipeline_metrics if isinstance(pipeline_metrics, dict) else None,
        duration_ms=duration_ms,
        breakdown_kwargs={"decimals": 1, "fallback_label_style": "title"},
        value_max_length=1024,
    ):
        embed.add_field(**field)

    cache_status = scan_result.get("cache_status")
    if cache_status:
        embed.add_field(
            name=localize_field_name(translator, "cache_status", guild_id),
            value=str(cache_status),
            inline=True,
        )

    for field_key, fallback_key in ("reason", "score"), ("category", "category"):
        value = scan_result.get(field_key)
        if value is None:
            continue
        if field_key == "reason":
            value = localize_reason(translator, value, guild_id)
        elif field_key == "category":
            value = localize_category(translator, value, guild_id)
        embed.add_field(
            name=localize_field_name(translator, fallback_key, guild_id),
            value=value,
            inline=True,
        )

    avatar = getattr(getattr(actor, "display_avatar", None), "url", None)
    if avatar:
        embed.set_thumbnail(url=avatar)

    try:
        await mod_logging.log_to_channel(
            embed=embed,
            channel_id=message.channel.id,
            bot=scanner.bot,
        )
    except Exception as exc:
        print(f"[verbose] Failed to send verbose embed: {exc}")


async def dispatch_callback(
    *,
    scanner,
    nsfw_callback,
    author,
    guild_id: int,
    scan_result: dict[str, Any],
    message: discord.Message,
    file: Optional[discord.File],
) -> None:
    translator = resolve_translator(scanner)
    category_name = scan_result.get("category") or "unspecified"
    confidence_value = None
    confidence_source = None
    try:
        if scan_result.get("score") is not None:
            confidence_value = float(scan_result.get("score"))
            confidence_source = "score"
        elif scan_result.get("similarity") is not None:
            confidence_value = float(scan_result.get("similarity"))
            confidence_source = "similarity"
    except Exception:
        confidence_value = None
        confidence_source = None

    category_label = localize_category(translator, category_name, guild_id)
    reason = localize_message(
        translator,
        SHARED_ROOT,
        "policy_violation",
        placeholders={"category": category_label},
        fallback="Detected potential policy violation (Category: **{category}**)",
        guild_id=guild_id,
    )

    evidence_file = file
    if evidence_file is None:
        return
    if not nsfw_callback or not scan_result:
        try:
            evidence_file.close()
        except Exception:
            pass
        file_obj = getattr(evidence_file, "fp", None)
        if file_obj:
            try:
                file_obj.close()
            except Exception:
                pass
        return

    try:
        await nsfw_callback(
            author,
            scanner.bot,
            guild_id,
            reason,
            evidence_file,
            message,
            confidence=confidence_value,
            confidence_source=confidence_source,
        )
    finally:
        try:
            evidence_file.close()
        except Exception:
            pass
        file_obj = getattr(evidence_file, "fp", None)
        if file_obj:
            try:
                file_obj.close()
            except Exception:
                pass


__all__ = [
    "emit_verbose_report",
    "dispatch_callback",
    "resolve_translator",
]
