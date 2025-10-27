from __future__ import annotations

from typing import Any


def clone_scan_result(result: dict[str, Any]) -> dict[str, Any]:
    cloned = dict(result)
    metrics = cloned.get("pipeline_metrics")
    if isinstance(metrics, dict):
        cloned["pipeline_metrics"] = dict(metrics)
    return cloned


def annotate_cache_status(result: dict[str, Any] | None, status: str | None) -> dict[str, Any] | None:
    if result is None or not status:
        return result

    result["cache_status"] = status
    metrics = result.get("pipeline_metrics")
    if isinstance(metrics, dict):
        metrics = dict(metrics)
        metrics["cache_status"] = status
        result["pipeline_metrics"] = metrics
    else:
        result["pipeline_metrics"] = {"cache_status": status}
    return result


__all__ = ["clone_scan_result", "annotate_cache_status"]
