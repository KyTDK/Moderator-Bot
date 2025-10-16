# Metrics Storage Overview

Moderator Bot keeps an always-on summary of moderation activity in the `moderation_metric_totals` table. The table is designed to contain **exactly one row**, so storage stays constant while still exposing high-value analytics.

## Table Schema

| Column | Description |
| --- | --- |
| `singleton_id` | Hard-coded primary key (always `1`) that guards the single-row invariant. |
| `scans_count` | Total number of moderation scans performed across the lifetime of the bot. |
| `flagged_count` | Number of scans that resulted in a violation (`is_nsfw` / flagged content). |
| `flags_sum` | Aggregate sum of all per-scan `flags_count` values (captures multi-trigger events). |
| `total_bytes` | Total size, in bytes, of media scanned. |
| `total_duration_ms` | Cumulative scan duration reported by the scanners (milliseconds). |
| `last_duration_ms` | Duration of the most recent scan that updated the totals (milliseconds). |
| `status_counts` | JSON map counting the outcome/status of every scan (for example `{"scan_complete": 1204, "unsupported_type": 17}`). |
| `last_flagged_at` | Timestamp of the most recent flagged scan. |
| `last_status` | Status string recorded for the most recent scan (flagged or not). |
| `last_reference` | Identifier for the most recent flagged scan (message ID + filename/reference when available). |
| `last_details` | JSON snapshot describing the most recent notable scan (flagged or non-standard status). |
| `updated_at` | Managed automatically by MySQL; reflects the last time the totals row changed. |

## How Totals Are Maintained

`modules/metrics/tracker.log_media_scan` is the canonical entry point for recording metrics. Each invocation forwards a distilled payload to `modules.utils.mysql.metrics.accumulate_media_metric`, which:

1. Updates any per-day/per-guild rollups (used internally for diagnostics).
2. Performs a transactional upsert on `moderation_metric_totals`, incrementing the global counters and refreshing status/detail fields.

The update is atomic, so partial writes are avoided even under concurrent loads.

## Reading the Totals

Call `modules.metrics.get_media_metrics_totals()` to fetch the current aggregate snapshot. The helper normalises types (for example converting JSON maps back into dictionaries and re-applying UTC to timestamps) so consumers can use the data directly in dashboards, alerts, or usage reporting.

In addition to the stored counters, the helper exposes convenience fields: `last_latency_ms` mirrors the raw `last_duration_ms` column, and `average_latency_ms` divides `total_duration_ms` by `scans_count` (returning `0.0` when no scans have been recorded).

Because only a single row is stored, queries are constant-time and the footprint remains tiny regardless of how many scans occur.
