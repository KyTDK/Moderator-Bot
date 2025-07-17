# 🛡️ Moderator Bot

Free AI-powered moderation for Discord. Detects nudity, gore, scams, and other violations in messages, media, and avatars. Fully configurable via slash commands.

---

## 📌 Features Overview

- **NSFW Filtering**  
  Detects explicit or harmful content in uploaded media (images, GIFs, Lottie, APNG), Tenor GIFs, and user avatars. Categories include nudity, sexual content, graphic violence, gore, and self-harm-related material.

- **Strikes**  
  Escalating punishment system with custom durations, action cycling, and optional DM notifications. Fully configurable.

- **AI Moderation**  
  Uses OpenAI models to moderate messages based on rules. Supports autonomous mode, batch scanning, and context-aware enforcement.  
  **Default: AI moderation runs in `report` mode when users @mention the bot.**

- **Scam Detection**  
  Detects scam/phishing messages using patterns, Google Safe Browsing, PhishTank, and link unshortening.

- **Banned Words**  
  Blocks slurs or custom words. Supports layered punishment and integrates with strikes.

- **Logging**  
  Tracks joins, leaves, bans, deletions, edits, timeouts, and invite usage.

- **Custom Settings**  
  Slash-command-driven configuration for rules, thresholds, exclusions, models, and more.

- **Private API Pool**  
  Users can contribute OpenAI keys (encrypted) to increase moderation capacity.

---

## 🔺 Strike System

Auto-escalation example:

- 1st Strike → 1d timeout  
- 2nd Strike → 7d timeout  
- 3rd Strike → Ban

🧠 Customizable with:

- `/strikes add_action`  
- `/settings strike-expiry`  
- `cycle-strike-actions`  
- `dm-on-strike`  
- Logs to: `strike-channel`

---

## 🤖 AI Moderation (Batch & Autonomous)

OpenAI-powered moderation based on rules.

- Triggered by `@mention` (default) or time interval
- Autonomous moderation: `autonomous-mod`
- Context-aware: `contextual-ai`
- Custom rules: `/ai_mod rules_set`
- Custom actions: `aimod-detection-action`

### Settings:
- `rules`: Rule definitions
- `aimod-model`: OpenAI model (e.g., `gpt-4o`)
- `autonomous-mod`: Enable AI auto-mod

### Commands:
- `/ai_mod toggle`
- `/ai_mod add_action`
- `/ai_mod view_actions`

---

## 🖼 NSFW Filtering

Detects NSFW in:

- Uploaded media (images, GIFs, stickers)
- Tenor GIFs (`check-tenor-gifs`)
- Profile pictures (`check-pfp`)
- Lottie/APNG animations

### Categories:
- `nsfw-detection-categories` (e.g., `sexual`, `violence_graphic`)
- `threshold`: Confidence threshold

### Actions:
- `nsfw-detection-action`
- `nsfw-pfp-action`
- `nsfw-pfp-message`
- `unmute-on-safe-pfp`

### Commands:
- `/nsfw add_action`
- `/nsfw add_category`
- `/nsfw view_actions`
- `/nsfw set_threshold`
- `/nsfw view_threshold`

---

## ❌ Scam & Link Protection

Scam prevention with:

- Pattern/URL matching
- Google Safe Browsing & PhishTank
- Link unshortening

### Actions:
- `scam-detection-action`
- `delete-scam-messages`

### Toggles:
- `check-links`
- `exclude-scam-channels`

### Commands:
- `/scam settings`
- `/scam view`
- `/scam list_patterns`
- `/scam list_urls`

---

## 💬 Banned Words System

- Built-in slur list or custom words
- Action: `banned-words-action`
- Logging: `monitor-channel`

### Commands:
- `/bannedwords add`
- `/bannedwords remove`
- `/bannedwords add_action`
- `/bannedwords view_actions`
- `/bannedwords clear`

---

## 📊 Monitoring & Logging

Logs:

- Joins, leaves, edits, deletions, bans, kicks, timeouts
- Tracks deleted messages (even uncached)
- Invite tracking

Output: `monitor-channel`  
Command: `/monitor set`

---

## ⚙️ Settings Snapshot

| Name                        | Type               | Description                         |
| --------------------------- | ------------------ | ----------------------------------- |
| `strike-channel`            | TextChannel        | Logs strikes                        |
| `nsfw-channel`              | TextChannel        | Logs NSFW previews                  |
| `monitor-channel`           | TextChannel        | Logs general events                 |
| `aimod-channel`             | TextChannel        | Logs AI violation results           |
| `api-key`                   | str (encrypted)    | OpenAI key for AI/NSFW moderation   |
| `strike-expiry`             | TimeString         | Duration before strikes expire      |
| `cycle-strike-actions`      | bool               | Loop fallback strike actions        |
| `dm-on-strike`              | bool               | DM users when they receive a strike |
| `strike-actions`            | dict               | Action mapping per strike level     |
| `check-pfp`                 | bool               | Scan avatars for NSFW               |
| `nsfw-detection-categories` | list\[str]         | Which NSFW categories to detect     |
| `threshold`                 | float              | Detection sensitivity               |
| `nsfw-pfp-action`           | list\[str]         | Action on NSFW avatars              |
| `nsfw-pfp-message`          | str                | Message on NSFW avatar detection    |
| `unmute-on-safe-pfp`        | bool               | Auto-unmute on safe avatar change   |
| `check-tenor-gifs`          | bool               | Scan Tenor GIFs for NSFW            |
| `use-default-banned-words`  | bool               | Use built-in slur list              |
| `banned-words-action`       | list\[str]         | Action on banned words              |
| `exclude-channels`          | list\[TextChannel] | Channels excluded from checks       |
| `delete-scam-messages`      | bool               | Auto-delete scam messages           |
| `scam-detection-action`     | list\[str]         | Actions for scam messages           |
| `check-links`               | bool               | Enable URL safety checks            |
| `exclude-scam-channels`     | list\[TextChannel] | Skip scam checks in these channels  |
| `rules`                     | str                | Server rules used by AI moderation  |
| `aimod-detection-action`    | list\[str]         | Action when AI flags content        |
| `autonomous-mod`            | bool               | Enable autonomous AI moderation     |
| `aimod-model`               | str                | Model used for AI mod               |
| `aimod-check-interval`      | TimeString         | How often to run AI moderation      |
| `contextual-ai`             | bool               | Enable context-aware moderation     |
| `aimod-mode`                | str                | `report` or `interval` mode         |
| `no-forward-from-role`      | list\[Role]        | Roles that can't forward messages   |

---

## 💬 Command Index

### General
- `/help`
- `/settings`
- `/api_pool`
- `/monitor`
- `/channels`

### Strikes
- `/strikes get`
- `/strikes remove`
- `/strikes clear`
- `/strikes add_action`
- `/strikes remove_action`
- `/strikes view_actions`

### NSFW
- `/nsfw add_action`
- `/nsfw remove_action`
- `/nsfw add_category`
- `/nsfw view_actions`
- `/nsfw set_threshold`
- `/nsfw view_threshold`

### AI Mod
- `/ai_mod toggle`
- `/ai_mod rules_set`
- `/ai_mod view_actions`
- `/ai_mod add_action`
- `/ai_mod remove_action`

### Banned Words
- `/bannedwords add`
- `/bannedwords remove`
- `/bannedwords defaults`
- `/bannedwords add_action`
- `/bannedwords view_actions`

### Scam
- `/scam settings`
- `/scam check_links`
- `/scam add_message`
- `/scam add_url`
- `/scam list_patterns`
- `/scam list_urls`
