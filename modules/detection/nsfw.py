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
from modules.utils import logging, mysql, api
from modules.moderation import strike
from urllib.parse import urlparse
from typing import Optional
from apnggif import apnggif
import openai
import numpy as np
from contextlib import asynccontextmanager
from tempfile import NamedTemporaryFile, gettempdir
from PIL import Image, ImageSequence

TMP_DIR = os.path.join(gettempdir(), "modbot")
os.makedirs(TMP_DIR, exist_ok=True)

moderator_api_category_exclusions = {"violence", "self_harm", "harassment"}
MAX_FRAMES_PER_VIDEO = 10          # hard cap so we never spawn hundreds of tasks
MAX_CONCURRENT_FRAMES = 4          # limits OpenAI calls running at once

@asynccontextmanager
async def temp_download(url: str, ext: str | None = None):
    """Download *url* into our tmp dir and yield the path, cleaning up automatically."""
    # normalise extension – always starts with a dot
    if ext and not ext.startswith('.'):
        ext = '.' + ext
    ext = ext or os.path.splitext(urlparse(url).path)[1] or ".bin"

    path = os.path.join(TMP_DIR, f"{uuid.uuid4().hex}{ext}")

    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
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

def _safe_delete(path: str):
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as e:
        print(f"[safe_delete] Failed to delete {path}: {e}")

def determine_file_type(file_path: str) -> str:
    kind = filetype.guess(file_path)
    ext = file_path.lower().split('.')[-1]

    # Special-case check for .webp and .gif animation
    if ext in {'webp', 'gif'}:
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

def _extract_frames_threaded(filename: str, wanted: int) -> tuple[list[str], float]:
    """Blocking OpenCV extraction, run in a thread."""
    cap = cv2.VideoCapture(filename)
    temp_frames: list[str] = []
    try:
        fps = cap.get(cv2.CAP_PROP_FPS) or 1
        total = int(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
        if total <= 0:
            return [], 0
        duration = total / fps
        idxs = np.linspace(0, total - 1, min(wanted, total), dtype=int)
        for idx in idxs:
            cap.set(cv2.CAP_PROP_POS_FRAMES, int(idx))
            ok, frame = cap.read()
            if not ok:
                continue
            name = f"{uuid.uuid4().hex[:8]}_{idx}.jpg"
            cv2.imwrite(name, frame)
            temp_frames.append(name)
        return temp_frames, duration
    finally:
        cap.release()

async def process_video(
    original_filename: str,
    nsfw_callback,
    member: discord.Member,
    guild_id: int,
    bot: commands.Bot,
) -> tuple[Optional[discord.File], bool]:
    print(f"[process_video] Starting video analysis: {original_filename}")
    
    temp_frames, _ = await asyncio.to_thread(
        _extract_frames_threaded, original_filename, MAX_FRAMES_PER_VIDEO
    )
    print(f"[process_video] Extracted {len(temp_frames)} frames")

    if not temp_frames:
        print(f"[process_video] No frames extracted, deleting original: {original_filename}")
        _safe_delete(original_filename)
        return None, False

    semaphore = asyncio.Semaphore(MAX_CONCURRENT_FRAMES)
    flagged_file: discord.File | None = None

    async def analyse(path: str):
        print(f"[process_video] Analyzing frame: {path}")
        async with semaphore:
            try:
                cat = await process_image(path, guild_id=guild_id, clean_up=False)
                print(f"[process_video] Frame {path} result: {cat}")
                return (path, cat) if cat else None
            except Exception as e:
                print(f"[process_video] Analyse error {path}: {e}")
                return None

    tasks = [asyncio.create_task(analyse(p)) for p in temp_frames]
    print("[process_video] Analysis tasks started")

    try:
        for done in asyncio.as_completed(tasks):
            res = await done
            if res:
                frame_path, category = res
                print(f"[process_video] NSFW detected in frame: {frame_path} (category: {category})")

                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

                if nsfw_callback:
                    flagged_file = discord.File(frame_path,
                                                filename=os.path.basename(frame_path))
                    await nsfw_callback(
                        member,
                        bot,
                        f"Detected potential policy violation (Category: **{category.title()}**)",
                        flagged_file,
                    )
                return flagged_file, True
        print("[process_video] No frames were flagged")
        return None, False
    finally:
        for p in temp_frames:
            print(f"[process_video] Deleting frame: {p}")
            _safe_delete(p)
        print(f"[process_video] Deleting original file: {original_filename}")
        _safe_delete(original_filename)

async def check_attachment(author,
                           temp_filename,
                           nsfw_callback,
                           filename,
                           bot):
    file_type = determine_file_type(temp_filename)

    guild_id = author.guild.id if isinstance(author, discord.Member) else None
    if guild_id is None:
        return False  # DM or system message

    if file_type == "Image":
        category = await process_image(temp_filename, guild_id=guild_id, clean_up=False)
        if category and nsfw_callback:
            await nsfw_callback(
                author,
                bot,
                f"Detected potential policy violation (Category: **{category.title()}**)",
                discord.File(temp_filename, filename=filename),
            )
        return bool(category)

    if file_type == "Video":
        _file, flagged = await process_video(
            temp_filename, nsfw_callback, author, guild_id, bot
        )
        return flagged

    print(f"[check_attachment] Unsupported media type: {file_type}")
    return False

async def is_nsfw(bot: commands.Bot,
                  message: discord.Message | None = None,
                  nsfw_callback=None,
                  url: str | None = None,
                  member: discord.Member | None = None,
                  filename: str | None = None) -> bool:

    if url:
        async with temp_download(url) as temp_filename:
            return await check_attachment(member, temp_filename, nsfw_callback, filename, bot)

    if message is None:
        print("Message is None")
        return False

    if message.reference and message.reference.cached_message:
        message = message.reference.cached_message

    for attachment in message.attachments:
        suffix = os.path.splitext(attachment.filename)[1] or ""
        with NamedTemporaryFile(delete=False, dir=TMP_DIR, suffix=suffix) as tmp:
            await attachment.save(tmp.name)
            temp_filename = tmp.name
        try:
            return await check_attachment(message.author, temp_filename, nsfw_callback, filename, bot)
        finally:
            _safe_delete(temp_filename)

    for embed in message.embeds:
        gif_url = (
            embed.image.url if embed.image else
            embed.video.url if embed.video else
            embed.thumbnail.url if embed.thumbnail else None
        )
        if not gif_url:
            continue
        domain = urlparse(gif_url).netloc.lower()
        if domain == "tenor.com" or domain.endswith(".tenor.com"):
            continue  # ignore Tenor
        async with temp_download(gif_url, "gif") as temp_location:
            return await check_attachment(message.author, gif_location, nsfw_callback, filename, bot)
        
    for sticker in message.stickers:
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
                return await check_attachment(
                    message.author, gif_location, nsfw_callback,
                    filename, bot
                )
            finally:
                if gif_location != temp_location:
                    _safe_delete(gif_location)

    return False

def _file_to_b64(path: str) -> str:
    with open(path, "rb") as f:
        return base64.b64encode(f.read()).decode()

async def moderator_api(text: str | None = None,
                        image_path: str | None = None,
                        guild_id: int | None = None,
                        max_attempts: int = 10) -> Optional[str]:
    inputs: list | str = []
    is_video = image_path is not None

    if text and not image_path:
        inputs = text

    if is_video:
        if not os.path.exists(image_path):
            print(f"Image path does not exist: {image_path}")
            return None
        try:
            b64 = await asyncio.to_thread(_file_to_b64, image_path)
        except Exception as e:
            print(f"Error reading/encoding image {image_path}: {e}")
            return None
        inputs.append({
            "type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
        })

    if not inputs:
        print("No inputs were provided")
        return None

    for attempt in range(max_attempts):
        client, encrypted_key = await api.get_api_client(guild_id)
        if not client:
            print("No available API key.")
            await asyncio.sleep(0.5)
            continue
        try:
            response = await client.moderations.create(
                model="omni-moderation-latest" if image_path else "text-moderation-latest",
                input=inputs
            )
        except openai.AuthenticationError:
            print("Authentication failed. Marking key as not working.")
            await api.set_api_key_not_working(encrypted_key)
            continue
        except openai.RateLimitError as e:
            print(f"Rate limit error: {e}. Marking key as not working.")
            await api.set_api_key_not_working(encrypted_key)
            continue
        except Exception as e:
            print(f"Unexpected error from OpenAI API: {e}")
            continue

        if not response or not response.results:
            print("No moderation results returned.")
            continue

        # Mark key healthy again if we got a good response
        if not await api.is_api_key_working(encrypted_key):
            await api.set_api_key_working(encrypted_key)

        results = response.results[0]
        for category, is_flagged in results.categories.__dict__.items():
            if not is_flagged:
                continue
            score = results.category_scores.__dict__.get(category, 0)
            if is_video and category in moderator_api_category_exclusions:
                continue
            if not is_video and score < 0.6:
                continue
            return category
        return None
    print("All API key attempts failed.")
    return None

async def process_image(original_filename: str,
                        guild_id: int | None = None,
                        clean_up: bool = True) -> Optional[str]:
    print(f"[process_image] Starting scan for: {original_filename} (guild: {guild_id})")
    try:
        result = await moderator_api(image_path=original_filename, guild_id=guild_id)
        print(f"[process_image] Moderation result for {original_filename}: {result}")
        return result
    except Exception as e:
        print(traceback.format_exc())
        print(f"[process_image] Error processing image {original_filename}: {e}")
        return None
    finally:
        if clean_up and os.path.exists(original_filename):
            print(f"[process_image] Cleaning up file: {original_filename}")
            _safe_delete(original_filename)

async def handle_nsfw_content(user: Member, bot: commands.Bot, reason: str, image: discord.File):
    if await mysql.get_settings(user.guild.id, "strike-nsfw") is not True:
        return

    embed = await strike.strike(user=user, bot=bot, reason=reason,
                                interaction=None, log_to_channel=False)
    embed.set_image(url=f"attachment://{image.filename}")

    nsfw_channel_id = await mysql.get_settings(user.guild.id, "nsfw-channel")
    strike_channel_id = await mysql.get_settings(user.guild.id, "strike-channel")

    if nsfw_channel_id:
        await logging.log_to_channel(embed, nsfw_channel_id, bot, image)
    elif strike_channel_id:
        await logging.log_to_channel(embed, strike_channel_id, bot)