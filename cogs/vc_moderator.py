import logging
from discord.ext import commands

from modules.core.health import FeatureStatus, report_feature

log = logging.getLogger(__name__)

try:
    from cogs.voice_moderation.voice_moderator import setup_voice_moderation  # type: ignore
    _VOICE_IMPORT_ERROR = None
    report_feature(
        "voice.moderation",
        label="Voice moderation",
        status=FeatureStatus.OK,
        category="voice",
        detail="Voice moderation cog ready.",
    )
except Exception as exc:  # pragma: no cover - optional dependency guard
    setup_voice_moderation = None  # type: ignore
    _VOICE_IMPORT_ERROR = exc
    report_feature(
        "voice.moderation",
        label="Voice moderation",
        status=FeatureStatus.DISABLED,
        category="voice",
        detail=str(exc),
        remedy="Install discord-ext-voice-recv and opus libraries.",
        using_fallback=True,
    )

async def setup(bot: commands.Bot):
    if setup_voice_moderation is None:
        log.warning(
            "Voice moderation cog skipped: %s. Install 'discord-ext-voice-recv' (and deps) "
            "or disable the vc_moderator extension.",
            _VOICE_IMPORT_ERROR,
        )
        return
    await setup_voice_moderation(bot)
