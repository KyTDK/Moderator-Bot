from discord.ext import commands
from cogs.autonomous_moderation.auto_commands import setup_commands
from cogs.autonomous_moderation.autonomous_moderator import setup_autonomous
from cogs.autonomous_moderation.adaptive_moderation import setup_adaptive

async def setup(bot: commands.Bot):
    await setup_autonomous(bot)
    await setup_adaptive(bot)
    await setup_commands(bot)