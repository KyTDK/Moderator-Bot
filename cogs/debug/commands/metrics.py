from __future__ import annotations

from typing import Optional

from modules.metrics.reset import reset_latency_averages

__all__ = ["reset_latency"]


async def reset_latency(
    cog,
    interaction,
    *,
    pattern: Optional[str],
    dry_run: bool,
) -> None:
    try:
        output = await reset_latency_averages(pattern=pattern or None, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001
        await interaction.followup.send(
            f"Reset failed: {exc}",
            ephemeral=True,
        )
        return

    details = output or "No hashes required updates (nothing matched the reset criteria)."
    summary = [
        f"Pattern: `{pattern or 'prefix:rollup:*'}`",
        f"Dry run: {'Yes' if dry_run else 'No'}",
        "",
        f"```\n{details[:1900]}\n```",
    ]
    await interaction.followup.send("\n".join(summary), ephemeral=True)
