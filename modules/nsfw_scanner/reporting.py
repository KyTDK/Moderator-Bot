from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

import discord

from modules.utils import mod_logging
from modules.utils.localization import localize_message

from .utils.file_types import FILE_TYPE_LABELS
from .utils.latency import build_latency_fields


@dataclass(frozen=True)
class ScanFieldSpec:
    """Configuration for adding a scan result field to a verbose embed."""

    field_key: str
    source_key: str | None = None
    inline: bool = True
    max_length: int | None = 1024
    formatter: Callable[[Any, dict[str, Any], Any, int, int], Any] | None = None

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


def _resolve_decision_key(scan_result: dict[str, Any] | None) -> str:
    if not isinstance(scan_result, dict):
        return "unknown"
    if scan_result.get("is_nsfw") is True:
        return "nsfw"
    if scan_result.get("is_nsfw") is False:
        return "safe"
    return "unknown"


def _resolve_file_type_label(
    translator,
    guild_id: int,
    file_type: str | None,
    detected_mime: str | None,
) -> str:
    normalized_file_type = (file_type or "unknown").lower()
    return localize_message(
        translator,
        REPORT_BASE,
        f"file_types.{normalized_file_type}",
        fallback=FILE_TYPE_LABELS.get(
            normalized_file_type, detected_mime or normalized_file_type.title()
        ),
        guild_id=guild_id,
    )


def _resolve_actor_mention(actor, translator, guild_id: int) -> str:
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
    return actor_mention


def _build_description_lines(
    *,
    translator,
    guild_id: int,
    actor_text: str,
    file_type_label: str,
    decision_label: str,
    filename: str | None,
    bold_labels: bool,
) -> list[str]:
    fallback_map = {
        True: {
            "user": "**User:** {user}",
            "file": "**File:** `{filename}`",
            "type": "**Type:** {file_type}",
            "decision": "**Decision:** {decision}",
        },
        False: {
            "user": "User: {user}",
            "file": "File: `{filename}`",
            "type": "Type: `{file_type}`",
            "decision": "Decision: **{decision}**",
        },
    }
    fallbacks = fallback_map[bold_labels]
    lines = [
        localize_message(
            translator,
            REPORT_BASE,
            "description.user",
            placeholders={"user": actor_text},
            fallback=fallbacks["user"],
            guild_id=guild_id,
        ),
    ]
    if filename:
        lines.append(
            localize_message(
                translator,
                REPORT_BASE,
                "description.file",
                placeholders={"filename": filename},
                fallback=fallbacks["file"],
                guild_id=guild_id,
            )
        )
    lines.append(
        localize_message(
            translator,
            REPORT_BASE,
            "description.type",
            placeholders={"file_type": file_type_label},
            fallback=fallbacks["type"],
            guild_id=guild_id,
        )
    )
    lines.append(
        localize_message(
            translator,
            REPORT_BASE,
            "description.decision",
            placeholders={"decision": decision_label},
            fallback=fallbacks["decision"],
            guild_id=guild_id,
        )
    )
    return lines


def _default_decision_color(decision_key: str, scan_result: dict[str, Any] | None) -> discord.Color:
    if decision_key == "nsfw":
        return discord.Color.orange()
    if decision_key == "safe":
        return discord.Color.green()
    return discord.Color.dark_grey()


def _append_scan_result_fields(
    embed: discord.Embed,
    translator,
    guild_id: int,
    scan_result: dict[str, Any],
    field_specs: Iterable[ScanFieldSpec],
    duration_ms: int,
) -> None:
    for spec in field_specs:
        if not isinstance(spec, ScanFieldSpec):
            continue
        source_key = spec.source_key if spec.source_key is not None else spec.field_key
        base_value = scan_result.get(source_key) if source_key is not None else None
        try:
            value = (
                spec.formatter(base_value, scan_result, translator, guild_id, duration_ms)
                if spec.formatter
                else base_value
            )
        except Exception:
            continue
        if value is None:
            continue
        value_str = str(value)
        if spec.max_length is not None:
            value_str = value_str[: spec.max_length]
        embed.add_field(
            name=localize_field_name(translator, spec.field_key, guild_id),
            value=value_str,
            inline=spec.inline,
        )


def _build_verbose_embed(
    *,
    translator,
    guild_id: int,
    actor,
    file_type: str | None,
    detected_mime: str | None,
    scan_result: dict[str, Any] | None,
    duration_ms: int,
    filename: str | None,
    bold_labels: bool,
    latency_kwargs: dict[str, Any] | None,
    field_specs: Iterable[ScanFieldSpec] | None,
    extra_fields: Iterable[dict[str, Any]] | None,
    color_resolver: Callable[[str, dict[str, Any] | None], discord.Color] | None,
    include_cache_status: bool,
) -> discord.Embed | None:
    if guild_id is None:
        return None

    scan_payload = scan_result or {}

    decision_key = _resolve_decision_key(scan_payload)
    decision_label = localize_decision(translator, decision_key, guild_id)
    file_type_label = _resolve_file_type_label(translator, guild_id, file_type, detected_mime)
    actor_text = _resolve_actor_mention(actor, translator, guild_id)

    description_lines = _build_description_lines(
        translator=translator,
        guild_id=guild_id,
        actor_text=actor_text,
        file_type_label=file_type_label,
        decision_label=decision_label,
        filename=filename,
        bold_labels=bold_labels,
    )

    color = (
        color_resolver(decision_key, scan_payload)
        if color_resolver is not None
        else _default_decision_color(decision_key, scan_payload)
    )

    embed = discord.Embed(
        title=localize_message(
            translator,
            REPORT_BASE,
            "title",
            fallback="NSFW Scan Report",
            guild_id=guild_id,
        ),
        description="\n".join(description_lines),
        color=color,
    )

    pipeline_metrics = scan_payload.get("pipeline_metrics")
    latency_config = {
        "duration_ms": duration_ms,
        "breakdown_kwargs": {"decimals": 1, "fallback_label_style": "title"},
        "value_max_length": 1024,
    }
    if latency_kwargs:
        breakdown_override = latency_kwargs.get("breakdown_kwargs")
        if (
            isinstance(breakdown_override, dict)
            and isinstance(latency_config.get("breakdown_kwargs"), dict)
        ):
            merged = latency_config["breakdown_kwargs"].copy()
            merged.update(breakdown_override)
            latency_config["breakdown_kwargs"] = merged
            latency_kwargs = {k: v for k, v in latency_kwargs.items() if k != "breakdown_kwargs"}
        latency_config.update(latency_kwargs)

    for field in build_latency_fields(
        lambda key: localize_field_name(translator, key, guild_id),
        pipeline_metrics if isinstance(pipeline_metrics, dict) else None,
        **latency_config,
    ):
        embed.add_field(**field)

    if include_cache_status:
        cache_status = scan_payload.get("cache_status")
        if cache_status:
            embed.add_field(
                name=localize_field_name(translator, "cache_status", guild_id),
                value=str(cache_status),
                inline=True,
            )

    if field_specs:
        _append_scan_result_fields(
            embed,
            translator,
            guild_id,
            scan_payload,
            field_specs,
            duration_ms,
        )

    if extra_fields:
        for extra_field in extra_fields:
            if not extra_field:
                continue
            embed.add_field(**extra_field)

    return embed


def _default_reason_formatter(value, scan_result, translator, guild_id: int, _duration: int):
    if value is None:
        return None
    return localize_reason(translator, value, guild_id)


def _default_category_formatter(value, scan_result, translator, guild_id: int, _duration: int):
    if value is None:
        return None
    return localize_category(translator, value, guild_id)


def _default_score_formatter(value, _scan_result, _translator, _guild_id: int, _duration: int):
    if value is None:
        return None
    try:
        return f"{float(value):.3f}"
    except (TypeError, ValueError):
        return None


_DEFAULT_FIELD_SPECS: tuple[ScanFieldSpec, ...] = (
    ScanFieldSpec(field_key="reason", inline=False, formatter=_default_reason_formatter),
    ScanFieldSpec(field_key="category", formatter=_default_category_formatter),
    ScanFieldSpec(field_key="score", formatter=_default_score_formatter),
)


def default_reason_formatter(value, scan_result, translator, guild_id: int, duration: int):
    """Public wrapper around the standard reason formatter."""

    return _default_reason_formatter(value, scan_result, translator, guild_id, duration)


def default_category_formatter(value, scan_result, translator, guild_id: int, duration: int):
    """Public wrapper around the standard category formatter."""

    return _default_category_formatter(value, scan_result, translator, guild_id, duration)


def default_score_formatter(value, scan_result, translator, guild_id: int, duration: int):
    """Public wrapper around the standard score formatter."""

    return _default_score_formatter(value, scan_result, translator, guild_id, duration)


DEFAULT_FIELD_SPECS: tuple[ScanFieldSpec, ...] = _DEFAULT_FIELD_SPECS


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
    filename: str | None = None,
    bold_labels: bool = True,
    latency_kwargs: dict[str, Any] | None = None,
    field_specs: Iterable[ScanFieldSpec] | None = None,
    extra_fields: Iterable[dict[str, Any]] | None = None,
    color_resolver: Callable[[str, dict[str, Any] | None], discord.Color] | None = None,
    include_cache_status: bool = True,
) -> None:
    if message is None or guild_id is None:
        return

    translator = resolve_translator(scanner)
    actor = author or getattr(message, "author", None)
    specs = tuple(field_specs) if field_specs is not None else _DEFAULT_FIELD_SPECS

    embed = _build_verbose_embed(
        translator=translator,
        guild_id=guild_id,
        actor=actor,
        file_type=file_type,
        detected_mime=detected_mime,
        scan_result=scan_result,
        duration_ms=duration_ms,
        filename=filename,
        bold_labels=bold_labels,
        latency_kwargs=latency_kwargs,
        field_specs=specs,
        extra_fields=extra_fields,
        color_resolver=color_resolver,
        include_cache_status=include_cache_status,
    )

    if embed is None:
        return

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
