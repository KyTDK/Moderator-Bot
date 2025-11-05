# Moderator Bot

Moderator Bot provides structured moderation automation for Discord servers, combining rule-based enforcement with AI-driven classifiers across media, text, and voice.

## Feature Summary

| Feature | Coverage | Availability | Key limitations |
| --- | --- | --- | --- |
| Media NSFW detection | Images, GIFs, stickers, Tenor GIFs, avatars, short video segments | All plans | Free tiers scan up to 5 frames per video and 128 MiB per file; Accelerated tiers (Core/Pro/Ultra) raise limits and unlock `nsfw-high-accuracy`. |
| NSFW text detection | Message content routed through the aggregated moderation queue | Enforcement requires Accelerated (Core+) | Without Accelerated the scanner records developer logs only. Supports strike-gated mode and channel exclusions. |
| AI message moderation | OpenAI-powered policy analysis of channel history | Accelerated (Core+) | Monthly budget defaults to USD 2 (Core), USD 4 (Pro), USD 10 (Ultra). `aimod-high-accuracy` (gpt-5-mini) consumes budget faster and requires Pro+. |
| Voice moderation | Live transcription and moderation of voice channels | Accelerated (Core+) | Charges the same plan-based budget; requires voice receive support and configured monitoring channels. |
| FAQ automation | Vector search FAQ responses with Redis-backed remote management | All plans | Entry caps: Free 5, Core 20, Pro 100, Ultra unlimited. Redis streams required for dashboard integrations. |
| Scam and link protection | Heuristic and reputation checks for phishing and malware | All plans (`check-links` toggle requires Accelerated) | Supports channel exclusions and configurable actions. |
| Strikes and escalation | Escalating enforcement ladder with DM notifications | All plans | Supports action cycling, expiry, and integration with other detectors. |
| Captcha verification | Onboarding workflow with DM or public delivery | All plans | Requires captcha API credentials and a logging channel. |
| Logging and monitoring | Join/leave/ban/edit/delete/invite auditing | All plans | Event filters configured via `monitor-events`. |

Accelerated refers to the paid Core, Pro, and Ultra plans. Any feature marked Accelerated is available on Core unless otherwise noted.

## Feature Details

### Media NSFW Moderation
- Scans attachments, Tenor GIFs, stickers, and user profile images; short videos are sampled with plan-specific frame limits (Free 5, Core 100, Pro 300, Ultra unlimited) and corresponding concurrency caps.
- Download caps follow the same tiering (128 MiB Free, 256 MiB Core, 512 MiB Pro, unlimited Ultra).
- Slash commands: `/nsfw add_action`, `/nsfw remove_action`, `/nsfw view_actions`, `/nsfw add_category`, `/nsfw remove_category`, `/nsfw view_categories`, `/nsfw set_threshold`, `/nsfw view_threshold`.
- The `nsfw-verbose` setting relays structured inspection embeds to the origin channel; enable only for teams that can handle the increased volume.
- High accuracy scanning (`nsfw-high-accuracy`) is restricted to Accelerated plans.

### NSFW Text Moderation (Accelerated)
- Expands the NSFW pipeline to cover message text, supporting categories for sexual content, self-harm, hate, harassment, and illicit activity.
- Commands: `/nsfw add_text_action`, `/nsfw remove_text_action`, `/nsfw view_text_actions`, `/nsfw add_text_category`, `/nsfw remove_text_category`, `/nsfw view_text_categories`, `/nsfw add_text_excluded_channel`, `/nsfw remove_text_excluded_channel`, `/nsfw view_text_excluded_channels`, `/nsfw set_text_threshold`, `/nsfw view_text_threshold`, `/nsfw set_text_scanning`, `/nsfw view_text_scanning`, `/nsfw set_text_strike_filter`, `/nsfw view_text_strike_filter`.
- `nsfw-text-strikes-only` restricts scanning to members with existing strikes. When Accelerated is inactive, detections write developer logs but enforcement callbacks are suppressed.

### AI Message Moderation (Accelerated)
- Autonomous moderation is configured through `/ai_mod rules_set`, `/ai_mod rules_show`, `/ai_mod toggle`, `/ai_mod add_action`, `/ai_mod remove_action`, `/ai_mod view_actions`, and the adaptive event commands (`/ai_mod add_adaptive_event`, `/ai_mod remove_adaptive_event`, `/ai_mod clear_adaptive_events`, `/ai_mod view_adaptive_events`).
- `aimod-mode` controls how the system runs (`report`, `interval`, or `adaptive`). By default, moderation runs in report mode when the bot is mentioned.
- Budgets default to USD 2 (Core), USD 4 (Pro), and USD 10 (Ultra) per billing cycle using gpt-5-nano. Enabling `aimod-high-accuracy` switches to gpt-5-mini and requires the Pro tier because of the higher token price.

### Voice Moderation (Accelerated)
- Uses `discord.ext.voice_recv` to capture PCM audio, transcribe speech, and apply the same moderation policies to live voice conversations.
- Charges against the voice moderation budget resolved through `modules.ai.mod_utils`. Ensure opus libraries are installed on the host.
- Channel selection, announcement behaviour, and rule sets are managed in the voice moderation configuration schema.

### FAQ Automation
- `/faq add`, `/faq remove`, `/faq list`, and `/faq enable` manage entries inside Discord.
- Remote systems integrate through the Redis stream processor (`modules.faq.stream.FAQStreamProcessor`). Configure via `FAQ_REDIS_URL`, `FAQ_COMMAND_STREAM`, `FAQ_RESPONSE_STREAM`, `FAQ_STREAM_GROUP`, `FAQ_STREAM_CONSUMER`, `FAQ_STREAM_BLOCK_MS`, `FAQ_STREAM_FETCH_COUNT`, and `FAQ_STREAM_RESPONSE_MAXLEN`. `FAQ_STREAM_ENABLED` overrides auto-start.
- Similarity is controlled by `faq-threshold` (defaults to 0.72, accepts 0.10–1.00). Responses are published only when `faq-enabled` is true.
- Entry quotas: Free 5, Core 20, Pro 100, Ultra unlimited. Exceeding the cap returns a structured `FAQLimitError` payload to the response stream.

### Scam and Link Protection
- Pattern-based detection runs on all plans. External reputation checks (`check-links`) require Accelerated because they rely on premium data sources.
- Commands: `/scam check_links`, `/scam add_action`, `/scam remove_action`, `/scam view`, `/scam exclude_channel_add`, `/scam exclude_channel_remove`, `/scam exclude_channel_list`.

### Strike Management
- `/strikes strike`, `/strikes get`, `/strikes clear`, `/strikes remove`, `/strikes add_action`, `/strikes remove_action`, `/strikes view_actions`, and `/intimidate` cover the strike lifecycle.
- Strike actions, expiries, DM notifications, and cycling behaviour are stored via the guild settings API (`modules/utils/mysql`).

### Captcha Verification
- Configuration uses the standard settings surface: `/settings set name=captcha-verification-enabled`, `captcha-grace-period`, `captcha-max-attempts`, `pre-captcha-roles`, `captcha-success-actions`, `captcha-failure-actions`, `captcha-delivery-method`, and `captcha-embed-channel-id`.
- Operational commands live under `/verification`: `sync` refreshes the embed, `request` manually triggers verification for a member.
- Logs are routed through `/channels set type=Captcha` and stored using `modules.utils.log_channel`.

### Logging and Monitoring
- `/monitor set`, `/monitor remove`, `/monitor show` manage the audit log channel.
- `monitor-events` allows granular enablement of join, leave, ban, kick, unban, timeout, message edit/delete, and invite tracking.
- Additional developer logging helpers are provided in `modules/nsfw_scanner/logging_utils.py` and `modules/utils/log_channel.py`.

## Settings Reference

### NSFW Media and Text

| Setting | Type | Default | Notes | Plan |
| --- | --- | --- | --- | --- |
| `nsfw-enabled` | bool | `true` | Enables media scanning for attachments, reactions, and avatars. | All |
| `threshold` | float | `0.70` | Confidence threshold for media detections. | All |
| `nsfw-high-accuracy` | bool | `false` | Uses higher-accuracy classifiers for imagery. | Accelerated |
| `nsfw-channel` | TextChannel | — | Destination for NSFW violation embeds. | All |
| `nsfw-text-enabled` | bool | `false` | Turns on NSFW text scanning. | Accelerated |
| `nsfw-text-threshold` | float | `0.70` | Confidence threshold for text detections. | Accelerated |
| `nsfw-text-action` | list[str] | `["delete"]` | Enforcement actions for flagged text. | Accelerated |
| `nsfw-text-categories` | list[str] | `["violence_graphic", "sexual"]` | Extend with hate, harassment, minors, illicit categories as required. | Accelerated |
| `nsfw-text-excluded-channels` | list[TextChannel] | `[]` | Channels skipped by text scanning. | Accelerated |
| `nsfw-text-strikes-only` | bool | `false` | Restricts text scanning to members with active strikes. | Accelerated |
| `check-pfp` | bool | `false` | Scans profile images; recommended for Accelerated plans. | Accelerated |
| `nsfw-pfp-action` | list[str] | `["kick"]` | Actions when avatars are flagged. | Accelerated |
| `nsfw-verbose` | bool | `false` | Sends detailed scan reports to the origin channel. | All |

### AI Moderation

| Setting | Type | Default | Notes | Plan |
| --- | --- | --- | --- | --- |
| `autonomous-mod` | bool | `false` | Enables background AI moderation. | Accelerated |
| `aimod-mode` | str | `report` | `report`, `interval`, or `adaptive`. | Accelerated |
| `aimod-check-interval` | TimeString | `1h` | Interval for interval mode. | Accelerated |
| `aimod-detection-action` | list[str] | `["auto"]` | Actions applied to confirmed violations. | Accelerated |
| `aimod-high-accuracy` | bool | `false` | Switches to gpt-5-mini (Pro or higher). | Accelerated Pro+ |
| `aimod-adaptive-events` | dict | see schema | Event triggers for adaptive mode. | Accelerated |
| `aimod-debug` | bool | `false` | Forces verbose debug embeds. | All |

### FAQ

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `faq-enabled` | bool | `false` | Turns on automated FAQ responses. |
| `faq-threshold` | float | `0.72` | Similarity range 0.10–1.00; lower values match more aggressively. |

### Scam and Link Protection

| Setting | Type | Default | Notes | Plan |
| --- | --- | --- | --- | --- |
| `check-links` | bool | `false` | Enables external reputation lookups. | Accelerated |
| `scam-detection-action` | list[str] | `["delete"]` | Actions applied to confirmed scams. | All |
| `exclude-scam-channels` | list[TextChannel] | `[]` | Skips scam detection in selected channels. | All |

### Strike and Captcha

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `strike-expiry` | TimeString | — | Optional automatic expiry for strikes. |
| `strike-actions` | dict[str,list[str]] | `{ "1": ["timeout:1d"], "2": ["timeout:7d"], "3": ["ban"] }` | Configurable escalation ladder. |
| `cycle-strike-actions` | bool | `true` | Repeats the ladder when more strikes are issued. |
| `dm-on-strike` | bool | `true` | Sends strike notifications via DM. |
| `captcha-grace-period` | TimeString | `10m` | Time allowed to finish the captcha. |
| `captcha-max-attempts` | int | `3` | Attempts allowed before failure actions run. |
| `pre-captcha-roles` | list[Role] | `[]` | Temporary roles while verification is pending. |
| `captcha-success-actions` | list[str] | `[]` | Actions after a successful captcha. |
| `captcha-failure-actions` | list[str] | `[]` | Enforcement when verification fails. |
| `captcha-log-channel` | TextChannel | — | Audit channel for verification events. |

### Logging

| Setting | Type | Default | Notes |
| --- | --- | --- | --- |
| `monitor-channel` | TextChannel | — | Destination for moderation logs. |
| `monitor-events` | dict[str,bool] | see schema | Toggles per event type. |
| `no-forward-from-role` | list[Role] | `[]` | Roles excluded from forwarding commands. |
| `exclude-channels` | list[TextChannel] | `[]` | Channels excluded from global detectors. |

## FAQ Stream Integration

1. Provide a Redis URL via `FAQ_REDIS_URL` or `REDIS_URL`. The consumer starts automatically unless `FAQ_STREAM_ENABLED=false`.
2. Override stream parameters with `FAQ_COMMAND_STREAM`, `FAQ_RESPONSE_STREAM`, `FAQ_STREAM_GROUP`, `FAQ_STREAM_CONSUMER`, `FAQ_STREAM_BLOCK_MS`, `FAQ_STREAM_FETCH_COUNT`, and `FAQ_STREAM_RESPONSE_MAXLEN`.
3. Command payloads require `action`, `guild_id`, and action-specific fields (`question`/`answer` for add, `entry_id` for delete).
4. Responses include `status`, `action`, `guild_id`, optional `entry_id`, serialized `entries`, and `error` when a request fails. Error responses are terminal; correct the payload before resending.

## Worker Queue Operations

Aggregated moderation workers are configured through `cogs/aggregated_moderation/config.py`. Key environment variables:

| Variable | Purpose | Default |
| --- | --- | --- |
| `FREE_MAX_WORKERS` | Baseline workers for the free queue | `2` |
| `ACCELERATED_MAX_WORKERS` | Baseline workers for the accelerated queue | `5` |
| `FREE_MAX_WORKERS_BURST` | Autoscale ceiling for the free queue | `5` |
| `ACCELERATED_MAX_WORKERS_BURST` | Autoscale ceiling for the accelerated queue | `10` |
| `FREE_WORKER_BACKLOG_HIGH` | Backlog threshold to scale up free workers | `100` |
| `ACCELERATED_WORKER_BACKLOG_HIGH` | Backlog threshold to scale up accelerated workers | `30` |
| `WORKER_BACKLOG_LOW` | Backlog level to scale down | `5` |
| `WORKER_AUTOSCALE_CHECK_INTERVAL` | Autoscale evaluation cadence (seconds) | `2` |
| `WORKER_AUTOSCALE_SCALE_DOWN_GRACE` | Cooldown before scaling down (seconds) | `15` |

---

Use the slash commands for day-to-day administration. Persistent settings are stored through the guild settings API in `modules/utils/mysql`. Tests for the NSFW text pipeline live in `tests/test_scanner_text.py` and should be run when adjusting thresholds or categories.
