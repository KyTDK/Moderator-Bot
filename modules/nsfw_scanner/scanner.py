import asyncio
import os
import traceback
import uuid
import re
from contextlib import asynccontextmanager
from tempfile import NamedTemporaryFile
from typing import Optional
from urllib.parse import urlparse

import aiofiles
import aiohttp
import discord
import openai
from apnggif import apnggif
from cogs.hydration import wait_for_hydration
from cogs.nsfw import NSFW_CATEGORY_SETTING
from discord.errors import NotFound
from discord.ext import commands
from PIL import Image

from modules.utils import mod_logging, mysql, api
from modules.utils import clip_vectors
import pillow_avif  # registers AVIF support

from .constants import (
    TMP_DIR,
    CLIP_THRESHOLD,
    HIGH_ACCURACY_SIMILARITY,
    MAX_FRAMES_PER_VIDEO,
    ACCELERATED_MAX_FRAMES_PER_VIDEO,
    MAX_CONCURRENT_FRAMES,
    ACCELERATED_MAX_CONCURRENT_FRAMES,
    ADD_SFW_VECTOR,
)
from .utils import (
    safe_delete,
    is_allowed_category,
    determine_file_type,
    extract_frames_threaded,
    file_to_b64,
    convert_to_png_safe,
)


class NSFWScanner:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.session = None
        self.tmp_dir = TMP_DIR

    async def start(self):
        self.session = aiohttp.ClientSession()
        os.makedirs(self.tmp_dir, exist_ok=True)

    async def stop(self):
        if self.session:
            await self.session.close()

    @asynccontextmanager
    async def temp_download(self, url: str, ext: str | None = None):
        # Ensure tmp dir survives reboots or tmp-cleaners
        os.makedirs(TMP_DIR, exist_ok=True)

        # normalise extension
        if ext and not ext.startswith("."):
            ext = "." + ext
        ext = ext or os.path.splitext(urlparse(url).path)[1] or ".bin"

        path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}{ext}")

        async with self.session.get(url) as resp:
            resp.raise_for_status()
            async with aiofiles.open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 14):
                    await f.write(chunk)
        try:
            yield path
        finally:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass

    async def process_video(
        self,
        original_filename: str,
        guild_id: int,
    ) -> tuple[Optional[discord.File], dict | None]:
        """
        Scan a video by sampling frames.  Returns (flagged_file, scan_result).
        `flagged_file` is the first offending frame wrapped in a discord.File,
        or None if clean.  `scan_result` is the result of the scan.
        """

        # Determine frames to scan
        frames_to_scan = MAX_FRAMES_PER_VIDEO
        if await mysql.is_accelerated(guild_id=guild_id):
            frames_to_scan = ACCELERATED_MAX_FRAMES_PER_VIDEO

        temp_frames = await asyncio.to_thread(
            extract_frames_threaded, original_filename, frames_to_scan
        )
        print(f"[process_video] extracted {len(temp_frames)} frames (target={frames_to_scan})")
        if not temp_frames:
            safe_delete(original_filename)
            # Return a clean result for verbosity if no frames could be extracted
            return None, {
                "is_nsfw": False,
                "reason": "No frames extracted",
                "video_frames_scanned": 0,
                "video_frames_target": frames_to_scan,
            }

        # Process frames concurrently, increase concurrency for accelerated users
        semaphore = asyncio.Semaphore(MAX_CONCURRENT_FRAMES)
        if await mysql.is_accelerated(guild_id=guild_id):
            semaphore = asyncio.Semaphore(ACCELERATED_MAX_CONCURRENT_FRAMES)

        async def analyse(path: str):
            async with semaphore:
                try:
                    scan = await self.process_image(
                        original_filename=path,
                        guild_id=guild_id,
                        clean_up=False,
                    )
                    # Return only definite hits; ignore low-score/safe frames
                    if isinstance(scan, dict) and scan.get("is_nsfw") is True:
                        # Attach video context metadata
                        scan.setdefault("video_frames_scanned", None)
                        scan.setdefault("video_frames_target", None)
                        scan["video_frames_scanned"] = len(temp_frames)
                        scan["video_frames_target"] = frames_to_scan
                        return (path, scan)
                    return None
                except Exception as e:
                    print(f"[process_video] Analyse error {path}: {e}")
                    return None

        tasks = [asyncio.create_task(analyse(p)) for p in temp_frames]
        try:
            for done in asyncio.as_completed(tasks):
                res = await done
                if not res:
                    continue

                frame_path, scan = res

                # Normalise scan output (it can be dict or legacy str)
                if isinstance(scan, dict):
                    if not scan.get("is_nsfw"):
                        continue  # safe frame
                    cat_name = scan.get("category") or "unspecified"
                else:  # legacy string/non-dict → treat any truthy value as category
                    cat_name = str(scan)

                print(f"[process_video] NSFW detected in frame: {frame_path} (category: {cat_name})")

                # cancel the remaining frame tasks
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

                # create flagged file
                flagged_file = discord.File(frame_path, filename=os.path.basename(frame_path))
                return flagged_file, scan

            # no frame flagged
            return None, {
                "is_nsfw": False,
                "reason": "No NSFW frames detected",
                "video_frames_scanned": len(temp_frames),
                "video_frames_target": frames_to_scan,
            }
        finally:
            for p in temp_frames:
                safe_delete(p)
            safe_delete(original_filename)

    async def process_image(
        self,
        original_filename: str,
        guild_id: int | None = None,
        clean_up: bool = True,
    ) -> dict | None:
        png_converted_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.png")
        result = await asyncio.to_thread(convert_to_png_safe, original_filename, png_converted_path)
        if not result:
            print(f"[process_image] PNG conversion failed: {original_filename}")
            return None

        try:
            with Image.open(png_converted_path) as image:
                # Try similarity match first
                settings = await mysql.get_settings(
                    guild_id,
                    [NSFW_CATEGORY_SETTING, "threshold", "nsfw-high-accuracy"],
                )
                allowed_categories = settings.get(NSFW_CATEGORY_SETTING, [])
                high_accuracy = bool(settings.get("nsfw-high-accuracy", False))
                similarity_response = clip_vectors.query_similar(image, threshold=0)

                # Track if we had any similarity above CLIP threshold
                had_similarity = bool(similarity_response)
                max_similarity = 0.0
                if similarity_response:
                    for item in similarity_response:
                        sim = float(item.get("similarity", 0) or 0)
                        if sim > max_similarity:
                            max_similarity = sim

                    for item in similarity_response:
                        category = item.get("category")
                        similarity = float(item.get("similarity", 0) or 0)

                        # Only consider results above CLIP_THRESHOLD
                        if similarity < CLIP_THRESHOLD:
                            continue

                        if not category:
                            print(f"[process_image] Similar SFW image found with similarity {similarity:.2f} for guild {guild_id}.")
                            if high_accuracy and max_similarity < HIGH_ACCURACY_SIMILARITY:
                                break # continue to API check
                            return {
                                "is_nsfw": False,
                                "reason": "Similarity match",
                                "max_similarity": max_similarity,
                                "high_accuracy": high_accuracy,
                                "clip_threshold": CLIP_THRESHOLD,
                                "similarity": similarity,
                            }

                        if is_allowed_category(category, allowed_categories):
                            print(f"[process_image] Found similar image category: {category} with similarity {similarity:.2f} for guild {guild_id}.")
                            # High-accuracy mode: require very high similarity to skip API
                            if high_accuracy and max_similarity < HIGH_ACCURACY_SIMILARITY:
                                break # continue to API check
                            return {
                                "is_nsfw": True,
                                "category": category,
                                "reason": "Similarity match",
                                "max_similarity": max_similarity,
                                "high_accuracy": high_accuracy,
                                "clip_threshold": CLIP_THRESHOLD,
                                "similarity": similarity,
                            }

                    # If we reached here, either no actionable NSFW similarity or high-accuracy wants API confirmation
                    if not high_accuracy:
                        # No NSFW detected by similarity alone
                        return {
                            "is_nsfw": False,
                            "reason": "No NSFW similarity match",
                            "max_similarity": max_similarity,
                            "high_accuracy": high_accuracy,
                            "clip_threshold": CLIP_THRESHOLD,
                        }

                # No similarity results above CLIP_THRESHOLD, or high-accuracy requested API confirmation
                response = await self.moderator_api(
                    image_path=png_converted_path,
                    guild_id=guild_id,
                    image=image,
                    skip_vector_add=had_similarity,
                )
                print(f"[process_image] Moderation result for {original_filename}: {response} (similarity={max_similarity:.2f}) for guild {guild_id}")
                # Attach similarity/meta context
                if isinstance(response, dict):
                    response.setdefault("max_similarity", max_similarity)
                    response.setdefault("high_accuracy", high_accuracy)
                    response.setdefault("clip_threshold", CLIP_THRESHOLD)
                return response
        except Exception as e:
            print(traceback.format_exc())
            print(f"[process_image] Error processing image {original_filename}: {e}")
            return None
        finally:
            safe_delete(png_converted_path)
            if clean_up:
                safe_delete(original_filename)

    async def check_attachment(
        self,
        author,
        temp_filename,
        nsfw_callback,
        guild_id,
        message,
        perform_actions=True,
    ) -> bool:
        filename = os.path.basename(temp_filename)
        file_type = determine_file_type(temp_filename)

        if guild_id is None:
            print("[check_attachment] Guild_id is None")
            return False  # DM or system message
        file, scan_result = None, None
        if file_type == "Image":
            scan_result = await self.process_image(
                original_filename=temp_filename,
                guild_id=guild_id,
                clean_up=False,
            )
            file = None
        elif file_type == "Video":
            file, scan_result = await self.process_video(
                original_filename=temp_filename,
                guild_id=guild_id,
            )
        else:
            print(f"[check_attachment] Unsupported file type: {file_type} for {filename}")
            return False

        # Verbose reporting in-channel when enabled (and a message context exists)
        try:
            if message is not None and await mysql.get_settings(guild_id, "nsfw-verbose"):
                decision = (
                    "NSFW" if (scan_result and scan_result.get("is_nsfw")) else ("Safe" if scan_result is not None else "Unknown")
                )
                embed = discord.Embed(
                    title="NSFW Scan Report",
                    description=(
                        f"User: {author.mention}\n"
                        f"File: `{filename}`\n"
                        f"Type: `{file_type}`\n"
                        f"Decision: **{decision}**"
                    ),
                    color=(discord.Color.orange() if decision == "Safe" else (discord.Color.red() if decision == "NSFW" else discord.Color.dark_grey())),
                )
                # Details
                if scan_result:
                    if scan_result.get("reason"):
                        embed.add_field(name="Reason", value=str(scan_result.get("reason"))[:1024], inline=False)
                    if scan_result.get("category"):
                        embed.add_field(name="Category", value=str(scan_result.get("category")), inline=True)
                    if scan_result.get("score") is not None:
                        embed.add_field(name="Score", value=f"{float(scan_result.get('score') or 0):.3f}", inline=True)
                    if scan_result.get("max_similarity") is not None:
                        embed.add_field(name="Max Similarity", value=f"{float(scan_result.get('max_similarity') or 0):.3f}", inline=True)
                    if scan_result.get("similarity") is not None:
                        embed.add_field(name="Matched Similarity", value=f"{float(scan_result.get('similarity') or 0):.3f}", inline=True)
                    if scan_result.get("high_accuracy") is not None:
                        embed.add_field(name="High Accuracy", value=str(bool(scan_result.get("high_accuracy"))).lower(), inline=True)
                    if scan_result.get("clip_threshold") is not None:
                        embed.add_field(name="CLIP Threshold", value=f"{float(scan_result.get('clip_threshold') or 0):.3f}", inline=True)
                    if scan_result.get("threshold") is not None:
                        try:
                            embed.add_field(name="Moderation Threshold", value=f"{float(scan_result.get('threshold') or 0):.3f}", inline=True)
                        except Exception:
                            pass
                    if scan_result.get("video_frames_scanned") is not None:
                        scanned = scan_result.get("video_frames_scanned")
                        target = scan_result.get("video_frames_target")
                        embed.add_field(name="Video Frames", value=f"{scanned}/{target}", inline=True)

                embed.set_thumbnail(url=author.display_avatar.url)
                await mod_logging.log_to_channel(embed=embed, channel_id=message.channel.id, bot=self.bot)
        except Exception as e:
            print(f"[verbose] Failed to send verbose embed: {e}")

        # Handle violations
        if not perform_actions:
            return False
        if nsfw_callback and scan_result and scan_result.get("is_nsfw"):
            cat_name = (scan_result.get("category") or "unspecified")
            # Build a simple confidence string if available (score from moderation or similarity fallback)
            confidence_str = ""
            try:
                if scan_result.get("score") is not None:
                    confidence_str = f", Confidence: {float(scan_result.get('score')):.2f}"
                elif scan_result.get("similarity") is not None:
                    confidence_str = f", Confidence: {float(scan_result.get('similarity')):.2f}"
            except Exception:
                # If formatting fails, skip confidence
                confidence_str = ""

            if file is None:
                file = discord.File(temp_filename, filename=filename)
            try:
                await nsfw_callback(
                    author,
                    self.bot,
                    guild_id,
                    f"Detected potential policy violation (Category: **{cat_name.title()}**{confidence_str})",
                    file,
                    message,
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

    async def is_nsfw(
        self,
        message: discord.Message | None = None,
        guild_id: int | None = None,
        nsfw_callback=None,
        url: str | None = None,
        member: discord.Member | None = None,
    ) -> bool:

        if url:
            async with self.temp_download(url) as temp_filename:
                return await self.check_attachment(member, temp_filename, nsfw_callback, guild_id, message)
        snapshots = getattr(message, "message_snapshots", None)
        snapshot = snapshots[0] if snapshots else None

        attachments = message.attachments if message.attachments else (snapshot.attachments if snapshot else [])
        embeds = message.embeds if message.embeds else (snapshot.embeds if snapshot else [])
        stickers = message.stickers if message.stickers else (snapshot.stickers if snapshot else [])

        # hydration fallback
        if not (attachments or embeds or stickers) and "http" in message.content:
            message = await wait_for_hydration(message)
            attachments = message.attachments
            embeds = message.embeds
            stickers = message.stickers

            if not (attachments or embeds or stickers):
                return False

        if message is None:
            print("Message is None")
            return False

        for attachment in attachments:
            suffix = os.path.splitext(attachment.filename)[1] or ""
            with NamedTemporaryFile(delete=False, dir=TMP_DIR, suffix=suffix) as tmp:
                try:
                    await attachment.save(tmp.name)
                except NotFound:
                    safe_delete(tmp.name)
                    print(f"[NSFW] Attachment not found: {attachment.url}")
                    continue
                temp_filename = tmp.name
            try:
                if await self.check_attachment(message.author, temp_filename, nsfw_callback, guild_id, message):
                    return True
            finally:
                safe_delete(temp_filename)

        for embed in embeds:
            possible_urls = []
            if embed.video and embed.video.url:
                possible_urls.append(embed.video.url)
            if embed.image and embed.image.url:
                possible_urls.append(embed.image.url)
            if embed.thumbnail and embed.thumbnail.url:
                possible_urls.append(embed.thumbnail.url)

            for gif_url in possible_urls:
                domain = urlparse(gif_url).netloc.lower()
                is_tenor = domain == "tenor.com" or domain.endswith(".tenor.com")
                # Don't scan if its tenor and check-tenor-gifs is False
                if is_tenor and not await mysql.get_settings(guild_id, "check-tenor-gifs"):
                    continue
                async with self.temp_download(gif_url) as temp_filename:
                    if await self.check_attachment(
                        author=message.author,
                        temp_filename=temp_filename,
                        nsfw_callback=nsfw_callback,
                        guild_id=guild_id,
                        message=message,
                    ):
                        return True

        for sticker in stickers:
            sticker_url = sticker.url
            if not sticker_url:
                continue

            extension = sticker.format.name.lower()

            async with self.temp_download(sticker_url, ext=extension) as temp_location:
                gif_location = temp_location

                if extension == "apng":
                    gif_location = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.gif")
                    await asyncio.to_thread(apnggif, temp_location, gif_location)

                try:
                    if await self.check_attachment(
                        message.author,
                        gif_location,
                        nsfw_callback,
                        guild_id,
                        message,
                    ):
                        return True
                finally:
                    if gif_location != temp_location:
                        safe_delete(gif_location)

        custom_emoji_tags = list(set(re.findall(r'<a?:\w+:\d+>', message.content)))
        for tag in custom_emoji_tags:
            match = re.match(r'<a?:(\w+):(\d+)>', tag)
            if not match:
                continue
            name, eid = match.groups()
            emoji_obj = self.bot.get_emoji(int(eid))
            if not emoji_obj:
                continue
            emoji_url = str(emoji_obj.url)
            try:
                async with self.temp_download(emoji_url) as emoji_path:
                    if await self.check_attachment(message.author, emoji_path, nsfw_callback, guild_id, message):
                        return True
            except Exception as e:
                print(f"[emoji-scan] Failed to scan custom emoji {emoji_obj}: {e}")

        return False

    async def moderator_api(
        self,
        text: str | None = None,
        image_path: str | None = None,
        image: Image.Image | None = None,
        guild_id: int | None = None,
        max_attempts: int = 3,
        skip_vector_add: bool = False,
    ) -> dict:
        result = {
            "is_nsfw": None,
            "category": None,
            "score": 0.0,
            "reason": None,
        }

        inputs: list | str = []
        is_video = image_path is not None

        if text and not image_path:
            inputs = text

        if is_video:
            if not os.path.exists(image_path):
                print(f"[moderator_api] Image path does not exist: {image_path}")
                return result
            try:
                b64 = await asyncio.to_thread(file_to_b64, image_path)
            except Exception as e:
                print(f"[moderator_api] Error reading/encoding image {image_path}: {e}")
                return result
            inputs.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                }
            )

        if not inputs:
            print("[moderator_api] No inputs were provided")
            return result

        for attempt in range(max_attempts):
            client, encrypted_key = await api.get_api_client(guild_id)
            if not client:
                print("[moderator_api] No available API key.")
                await asyncio.sleep(2)
                continue
            try:
                response = await client.moderations.create(
                    model="omni-moderation-latest" if image_path else "text-moderation-latest",
                    input=inputs,
                )
            except openai.AuthenticationError:
                print("[moderator_api] Authentication failed. Marking key as not working.")
                await api.set_api_key_not_working(api_key=encrypted_key, bot=self.bot)
                continue
            except openai.RateLimitError as e:
                print(f"[moderator_api] Rate limit error: {e}. Marking key as not working.")
                await api.set_api_key_not_working(api_key=encrypted_key, bot=self.bot)
                continue
            except Exception as e:
                print(f"[moderator_api] Unexpected error from OpenAI API: {e}.")
                continue

            if not response or not response.results:
                print("[moderator_api] No moderation results returned.")
                continue

            if not await api.is_api_key_working(encrypted_key):
                await api.set_api_key_working(encrypted_key)

            results = response.results[0]
            settings = await mysql.get_settings(guild_id, [NSFW_CATEGORY_SETTING, "threshold"])
            allowed_categories = settings.get(NSFW_CATEGORY_SETTING, [])
            try:
                threshold = float(settings.get("threshold", 0.7))
            except (TypeError, ValueError):
                threshold = 0.7
            flagged_categories = []
            flagged_any = False
            for category, is_flagged in results.categories.__dict__.items():
                normalized_category = category.replace("/", "_").replace("-", "_")
                score = results.category_scores.__dict__.get(category, 0)
                # Filter out categories that are not flagged
                if not is_flagged:
                    continue
                flagged_any = True
                # Add vector for flagged category unless similarity already matched
                if not skip_vector_add:
                    print(f"[moderator_api] Adding vector for category '{normalized_category}' with score {score:.2f}")
                    clip_vectors.add_vector(image, metadata={"category": normalized_category, "score": score})
                # Ignore low confidence scores based on guild preferences
                if score < threshold:
                    print(f"[moderator_api] Category '{normalized_category}' flagged with low score {score:.2f}. Ignoring.")
                    continue
                # Check if category is allowed in this guild
                if allowed_categories and not is_allowed_category(category, allowed_categories):
                    continue
                # Add to flagged categories
                flagged_categories.append((normalized_category, score))

            if not flagged_categories and ADD_SFW_VECTOR and not flagged_any and not skip_vector_add:
                print("[moderator_api] Adding SFW vector to index")
                clip_vectors.add_vector(image, metadata={"category": None, "score": 0})

            if flagged_categories:
                # Return highest scored category
                flagged_categories.sort(key=lambda x: x[1], reverse=True)
                best_category, best_score = flagged_categories[0]
                return {
                    "is_nsfw": True,
                    "category": best_category,
                    "score": best_score,
                    "reason": "OpenAI moderation",
                    "threshold": threshold,
                }

            return {"is_nsfw": False, "reason": "OpenAI moderation", "threshold": threshold}

        return result
