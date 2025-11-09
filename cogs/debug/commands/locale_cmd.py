from __future__ import annotations

__all__ = ["send_current_locale"]


async def send_current_locale(cog, interaction):
    current = cog.bot.current_locale()
    fallback = cog.bot.translator.default_locale
    locale_texts = cog.bot.translate(
        "cogs.debug.locale",
        guild_id=interaction.guild.id if interaction.guild else None,
    )
    message = locale_texts["current"].format(locale=current or fallback)
    await interaction.response.send_message(message, ephemeral=True)
