# Localization Progress

## Completed
- [x] cogs/accelerated.py — Localized slash command group and command descriptions for premium management.
- [x] modules/utils/api.py — Added a structured validation error for OpenAI API keys with locale-backed messaging.
- [x] cogs/api_pool.py — Localized API key validation responses using the new translator-aware error details.
- [x] cogs/nsfw.py — Moved slash-command metadata and category choices into locale-driven strings.
- [x] cogs/banned_words.py — Localized command descriptions, parameter prompts, and action choices.
- [x] cogs/strikes.py — Localized command metadata and parameter descriptions for moderation utilities.
- [x] cogs/channel_config.py — Localized channel configuration metadata and translated choice labels.

## To Do
- [ ] cogs/settings.py — Help command embeds and slash metadata still contain hard-coded English strings.
- [ ] cogs/dashboard.py — Slash command description remains inline.
- [ ] cogs/scam_detection.py — Multiple command descriptions and parameter labels remain hard-coded.
- [ ] cogs/monitoring.py — Slash command descriptions and parameter labels require localization.
- [ ] cogs/captcha/cog.py — Slash command metadata still needs to be moved into locale files.
- [ ] cogs/banned_urls.py — Command metadata remains hard-coded.
- [ ] cogs/debug.py — Stats command description is not localized.
- [ ] cogs/autonomous_moderation/auto_commands.py — Command descriptions and parameter labels require localization.
- [ ] cogs/autonomous_moderation/voice_moderator.py — User-facing log message strings should be migrated to locales.
- [ ] Expand audit to remaining modules for embedded message strings and prompts.
