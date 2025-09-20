# 🛡️ Moderator Bot

Free AI-powered moderation for Discord. Detects nudity, gore, scams, and other violations in messages, media, and avatars. Fully configurable via slash commands.

---

## 📌 Features Overview

* **Anti-NSFW**
  Detects explicit or harmful content in uploaded media (images, GIFs, Lottie, APNG), Tenor GIFs, and user avatars. Categories include nudity, sexual content, graphic violence, gore, and self-harm-related material.

* **Strikes**
  Escalating punishment system with custom durations, action cycling, and optional DM notifications. Fully configurable.

* **🤖 AI Moderation** *(Accelerated only)*
  Uses OpenAI models to moderate messages based on rules. Supports autonomous mode, batch scanning, and context-aware enforcement.
  **Default: AI moderation runs in `report` mode when users @mention the bot.**


* **Voice Chat Moderation** *(Accelerated only)*
  Transcribes and scans live voice channels for violations (e.g., hate speech, self-harm encouragement, harassment). Supports configurable actions and monthly budget controls.

* **Scam Detection**
  Detects scam/phishing messages using patterns, Google Safe Browsing, PhishTank, and link unshortening.

* **Banned Words**
  Blocks slurs or custom words. Supports layered punishment and integrates with strikes.

* **Logging**
  Tracks joins, leaves, bans, deletions, edits, timeouts, and invite usage.

* **Custom Settings**
  Slash-command-driven configuration for rules, thresholds, exclusions, and more.

* **Private API Pool**
  Users can contribute OpenAI keys (encrypted) to a shared pool to increase moderation capacity. Guild-level keys are not used.

---

## 🔺 Strike System

Automatically issues escalating punishments based on a user’s strike count:

* 1st Strike → 1d timeout
* 2nd Strike → 7d timeout
* 3rd Strike → Ban

### ⚙️ Commands:

* `/strikes strike` – Manually give a strike to a user
* `/strikes get` – View a user's current strikes
* `/strikes clear` – Remove all active strikes from a user
* `/strikes remove` – Remove a specific strike by ID
* `/strikes add_action` – Add an action for a strike level
* `/strikes remove_action` – Remove a strike action
* `/strikes view_actions` – View configured strike actions
* `/intimidate` – Issue a serious warning (DM or channel)

---

## 🤖 AI Moderation

Uses OpenAI to detect violations in user messages.

Budget: AI moderation is capped at $2 per billing cycle. Pricing is $0.45 per 1M tokens; once the cycle budget is reached, autonomous moderation pauses until the next cycle.
Note: Enable higher-accuracy AI moderation with gpt-5-mini (approx. 2.25 USD per 1M tokens) via the `aimod-high-accuracy` setting. This consumes the monthly budget faster than the default gpt-5-nano (0.45 USD per 1M tokens).


## 🎙️ Voice Chat Moderation

Real-time moderation for Discord voice channels. Audio is transcribed, analyzed by AI, and checked against your configured rules.

### 🔧 Configuration:

* `/ai_mod rules_set` – Define custom server rules
* `/ai_mod set_mode` – Choose between `report` or `interval` scanning
* `/ai_mod toggle` – Enable/disable autonomous moderation
* `/ai_mod add_adaptive_event` – Add adaptive triggers
* `/ai_mod remove_adaptive_event` – Remove adaptive triggers

### ⚙️ Actions:

* `/ai_mod add_action` – Define what happens on violations
* `/ai_mod remove_action` – Remove an AI action
* `/ai_mod view_actions` – Show all AI-triggered actions
* `/ai_mod clear_adaptive_events` – Clear all adaptive triggers
* `/ai_mod view_adaptive_events` – List active adaptive triggers

---

## 🖼 NSFW Filtering

Detects nudity, graphic violence, and explicit content in:

* Uploaded media (images, GIFs, stickers, emojis, videos)
* Tenor GIFs
* Profile pictures
* Lottie / APNG animations

### 🔧 Configuration:

* `/nsfw set_threshold` – Set detection confidence
* `/nsfw add_category` – Add custom categories to detect
* `/nsfw add_action` – Action to take when NSFW is detected
* `/nsfw remove_action` – Remove NSFW actions

### 📜 Inspection:

* `/nsfw view_actions` – View active actions
* `/nsfw view_threshold` – Check the current threshold

---

## ❌ Scam & Link Protection

Detects and removes scam/phishing messages using:

* Pattern and URL matching
* Google Safe Browsing & PhishTank integration
* Smart link unshortening with scam checks

### 🔧 Configuration:

* `/scam check_links` – Enable or disable link safety checks
* `/scam exclude_channel_add` – Exclude a channel from scam detection
* `/scam exclude_channel_remove` – Remove a channel from the exclusion list
* `/scam view` – Show current scam detection settings

### ⚙️ Actions:

* `/scam add_action` – Add moderation actions (e.g., `timeout`, `ban`)
* `/scam remove_action` – Remove configured actions
* `/scam settings` – Manage detection settings

### 📜 Logs & Lists:

* `/scam exclude_channel_list` – View excluded channels

---

## 💬 Banned Words System

Blocks slurs and custom word lists.

### 🔧 Configuration:

* `/bannedwords add` – Add a custom banned word
* `/bannedwords remove` – Remove a word from the list
* `/bannedwords defaults` – Enable default slur list
* `/bannedwords clear` – Clear all custom banned words
* `/bannedwords add_action` – Set action when banned words are triggered
* `/bannedwords remove_action` – Remove a word action

### 📜 Inspection:

* `/bannedwords view_actions` – View all punishment actions

---

## 📊 Monitoring & Logging

Tracks and logs key server events:

* Joins, leaves, bans, kicks
* Edits, deletions, timeouts
* Message deletions and audit logs
* Invite usage tracking

### 🔧 Configuration:

* `/monitor set` – Set log output channel
* `/monitor remove` – Disable monitoring
* `/monitor show` – View current log channel

---

## ⚙️ Settings Snapshot

| Name                    | Type               | Description                         |
| ----------------------- | ------------------ | ----------------------------------- |
| `strike-expiry`         | TimeString         | Duration before strikes expire      |
| `cycle-strike-actions`  | bool               | Loop fallback strike actions        |
| `dm-on-strike`          | bool               | DM users when they receive a strike |
| `check-pfp`             | bool               | Scan avatars for NSFW               |
| `nsfw-pfp-action`       | list\[str]         | Action on NSFW avatars              |
| `nsfw-pfp-message`      | str                | Message on NSFW avatar detection    |
| `unmute-on-safe-pfp`    | bool               | Auto-unmute on safe avatar change   |
| `check-tenor-gifs`      | bool               | Scan Tenor GIFs for NSFW            |
| `nsfw-high-accuracy`    | bool (Accelerated) | High-accuracy NSFW scans            |
| `banned-words-action`   | list\[str]         | Action on banned words              |
| `exclude-channels`      | list\[TextChannel] | Channels excluded from checks       |
| `scam-detection-action` | list\[str]         | Actions for scam messages           |
| `check-links`           | bool               | Enable URL safety checks            |
| `exclude-scam-channels` | list\[TextChannel] | Skip scam checks in these channels  |
| `aimod-check-interval`  | TimeString         | How often to run AI moderation      |
| `no-forward-from-role`  | list\[Role]        | Roles that can't forward messages   |

---
## Shard Coordination

Moderator Bot coordinates shard ownership in MySQL so multiple instances do not overlap. Each process:

- Ensures placeholder rows exist in the `bot_shards` table for the configured shard range.
- Claims the next available shard (or a preferred shard) and records the runner instance ID.
- Sends heartbeats while connected so stale processes can be recycled automatically.
- Releases the shard slot on shutdown so other runners can take over quickly.

### Configuration

Set the following environment variables on every worker:

- `MODBOT_TOTAL_SHARDS`: Total number of gateway shards you plan to run (minimum 1).
- `MODBOT_PREFERRED_SHARD`: Optional numeric hint if you want a process to request a specific shard.
- `MODBOT_SHARD_STALE_SECONDS`: How long (in seconds) before an unresponsive shard is recycled. Defaults to 300.
- `MODBOT_SHARD_HEARTBEAT_SECONDS`: Heartbeat frequency in seconds. Defaults to 60.
- `MODBOT_INSTANCE_ID`: Optional stable identifier for the process. If omitted, a hostname/PID/UUID combo is generated automatically.
- `MODBOT_STANDBY_WHEN_FULL`: Set to `0` to exit instead of waiting when all shards are busy. Defaults to standby mode.
- `MODBOT_STANDBY_POLL_SECONDS`: How often to retry claiming a shard while standing by. Defaults to 30 seconds.

Shard rows and the required table are created automatically on startup; no manual migrations are needed.
