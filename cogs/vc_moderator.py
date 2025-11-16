import logging
from discord.ext import commands

log = logging.getLogger(__name__)

try:
    from cogs.voice_moderation.voice_moderator import setup_voice_moderation  # type: ignore
    _VOICE_IMPORT_ERROR = None
except Exception as exc:
    setup_voice_moderation = None  # type: ignore
    _VOICE_IMPORT_ERROR = exc

async def setup(bot: commands.Bot):
    if setup_voice_moderation is None:
        log.warning(
            "Voice moderation cog skipped: %s. Install 'discord-ext-voice-recv' (and deps) "
            "or disable the vc_moderator extension.",
            _VOICE_IMPORT_ERROR,
        )
        return
    await setup_voice_moderation(bot)
