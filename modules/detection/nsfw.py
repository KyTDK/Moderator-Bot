import os
import traceback
import asyncio
from dotenv import load_dotenv
from nudenet import NudeDetector
from PIL import Image
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

USE_MODERATOR_API = os.getenv('USE_MODERATOR_API') == 'True'
load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API')

moderator_api_category_exclusions = {"violence", "self_harm", "harassment"}
nsfw_labels = {
    "BUTTOCKS_EXPOSED", "FEMALE_BREAST_EXPOSED", "FEMALE_GENITALIA_EXPOSED",
    "ANUS_EXPOSED", "MALE_GENITALIA_EXPOSED"
}

if not USE_MODERATOR_API:
    detector = NudeDetector(model_path="640m.onnx", inference_resolution=640)
elif not OPENAI_API_KEY:
    raise ValueError("OPENAI_API_KEY is not set.")

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

async def process_video(original_filename: str, nsfw_callback, message: discord.Message, bot: commands.Bot, frame_interval: int = 20) -> Tuple[Optional[discord.File], bool]:
    file_to_send = None
    print("Processing video for " + str(original_filename))
    try:
        vidcap = await asyncio.to_thread(cv2.VideoCapture, original_filename)
        count = 0
        while True:
            success, image = await asyncio.to_thread(vidcap.read)
            if not success:
                break

            if count % frame_interval == 0:
                frame_filename = f"{uuid.uuid4().hex[:8]}_frame{count}.jpg"
                await asyncio.to_thread(cv2.imwrite, frame_filename, image)
                try:
                    if await process_image(frame_filename, guild_id=message.guild.id):
                        if nsfw_callback:
                            file_to_send = discord.File(frame_filename, filename=os.path.basename(frame_filename))
                            await nsfw_callback(message.author, bot, "Uploading explicit content", file_to_send)
                        return file_to_send, True
                finally:
                    if os.path.exists(frame_filename):
                        os.remove(frame_filename)
            count += 1
    finally:
        await asyncio.to_thread(vidcap.release)
        if os.path.exists(original_filename):
            os.remove(original_filename)
    return None, False

async def is_nsfw(message: discord.Message, bot: commands.Bot, nsfw_callback=None) -> bool:
    for attachment in message.attachments:
        temp_filename = os.path.join(os.getcwd(), f"temp_{attachment.filename}")
        await attachment.save(temp_filename)
        try:
            file_type = determine_file_type(temp_filename)
            if file_type == "Image":
                if await process_image(temp_filename, guild_id=message.guild.id):
                    if nsfw_callback:
                        file = discord.File(temp_filename, filename=attachment.filename)
                        await nsfw_callback(message.author, bot, "Uploading explicit content", file)
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
    is_video = image_path is not None
    if text and not image_path:
        inputs = text
    if is_video:
        try:
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
        client, encrypted_key = api.get_api_client(guild_id)
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
            api.set_api_key_not_working(encrypted_key)
            continue
        except openai.RateLimitError as e:
            print(f"Rate limit error: {e}. Marking key as not working.")
            api.set_api_key_not_working(encrypted_key)
            continue
        except Exception as e:
            print(f"Unexpected error from OpenAI API: {e}")
            continue
        
        if not response or not response.results:
            print("No moderation results returned.")
            continue
        if not api.is_api_key_working(encrypted_key):
            api.set_api_key_working(encrypted_key)

        results = response.results[0]
        categories = results.categories

        for category, is_flagged in categories.__dict__.items():
            if is_flagged:
                if is_video and category in moderator_api_category_exclusions:
                    continue
                print(f"Category {category} is flagged.")
                return category
        return None
    print("All API key attempts failed.")
    return None

def nsfw_model(converted_filename: str):
    results = detector.detect(converted_filename)
    for result in results:
        if result['class'] in nsfw_labels and result['score'] >= 0.8:
            return result['class']
    return None

async def process_image(original_filename, guild_id=None):
    converted_filename = os.path.join(os.getcwd(), 'converted_image.jpg')
    try:
        try:
            await asyncio.to_thread(convert_to_jpeg, original_filename, converted_filename)
        except Exception:
            print("Error converting image:")
            print(traceback.format_exc())
            return False

        try:
            if USE_MODERATOR_API:
                category = await moderator_api(image_path=converted_filename, guild_id=guild_id)
            else:
                category = await asyncio.to_thread(nsfw_model, converted_filename)
            return category is not None
        except Exception:
            print("Error during detection:")
            print(traceback.format_exc())
            return False
    finally:
        if os.path.exists(converted_filename):
            os.remove(converted_filename)

def convert_to_jpeg(src_path, dst_path):
    with Image.open(src_path) as img:
        rgb_img = img.convert("RGB")
        rgb_img.save(dst_path, "JPEG")

async def handle_nsfw_content(user: Member, bot: commands.Bot, reason: str, image: discord.File):
    if mysql.get_settings(user.guild.id, "strike-nsfw") == True:
        embed = await strike.strike(user=user, bot=bot, reason=reason, interaction=None)
        embed.title = "NSFW Content strike"
        embed.set_image(url=f"attachment://{image.filename}")
        NSFW_STRIKES_ID = mysql.get_settings(user.guild.id, "nsfw-channel")
        STRIKE_CHANNEL_ID = mysql.get_settings(user.guild.id, "strike-channel")
        if NSFW_STRIKES_ID:
            await logging.log_to_channel(embed, NSFW_STRIKES_ID, bot, image)
        elif STRIKE_CHANNEL_ID:
            await logging.log_to_channel(embed, STRIKE_CHANNEL_ID, bot)