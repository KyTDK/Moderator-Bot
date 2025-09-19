import os
from typing import Any

import discord

from modules.utils import mod_logging, mysql

from ..utils import determine_file_type
from .images import process_image
from .videos import process_video


async def check_attachment(
    scanner,
    author,
    temp_filename: str,
    nsfw_callback,
    guild_id: int | None,
    message,
    perform_actions: bool = True,
) -> bool:
    filename = os.path.basename(temp_filename)
    file_type = determine_file_type(temp_filename)

    if guild_id is None:
        print("[check_attachment] Guild_id is None")
        return False

    file = None
    scan_result: dict[str, Any] | None = None

    if file_type == "Image":
        scan_result = await process_image(
            scanner,
            original_filename=temp_filename,
            guild_id=guild_id,
            clean_up=False,
        )
    elif file_type == "Video":
        file, scan_result = await process_video(
            scanner,
            original_filename=temp_filename,
            guild_id=guild_id,
        )
    else:
        print(
            f"[check_attachment] Unsupported file type: {file_type} for {filename}"
        )
        return False

    try:
        if message is not None and await mysql.get_settings(guild_id, "nsfw-verbose"):
            decision = "Unknown"
            if scan_result is not None:
                decision = (
                    "NSFW"
                    if scan_result.get("is_nsfw")
                    else "Safe"
                )
            embed = discord.Embed(
                title="NSFW Scan Report",
                description=(
                    f"User: {author.mention}\n"
                    f"File: `{filename}`\n"
                    f"Type: `{file_type}`\n"
                    f"Decision: **{decision}**"
                ),
                color=(
                    discord.Color.orange()
                    if decision == "Safe"
                    else (
                        discord.Color.red()
                        if decision == "NSFW"
                        else discord.Color.dark_grey()
                    )
                ),
            )
            if scan_result:
                if scan_result.get("reason"):
                    embed.add_field(
                        name="Reason",
                        value=str(scan_result.get("reason"))[:1024],
                        inline=False,
                    )
                if scan_result.get("category"):
                    embed.add_field(
                        name="Category",
                        value=str(scan_result.get("category")),
                        inline=True,
                    )
                if scan_result.get("score") is not None:
                    embed.add_field(
                        name="Score",
                        value=f"{float(scan_result.get('score') or 0):.3f}",
                        inline=True,
                    )
                if scan_result.get("flagged_any") is not None:
                    embed.add_field(
                        name="Flagged Any",
                        value=str(bool(scan_result.get("flagged_any"))).lower(),
                        inline=True,
                    )
                if scan_result.get("summary_categories") is not None:
                    embed.add_field(
                        name="Summary Categories",
                        value=str(scan_result.get("summary_categories")),
                        inline=False,
                    )
                if scan_result.get("max_similarity") is not None:
                    embed.add_field(
                        name="Max Similarity",
                        value=f"{float(scan_result.get('max_similarity') or 0):.3f}",
                        inline=True,
                    )
                if scan_result.get("max_category") is not None:
                    embed.add_field(
                        name="Max Similarity Category",
                        value=str(scan_result.get("max_category")),
                        inline=True,
                    )
                if scan_result.get("similarity") is not None:
                    embed.add_field(
                        name="Matched Similarity",
                        value=f"{float(scan_result.get('similarity') or 0):.3f}",
                        inline=True,
                    )
                if scan_result.get("high_accuracy") is not None:
                    embed.add_field(
                        name="High Accuracy",
                        value=str(bool(scan_result.get("high_accuracy"))).lower(),
                        inline=True,
                    )
                if scan_result.get("clip_threshold") is not None:
                    embed.add_field(
                        name="CLIP Threshold",
                        value=f"{float(scan_result.get('clip_threshold') or 0):.3f}",
                        inline=True,
                    )
                if scan_result.get("threshold") is not None:
                    try:
                        embed.add_field(
                            name="Moderation Threshold",
                            value=f"{float(scan_result.get('threshold') or 0):.3f}",
                            inline=True,
                        )
                    except Exception:
                        pass
                if scan_result.get("video_frames_scanned") is not None:
                    scanned = scan_result.get("video_frames_scanned")
                    target = scan_result.get("video_frames_target")
                    embed.add_field(
                        name="Video Frames",
                        value=f"{scanned}/{target}",
                        inline=True,
                    )

            embed.set_thumbnail(url=author.display_avatar.url)
            await mod_logging.log_to_channel(
                embed=embed,
                channel_id=message.channel.id,
                bot=scanner.bot,
            )
    except Exception as exc:
        print(f"[verbose] Failed to send verbose embed: {exc}")

    if not perform_actions:
        return False

    if nsfw_callback and scan_result and scan_result.get("is_nsfw"):
        category_name = scan_result.get("category") or "unspecified"
        confidence_value = None
        confidence_source = None
        try:
            if scan_result.get("score") is not None:
                confidence_value = float(scan_result.get("score"))
                confidence_source = "Score"
            elif scan_result.get("similarity") is not None:
                confidence_value = float(scan_result.get("similarity"))
                confidence_source = "Similarity"
        except Exception:
            confidence_value = None
            confidence_source = None

        if file is None:
            file = discord.File(temp_filename, filename=filename)
        try:
            await nsfw_callback(
                author,
                scanner.bot,
                guild_id,
                f"Detected potential policy violation (Category: **{category_name.title()}**)",
                file,
                message,
                confidence=confidence_value,
                confidence_source=confidence_source,
            )
        finally:
            try:
                file.close()
            except Exception:
                try:
                    file.fp.close()
                except Exception:
                    pass
        return True

    return False
