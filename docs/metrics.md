## Metrics Storage Overview

Moderator Bot now records moderation activity in Redis, pairing a realtime stream with durable rollups that replace the old MySQL tables. Every scan generates a stream event for live dashboards and updates Redis hashes that keep the aggregate counts lightweight and queryable.

### Redis Layout

- **Stream events** live in the Redis stream defined by `METRICS_REDIS_STREAM` (default `moderator:metrics`). Each entry now carries the full scan payload plus the `accelerated` flag so downstream consumers can separate fast-path scans without rehydrating the detail blob.
- **Daily rollups** are stored under keys that follow `"{prefix}:rollup:{YYYY-MM-DD}:{guild_id}:{content_type}"` where `prefix` is `METRICS_REDIS_PREFIX` (default `moderator:metrics`). Each hash tracks:
  - Core counters: `scans_count`, `flagged_count`, `flags_sum`.
  - Latency and size totals: `total_duration_ms`, `total_duration_sq_ms`, `total_bytes`, `total_bytes_sq`, `last_duration_ms`.
  - Workload totals: `total_frames_scanned`, `total_frames_target`.
  - Derived values (computed when reading): `average_latency_ms`, `latency_std_dev_ms`, `average_bytes`, `bytes_std_dev`, `flagged_rate`, `average_flags_per_scan`.
  - Workload-derived values: `average_frames_per_scan`, `average_latency_per_frame_ms`, `frames_per_second`, `frame_coverage_rate`.
  - Snapshots: `last_status`, `last_reference`, `last_flagged_at`, `last_details`, and `updated_at`.
  - Per-acceleration breakdowns (`accelerated_*`, `non_accelerated_*`, `unknown_acceleration_*`) mirroring the same counters, totals, and snapshots for each execution path.
  Status histograms are kept in a sibling hash that appends `:status` to the rollup key.
- **Global daily rollups** reuse the same schema but set `guild_id` to `0`. They aggregate every scan across all guilds for each `content_type`, enabling dashboards to plot network-wide trends without re-summing per-guild data. Their rollup keys also live in the guild index `"{prefix}:rollups:index:guild:0"`.
- **Global totals** sit in `"{prefix}:totals"`, mirroring the enriched rollup schema (including frame workload counters and per-frame latency metrics) but aggregated across every guild and content type. Per-status counts live under `"{prefix}:totals:status"`, and `updated_at` reflects the last time any metric changed.
- **Indexes** use sorted sets for fast scans: `"{prefix}:rollups:index"` contains every rollup key scored by `date.toordinal()`, and `"{prefix}:rollups:index:guild:{guild_id}"` scopes the index for guild-specific queries.

#### Acceleration Breakdown

Every rollup and the global totals expose an `acceleration` map with three buckets:

| Bucket key | Redis prefix | Notes |
| --- | --- | --- |
| `accelerated` | `accelerated_*` | Scans that ran on the accelerated pipeline (`accelerated=True`). |
| `non_accelerated` | `non_accelerated_*` | Scans that took the normal code path (`accelerated=False`). |
| `unknown` | `unknown_acceleration_*` | Scans where the caller did not specify the acceleration flag. |

Each bucket contains:

- `scans_count`, `flagged_count`, `flags_sum`
- `total_duration_ms`, `total_duration_sq_ms`, `last_latency_ms`, `average_latency_ms`, `latency_std_dev_ms`
- `total_bytes`, `total_bytes_sq`, `average_bytes`, `bytes_std_dev`
- `total_frames_scanned`, `total_frames_target`, `average_frames_per_scan`
- `average_latency_per_frame_ms`, `frames_per_second`, `frame_coverage_rate`
- `flagged_rate`, `average_flags_per_scan`
- Snapshot metadata: `last_status`, `last_reference`, `last_flagged_at`, `last_at`, `last_details`

### Writing Metrics

`modules/metrics/tracker.log_media_scan` remains the public entry point. It builds a normalised payload and delegates to `modules.metrics.backend.accumulate_media_metric`, which:

1. Emits the raw scan to the Redis stream (respecting optional `METRICS_REDIS_STREAM_MAXLEN` bounds).
2. Increments the relevant per-guild rollup hash and the global daily rollup, updating status snapshots and status-count hashes as needed.
3. Applies the same increments to the global totals hash.

When available the tracker records `video_frames_scanned` and `video_frames_target` so downstream rollups can normalise latency per frame and show throughput improvements for accelerated scans.

Because Redis operations are idempotent increments and hash updates, the write path stays low-latency even at high scan volumes.

### Reading Metrics

- `modules.metrics.get_media_metric_rollups()` walks the sorted-set indexes to collect the latest rollups, returning the raw counters plus derived statistics (averages, standard deviations, flag rates, bytes stats, frame-normalised latency) and per-acceleration breakdowns. Each rollup also includes `status_counts`, `updated_at`, and the latest flagged snapshot.
- `modules.metrics.get_media_metric_global_rollups()` mirrors the structure above but retrieves the guild-agnostic daily rollups (`guild_id=0`) so dashboards can plot network-wide activity.
- `modules.metrics.get_media_metrics_summary()` aggregates rollups by `content_type`, surfacing the same enriched metrics and acceleration splits for each content bucket.
- `modules.metrics.get_media_metrics_totals()` fetches the global snapshot in the enriched format so existing consumers automatically gain the expanded measurements, including acceleration metrics, per-frame latency, and `status_counts`.

All helpers return native Python types (UTC-aware datetimes, integers, decoded JSON dicts) so callers don't need to manipulate Redis-specific representations.

### Configuration

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `METRICS_REDIS_URL` | Connection string for Redis (falls back to `REDIS_URL`). | _required for live usage_ |
| `METRICS_REDIS_STREAM` | Redis stream name used for realtime fan-out. | `moderator:metrics` |
| `METRICS_REDIS_STREAM_MAXLEN` | Optional trimming limit for the stream (`XADD MAXLEN`). | disabled |
| `METRICS_REDIS_STREAM_APPROX` | Whether to use Redis approximate trimming (`~`). | `true` |
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
