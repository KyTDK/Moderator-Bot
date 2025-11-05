# FAQ Integration Guide

This document describes the public interface for managing FAQ entries from external services (for example, the dashboard). The bot listens for FAQ commands on Redis and exposes two guild-level settings that gate behaviour.

---

## Settings

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `faq-enabled` | boolean | `false` | Enables automatic FAQ responses for the guild. When `false`, messages are ignored. |
| `faq-threshold` | float | `0.72` | Minimum similarity score (0.10–1.00) required before a stored FAQ answer is considered a match. Values outside the range are rejected. |

Settings are stored through the standard guild settings API (`modules/utils/mysql`). Update them the same way you manage other feature toggles.

---

## Redis Command Stream

The bot consumes FAQ commands via Redis using `modules.faq.stream.FAQStreamProcessor` (built on the shared `RedisStreamConsumer`). Configure the integration with the following environment variables:

| Variable | Description | Default |
| --- | --- | --- |
| `FAQ_STREAM_ENABLED` | Enables the consumer (`true`/`false`). When unset, the processor starts automatically if a Redis URL is available. | — |
| `FAQ_REDIS_URL` | Redis connection string (falls back to `REDIS_URL`). | — |
| `FAQ_COMMAND_STREAM` | Stream for inbound commands. | `faq:commands` |
| `FAQ_RESPONSE_STREAM` | Stream for responses. | `faq:responses` |
| `FAQ_STREAM_GROUP` | Consumer group name. | `modbot-faq` |
| `FAQ_STREAM_CONSUMER` | Optional explicit consumer identifier; auto-generated when omitted. | hostname/PID/UUID |
| `FAQ_STREAM_BLOCK_MS` | Poll timeout (milliseconds). | `10000` |
| `FAQ_STREAM_FETCH_COUNT` | Maximum messages fetched per poll. | `20` |
| `FAQ_STREAM_RESPONSE_MAXLEN` | Approximate maximum length of the response stream (`XADD ... MAXLEN`). | `1000` |

### Command Payload

Publish command entries to `FAQ_COMMAND_STREAM` with the following fields (all values should be strings):

```
action      # Required: "add", "delete", or "list"
request_id  # Optional correlation id echoed in the response
guild_id    # Required Discord guild id
question    # Required when action == add
answer      # Required when action == add
entry_id    # Required when action == delete
```

### Response Payload

After processing, the bot appends a single entry to `FAQ_RESPONSE_STREAM` containing:

```
{
  "request_id": "...",      # Mirrors the incoming request_id (may be empty)
  "status": "ok" | "error",
  "action": "add" | "delete" | "list",
  "guild_id": "...",
  "entry_id": "...",        # Present for add/delete success
  "entries": "[...]",        # JSON array (string) when action == list
  "error": "..."             # Present only when status == error
}
```

Consumers should read from the response stream promptly; the processor acknowledges and deletes command entries once handled.

---

## Operational Notes

- The Redis consumer is built on `modules.utils.redis_stream.RedisStreamConsumer`, so other features can reuse the same helper.
- The service enforces plan limits and validation internally; error messages are surfaced through the response stream (`status == "error"`).
- Requests should be idempotent where possible (e.g., use the `entry_id` supplied in successful `add` responses when deleting).
