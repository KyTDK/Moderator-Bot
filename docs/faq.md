# FAQ Integration Guide

This document describes the public interface for managing FAQ entries from external services (for example, the dashboard). The bot listens for FAQ commands on Redis and exposes a handful of guild-level settings that gate behaviour.

---

## Settings

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `faq-enabled` | boolean | `false` | Enables automatic FAQ responses for the guild. When `false`, messages are ignored. |
| `faq-threshold` | float | `0.72` | Minimum similarity score (0.10–1.00) required before a stored FAQ answer is considered a match. Values outside the range are rejected. |
| `faq-direct-reply` | boolean | `false` | (Accelerated only) Reply directly to matching messages with the stored FAQ answer instead of sending an embed. |

Settings are stored through the standard guild settings API (`modules/utils/mysql`). Update them the same way you manage other feature toggles.

Guild moderators can also toggle the feature directly inside Discord with the `/faq enable` command (Manage Messages permission required). Pass `enabled: true` to turn responses on, or `enabled: false` to disable them.

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

### Error Handling

Treat every `status == "error"` response as final; the command has already been acknowledged and removed from the request stream. Surface the error to the caller and only retry after correcting the underlying issue.

Known error shapes:

- Limit reached – adding an entry after the plan allotment is exhausted raises a `FAQLimitError`, surfaced as `error: "FAQ limit reached (<limit> entries for plan <plan>)"`. Inform the user that they must delete an existing FAQ or upgrade their plan before retrying.
- Entry missing – deleting a non-existent entry raises `FAQEntryNotFoundError`, surfaced as `error: "FAQ entry <id> not found"`. Drop the entry from your local state so subsequent deletes do not repeat the error.
- Validation failures – missing required fields (e.g., `guild_id`, `action`, `entry_id`, `question`, `answer`) return short error strings describing the missing field. Fix the payload and requeue if needed.
- Unexpected failures – any uncaught exception is stringified into `error`. Log these for follow-up; retries may succeed once the underlying service issue is resolved.

---

## Operational Notes

- The Redis consumer is built on `modules.utils.redis_stream.RedisStreamConsumer`, so other features can reuse the same helper.
- The service enforces plan limits and validation internally; error messages are surfaced through the response stream (`status == "error"`).
- Requests should be idempotent where possible (e.g., use the `entry_id` supplied in successful `add` responses when deleting).
