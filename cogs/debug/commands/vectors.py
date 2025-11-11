from __future__ import annotations

from typing import Any, Awaitable, Callable, Dict, Optional

from modules.utils import clip_vectors, text_vectors
from modules.utils.vector_spaces import VectorDeleteStats

__all__ = ["VECTOR_STORE_CHOICES", "reset_vectors", "report_vector_status"]

VectorResetFn = Callable[[], Awaitable[Optional[VectorDeleteStats]]]
VectorInfoFn = Callable[[], Dict[str, Any]]


class _VectorStoreConfig:
    __slots__ = ("key", "label", "reset", "debug")

    def __init__(
        self,
        *,
        key: str,
        label: str,
        reset: VectorResetFn,
        debug: VectorInfoFn,
    ) -> None:
        self.key = key
        self.label = label
        self.reset = reset
        self.debug = debug


VECTOR_STORE_CHOICES = (
    _VectorStoreConfig(
        key="clip",
        label="CLIP image vectors",
        reset=clip_vectors.reset_collection,
        debug=clip_vectors.get_debug_info,
    ),
    _VectorStoreConfig(
        key="nsfw_text",
        label="NSFW text vectors",
        reset=text_vectors.reset_collection,
        debug=text_vectors.get_debug_info,
    ),
)

_CHOICE_LOOKUP: dict[str, _VectorStoreConfig] = {cfg.key: cfg for cfg in VECTOR_STORE_CHOICES}


def _format_int(value: Optional[int]) -> str:
    return f"{value:,}" if isinstance(value, int) else "unknown"


def _format_ms(value: Optional[float]) -> str:
    if value is None:
        return "n/a"
    return f"{value:.1f} ms"


async def reset_vectors(interaction, store_key: str) -> None:
    config = _CHOICE_LOOKUP.get(store_key)
    if config is None:  # pragma: no cover - defensive guard
        await interaction.followup.send("Unknown vector store requested.", ephemeral=True)
        return

    try:
        stats = await config.reset()
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(
            f"Failed to reset {config.label}: {exc}",
            ephemeral=True,
        )
        return

    if not stats:
        await interaction.followup.send(
            f"{config.label} is not available; nothing to reset.",
            ephemeral=True,
        )
        return

    details = config.debug()
    summary = [
        f"**{config.label}** (`{details.get('collection', 'unknown')}`)",
        f"- Deleted vectors: {_format_int(stats.deleted_count)}",
        f"- Remaining vectors: {_format_int(stats.remaining_count)}",
        f"- Delete duration: {_format_ms(stats.delete_ms)}",
        f"- Flush duration: {_format_ms(stats.flush_ms)}",
        f"- Total duration: {_format_ms(stats.total_ms)}",
    ]
    if not details.get("collection_ready"):
        summary.append("- Warning: collection is not marked ready after reset.")
    if details.get("fallback_active"):
        summary.append("- Warning: fallback mode is active.")
    last_error = details.get("last_error")
    if last_error:
        summary.append(f"- Last error: `{last_error}`")

    await interaction.followup.send("\n".join(summary), ephemeral=True)


async def report_vector_status(interaction) -> None:
    sections: list[str] = []
    for config in VECTOR_STORE_CHOICES:
        info = config.debug()
        lines = [
            f"**{config.label}** (`{info.get('collection', 'unknown')}`)",
            f"- Milvus dependency: {'available' if info.get('milvus_dependency') else 'missing'}",
            f"- Collection ready: {'yes' if info.get('collection_ready') else 'no'}",
            f"- Entity count: {_format_int(info.get('entity_count'))}",
            f"- Fallback active: {'yes' if info.get('fallback_active') else 'no'}",
        ]
        indexes = info.get("indexes") or []
        if indexes:
            formatted_indexes = []
            for idx in indexes:
                if isinstance(idx, dict):
                    idx_type = idx.get("type") or "unknown"
                    params = idx.get("params") or {}
                    formatted_indexes.append(f"{idx_type} {params}")
                else:
                    formatted_indexes.append(str(idx))
            lines.append(f"- Indexes: {', '.join(formatted_indexes)}")
        last_error = info.get("last_error")
        if last_error:
            lines.append(f"- Last error: `{last_error}`")
        sections.append("\n".join(lines))

    await interaction.followup.send("\n\n".join(sections), ephemeral=True)
