from discord.ext import commands
from modules.utils import mysql
import os
from dotenv import load_dotenv

CAPTCHA_API_URL = "https://modbot.moderatorbot.com/api/accelerated/captcha?gid="

load_dotenv()

CAPTCHA_API_TOKEN = os.getenv("CAPTCHA_API_TOKEN") 

class CaptchaCog(commands.Cog):
    """A cog for captcha verification."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # Handle on join event
    @commands.Cog.listener()
    async def on_member_join(self, member):
        settings = await mysql.get_settings(member.guild.id, 
                                            [
                                                "captcha-verification-enabled",
                                                "captcha-success-roles",
                                                ])
        if not settings.get("captcha-verification-enabled"):
            return
        
        # Call the captcha API to create a new captcha challenge
        async with self.bot.session.post(CAPTCHA_API_URL + str(member.guild.id)) as resp:
            if resp.status != 200:
                print(f"Failed to create captcha challenge for guild {member.guild.id}")
                return
            data = await resp.json()
            
        

async def setup(bot: commands.Bot):
    await bot.add_cog(CaptchaCog(bot))