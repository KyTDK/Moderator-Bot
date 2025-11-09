#!/usr/bin/env python3
"""
Interactive Redis metrics tool.

Features:
- Quick Avg Latency (default): show only the global average latency (ms) from {prefix}:totals.
- Reset: zero just the *_total and *_total_sq fields that drive averages/throughput.
- Report: compute mean & stddev for duration/bytes/frame metrics using totals and squares.
- Coverage: surface accelerated and non-accelerated frame coverage ratios when frame totals exist.

Notes:
- Connects via TCP (defaults: 127.0.0.1:6379, DB 1).
- Keys scanned: {prefix}:totals and {prefix}:rollup:* (overridable by pattern).
- Count detection heuristics per hash: prefer <base>_scans_count, <base>_count, <base>_samples,
  else fall back to scans_count. If count missing/zero, a metric is skipped.
"""

from __future__ import annotations

import math
import sys
import types
from typing import Iterable, NamedTuple, Optional, Tuple


def _import_redis() -> types.ModuleType:
    try:
        import redis  # type: ignore
        return redis
    except ModuleNotFoundError as exc:
        if getattr(exc, "name", None) != "async_timeout":
            print("The 'redis' package is required to run this script.", file=sys.stderr)
            raise SystemExit(1) from exc

    # Provide a minimal async_timeout stub and retry.
    stub = types.ModuleType("async_timeout")

    class _Timeout:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass
        async def __aenter__(self) -> "_Timeout":
            return self
        async def __aexit__(self, *args: object) -> None:
            return None

    def timeout(*args: object, **kwargs: object) -> _Timeout:
        return _Timeout(*args, **kwargs)

    stub.timeout = timeout  # type: ignore[attr-defined]
    sys.modules["async_timeout"] = stub

    import redis  # type: ignore
    return redis


redis = _import_redis()

# Fields ending with these suffixes are zeroed so that derived averages restart.
RESET_SUFFIXES: tuple[str, ...] = (
    "_total_duration_ms",
    "_total_duration_sq_ms",
    "duration_total_ms",
    "duration_total_sq_ms",
)

# Exact field names to reset (covers non-suffixed variants in totals hashes).
RESET_EXACT: set[str] = {
    "total_duration_ms",
    "total_duration_sq_ms",
}

# Map suffix → (units, category label)
SUFFIX_META = {
    "_total_duration_ms": ("ms", "duration"),
    "_total_duration_sq_ms": ("ms^2", "duration_sq"),
    "duration_total_ms": ("ms", "duration"),
    "duration_total_sq_ms": ("ms^2", "duration_sq"),
    "total_duration_ms": ("ms", "duration"),
    "total_duration_sq_ms": ("ms^2", "duration_sq"),

    "_total_bytes": ("bytes", "bytes"),
    "_total_bytes_sq": ("bytes^2", "bytes_sq"),
    "bytes_total": ("bytes", "bytes"),
    "bytes_total_sq": ("bytes^2", "bytes_sq"),
    "total_bytes": ("bytes", "bytes"),
    "total_bytes_sq": ("bytes^2", "bytes_sq"),

    "_total_frames_scanned": ("frames", "frames"),
    "_total_frames_target": ("frames", "frames"),
    "frames_total_scanned": ("frames", "frames"),
    "frames_total_target": ("frames", "frames"),
    "total_frames_scanned": ("frames", "frames"),
    "total_frames_target": ("frames", "frames"),
}

# Preferred count-field endings to try, in order, for a given base name.
COUNT_SUFFIX_CANDIDATES = ("_scans_count", "_count", "_samples")
COUNT_FALLBACK_FIELDS = ("scans_count", "count", "samples")

# Map a duration total suffix to its squared counterpart for stddev calculations.
DURATION_SQ_SUFFIX_BY_TOTAL = {
    "_total_duration_ms": "_total_duration_sq_ms",
    "duration_total_ms": "duration_total_sq_ms",
    "total_duration_ms": "total_duration_sq_ms",
}


def parse_int_safe(v: str) -> Optional[int]:
    try:
        return int(v)
    except Exception:
        try:
            return int(float(v))
        except Exception:
            return None


def parse_float_safe(v: str) -> Optional[float]:
    try:
        return float(v)
    except Exception:
        return None


class FrameCoverage(NamedTuple):
    prefix: str
    scanned: float
    denominator: float
    denominator_field: str
    coverage: float


def compute_frame_coverage_from_hash(h: dict[str, str], prefix: str) -> Optional[FrameCoverage]:
    """
    Compute the coverage ratio for the given acceleration prefix using the
    workload totals described in docs/metrics.md.
    """
    scanned_key = f"{prefix}_total_frames_scanned"
    target_key = f"{prefix}_total_frames_target"
    media_key = f"{prefix}_total_frames_media"

    if scanned_key not in h and target_key not in h and media_key not in h:
        return None

    scanned_val = parse_float_safe(h.get(scanned_key, "0"))
    target_val = parse_float_safe(h.get(target_key, "0"))
    media_val = parse_float_safe(h.get(media_key, "0"))

    frames = max(scanned_val if scanned_val is not None else 0.0, 0.0)
    target_total = max(target_val if target_val is not None else 0.0, 0.0)
    media_total = max(media_val if media_val is not None else 0.0, 0.0)

    denominator = 0.0
    denominator_field = ""

    if media_total > 0:
        denominator = media_total
        denominator_field = media_key
    elif target_total > 0:
        denominator = target_total
        denominator_field = target_key

    if frames > 0 and denominator <= 0:
        denominator = frames
        denominator_field = scanned_key

    if denominator <= 0:
        return None

    if frames > denominator:
        denominator = frames
        denominator_field = scanned_key

    coverage = frames / denominator if denominator > 0 else 0.0
    return FrameCoverage(prefix, frames, denominator, denominator_field, coverage)


def needs_reset(field: str) -> bool:
    if field in RESET_EXACT:
        return True
    return any(field.endswith(suffix) for suffix in RESET_SUFFIXES)


def iter_rollup_keys(client: "redis.Redis", pattern: str) -> Iterable[str]:
    cursor = 0
    while True:
        cursor, batch = client.scan(cursor=cursor, match=pattern)
        for key in batch:
            yield key if isinstance(key, str) else key.decode()
        if cursor == 0:
            break


def detect_count_field(h: dict[str, str], base: str) -> Optional[Tuple[str, int]]:
    """
    Try to find a count field to pair with a given metric 'base'.
    Returns (field_name, count_value) or None.
    """
    for suff in COUNT_SUFFIX_CANDIDATES:
        fname = f"{base}{suff}"
        if fname in h:
            cnt = parse_int_safe(h[fname])
            if cnt is not None and cnt > 0:
                return fname, cnt

    for fname in COUNT_FALLBACK_FIELDS:
        if fname in h:
            cnt = parse_int_safe(h[fname])
            if cnt is not None and cnt > 0:
                return fname, cnt

    return None


def extract_count_fields(h: dict[str, str]) -> dict[str, str]:
    """
    Return all count-like fields in the provided hash.
    """
    counts: dict[str, str] = {}
    for field, sval in h.items():
        if any(field.endswith(suffix) for suffix in COUNT_SUFFIX_CANDIDATES) or field in COUNT_FALLBACK_FIELDS:
            if parse_int_safe(sval) is not None:
                counts[field] = sval
    return counts


def load_count_baselines(client: "redis.Redis", key: str) -> dict[str, int]:
    """
    Load any stored baseline counts for the given metrics hash.
    """
    baseline_key = f"{key}:baseline"
    data = client.hgetall(baseline_key)
    baselines: dict[str, int] = {}
    for field, sval in data.items():
        parsed = parse_int_safe(sval)
        if parsed is not None:
            baselines[field] = parsed
    return baselines


def apply_count_baseline(
    cnt_field: str, cnt_val: int, baselines: dict[str, int]
) -> tuple[int, Optional[int], Optional[int], str]:
    """
    Adjust the raw count by subtracting any stored baseline.
    Returns (adjusted_count, baseline_value, delta_from_baseline, status).
    """
    baseline_val = baselines.get(cnt_field)
    if baseline_val is None:
        return cnt_val, None, None, "none"

    delta = cnt_val - baseline_val
    if delta > 0:
        return delta, baseline_val, delta, "used"
    if delta == 0:
        return 0, baseline_val, 0, "zero"

    # Baseline is ahead of the current count (likely manual edits); keep the raw value.
    return cnt_val, baseline_val, delta, "invalid"


def split_base_and_suffix(field: str) -> Optional[Tuple[str, str]]:
    """
    If 'field' is a supported totals/totals_sq field, return (base, suffix_key_for_SUFFIX_META).
    """
    sorted_suffixes = sorted(SUFFIX_META.keys(), key=len, reverse=True)
    for suff in sorted_suffixes:
        if field.endswith(suff):
            base = field[: -len(suff)] if len(field) > len(suff) else ""
            return base, suff
    return None


def compute_mean_std(total: float, total_sq: Optional[float], count: int):
    """
    Returns (mean, pop_std, sample_std). total_sq can be None (stds then None).
    """
    mean = total / count
    if total_sq is None:
        return mean, None, None

    pop_var = max(total_sq / count - mean * mean, 0.0)
    pop_std = math.sqrt(pop_var)

    if count > 1:
        sample_var = max((total_sq - (total * total) / count) / (count - 1), 0.0)
        sample_std = math.sqrt(sample_var)
    else:
        sample_std = None

    return mean, pop_std, sample_std


def print_table(title: str, rows: list[tuple[str, str, str, float, Optional[float], Optional[float]]]) -> None:
    if not rows:
        return
    print(f"\n== {title} ==")
    headers = ("Metric", "Units", "Count", "Mean", "Pop σ", "Sample σ")
    widths = [len(h) for h in headers]
    str_rows = []
    for metric, units, cnt, mean, pop_std, samp_std in rows:
        r = (
            metric,
            units,
            str(cnt),
            f"{mean:.3f}",
            "-" if pop_std is None else f"{pop_std:.3f}",
            "-" if samp_std is None else f"{samp_std:.3f}",
        )
        str_rows.append(r)
        widths = [max(w, len(col)) for w, col in zip(widths, r)]

    line = "  ".join(h.ljust(w) for h, w in zip(headers, widths))
    sep = "  ".join("-" * w for w in widths)
    print(line)
    print(sep)
    for r in str_rows:
        print("  ".join(col.ljust(w) for col, w in zip(r, widths)))


def action_reset(client: "redis.Redis", prefix: str, pattern: Optional[str], dry_run: bool) -> None:
    rollup_pattern = pattern or f"{prefix}:rollup:*"
    totals_key = f"{prefix}:totals"

    updated_hashes = 0
    updated_fields = 0

    def reset_hash(key: str, store_baseline: bool = False) -> None:
        nonlocal updated_hashes, updated_fields
        data = client.hgetall(key)
        if not data:
            return
        updates = {field: "0" for field in data if needs_reset(field)}
        baseline_counts = extract_count_fields(data) if store_baseline else {}
        if not updates and not baseline_counts:
            return

        if updates:
            updated_hashes += 1
            updated_fields += len(updates)

        if dry_run:
            if updates:
                print(f"[dry-run] {key}: resetting {', '.join(sorted(updates))}")
            if baseline_counts:
                print(f"[dry-run] {key}: would store count baselines {', '.join(sorted(baseline_counts))}")
        else:
            if baseline_counts:
                baseline_key = f"{key}:baseline"
                client.hset(baseline_key, mapping=baseline_counts)
            if updates:
                client.hset(key, mapping=updates)
            if updates and baseline_counts:
                print(f"{key}: reset {len(updates)} fields; stored baselines for {len(baseline_counts)} count field(s)")
            elif updates:
                print(f"{key}: reset {len(updates)} fields")
            elif baseline_counts:
                print(f"{key}: stored baselines for {len(baseline_counts)} count field(s)")

    reset_hash(totals_key, store_baseline=True)
    for key in iter_rollup_keys(client, rollup_pattern):
        reset_hash(key, store_baseline=True)

    if updated_hashes == 0:
        print("\nNo hashes required updates (nothing matched the reset criteria).")
    else:
        action = "would reset" if dry_run else "reset"
        print(f"\n{action} {updated_fields} fields across {updated_hashes} hash{'es' if updated_hashes != 1 else ''}.")


def action_report(client: "redis.Redis", prefix: str, pattern: Optional[str]) -> None:
    rollup_pattern = pattern or f"{prefix}:rollup:*"
    totals_key = f"{prefix}:totals"

    def report_hash(key: str) -> None:
        h = client.hgetall(key)
        if not h:
            return

        rows: list[tuple[str, str, str, float, Optional[float], Optional[float]]] = []

        baselines = load_count_baselines(client, key)

        totals_by_base: dict[tuple[str, str], Tuple[str, float, str]] = {}
        squares_by_base: dict[tuple[str, str], Tuple[str, float, str]] = {}

        for field, sval in h.items():
            parsed = split_base_and_suffix(field)
            if not parsed:
                continue
            base, suff = parsed
            units, kind = SUFFIX_META[suff]
            val = parse_float_safe(sval)
            if val is None:
                continue

            if kind in ("duration", "bytes", "frames"):
                totals_by_base[(base, units)] = (field, val, units)
            elif kind in ("duration_sq", "bytes_sq"):
                squares_by_base[(base, units)] = (field, val, units)

        for (base, units), (total_field, total_val, _u) in totals_by_base.items():
            sq = squares_by_base.get((base, units))
            total_sq_val: Optional[float] = sq[1] if sq else None

            cnt_info = detect_count_field(h, base) or (detect_count_field(h, "") if base == "" else None)
            if not cnt_info:
                continue
            cnt_field, cnt_val = cnt_info
            if cnt_val <= 0:
                continue

            adjusted_cnt, baseline_val, baseline_delta, baseline_status = apply_count_baseline(
                cnt_field, cnt_val, baselines
            )
            if baseline_status == "zero":
                # Nothing new since the last reset for this metric; skip it.
                continue

            if adjusted_cnt <= 0:
                continue

            mean, pop_std, sample_std = compute_mean_std(total_val, total_sq_val, adjusted_cnt)
            metric_label = (base.rstrip(":_") or "global") + "/" + total_field.split(":")[-1]
            if baseline_status == "used" and baseline_val is not None and baseline_delta is not None:
                count_display = f"{baseline_delta} (raw {cnt_val}, baseline {baseline_val})"
            elif baseline_status == "invalid" and baseline_val is not None and baseline_delta is not None:
                count_display = (
                    f"{adjusted_cnt} (raw {cnt_val}, baseline {baseline_val}, delta {baseline_delta} invalid)"
                )
            else:
                count_display = str(adjusted_cnt)
            rows.append((metric_label, units, count_display, mean, pop_std, sample_std))

        if rows:
            print_table(key, rows)

        coverage_pairs: list[tuple[str, Optional[FrameCoverage]]] = [
            ("Accelerated", compute_frame_coverage_from_hash(h, "accelerated")),
            ("Non-Accelerated", compute_frame_coverage_from_hash(h, "non_accelerated")),
        ]

        if any(info is not None for _, info in coverage_pairs):
            if not rows:
                print(f"\n== {key} ==")
            print("  Frame coverage:")
            for label, info in coverage_pairs:
                if info:
                    coverage_pct = info.coverage * 100.0
                    scanned_display = f"{info.scanned:.0f}"
                    denom_display = f"{info.denominator:.0f}"
                    print(
                        f"    {label}: {coverage_pct:.2f}% "
                        f"({scanned_display}/{denom_display} via {info.denominator_field})"
                    )
                else:
                    print(f"    {label}: n/a")

    report_hash(totals_key)
    for key in iter_rollup_keys(client, rollup_pattern):
        report_hash(key)


def action_quick_avg_latency(client: "redis.Redis", prefix: str) -> int:
    """
    Show only the global average latency (ms) from {prefix}:totals.
    Looks for a duration total in {prefix}:totals with empty base name (pure total),
    falls back to the first duration total if needed. Uses best-available count.
    """
    totals_key = f"{prefix}:totals"
    h = client.hgetall(totals_key)
    if not h:
        print(f"No totals hash found at '{totals_key}'.")
        return 1

    # Gather candidate duration totals in this hash
    duration_totals: list[tuple[str, str, str]] = []  # (field, base, suffix)
    for field in h.keys():
        parsed = split_base_and_suffix(field)
        if not parsed:
            continue
        base, suff = parsed
        units, kind = SUFFIX_META[suff]
        if kind == "duration" and units == "ms":
            duration_totals.append((field, base, suff))

    if not duration_totals:
        print(f"No duration totals (…_total_duration_ms or duration_total_ms or total_duration_ms) in '{totals_key}'.")
        return 1

    # Prefer global (empty base) first
    field, base, field_suffix = next((t for t in duration_totals if t[1] == ""), duration_totals[0])
    total_val = parse_float_safe(h.get(field, ""))
    if total_val is None:
        print(f"Duration total field '{field}' in '{totals_key}' is not numeric.")
        return 1

    sq_field: Optional[str] = None
    total_sq_val: Optional[float] = None
    sq_suffix = DURATION_SQ_SUFFIX_BY_TOTAL.get(field_suffix)
    if sq_suffix is not None:
        candidate = f"{base}{sq_suffix}" if base else sq_suffix
        sq_field = candidate
        total_sq_val = parse_float_safe(h.get(candidate, ""))

    baselines = load_count_baselines(client, totals_key)
    baseline_key = f"{totals_key}:baseline"

    cnt_info = detect_count_field(h, base) or detect_count_field(h, "")
    if not cnt_info:
        print(f"No suitable count field found in '{totals_key}' to compute average.")
        return 1
    cnt_field, cnt_val = cnt_info
    if cnt_val <= 0:
        print(f"Count field '{cnt_field}' in '{totals_key}' is zero.")
        return 1
    adjusted_cnt, baseline_val, baseline_delta, baseline_status = apply_count_baseline(
        cnt_field, cnt_val, baselines
    )
    if baseline_status == "zero" and baseline_val is not None:
        print(f"No new scans since the last reset (baseline {baseline_val} for '{cnt_field}').")
        return 1
    if baseline_status == "invalid" and baseline_val is not None and baseline_delta is not None:
        print(
            f"Baseline {baseline_val} for '{cnt_field}' in '{baseline_key}' exceeds current count {cnt_val}; using raw count."
        )

    if adjusted_cnt <= 0:
        print(f"Adjusted count for '{cnt_field}' in '{totals_key}' is not positive.")
        return 1

    mean_ms, pop_std_ms, sample_std_ms = compute_mean_std(total_val, total_sq_val, adjusted_cnt)
    avg_ms = mean_ms
    print("\n== Quick Avg Latency ==")
    print(f"Totals Key : {totals_key}")
    print(f"Duration   : {field} = {total_val:g}")
    if sq_field and total_sq_val is not None:
        print(f"Squares    : {sq_field} = {total_sq_val:g}")
    if baseline_status == "used" and baseline_val is not None and baseline_delta is not None:
        print(f"Count      : {cnt_field} = {cnt_val} (baseline {baseline_val}, delta {baseline_delta})")
        equation = (
            f"{field} / ({cnt_field} - baseline) = "
            f"{total_val:g} / ({cnt_val} - {baseline_val}) = "
            f"{total_val:g} / {baseline_delta} = {avg_ms:.3f} ms"
        )
    elif baseline_status == "invalid" and baseline_val is not None and baseline_delta is not None:
        print(
            f"Count      : {cnt_field} = {cnt_val} (baseline {baseline_val}, delta {baseline_delta} invalid; using raw count)"
        )
        equation = f"{field} / {cnt_field} = {total_val:g} / {adjusted_cnt} = {avg_ms:.3f} ms"
    else:
        print(f"Count      : {cnt_field} = {cnt_val}")
        equation = f"{field} / {cnt_field} = {total_val:g} / {adjusted_cnt} = {avg_ms:.3f} ms"
    print(f"Equation   : {equation}")
    print(f"Average    : {avg_ms:.3f} ms")
    if total_sq_val is None:
        print("Pop σ      : n/a (missing squared totals)")
        print("Sample σ   : n/a (missing squared totals)")
    else:
        pop_display = f"{pop_std_ms:.3f} ms" if pop_std_ms is not None else "n/a"
        sample_display = f"{sample_std_ms:.3f} ms" if sample_std_ms is not None else "n/a"
        print(f"Pop σ      : {pop_display}")
        print(f"Sample σ   : {sample_display}")

    for label, prefix in (
        ("Accelerated Coverage", "accelerated"),
        ("Non-Accelerated Coverage", "non_accelerated"),
    ):
        coverage_info = compute_frame_coverage_from_hash(h, prefix)
        if coverage_info:
            coverage_pct = coverage_info.coverage * 100.0
            scanned_display = f"{coverage_info.scanned:.0f}"
            denom_display = f"{coverage_info.denominator:.0f}"
            print(
                f"{label}: {coverage_pct:.2f}% "
                f"({scanned_display}/{denom_display} via {coverage_info.denominator_field})"
            )
        else:
            print(f"{label}: n/a")
    return 0


def ask(prompt: str, default: Optional[str] = None, cast=None):
    sfx = f" [{default}]" if default is not None else ""
    val = input(f"{prompt}{sfx}: ").strip()
    if not val and default is not None:
        val = default
    if cast and val is not None and val != "":
        try:
            return cast(val)
        except Exception:
            print(f"Invalid value, using default {default!r}.")
            return cast(default) if default is not None else None
    return val


def main() -> int:
    print("=== Metrics Reset/Report Tool (Redis) ===")
    host = ask("Redis host", "127.0.0.1")
    port = ask("Redis port", "6379", int)
    db = ask("Redis DB index", "1", int)
    prefix = ask("Metrics key prefix", "moderator:metrics")
    pattern = ask('Optional SCAN pattern (Enter to use "{prefix}:rollup:*")', None)
    if pattern:
        pattern = pattern.replace("{prefix}", prefix)

    print("\nChoose an action (press Enter for default):")
    print("  [1] Reset totals (zero average/throughput accumulators)")
    print("  [2] Quick Avg Latency (global {prefix}:totals)  ← default")
    print("  [3] Full Report (compute mean & stddev)")
    print("  [q] Quit")
    choice = input("Enter choice [1/2/3/q]: ").strip().lower() or "2"

    if choice in ("q", "quit", "exit"):
        return 0

    try:
        client = redis.Redis(host=host, port=port, db=db, decode_responses=True)
        client.ping()
    except Exception as e:
        print(f"Failed to connect to Redis at {host}:{port}/{db}: {e}", file=sys.stderr)
        return 1

    if choice == "1":
        dry = input("Dry run? [y/N]: ").strip().lower() in ("y", "yes")
        print("\n-- RESET --")
        action_reset(client, prefix, pattern, dry_run=dry)
    elif choice == "2":
        print("\n-- QUICK AVG LATENCY --")
        return action_quick_avg_latency(client, prefix)
    elif choice == "3":
        print("\n-- REPORT --")
        action_report(client, prefix, pattern)
    else:
        print("Unknown choice. Exiting.")
        return 1

    print("\nDone.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
