# Moderator Bot

Moderator Bot is a moderation assistant for Discord servers. It combines automated scanning, configurable enforcement, and structured logging so teams can manage risk without building custom tooling. Visit https://modbot.neomechanical.com for plan details, dashboards, and support resources.

## Feature Availability

| Feature | Free Plan | Core (Accelerated) | Pro (Accelerated) | Ultra (Accelerated) |
| --- | --- | --- | --- | --- |
| Media NSFW detection | ✔️ | ✔️ higher thresholds | ✔️ higher thresholds | ✔️ no media caps |
| NSFW text detection | — | ✔️ | ✔️ | ✔️ |
| AI message moderation | — | ✔️ (gpt-5-nano) | ✔️ (`aimod-high-accuracy`) | ✔️ increased budget |
| Voice moderation | — | ✔️ | ✔️ | ✔️ |
| Scam detection (pattern checks) | ✔️ | ✔️ | ✔️ | ✔️ |
| Scam detection (`check-links`) | — | ✔️ | ✔️ | ✔️ |
| FAQ responses | ✔️ (5 entries) | ✔️ (20 entries) | ✔️ (100 entries) | ✔️ (unlimited) |
| Captcha verification | ✔️ | ✔️ | ✔️ | ✔️ |
| Strike system & logging | ✔️ | ✔️ | ✔️ | ✔️ |

Accelerated plans refer to Core, Pro, and Ultra tiers.

## What the Bot Does

### Media NSFW Detection
Scans images, GIFs, stickers, Tenor GIFs, and avatars. Free servers scan a limited number of frames per video (up to 5) and files up to 128 MiB. Accelerated plans raise those limits and can switch on high-accuracy models.

### NSFW Text Detection (Accelerated)
Reviews message text for sexual content, self-harm, hate, harassment, and illicit activity. Guilds can keep actions automatic or restrict scanning to members with active strikes.

### AI Message Moderation (Accelerated)
Evaluates recent conversation history to check compliance with your server rules. Moderation runs on demand (`report` mode) by default and can be scheduled (`interval`) or adapted dynamically (`adaptive`). Core uses gpt-5-nano with a USD 2 monthly budget; Pro and Ultra increase the budget and can enable `aimod-high-accuracy` (gpt-5-mini).

### Voice Moderation (Accelerated)
Captures live audio, transcribes participants, and evaluates the transcript with the same policy logic used for text channels. Voice monitoring draws from the same budget system as AI message moderation.

### Scam and Link Protection
Pattern matching blocks known scam phrases on every plan. The `check-links` option, available only to Accelerated guilds, queries reputation feeds and unshortens URLs before enforcing actions. Channel-level bypass lists are supported.

### FAQ Responses
Stores frequently asked questions and auto-replies when member messages match above the configured similarity threshold (default 0.72). Entry limits scale with the plan, and external tools can manage content through the documented Redis streams.

### Strike System
Tracks escalating actions when violations are confirmed. Default actions are configurable, strikes can expire automatically, and optional DM notifications keep members informed.

### Captcha Verification
Confirms new members before they gain access. Grace periods, attempt limits, and delivery method (DM or staged embed) are configurable. Logging records each pass and failure.

### Monitoring and Reporting
Logs joins, leaves, bans, kicks, message edits and deletions, timeouts, and invite usage. Teams can disable individual event types, change log destinations, and request verbose NSFW inspection reports when needed.

## Getting Started

1. Invite Moderator Bot and grant the required permissions.
2. Run `/settings view` to review defaults, then adjust core items such as log channels, strike actions, and captcha behaviour.
3. For Accelerated features, verify your plan status with `/plan info` (or the hosting dashboard) before enabling AI, voice, NSFW text, or `check-links`.
4. Use the `/faq`, `/nsfw`, `/ai_mod`, `/scam`, `/strikes`, `/monitor`, and `/verification` command groups to maintain content, thresholds, and enforcement policies.

All configuration is stored at the guild level, so changes made through slash commands, the dashboard, or API integrations stay in sync.
