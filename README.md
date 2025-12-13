# 🛡️ Moderator Bot

Free AI-powered moderation for Discord. Detects nudity, gore, scams, and other violations in messages, media, and avatars. Fully configurable via slash commands.

Learn more at https://modbot.neomechanical.com.

---

## 📌 Features Overview

* **Anti-NSFW**
  Detects explicit or harmful content in uploaded media (images, GIFs, Lottie, APNG), Tenor GIFs, and user avatars. Categories include nudity, sexual content, graphic violence, gore, and self-harm-related material.

* **Strikes**
  Escalating punishment system with custom durations, action cycling, and optional DM notifications. Fully configurable.

* **Captcha Verification**
  Require newcomers to pass a captcha challenge before gaining access. Customize grace periods, attempt limits, pre-access roles, success/failure actions, and logging.

* **🤖 AI Moderation** *(Accelerated only)*
  Uses OpenAI models to moderate messages based on rules. Supports autonomous mode, batch scanning, and context-aware enforcement.
  **Default: AI moderation runs in `report` mode when users @mention the bot.**


* **Voice Chat Moderation** *(Accelerated only)*
  Transcribes and scans live voice channels for violations (e.g., hate speech, self-harm encouragement, harassment). Supports configurable actions and monthly budget controls.

* **Scam Detection**
  Detects scam/phishing messages using patterns, Google Safe Browsing, PhishTank, and link unshortening.

* **Banned Words**
  Blocks slurs or custom words. Supports layered punishment and integrates with strikes.

* **FAQ Auto Replies**
  Instantly answer recurring questions with saved responses, with optional direct replies for Accelerated servers.

* **Banned URL Enforcement**
  Catch blocked domains or exact links and trigger custom actions the moment they appear.

* **Web Dashboard**
  Manage settings, FAQ entries, and Accelerated subscriptions from https://modbot.neomechanical.com.

* **Logging**
  Tracks joins, leaves, bans, deletions, edits, timeouts, and invite usage.

* **Custom Settings**
  Slash-command-driven configuration for rules, thresholds, exclusions, and more.

* **Private API Pool**
  Users can contribute OpenAI keys (encrypted) to a shared pool to increase moderation capacity. Guild-level keys are not used.

---

## 🧩 Captcha Verification

Require new members to complete a captcha before they can see or interact with the rest of your server. Moderator Bot DMs the verification link (with a public channel fallback) and automatically cleans up pending sessions when members leave. On success, it can grant roles or run other onboarding actions; on failure, it enforces your chosen punishments and logs the outcome.

### 🔧 Configuration:

* `/settings set name=captcha-verification-enabled value=true` – Turn captcha verification on or off.
* `/settings set name=captcha-grace-period value=10m` – Control how long newcomers have to finish the captcha.
* `/settings set name=captcha-max-attempts value=3` – Limit how many attempts a user gets before failure actions trigger.
* `/settings set name=pre-captcha-roles role=@Visitor` – Assign temporary roles while members are awaiting verification.
* `/settings set name=captcha-success-actions value="give_role:Member"` – Run success actions (e.g., grant roles, clear timeouts) after completion.
* `/settings set name=captcha-failure-actions value="timeout:1d"` – Apply disciplinary actions when the captcha is failed.
* `/settings set name=captcha-delivery-method value=embed` – Deliver captcha prompts via DM or a persistent embed in your server.
* `/settings set name=captcha-embed-channel-id channel=#verify-here` – Choose the channel that hosts the verification embed.
* `/settings set name=vpn-detection-enabled value=true` – Screen newcomers for VPNs and proxies before allowing access.
* `/settings set name=vpn-pre-actions value="remove_role:Visitor"` – Adjust roles before VPN screening; pair with `vpn-post-actions` to grant roles after a clean result.
* `/settings set name=vpn-detection-actions value="kick"` – Decide what happens when VPN screening fails (e.g., kick, timeout).

### 📜 Logging:

* `/channels set type=Captcha channel=#mod-logs` – Choose where captcha pass/fail embeds are posted.
* `/settings set name=captcha-log-channel channel=#captcha-logs` – Send detailed verification events to a dedicated channel.

### 🛡️ Verification Extras:

* `/verification sync` – Refresh the verification embed after changing its design or settings.
* `/verification request member=@User` – Manually trigger the captcha & VPN flow for a specific member.

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
* `/settings set name=exclude-bannedwords-channels value="#memes"` – Exclude light-hearted channels from filtering.
* `/settings set name=use-default-banned-words value=true` – Pair your custom list with the built-in profanity filter.

### 📜 Inspection:

* `/bannedwords view_actions` – View all punishment actions

---

## ❓ FAQ Auto Replies

Answer repeat questions automatically. Moderator Bot listens for incoming messages, finds the best match, and responds with your saved answer.

### 🔧 Configuration:

* `/faq enable enabled:true` – Turn automatic FAQ replies on (or set `false` to pause them).
* `/settings set name=faq-threshold value=0.72` – Adjust how closely a message must match before the bot replies.
* `/settings set name=faq-direct-reply value=true` – (Accelerated) Respond directly in the channel instead of posting an embed.

### ✏️ Commands:

* `/faq add question:"How do I appeal?" answer:"Appeal instructions..."` – Save a new entry.
* `/faq remove entry_id:123` – Delete a stored answer.
* `/faq list` – Review your current FAQ catalogue.

---

## 🔗 Banned URL Enforcement

Block specific domains or exact links and apply custom punishments when they appear.

### 🔧 Configuration:

* `/bannedurls add url:badsite.com` – Add a domain or full URL to the blocked list.
* `/bannedurls remove url:badsite.com` – Unblock a link.
* `/bannedurls list` – Export the current list as a file.
* `/bannedurls clear` – Wipe all blocked URLs.
* `/bannedurls add_action action:timeout duration:1d` – Decide what happens when a banned link is posted.
* `/settings set name=exclude-url-channels value="#partnerships"` – Allow specific channels to bypass URL blocking.

### 📜 Inspection:

* `/bannedurls view_actions` – Review the enforcement actions currently configured.

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

## 🖥️ Web Dashboard & Self-Service

Access https://modbot.neomechanical.com to review moderation stats, tune settings, and manage your Accelerated subscription.

### 🔧 Quick Actions:

* `/dashboard` – Receive a private link to your server’s dashboard.
* `/accelerated status` – Check your current Accelerated tier and renewal date.
* `/accelerated subscribe` – Generate an upgrade link for your guild.
* `/accelerated cancel` – Get instructions for cancelling your subscription.

---

## ⚙️ Settings Snapshot

| Name                               | Type                  | Description                                         |
| ---------------------------------- | --------------------- | --------------------------------------------------- |
| `strike-channel`                   | TextChannel           | Channel where strike embeds are posted              |
| `strike-expiry`                    | TimeString            | Duration before strikes expire                      |
| `cycle-strike-actions`             | bool                  | Loop fallback strike actions                        |
| `strike-actions`                   | dict\[str, list\[str]] | Custom actions for each strike level                |
| `dm-on-strike`                     | bool                  | DM users when they receive a strike                 |
| `use-default-banned-words`         | bool                  | Enable the built-in profanity list                  |
| `banned-words-action`              | list\[str]            | Action on banned words                              |
| `exclude-bannedwords-channels`     | list\[TextChannel]    | Channels excluded from banned words scanning        |
| `check-pfp`                        | bool                  | Scan avatars for NSFW                               |
| `nsfw-pfp-action`                  | list\[str]            | Action on NSFW avatars                              |
| `nsfw-pfp-message`                 | str                   | Message on NSFW avatar detection                    |
| `unmute-on-safe-pfp`               | bool                  | Auto-unmute on safe avatar change                   |
| `check-tenor-gifs`                 | bool                  | Scan Tenor GIFs for NSFW                            |
| `nsfw-high-accuracy`               | bool (Accelerated)    | High-accuracy NSFW scans                            |
| `exclude-channels`                 | list\[TextChannel]    | Channels excluded from checks                       |
| `monitor-channel`                  | TextChannel           | All-purpose moderation log channel                  |
| `monitor-events`                   | dict\[str, bool]      | Toggle individual logging events                    |
| `scam-detection-action`            | list\[str]            | Actions for scam messages                           |
| `delete-scam-messages`             | bool                  | Auto-delete detected scam content                   |
| `check-links`                      | bool                  | Enable URL safety checks                            |
| `exclude-scam-channels`            | list\[TextChannel]    | Skip scam checks in these channels                  |
| `banned-urls`                      | list\[str]            | Custom list of blocked links                        |
| `url-detection-action`             | list\[str]            | Actions when banned URLs appear                     |
| `exclude-url-channels`             | list\[TextChannel]    | Channels that bypass URL blocking                   |
| `aimod-channel`                    | TextChannel           | Where AI violation logs are posted                  |
| `aimod-debug`                      | bool                  | Always share detailed AI moderation context         |
| `aimod-check-interval`             | TimeString            | How often to run AI moderation                      |
| `aimod-mode`                       | str                   | Choose `report`, `interval`, or `adaptive` scanning |
| `aimod-adaptive-events`            | dict\[str, list\[str]] | Events that shift AI moderation modes               |
| `aimod-detection-action`           | list\[str]            | Action when AI moderation flags content             |
| `aimod-high-accuracy`              | bool (Accelerated)    | Use gpt-5-mini for tighter moderation               |
| `autonomous-mod`                   | bool (Accelerated)    | Let AI enforce rules automatically                  |
| `no-forward-from-role`             | list\[Role]           | Roles that can't forward messages                   |
| `faq-enabled`                      | bool                  | Toggle automatic FAQ replies                        |
| `faq-threshold`                    | float                 | Match score required before replying                |
| `faq-direct-reply`                 | bool (Accelerated)    | Reply directly instead of using embeds              |
| `captcha-log-channel`              | TextChannel           | Verification log channel                            |
| `captcha-delivery-method`          | str                   | Choose DM or embed delivery for captcha             |
| `captcha-embed-channel-id`         | TextChannel           | Channel hosting the captcha embed                   |
| `pre-captcha-roles`                | list\[Role]           | Roles assigned before verification                  |
| `captcha-success-actions`          | list\[str]            | Actions after passing verification                  |
| `captcha-failure-actions`          | list\[str]            | Actions after failing verification                  |
| `vpn-detection-enabled`            | bool                  | Screen newcomers for VPN usage                      |
| `vpn-detection-actions`            | list\[str]            | Actions when VPN screening fails                    |
| `vpn-pre-actions`                  | list\[str]            | Role adjustments before VPN screening               |
| `vpn-post-actions`                 | list\[str]            | Role adjustments after VPN screening succeeds       |
| `vcmod-enabled`                    | bool (Accelerated)    | Enable voice moderation                             |
| `vcmod-channels`                   | list\[VoiceChannel \| StageChannel] | Voice/stage channels monitored by the bot           |
| `vcmod-categories`                 | list\[CategoryChannel] | Voice categories whose channels are cycled          |
| `vcmod-listen-duration`            | TimeString            | Active listening window per channel                 |
| `vcmod-idle-duration`              | TimeString            | Pause between transcripts in saver mode             |
| `vcmod-saver-mode`                 | bool                  | Lightweight cycling without constant recording      |
| `vcmod-rules`                      | str                   | Custom rules for voice chat moderation              |
| `vcmod-detection-action`           | list\[str]            | Response when voice moderation flags audio          |
| `vcmod-high-accuracy`              | bool (Accelerated)    | Higher-accuracy voice analysis                      |
| `vcmod-high-quality-transcription` | bool (Accelerated)    | Premium transcription quality                       |
| `vcmod-transcript-channel`         | TextChannel           | Where transcripts are posted                        |
| `vcmod-transcript-only`            | bool                  | Transcribe voice chat without enforcing rules       |
| `vcmod-join-announcement`          | bool                  | Play TTS when the bot joins a voice channel         |

---
