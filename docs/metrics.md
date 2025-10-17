## Metrics Storage Overview

Moderator Bot now records moderation activity in Redis, pairing a realtime stream with durable rollups that replace the old MySQL tables. Every scan generates a stream event for live dashboards and updates Redis hashes that keep the aggregate counts lightweight and queryable.

### Redis Layout

- **Stream events** live in the Redis stream defined by `METRICS_REDIS_STREAM` (default `moderator:metrics`). Each entry contains the full scan payload (`occurred_at`, guild/channel/message IDs, status, flags, file metadata, etc.) so consumers can fan out updates as they happen.
- **Daily rollups** are stored under keys that follow `"{prefix}:rollup:{YYYY-MM-DD}:{guild_id}:{content_type}"` where `prefix` is `METRICS_REDIS_PREFIX` (default `moderator:metrics`). The hash tracks counters (`scans_count`, `flagged_count`, `flags_sum`, `total_bytes`, `total_duration_ms`, `last_duration_ms`) and snapshot fields (`last_status`, `last_reference`, `last_flagged_at`, `last_details`). Status histograms are kept in a sibling hash that appends `:status` to the rollup key.
- **Global totals** sit in the hash `"{prefix}:totals"`, mirroring the old single-row table. Status counts are kept in `"{prefix}:totals:status"`. `updated_at` reflects the last time any metric changed.
- **Indexes** use sorted sets so lookups stay efficient: `"{prefix}:rollups:index"` contains every rollup key scored by `date.toordinal()`, and `"{prefix}:rollups:index:guild:{guild_id}"` scopes the index for guild-specific queries.

### Writing Metrics

`modules/metrics/tracker.log_media_scan` remains the public entry point. It builds a normalised payload and delegates to `modules.metrics.backend.accumulate_media_metric`, which:

1. Emits the raw scan to the Redis stream (respecting optional `METRICS_REDIS_STREAM_MAXLEN` bounds).
2. Increments the relevant rollup hash, updating status snapshots and status-count hashes as needed.
3. Applies the same increments to the global totals hash.

Because Redis operations are idempotent increments and hash updates, the write path stays low-latency even at high scan volumes.

### Reading Metrics

- `modules.metrics.get_media_metric_rollups()` walks the sorted-set indexes to collect the latest rollups, rehydrating JSON payloads and recomputing `average_latency_ms` on the fly.
- `modules.metrics.get_media_metrics_summary()` aggregates rollups by `content_type`, preserving the behaviour of the previous SQL aggregation.
- `modules.metrics.get_media_metrics_totals()` fetches the global snapshot and returns the same structure that the bot previously served from MySQL.

All helpers return native Python types (UTC-aware datetimes, integers, decoded JSON dicts) so callers don’t need to manipulate Redis-specific representations.

### Configuration

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `METRICS_REDIS_URL` | Connection string for Redis (falls back to `REDIS_URL`). | _required for live usage_ |
| `METRICS_REDIS_STREAM` | Redis stream name used for realtime fan-out. | `moderator:metrics` |
| `METRICS_REDIS_STREAM_MAXLEN` | Optional trimming limit for the stream (`XADD MAXLEN`). | disabled |
| `METRICS_REDIS_STREAM_APPROX` | Whether to use Redis’ approximate trimming (`~`). | `true` |
| `METRICS_REDIS_PREFIX` | Prefix for rollup/totals keys and indexes. | `moderator:metrics` |

If `METRICS_REDIS_URL` is not provided the backend stays inactive, but tests can still inject a fake client via `modules.metrics.backend.set_client_override`.

### Migrating Legacy Data

A one-off migration script ships alongside the codebase:

```bash
python scripts/migrate_metrics_to_redis.py
```

The script:

1. Reads every row from `moderation_metric_rollups` and `moderation_metric_totals`.
2. Imports those aggregates into the new Redis hashes (preserving timestamps, status histograms, and last-detail payloads).
3. Drops the legacy MySQL tables once all writes succeed.

Ensure `METRICS_REDIS_URL` points at the production Redis instance before running the script. After the migration completes, realtime metrics use Redis exclusively and MySQL no longer stores any metrics-specific tables.
