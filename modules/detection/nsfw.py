import os
import traceback
import asyncio
import cv2
import filetype
import uuid
import aiohttp
import aiofiles
from discord import Member
from discord.ext import commands
import discord
from lottie.exporters.gif import export_gif
import lottie
import base64
from cogs.nsfw import NSFW_ACTION_SETTING, NSFW_CATEGORY_SETTING
from modules.utils import mod_logging, mysql, api
from modules.moderation import strike
from urllib.parse import urlparse
from typing import Optional
from apnggif import apnggif
import openai
import numpy as np
from contextlib import asynccontextmanager
from tempfile import NamedTemporaryFile, gettempdir
from PIL import Image, ImageSequence
from modules.utils import clip_vectors
import pillow_avif

TMP_DIR = os.path.join(gettempdir(), "modbot")
os.makedirs(TMP_DIR, exist_ok=True)

MAX_FRAMES_PER_VIDEO = 10          # hard cap so we never spawn hundreds of tasks
MAX_CONCURRENT_FRAMES = 4          # limits OpenAI calls running at once

@asynccontextmanager
async def temp_download(url: str, ext: str | None = None):
    # Ensure tmp dir survives reboots or tmp-cleaners
    os.makedirs(TMP_DIR, exist_ok=True)

    # normalise extension
    if ext and not ext.startswith("."):
        ext = "." + ext
    ext = ext or os.path.splitext(urlparse(url).path)[1] or ".bin"

    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}{ext}")

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            resp.raise_for_status()
            total_written = 0
            async with aiofiles.open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(1 << 14):
                    await f.write(chunk)
                    total_written += len(chunk)
    try:
        yield path
    finally:
        try:
            os.remove(path)
            print(f"[temp_download] Cleaned up: {path}")
        except FileNotFoundError:
            print(f"[temp_download] File already deleted: {path}")

def _safe_delete(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"[safe_delete] Failed to delete {path}: {e}")

def _is_allowed_category(category: str, allowed_categories: list[str]) -> bool:
    """Normalizes and checks if category is allowed based on guild settings."""
    normalized = category.replace("/", "_").replace("-", "_")
    normalized_allowed = [c.replace("/", "_").replace("-", "_") for c in allowed_categories]
    return normalized in normalized_allowed

def determine_file_type(file_path: str) -> str:
    kind = filetype.guess(file_path)
    ext = file_path.lower().split('.')[-1]

    if ext in {'webp', 'gif', 'avif'}:
        try:
            media = Image.open(file_path)
            index = 0
            for frame in ImageSequence.Iterator(media):
                index += 1
            if index > 1:
                return 'Video'
            else:
                return 'Image'
        except Exception as e:
            print(f"[determine_file_type] Failed to open {file_path}: {e}")
            return 'Unknown'

    if kind is None:
        if ext == 'lottie':
            return 'Video'
        return 'Unknown'

    if kind.mime.startswith('image'):
        return 'Image'

    if kind.mime.startswith('video'):
        return 'Video'

    if ext == 'lottie':
        return 'Video'

    return kind.mime

def _extract_frames_threaded(filename: str, wanted: int) -> list[str]:
    temp_frames: list[str] = []

    ext = os.path.splitext(filename)[1].lower()
    if ext in {".webp", ".apng", ".avif"}:
        try:
            with Image.open(filename) as img:
                n = getattr(img, "n_frames", 1)
                if n <= 1:
                    return []
                idxs = np.linspace(0, n - 1, min(wanted, n), dtype=int)
                for idx in idxs:
                    img.seek(int(idx))
                    frame = img.convert("RGBA")
                    out = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:8]}_{idx}.png")
                    frame.save(out, format="PNG")
                    temp_frames.append(out)
                return temp_frames
        except Exception as e:
            print(f"[extract_frames_threaded] Pillow failed on {filename}: {e}")

    # Fallback: treat as video
    cap = cv2.VideoCapture(filename)
    try:
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0 or wanted <= 0:
            return []

        idxs = set(np.linspace(0, total - 1, min(wanted, total), dtype=int))
        if not idxs:
            return []

        max_idx = max(idxs)

        current_frame = 0
        while cap.isOpened() and current_frame <= max_idx:
            ok, frame = cap.read()
            if not ok:
                break

            if current_frame in idxs:
                out_name = os.path.join(
                    TMP_DIR, f"{uuid.uuid4().hex[:8]}_{current_frame}.jpg"
                )
                cv2.imwrite(out_name, frame, [cv2.IMWRITE_JPEG_QUALITY, 80])
                temp_frames.append(out_name)

                if len(temp_frames) == len(idxs):
                    break

            current_frame += 1

        return temp_frames

    except Exception as e:
        print(f"[extract_frames_threaded] VideoCapture failed on {filename}: {e}")
        return []
    finally:
        cap.release()

async def process_video(
    original_filename: str,
    guild_id: int,
    bot: commands.Bot,
) -> tuple[Optional[discord.File], dict | None]:
    """
    Scan a video by sampling frames.  Returns (flagged_file, scan_result).
    `flagged_file` is the first offending frame wrapped in a discord.File,
    or None if clean.  `scan_result` is the result of the scan.
    """

    temp_frames = await asyncio.to_thread(
        _extract_frames_threaded, original_filename, MAX_FRAMES_PER_VIDEO
    )
    if not temp_frames:
        _safe_delete(original_filename)
        return None, None

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FRAMES)

    async def analyse(path: str):
        async with semaphore:
            try:
                result = await process_image(
                    original_filename=path,
                    guild_id=guild_id,
                    clean_up=False,
                    bot=bot,
                )
                return (path, result) if result else None
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
        return None, None
    finally:
        for p in temp_frames:
            _safe_delete(p)
        _safe_delete(original_filename)

async def check_attachment(author,
                           temp_filename,
                           nsfw_callback,
                           bot,
                           guild_id,
                           message,
                           perform_actions=True) -> bool:
    filename = os.path.basename(temp_filename)
    file_type = determine_file_type(temp_filename)

    if guild_id is None:
        print("[check_attachment] Guild_id is None")
        return False  # DM or system message
    file, scan_result = None, None
    if file_type == "Image":
        scan_result = await process_image(original_filename=temp_filename, 
                                       guild_id=guild_id, 
                                       clean_up=False,
                                       bot=bot)
        file = discord.File(temp_filename, filename=filename)
    elif file_type == "Video":
        file, scan_result = await process_video(
            original_filename=temp_filename,
            guild_id=guild_id,
            bot=bot
        )
    else:
        print(f"[check_attachment] Unsupported file type: {file_type} for {filename}")
        return False
    
    # Handle violations
    if not perform_actions:
        return False
    if nsfw_callback and file and scan_result:
        if scan_result.get("is_nsfw"):
            cat_name = (scan_result.get("category") or "unspecified")
            await nsfw_callback(
                author,
                bot,
                guild_id,
                f"Detected potential policy violation (Category: **{cat_name.title()}**)",
                file,
                message
            )
            return True
        else:
            return False

async def is_nsfw(bot: commands.Bot,
                  message: discord.Message | None = None,
                  guild_id: int | None = None,
                  nsfw_callback=None,
                  url: str | None = None,
                  member: discord.Member | None = None
                  ) -> bool:

    if url:
        async with temp_download(url) as temp_filename:
            return await check_attachment(member, temp_filename, nsfw_callback, bot, guild_id, message)
    snapshots = getattr(message, "message_snapshots", None)
    snapshot = snapshots[0] if snapshots else None

    attachments = message.attachments if message.attachments else (snapshot.attachments if snapshot else [])
    embeds = message.embeds if message.embeds else (snapshot.embeds if snapshot else [])
    stickers = message.stickers if message.stickers else (snapshot.stickers if snapshot else [])

    # hydration fallback
    if not (attachments or embeds or stickers) and "http" in message.content:
        for attempt in range(3):
            await asyncio.sleep(1.0 + attempt) 
            try:
                message = await message.channel.fetch_message(message.id)
                embeds = message.embeds
                attachments = message.attachments
                stickers = message.stickers
                if embeds or attachments or stickers:
                    print(f"[HYDRATE] Attempt {attempt+1}: got {len(embeds)} embeds.")
                    break
            except (discord.NotFound, discord.HTTPException) as e:
                print(f"[HYDRATE] Failed to refetch message {message.id}: {e}")
                return False
        else:
            return False

    if message is None:
        print("Message is None")
        return False

    for attachment in attachments:
        suffix = os.path.splitext(attachment.filename)[1] or ""
        with NamedTemporaryFile(delete=False, dir=TMP_DIR, suffix=suffix) as tmp:
            await attachment.save(tmp.name)
            temp_filename = tmp.name
        try:
            if await check_attachment(message.author, temp_filename, nsfw_callback, bot, guild_id, message):
                return True
        finally:
            _safe_delete(temp_filename)

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
            # Perform actions only if check-tenor-gifs is enabled
            perform_actions = True
            if is_tenor and not await mysql.get_settings(guild_id, "check-tenor-gifs"):
                perform_actions = False

            async with temp_download(gif_url) as temp_filename:
                if await check_attachment(author=message.author, 
                                          temp_filename=temp_filename, 
                                          nsfw_callback=nsfw_callback, 
                                          bot=bot, 
                                          guild_id=guild_id, 
                                          message=message,
                                          perform_actions=perform_actions):
                    return True
        
    for sticker in stickers:
        sticker_url = sticker.url
        if not sticker_url:
            continue

        extension = sticker.format.name.lower()

        async with temp_download(sticker_url, ext=extension) as temp_location:
            gif_location = temp_location

            if extension == "lottie":
                gif_location = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.gif")
                print(f"[sticker] Starting Lottie export: {temp_location} → {gif_location}")
                animation = await asyncio.to_thread(lottie.parsers.tgs.parse_tgs, temp_location)
                await asyncio.to_thread(export_gif, animation, gif_location, skip_frames=4)
                print(f"[sticker] Finished Lottie export: {gif_location}")

            elif extension == "apng":
                gif_location = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.gif")
                await asyncio.to_thread(apnggif, temp_location, gif_location)

            try:
                if await check_attachment(
                    message.author,
                    gif_location,
                    nsfw_callback,
                    bot,
                    guild_id,
                    message
                ):
                    return True
            finally:
                if gif_location != temp_location:
                    _safe_delete(gif_location)

    return False

def _file_to_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

async def moderator_api(text: str | None = None,
                        image_path: str | None = None,
                        image: Image.Image | None = None,
                        guild_id: int | None = None,
                        bot: commands.Bot | None = None,
                        max_attempts: int = 3) -> dict:
    result = {
        "is_nsfw": None,
        "category": None,
        "reason": None
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
            b64 = await asyncio.to_thread(_file_to_b64, image_path)
        except Exception as e:
            print(f"[moderator_api] Error reading/encoding image {image_path}: {e}")
            return result
        inputs.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        })

    if not inputs:
        print("[moderator_api] No inputs were provided")
        return result

    for attempt in range(max_attempts):
        client, encrypted_key = await api.get_api_client(guild_id)
        if not client:
            print("[moderator_api] No available API key.")
            await asyncio.sleep(0.5)
            continue
        try:
            response = await client.moderations.create(
                model="omni-moderation-latest" if image_path else "text-moderation-latest",
                input=inputs
            )
        except openai.AuthenticationError:
            print("[moderator_api] Authentication failed. Marking key as not working.")
            await api.set_api_key_not_working(api_key=encrypted_key, bot=bot)
            continue
        except openai.RateLimitError as e:
            print(f"[moderator_api] Rate limit error: {e}. Marking key as not working.")
            await api.set_api_key_not_working(api_key=encrypted_key, bot=bot)
            continue
        except Exception as e:
            print(f"[moderator_api] Unexpected error from OpenAI API: {e}. Marking key as not working.")
            await api.set_api_key_not_working(api_key=encrypted_key, bot=bot)
            continue

        if not response or not response.results:
            continue

        if not await api.is_api_key_working(encrypted_key):
            await api.set_api_key_working(encrypted_key)

        results = response.results[0]
        settings = await mysql.get_settings(guild_id, [NSFW_CATEGORY_SETTING, "threshold"])
        allowed_categories = settings.get(NSFW_CATEGORY_SETTING, [])
        threshold = settings.get("threshold", 0.7)
        flagged_any = False
        for category, is_flagged in results.categories.__dict__.items():
            normalized_category = category.replace("/", "_").replace("-", "_")
            score = results.category_scores.__dict__.get(category, 0)
            # Filter out categories that are not flagged
            if not is_flagged:
                continue
            flagged_any = True
            # Add vector for flagged category
            print(f"[moderator_api] Adding vector for category '{normalized_category}' with score {score:.2f}")
            clip_vectors.add_vector(image, metadata={"category": normalized_category, "score": score})
            # Ignore low confidence scores - Global settings
            if score < threshold:
                print(f"[moderator_api] Category '{normalized_category}' flagged with low score {score:.2f}. Ignoring.")
                continue
            # Check if category is allowed in this guild
            if allowed_categories and not _is_allowed_category(category, allowed_categories):
                print(f"[moderator_api] Category '{normalized_category}' is not allowed in this guild.")
                continue
            result["is_nsfw"] = True
            result["category"] = normalized_category
            result["reason"] = f"Flagged as {normalized_category} with score {score:.2f}"
            return result

        result["is_nsfw"] = False
        # None represents SFW
        if not flagged_any:
            print("[moderator_api] Adding vector for SFW image.")
            clip_vectors.add_vector(image, metadata={"category": None, "score": 0.0})
        return result
    
    print("[moderator_api] All API key attempts failed.")
    return result

def _convert_to_png_safe(input_path: str, output_path: str) -> Optional[str]:
    try:
        with Image.open(input_path) as img:
            img = img.convert("RGBA")
            img.save(output_path, format="PNG")
        return output_path
    except Exception as e:
        print(f"[convert] Failed to convert {input_path} to PNG: {e}")
        return None

async def process_image(original_filename: str,
                        guild_id: int | None = None,
                        clean_up: bool = True,
                        bot: commands.Bot | None = None) -> dict | None:
                        
    try:
        png_converted_path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex[:12]}.png")
        # Convert to PNG and reload as RGB as OpenAI expects RGB images
        result = await asyncio.to_thread(_convert_to_png_safe, original_filename, png_converted_path)
        if not result:
            print(f"[process_image] PNG conversion failed: {original_filename}")
            return None

        image = Image.open(png_converted_path).convert("RGB")

        # Try similarity match first
        settings = await mysql.get_settings(guild_id, [NSFW_CATEGORY_SETTING, "threshold"])
        allowed_categories = settings.get(NSFW_CATEGORY_SETTING, [])
        threshold = settings.get("threshold", 0.70)
        similarity_response = clip_vectors.query_similar(image, threshold=0.70)
        if similarity_response:
            for item in similarity_response:
                category = item.get("category")
                similarity = item.get("similarity", 0) # Similarity score from vector search
                score = item.get("score", 0) # OpenAI API determined score
                if category:
                    if score < threshold:
                        continue
                    if _is_allowed_category(category, allowed_categories):
                        return {"is_nsfw": True, "category": category, "reason": "Similarity match"}
                    else:
                        return {"is_nsfw": False, "category": category, "reason": "Excluded similarity match"}
                else:
                    return {"is_nsfw": False, "category": None, "reason": "Similarity match"}

        response = await moderator_api(image_path=png_converted_path,
                                    guild_id=guild_id,
                                    bot=bot,
                                    image=image)
        return response

    except Exception as e:
        print(traceback.format_exc())
        print(f"[process_image] Error processing image {original_filename}: {e}")
        return None
    finally:
        _safe_delete(png_converted_path)
        if clean_up:
            _safe_delete(original_filename)

async def handle_nsfw_content(user: Member, bot: commands.Bot, guild_id:int,  reason: str, image: discord.File, message: discord.Message):

    action_flag = await mysql.get_settings(guild_id, NSFW_ACTION_SETTING)
    if action_flag:
        try:
            await strike.perform_disciplinary_action(
                user=user,
                bot=bot,
                action_string=action_flag,
                reason=reason,
                source="nsfw",
                message=message
            )
        except Exception:
            pass
    
    embed = discord.Embed(
    title="NSFW Content Detected",
    description=(
        f"**User:** {user.mention} ({user.display_name})\n"
        f"**Reason:** {reason}"
    ),
    color=discord.Color.red()
    )
    embed.set_thumbnail(url=user.display_avatar.url)
    embed.set_image(url=f"attachment://{image.filename}")
    embed.set_footer(text=f"User ID: {user.id}")

    nsfw_channel_id = await mysql.get_settings(user.guild.id, "nsfw-channel")
    strike_channel_id = await mysql.get_settings(user.guild.id, "strike-channel")

    if nsfw_channel_id:
        await mod_logging.log_to_channel(embed, nsfw_channel_id, bot, image)
    elif strike_channel_id:
        await mod_logging.log_to_channel(embed, strike_channel_id, bot)
    
    try:
        image.close()
        _safe_delete(image.fp.name)
    except Exception as e:
        print(f"[cleanup] couldn't delete evidence file: {e}")