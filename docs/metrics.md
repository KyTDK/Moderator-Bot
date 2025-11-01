## Metrics Storage Overview

Moderator Bot now records moderation activity in Redis, pairing a realtime stream with durable rollups that replace the old MySQL tables. Every scan generates a stream event for live dashboards and updates Redis hashes that keep the aggregate counts lightweight and queryable.

### Redis Layout

- **Stream events** live in the Redis stream defined by `METRICS_REDIS_STREAM` (default `moderator:metrics`). Each entry emits the normalised rollup payload: metric date, guild identifier (with `0` representing the global aggregate), content type, aggregate counters, workload totals, acceleration flag, and the sanitised snapshot metadata used for `last_details`.
- **Daily rollups** are stored under keys that follow `"{prefix}:rollup:{YYYY-MM-DD}:{guild_id}:{content_type}"` where `prefix` is `METRICS_REDIS_PREFIX` (default `moderator:metrics`). Each hash tracks:
  - Core counters: `scans_count`, `flagged_count`, `flags_sum`.
  - Latency and size totals: `total_duration_ms`, `total_duration_sq_ms`, `total_bytes`, `total_bytes_sq`, `last_duration_ms`.
  - Workload totals: `total_frames_scanned`, `total_frames_target`, `total_frames_media`.
  - Derived values (computed when reading): `average_latency_ms`, `latency_std_dev_ms`, `average_bytes`, `bytes_std_dev`, `flagged_rate`, `average_flags_per_scan`.
  - Workload-derived values: `average_frames_per_scan`, `average_latency_per_frame_ms`, `frames_per_second`, `frame_coverage_rate` (which uses `total_frames_media` when present).
  - Snapshots: `last_status`, `last_flagged_at`, `last_details`, and `updated_at`. The `last_details` blob only stores sanitised scanner/source metadata, aggregate counts, workload totals, and scan summaries—never raw payloads, filenames, message identifiers, or user information.
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
- `total_frames_scanned`, `total_frames_target`, `total_frames_media`, `average_frames_per_scan`
- `average_latency_per_frame_ms`, `frames_per_second`, `frame_coverage_rate`
- `flagged_rate`, `average_flags_per_scan`
- Snapshot metadata: `last_status`, `last_flagged_at`, `last_at`, `last_details`

### Writing Metrics

`modules/metrics/tracker.log_media_scan` remains the public entry point. It builds a normalised payload and delegates to `modules.metrics.backend.accumulate_media_metric`, which:

1. Emits the raw scan to the Redis stream (respecting optional `METRICS_REDIS_STREAM_MAXLEN` bounds).
2. Increments the relevant per-guild rollup hash and the global daily rollup, updating status snapshots and status-count hashes as needed.
3. Applies the same increments to the global totals hash.

When available the tracker records `video_frames_scanned`, `video_frames_target`, and `video_frames_media_total` so downstream rollups can normalise latency per frame, calculate coverage against the true media length, and show throughput improvements for accelerated scans.

Because Redis operations are idempotent increments and hash updates, the write path stays low-latency even at high scan volumes.

#### Per-Scan Telemetry

Every scan payload carries a `pipeline_metrics` map that mirrors the work performed by the scanners. The helpers in `modules/nsfw_scanner/helpers/metrics.py` feed a `LatencyTracker` (and, for video, a `VideoMetrics` accumulator) so the telemetry is standardised before it reaches `log_media_scan`.

Key fields include:

- `total_latency_ms` / `total_duration_ms` – the wall-clock latency for the scan, including time spent waiting in the worker queue. When an upstream duration already exists it is preserved as `pipeline_total_latency_ms`.
- `latency_breakdown_ms` – nested map of `{step_name: {"duration_ms": float, "label": str}}`. Common keys are `queue_wait`, `attachment_mime_detection`, `attachment_context_build`, `image_open`, `image_encode`, `frame_decode`, `frame_moderation`, `frame_moderation_wait`, `frame_dedupe`, `frame_extraction`, `frame_queue_wait`, `frame_pipeline`, and `coordination`. The new queue wait entry makes backlog hotspots visible in both Discord embeds and Redis.
- `bytes_downloaded` – best-effort size of the processed payload (preferring download telemetry, falling back to local file size).
- `early_exit` – optional string explaining why work stopped early (`high_confidence_hit`, `low_risk_streak`, `flat_motion`, etc.).
- Video diagnostics – `frames_submitted`, `frames_processed`, `frames_scanned`, `frames_target`, `frames_media_total`, `dedupe_skipped`, `flush_count`, `avg_frame_interarrival_ms`, `effective_flush_timeout_ms`, `residual_low_risk_streak`, `residual_motion_plateau`, plus a `queue_wait_latency_ms` counter. These values flow through to Redis so dashboards can highlight throughput regressions or dedupe effectiveness.

All telemetry helpers also emit preformatted strings (latency breakdown lists, frame summaries) that match the data stored in Redis, ensuring operator alerts and dashboards stay in sync.

### Reading Metrics

- `modules.metrics.get_media_metric_rollups()` walks the sorted-set indexes to collect the latest rollups, returning the raw counters plus derived statistics (averages, standard deviations, flag rates, bytes stats, frame-normalised latency) and per-acceleration breakdowns. Each rollup also includes `status_counts`, `updated_at`, and the latest flagged snapshot.
- `modules.metrics.get_media_metric_global_rollups()` mirrors the structure above but retrieves the guild-agnostic daily rollups (`guild_id=0`) so dashboards can plot network-wide activity.
- `modules.metrics.get_media_metrics_summary()` aggregates rollups by `content_type`, surfacing the same enriched metrics and acceleration splits for each content bucket.
- `modules.metrics.get_media_metrics_totals()` fetches the global snapshot in the enriched format so existing consumers automatically gain the expanded measurements, including acceleration metrics, per-frame latency, and `status_counts`.

All helpers return native Python types (UTC-aware datetimes, integers, decoded JSON dicts) so callers don't need to manipulate Redis-specific representations.

### Derived Metric Reference

The Redis hashes only persist raw counters; every reader recomputes the derived fields using shared helpers:

- `average_latency_ms` = `total_duration_ms / max(scans_count, 1)`
- `latency_std_dev_ms` uses the population variance implied by `total_duration_sq_ms`
- `average_bytes` = `total_bytes / max(scans_count, 1)`
- `bytes_std_dev` mirrors the latency calculation but with `total_bytes_sq`
- `flagged_rate` = `flagged_count / max(scans_count, 1)`
- `average_flags_per_scan` = `flags_sum / max(scans_count, 1)`
- `average_frames_per_scan` = `total_frames_scanned / max(scans_count, 1)`
- `average_latency_per_frame_ms` divides `total_duration_ms` by `total_frames_scanned`, falling back to `scans_count` whenever no frame totals were recorded
- `frames_per_second` = `(total_frames_scanned * 1000) / total_duration_ms` (returns `0` when the denominator is non-positive)
- `frame_coverage_rate` divides `total_frames_scanned` by `total_frames_media` when available, otherwise it falls back to the larger of `total_frames_target` or `total_frames_scanned` (returning `0` when no frames were processed)

Summary payloads expose `scans`, `flagged`, `flags_sum`, `bytes_total`, `duration_total_ms`, `frames_total_scanned`, `frames_total_target`, and `frames_total_media`. The `acceleration` buckets mirror that structure after running through the same formulas, so you can compare accelerated, non-accelerated, and unknown execution paths with identical metrics.

### Configuration

| Environment variable | Purpose | Default |
| --- | --- | --- |
| `METRICS_REDIS_URL` | Connection string for the metrics Redis database (always normalised to `/1`). | _required for live usage_ |
| `METRICS_REDIS_STREAM` | Redis stream name used for realtime fan-out. | `moderator:metrics` |
| `METRICS_REDIS_STREAM_MAXLEN` | Optional trimming limit for the stream (`XADD MAXLEN`). | disabled |
| `METRICS_REDIS_STREAM_APPROX` | Whether to use Redis approximate trimming (`~`). | `true` |
| `METRICS_REDIS_PREFIX` | Prefix for rollup/totals keys and indexes. | `moderator:metrics` |

If `METRICS_REDIS_URL` is not provided the backend stays inactive, but tests can still inject a fake client via `modules.metrics.backend.set_client_override`. The URL is automatically rewritten to target database `1` so metrics never share the default Redis database.

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

### Access Patterns and Examples

- **Logging scans:** call `log_media_scan(...)` whenever the NSFW scanner finishes. Populate `video_frames_scanned`, `video_frames_target`, and `video_frames_media_total` for videos so downstream consumers can compute per-frame latency, throughput, and coverage.
- **Guild dashboards:** `await get_media_metric_rollups(guild_id=..., limit=...)` returns the newest rollups first. Filter by `content_type` to generate per-media charts, or merge guild IDs to monitor partner servers.
- **Network overview:** `await get_media_metric_global_rollups(content_type="image")` produces cross-guild trends, while `await get_media_metrics_totals()` supplies headline KPIs (flagged counts, latency, acceleration splits).
- **Content mix summaries:** `await get_media_metrics_summary(guild_id=...)` collapses every rollup into a per-content bucket with `acceleration` subtrees. Sort by `scans` or `flagged_rate` to build workload leaderboards.
- **Status monitoring:** inspect `status_counts` (and the acceleration snapshots) to alert on spikes in `unsupported_type`, `scan_failed`, or other non-success states.
- **Testing:** use `modules.metrics.backend.set_client_override(fake_client)`—as shown in `tests/test_metrics.py`—to run unit tests without touching a real Redis instance.

### Suggested Dashboards and Alerts

- Track **moderation latency** by plotting `average_latency_ms` per content type and overlaying acceleration buckets to quantify pipeline gains.
- Use **throughput summaries** (`frames_per_second`, `average_frames_per_scan`, `frame_coverage_rate`) for video-heavy guilds to ensure frame extraction keeps up with uploads.
- Build a **flagged-rate leaderboard** from summary payloads to spotlight guilds or content types that generate the most escalations.
- Trigger **error alerts** when `status_counts["unsupported_type"]` or `status_counts["scan_failed"]` exceeds a rolling baseline, or when the totals hash `updated_at` stops advancing.
- Compare **acceleration adoption** by charting `acceleration.accelerated.scans` versus `acceleration.non_accelerated.scans`; large deltas highlight servers still relying on the slower path.

### Operational Tips

- Redis stores every value as a string; always go through the helper APIs to obtain type-coerced integers, floats, and datetimes.
- When you need to rebuild a bucket, delete only the affected rollup hash and its `:status` companion—fresh scans will repopulate it without disturbing other metrics.
- Monitor the Redis stream defined by `METRICS_REDIS_STREAM`; if it stops receiving entries, neither rollups nor totals will advance.
- When introducing a new KPI, add raw counters to the write path and derive the statistic during reads (following the existing pattern) to keep write amplification low.
