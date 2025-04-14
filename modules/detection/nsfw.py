import os
import traceback
from nudenet import NudeDetector
from PIL import Image
import cv2
import filetype
import uuid
import requests
from discord import Member
from discord.ext import commands
from modules.moderation import strike
import discord
from modules.utils import logging
from dotenv import load_dotenv
from lottie.exporters.gif import export_gif
import lottie
import base64
from modules.utils import mysql, api
import time
import openai
import asyncio

USE_MODERATOR_API = os.getenv('USE_MODERATOR_API') == 'True'


load_dotenv()
OPENAI_API_KEY = os.getenv('OPENAI_API')

def determine_file_type(file_path):
    kind = filetype.guess(file_path)
    if kind is None:
        return 'Unknown'
    elif kind.mime.startswith('image'):
        return 'Image'
    elif kind.mime.startswith('video'):
        return 'Video'
    else:
        return 'Other'
#todo: enable user to set the categories to exclude from the nsfw check
moderator_api_category_exclusions = {
    "violence", # Exclude violence category from the NSFW check as triggers too many false positives
    "self_harm", # Exclude self-harm category from the NSFW check as triggers too many false positives
    "harassment", # Exclude harassment category from the NSFW check as triggers too many false positives
    } 
# NSFW labels to check against
nsfw_labels = {
    "BUTTOCKS_EXPOSED",
    "FEMALE_BREAST_EXPOSED",
    "FEMALE_GENITALIA_EXPOSED",
    #"MALE_BREAST_EXPOSED",
    "ANUS_EXPOSED",
    "MALE_GENITALIA_EXPOSED",
}

# Initialize the NSFW detector
if not USE_MODERATOR_API:
    detector = NudeDetector(model_path="640m.onnx", inference_resolution=640)
else:
    if not OPENAI_API_KEY :
        raise ValueError("OPENAI_API_KEY  is not set.")

import os
import uuid
import cv2
import requests
import discord
from discord.ext import commands
from typing import Optional, Tuple

async def process_video(
    original_filename: str,
    nsfw_callback,
    message: discord.Message,
    bot: commands.Bot,
    frame_interval: int = 20  # Extract every 20th frame
) -> Tuple[Optional[discord.File], bool]:
    """Process a video file frame by frame, calling process_image on selected frames."""
    vidcap = cv2.VideoCapture(original_filename)
    count = 0
    file_to_send = None

    try:
        while True:
            success, image = vidcap.read()
            if not success:
                break
            
            if count % frame_interval == 0:
                # Generate a unique filename for the frame
                frame_filename = f"{uuid.uuid4().hex[:8]}_frame{count}.jpg"
                cv2.imwrite(frame_filename, image)
                try:
                    # Process the extracted frame
                    if await process_image(frame_filename, guild_id=message.guild.id):
                        if nsfw_callback:
                            file_to_send = discord.File(
                                frame_filename, filename=os.path.basename(frame_filename)
                            )
                            await nsfw_callback(message.author, bot, "Uploading explicit content", file_to_send)
                        return file_to_send, True
                finally:
                    if os.path.exists(frame_filename):
                        os.remove(frame_filename)
            count += 1
    finally:
        vidcap.release()
        if os.path.exists(original_filename):
            os.remove(original_filename)

    return None, False

async def is_nsfw(
    message: discord.Message,
    bot: commands.Bot,
    nsfw_callback=None
) -> bool:
    """Check attachments, embeds, and stickers for explicit content."""
    # Process attachments (images or videos)
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
                print("Unable to check media")
        finally:
            if os.path.exists(temp_filename):
                os.remove(temp_filename)

    # Process embeds (e.g. GIFs from image or thumbnail URLs)
    for embed in message.embeds:
        gif_url = embed.image.proxy_url if embed.image else embed.video.url if embed.video else embed.thumbnail.proxy_url if embed.thumbnail else None
        temp_location = f"{uuid.uuid4().hex[:12]}.gif"
        if gif_url:
            try:
                data = requests.get(gif_url).content
                with open(temp_location, "wb") as f:
                    f.write(data)
                file, result = await process_video(temp_location, nsfw_callback, message, bot)
                return result
            finally:
                if os.path.exists(temp_location):
                    os.remove(temp_location)

    # Process stickers
    for sticker in message.stickers:
        sticker_url = sticker.url
        if sticker_url:
            extension = sticker.format.name.lower()
            temp_location = f"{uuid.uuid4().hex[:12]}.{extension}"
            try:
                data = requests.get(sticker_url).content
                with open(temp_location, 'wb') as f:
                    f.write(data)
                if extension == "lottie":
                    # Load the Lottie animation
                    gif_location = f"{uuid.uuid4().hex[:12]}.gif"
                    animation = lottie.parsers.tgs.parse_tgs(temp_location)
                    export_gif(animation, gif_location, skip_frames=4)
                    if os.path.exists(temp_location):
                        os.remove(temp_location)
                    temp_location = gif_location
                file, result = await process_video(temp_location, nsfw_callback, message, bot)
                if result:
                    return True
            finally:
                if os.path.exists(temp_location):
                    os.remove(temp_location)
    return False    


async def moderator_api(text: str = None,
                  image_path: str = None,
                  guild_id: int = None,
                  retries: int = 2,
                  backoff: float = 0.5) -> str:
    """Returns True if any moderation category (not excluded) is flagged."""
    # 1) Build the payload
    inputs = []
    if text and not image_path: # text-moderation
        inputs = text
    
    # Only add the image payload if image_path is not None.
    if image_path is not None:
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
            # Optionally, you can decide to continue without the image or return an error.

    # 2) Initialize client
    client = api.get_api_client(guild_id)
    if not client:
        print("No available API key.")
        return None

    # 3) Attempt the call, with a simple retry if results list is empty
    for attempt in range(1, retries + 1):
        try:
            response = await client.moderations.create(
                model="omni-moderation-latest" if image_path else "text-moderation-latest", # switch to text-moderation to avoid RPD
                input=inputs
            )
        except openai.AuthenticationError: # No access, mark as not working
            api.set_api_key_not_working(client.api_key)
            break 
        except openai.RateLimitError as e: # Keep trying, might be brief rate limit
            if attempt < retries:
                asyncio.sleep(backoff)
                continue
            else: # Rate limit reached, mark as not working
                api.set_api_key_not_working(client.api_key)
                print(f"Rate limit exceeded: {e}")
                break

        results = getattr(response, "results", [])
        if not results:
            # no results returned — maybe transient; retry if we still can
            if attempt < retries:
                time.sleep(backoff)
                continue
            # final attempt, bail out
            return None
        
        # API key now works, change in DB
        if not api.is_api_key_working(client.api_key):
            api.set_api_key_working(client.api_key)

        # we have at least one result, inspect the first
        first = results[0]
        for category, flagged in vars(first.categories).items():
            if flagged and category not in moderator_api_category_exclusions:
                print(f"Category {category} is flagged.")
                return category
        # none of the categories (after exclusions) were flagged
        return None

    # shouldn't get here, but safe default
    return None

def nsfw_model(converted_filename: str):
    results = detector.detect(converted_filename)
    print(results)
    for result in results:
        if result['class'] in nsfw_labels and result['score'] >= 0.8:
            return result['class']
    return None

async def process_image(original_filename, guild_id=None):
    converted_filename = os.path.join(os.getcwd(), 'converted_image.jpg')
    try:
        # Convert the image to JPEG using Pillow
        try:
            with Image.open(original_filename) as img:
                rgb_img = img.convert("RGB")  # JPEG doesn't support alpha channels
                rgb_img.save(converted_filename, "JPEG")
        except Exception:
            print("Error converting image:")
            print(traceback.format_exc())
            return False

        # Run the NSFW detector on the converted image
        try:            
            if USE_MODERATOR_API:
                category = await moderator_api(image_path=converted_filename, guild_id=guild_id)
            else:
                category = nsfw_model(converted_filename)
                
            return category is not None
        except Exception:
            print("Error during detection:")
            print(traceback.format_exc())
            return False
    finally:
        if os.path.exists(converted_filename):
            os.remove(converted_filename)

async def handle_nsfw_content(user: Member, bot: commands.Bot, reason: str, image: discord.File):
    if mysql.get_settings(user.guild.id, "strike-nsfw") == True:
        await strike.strike(user=user, bot=bot, reason=reason, interaction=None)
        embed = discord.Embed(
            title="NSFW Content strike",
            description=f"{user.mention} has received a strike for posting NSFW content.",
            color=discord.Color.red()
        )
        embed.set_image(url=f"attachment://{image.filename}")
        # Add a field for the reason
        embed.add_field(name="Reason", value=reason, inline=False)
        NSFW_STRIKES_ID = mysql.get_settings(user.guild.id, "nsfw-channel")
        STRIKE_CHANNEL_ID = mysql.get_settings(user.guild.id, "strike-channel")
        if NSFW_STRIKES_ID:
            await logging.log_to_channel(embed, NSFW_STRIKES_ID, bot, image)
        elif STRIKE_CHANNEL_ID:
            # exclude image from logging as it is not allowed in the strike channel
            await logging.log_to_channel(embed, STRIKE_CHANNEL_ID, bot)