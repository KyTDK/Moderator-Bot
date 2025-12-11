from __future__ import annotations

from dataclasses import dataclass

from .connection import execute_query


@dataclass(slots=True)
class CaptchaEmbedRecord:
    """Represents the stored captcha verification embed for a guild."""

    guild_id: int
    channel_id: int
    message_id: int


async def get_captcha_embed_record(guild_id: int) -> CaptchaEmbedRecord | None:
    """Fetch the stored captcha embed metadata for the given guild."""

    row, _ = await execute_query(
        "SELECT channel_id, message_id FROM captcha_embeds WHERE guild_id = %s",
        (guild_id,),
        fetch_one=True,
    )
    if not row:
        return None

    channel_id_raw, message_id_raw = row
    try:
        channel_id = int(channel_id_raw)
        message_id = int(message_id_raw)
    except (TypeError, ValueError):
        return None

    return CaptchaEmbedRecord(
        guild_id=guild_id,
        channel_id=channel_id,
        message_id=message_id,
    )


async def upsert_captcha_embed_record(
    guild_id: int,
    channel_id: int,
    message_id: int,
) -> None:
    """Store or update the captcha embed metadata for a guild."""

    await execute_query(
        """
        INSERT INTO captcha_embeds (guild_id, channel_id, message_id)
        VALUES (%s, %s, %s) AS new_values
        ON DUPLICATE KEY UPDATE
            channel_id = new_values.channel_id,
            message_id = new_values.message_id,
            updated_at = CURRENT_TIMESTAMP
        """,
        (guild_id, channel_id, message_id),
    )


async def delete_captcha_embed_record(guild_id: int) -> None:
    """Remove any stored captcha embed metadata for the guild."""

    await execute_query(
        "DELETE FROM captcha_embeds WHERE guild_id = %s",
        (guild_id,),
    )
