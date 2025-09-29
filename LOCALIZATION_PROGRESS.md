# Localization Progress

## Completed
- [x] cogs/accelerated.py — Localized slash command group and command descriptions for premium management.
- [x] modules/utils/api.py — Added a structured validation error for OpenAI API keys with locale-backed messaging.
- [x] cogs/api_pool.py — Localized API key validation responses using the new translator-aware error details.
- [x] cogs/nsfw.py — Moved slash-command metadata and category choices into locale-driven strings.
- [x] cogs/banned_words.py — Localized command descriptions, parameter prompts, and action choices.
- [x] cogs/strikes.py — Localized command metadata and parameter descriptions for moderation utilities.
- [x] cogs/channel_config.py — Localized channel configuration metadata and translated choice labels.
- [x] cogs/settings.py — Localized group metadata and command descriptions.
- [x] cogs/settings.py — Localized help overview embeds, dashboard prompts, and group listings.
- [x] cogs/dashboard.py — Moved slash command description into locale files.
- [x] cogs/scam_detection.py — Localized command metadata, parameter prompts, and link-checking responses.
- [x] cogs/monitoring.py — Localized command metadata, choice labels, and empty-content placeholders.
- [x] cogs/captcha/cog.py — Localized slash command metadata.
- [x] cogs/banned_urls.py — Localized command metadata.
- [x] cogs/debug.py — Localized command metadata and locale summary message.
- [x] cogs/autonomous_moderation/auto_commands.py — Localized command metadata, choice labels, and mode status output.
- [x] cogs/voice_moderation/voice_moderator.py — Localized transcript embeds and budget notifications.
- [x] modules/utils/actions.py — Localized moderation action choices using locale-backed labels.
- [x] modules/utils/mod_logging.py — Localized promotional footer for log embeds in non-Accelerated guilds.
- [x] cogs/captcha/base.py — Localized captcha embed scaffolding, duration text, and default button labels.
- [x] cogs/captcha/delivery.py — Localized DM prompts, embed helpers, and verification call-to-action labels.
- [x] cogs/captcha/embed.py — Localized verification embed title, body copy, footer, and button label.

## To Do
- [ ] Audit remaining modules for embedded message strings and prompts.
