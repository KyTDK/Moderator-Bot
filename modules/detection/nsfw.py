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
from typing import Optional, Tuple
from apnggif import apnggif
import openai

moderator_api_category_exclusions = {"violence", "self_harm", "harassment"}

def determine_file_type(file_path):
    kind = filetype.guess(file_path)
    if kind is None:
        return 'Unknown'
    elif kind.mime.startswith('image'):
        return 'Image'
    elif kind.mime.startswith('video'):
        return 'Video'
    else:
        return kind.mime

async def process_video(original_filename: str, nsfw_callback, message: discord.Message, bot: commands.Bot, seconds_interval: float = None) -> Tuple[Optional[discord.File], bool]:
    print(f"Processing video: {original_filename}")
    temp_frames = []
    file_to_send = None
    semaphore = asyncio.Semaphore(5)

    try:
        vidcap = await asyncio.to_thread(cv2.VideoCapture, original_filename)
        fps = await asyncio.to_thread(vidcap.get, cv2.CAP_PROP_FPS)
        total_frames = await asyncio.to_thread(vidcap.get, cv2.CAP_PROP_FRAME_COUNT)
        duration_secs = total_frames / fps if fps else 0

        if not seconds_interval:
            if duration_secs <= 30:
                seconds_interval = 0.5
            elif duration_secs <= 60:
                seconds_interval = 2
            elif duration_secs <= 300:
                seconds_interval = 3
            else:
                seconds_interval = 5

        frame_interval = max(1, int(fps * seconds_interval))
        print(f"FPS: {fps}, Duration: {duration_secs:.2f}s, Frame Interval: {frame_interval}")

        count = 0
        while True:
            success, image = await asyncio.to_thread(vidcap.read)
            if not success:
                break

            if count % frame_interval == 0:
                frame_filename = f"{uuid.uuid4().hex[:8]}_frame{count}.jpg"
                await asyncio.to_thread(cv2.imwrite, frame_filename, image)
                temp_frames.append(frame_filename)

            count += 1

        await asyncio.to_thread(vidcap.release)

        async def analyze_frame(frame_path):
            try:
                if os.path.exists(frame_path):
                    async with semaphore:
                        category = await process_image(frame_path, guild_id=message.guild.id, clean_up=False)
                        return (frame_path, category) if category else None
            except Exception as e:
                print(traceback.format_exc())
                print(f"Error processing frame {frame_path}: {e}")
                return None

        tasks = [asyncio.create_task(analyze_frame(f)) for f in temp_frames]

        for coro in asyncio.as_completed(tasks):
            try:
                result = await coro
                if result:
                    frame_path, category = result
                    if os.path.exists(frame_path):
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        if nsfw_callback:
                            file_to_send = discord.File(frame_path, filename=os.path.basename(frame_path))
                            await nsfw_callback(message.author, bot, f"Detected potential policy violation (Category: **{category.title()}**)", file_to_send)
                        return file_to_send, True
            except asyncio.CancelledError:
                print("Task was cancelled.")
                continue

        return None, False

    finally:
        for f in temp_frames:
            if os.path.exists(f):
                os.remove(f)
        if os.path.exists(original_filename):
            os.remove(original_filename)

async def is_nsfw(message: discord.Message, bot: commands.Bot, nsfw_callback=None) -> bool:
    for attachment in message.attachments:
        temp_filename = os.path.join(os.getcwd(), f"temp_{attachment.filename}")
        await attachment.save(temp_filename)
        try:
            file_type = determine_file_type(temp_filename)
            if file_type == "Image":
                category = await process_image(temp_filename, guild_id=message.guild.id, clean_up=False)
                if category != None:
                    if nsfw_callback:
                        file = discord.File(temp_filename, filename=attachment.filename)
                        await nsfw_callback(message.author, bot, f"Detected potential policy violation (Category: **{category.title()}**)", file)
                    return True
            elif file_type == "Video":
                file, result = await process_video(temp_filename, nsfw_callback, message, bot)
                if result:
                    return True
            else:
                print("Unable to check media: " + file_type)
        finally:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    for embed in message.embeds:
        gif_url = (
            embed.image.url if embed.image else
            embed.video.url if embed.video else
            embed.thumbnail.url if embed.thumbnail else None
        )

        if gif_url:
            domain = urlparse(gif_url).netloc.lower()
            if domain == "tenor.com" or domain.endswith(".tenor.com"):
                continue

            temp_location = f"{uuid.uuid4().hex[:12]}.gif"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(gif_url) as resp:
                        if resp.status == 200:
                            content = await resp.read()
                            async with aiofiles.open(temp_location, "wb") as f:
                                await f.write(content)
                        else:
                            print(f"Failed to get media: {resp.status}")
                            continue
                file, result = await process_video(temp_location, nsfw_callback, message, bot)
                return result
            finally:
                if os.path.exists(temp_location):
                    os.remove(temp_location)

    for sticker in message.stickers:
        sticker_url = sticker.url
        if not sticker_url:
            continue

        extension = sticker.format.name.lower()
        temp_location = f"{uuid.uuid4().hex[:12]}.{extension}"
        gif_location = f"{uuid.uuid4().hex[:12]}.gif"

        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(sticker_url) as resp:
                    if resp.status == 200:
                        content = await resp.read()
                        async with aiofiles.open(temp_location, 'wb') as f:
                            await f.write(content)
                    else:
                        continue

            if extension == "lottie":
                animation = await asyncio.to_thread(lottie.parsers.tgs.parse_tgs, temp_location)
                await asyncio.to_thread(export_gif, animation, gif_location, skip_frames=4)
            elif extension == "apng":
                await asyncio.to_thread(apnggif, temp_location, gif_location)
            else:
                gif_location = temp_location
                print(f"Unhandled extension: {extension}")

            file, result = await process_video(gif_location, nsfw_callback, message, bot)
            if result:
                return True

        finally:
            if os.path.exists(temp_location):
                os.remove(temp_location)
            if os.path.exists(gif_location):
                os.remove(gif_location)

    return False

async def moderator_api(text: str = None, image_path: str = None, guild_id: int = None, max_attempts: int = 10) -> str:
    inputs = []
    if text and not image_path:
        inputs = text
    if image_path is not None:
        try:
            if not os.path.exists(image_path):
                print(f"Image path does not exist: {image_path}")
                return None
            with open(image_path, "rb") as f:
                img_data = f.read()
            b64 = base64.b64encode(img_data).decode("utf-8")
            inputs.append({
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{b64}"}
            })
        except Exception as e:
            print(f"Error opening image {image_path}: {e}")
            return None

    if not inputs:
        return

    for attempt in range(max_attempts):
        client, encrypted_key = await api.get_api_client(guild_id)
        if not client:
            print("No available API key.")
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
        if not await api.is_api_key_working(encrypted_key):
            await api.set_api_key_working(encrypted_key)

        results = response.results[0]
        categories = results.categories

        for category, is_flagged in categories.__dict__.items():
            if is_flagged:
                if category in moderator_api_category_exclusions:
                    continue
                print(f"Category {category} is flagged.")
                return category
        return None
    print("All API key attempts failed.")
    return None

async def process_image(original_filename, guild_id=None, clean_up=True):
    try:
        converted_filename = os.path.join(os.getcwd(), f"converted_{uuid.uuid4().hex[:8]}.jpg")
        category = await moderator_api(image_path=original_filename, guild_id=guild_id)
        return category
    except Exception as e:
        print(traceback.format_exc())
        print(f"Error processing image {original_filename}: {e}")
        return False
    finally:
        if clean_up:
            if os.path.exists(original_filename):
                os.remove(original_filename)
            if os.path.exists(converted_filename):
                os.remove(converted_filename)

async def handle_nsfw_content(user: Member, bot: commands.Bot, reason: str, image: discord.File):
    if await mysql.get_settings(user.guild.id, "strike-nsfw") == True:
        embed = await strike.strike(user=user, bot=bot, reason=reason, interaction=None, log_to_channel=False)
        embed.set_image(url=f"attachment://{image.filename}")
        NSFW_STRIKES_ID = await mysql.get_settings(user.guild.id, "nsfw-channel")
        STRIKE_CHANNEL_ID = await mysql.get_settings(user.guild.id, "strike-channel")
        if NSFW_STRIKES_ID:
            await logging.log_to_channel(embed, NSFW_STRIKES_ID, bot, image)
        elif STRIKE_CHANNEL_ID:
            await logging.log_to_channel(embed, STRIKE_CHANNEL_ID, bot)