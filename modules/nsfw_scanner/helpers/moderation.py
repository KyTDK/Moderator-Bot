import asyncio
import base64
import os
import time
from collections import Counter
from typing import Any

import openai
import httpx
from PIL import Image

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import api, clip_vectors, mysql

from ..constants import ADD_SFW_VECTOR, SFW_VECTOR_MAX_SIMILARITY
from ..utils.categories import is_allowed_category
from ..utils.file_ops import file_to_b64


class ModeratorLatencyTracker:
    _BREAKDOWN_LABELS: dict[str, tuple[str, str]] = {
        "payload_prepare_ms": ("moderation_payload", "Moderator Payload Prep"),
        "key_acquire_ms": ("moderation_key_acquire", "API Client Acquire"),
        "key_wait_ms": ("moderation_key_wait", "API Key Wait"),
        "resource_latency_ms": ("moderation_resource", "Moderator Client Resolve"),
        "api_call_ms": ("moderation_request", "Moderator API Request"),
        "response_parse_ms": (
            "moderation_response_parse",
            "Moderator Response Parse",
        ),
        "vector_add_ms": ("moderation_vector", "Vector Maintenance"),
    }

    def __init__(self) -> None:
        self.timings: dict[str, float] = {
            key: 0.0 for key in self._BREAKDOWN_LABELS.keys()
        }
        self._metrics: dict[str, Any] = {
            "attempts": 0,
            "no_key_waits": 0,
            "failures": Counter(),
        }
        self._successful_attempt: bool = False

    def start(self, key: str) -> float:
        return time.perf_counter()

    def stop(self, key: str, started: float | None) -> None:
        if started is None:
            return
        self.timings[key] = self.timings.get(key, 0.0) + (
            time.perf_counter() - started
        ) * 1000

    def record_attempt(self) -> None:
        self._metrics["attempts"] += 1

    def record_success(self) -> None:
        self._successful_attempt = True

    def record_no_key_wait(self) -> None:
        self._metrics["no_key_waits"] += 1

    def record_failure(self, reason: str) -> None:
        self._metrics["failures"][reason] += 1

    def finalize(self, payload: dict[str, Any]) -> dict[str, Any]:
        breakdown: dict[str, dict[str, Any]] = {}
        for raw_key, (output_key, label) in self._BREAKDOWN_LABELS.items():
            duration = self.timings.get(raw_key, 0.0)
            if duration > 0:
                breakdown[output_key] = {
                    "duration_ms": duration,
                    "label": label,
                }

        if breakdown:
            pipeline_metrics = payload.setdefault("pipeline_metrics", {})
            moderator_breakdown = pipeline_metrics.setdefault(
                "moderator_breakdown_ms", {}
            )
            moderator_breakdown.update(breakdown)

        metadata: dict[str, Any] = {}
        if self._metrics["attempts"]:
            metadata["attempts"] = self._metrics["attempts"]
        if self._metrics["no_key_waits"]:
            metadata["no_key_waits"] = self._metrics["no_key_waits"]
        if self._metrics["failures"]:
            metadata["failures"] = dict(self._metrics["failures"])
        metadata["had_successful_attempt"] = self._successful_attempt

        if metadata:
            pipeline_metrics = payload.setdefault("pipeline_metrics", {})
            moderator_metadata = pipeline_metrics.setdefault(
                "moderator_metadata", {}
            )
            moderator_metadata.update(metadata)

        return payload


def _should_add_sfw_vector(
    flagged_any: bool,
    skip_vector_add: bool,
    max_similarity: float | None,
) -> bool:
    if flagged_any or skip_vector_add:
        return False
    if max_similarity is None:
        return True
    return max_similarity <= SFW_VECTOR_MAX_SIMILARITY


async def _get_moderations_resource(client):
    """
    Lazily resolve client.moderations in a thread so that the heavy OpenAI
    imports it triggers do not block the event loop when the first scan runs.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, lambda: client.moderations)


async def moderator_api(
    scanner,
    text: str | None = None,
    image_path: str | None = None,
    image: Image.Image | None = None,
    image_bytes: bytes | None = None,
    image_mime: str | None = None,
    guild_id: int | None = None,
    max_attempts: int = 2,
    skip_vector_add: bool = False,
    max_similarity: float | None = None,
    allowed_categories: list[str] | None = None,
    threshold: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "is_nsfw": None,
        "category": None,
        "score": 0.0,
        "reason": None,
    }

    inputs: list[Any] | str = []
    has_image_input = image_path is not None or image_bytes is not None

    latency_tracker = ModeratorLatencyTracker()

    def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
        return latency_tracker.finalize(payload)

    if text and not has_image_input:
        inputs = text

    if has_image_input:
        b64_data: str | None = None
        if image_bytes is not None:
            try:
                payload_timer = latency_tracker.start("payload_prepare_ms")
                b64_data = base64.b64encode(image_bytes).decode()
                latency_tracker.stop("payload_prepare_ms", payload_timer)
            except Exception as exc:
                print(f"[moderator_api] Failed to encode image bytes: {exc}")
                return _finalize(result)
        elif image_path is not None:
            if not os.path.exists(image_path):
                print(f"[moderator_api] Image path does not exist: {image_path}")
                return _finalize(result)
            try:
                payload_timer = latency_tracker.start("payload_prepare_ms")
                b64_data = await asyncio.to_thread(file_to_b64, image_path)
                latency_tracker.stop("payload_prepare_ms", payload_timer)
            except Exception as exc:  # pragma: no cover - best effort logging
                print(f"[moderator_api] Error reading image {image_path}: {exc}")
                return _finalize(result)
        if not b64_data:
            print("[moderator_api] No image content was provided")
            return _finalize(result)
        mime_type = image_mime or "image/jpeg"
        inputs = [
            {
                "type": "image_url",
                "image_url": {"url": f"data:{mime_type};base64,{b64_data}"},
            }
        ]

    if not inputs:
        print("[moderator_api] No inputs were provided")
        return _finalize(result)

    resolved_allowed_categories = allowed_categories
    resolved_threshold = threshold
    settings_map: dict[str, Any] | None = None

    if guild_id is not None and (
        resolved_allowed_categories is None or resolved_threshold is None
    ):
        settings_map = await mysql.get_settings(
            guild_id, [NSFW_CATEGORY_SETTING, "threshold"]
        )

    if resolved_allowed_categories is None:
        resolved_allowed_categories = (settings_map or {}).get(
            NSFW_CATEGORY_SETTING, []
        ) or []

    if resolved_threshold is None:
        try:
            resolved_threshold = float((settings_map or {}).get("threshold", 0.7))
        except (TypeError, ValueError):
            resolved_threshold = 0.7

    if resolved_allowed_categories is None:
        resolved_allowed_categories = []
    if resolved_threshold is None:
        resolved_threshold = 0.7

    had_openai_timeout = False
    had_http_timeout = False

    for attempt_index in range(max_attempts):
        attempt_number = attempt_index + 1
        latency_tracker.record_attempt()
        key_timer = latency_tracker.start("key_acquire_ms")
        client, encrypted_key = await api.get_api_client(guild_id)
        latency_tracker.stop("key_acquire_ms", key_timer)
        if not client:
            print("[moderator_api] No available API key.")
            latency_tracker.record_failure("no_key_available")
            wait_timer = latency_tracker.start("key_wait_ms")
            await asyncio.sleep(2)
            latency_tracker.stop("key_wait_ms", wait_timer)
            latency_tracker.record_no_key_wait()
            continue
        api_started: float | None = None
        try:
            resource_timer = latency_tracker.start("resource_latency_ms")
            moderations_resource = await _get_moderations_resource(client)
            latency_tracker.stop("resource_latency_ms", resource_timer)
            api_started = latency_tracker.start("api_call_ms")
            response = await moderations_resource.create(
                model="omni-moderation-latest" if has_image_input else "text-moderation-latest",
                input=inputs,
            )
            latency_tracker.stop("api_call_ms", api_started)
        except openai.AuthenticationError:
            latency_tracker.stop("api_call_ms", api_started)
            print("[moderator_api] Authentication failed. Marking key as not working.")
            await api.set_api_key_not_working(api_key=encrypted_key, bot=scanner.bot)
            latency_tracker.record_failure("authentication_error")
            continue
        except openai.RateLimitError as exc:
            latency_tracker.stop("api_call_ms", api_started)
            print(
                f"[moderator_api] Rate limit error on attempt {attempt_number}/{max_attempts}: {exc}."
            )
            latency_tracker.record_failure("rate_limit_error")
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except openai.APITimeoutError as exc:
            latency_tracker.stop("api_call_ms", api_started)
            print(
                f"[moderator_api] Moderation request timed out on attempt "
                f"{attempt_number}/{max_attempts}: {exc}."
            )
            had_openai_timeout = True
            latency_tracker.record_failure("openai_timeout")
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except httpx.TimeoutException as exc:
            latency_tracker.stop("api_call_ms", api_started)
            print(
                f"[moderator_api] HTTP timeout during moderation request on attempt "
                f"{attempt_number}/{max_attempts}: {exc}."
            )
            had_http_timeout = True
            latency_tracker.record_failure("http_timeout")
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except Exception as exc:
            latency_tracker.stop("api_call_ms", api_started)
            print(f"[moderator_api] Unexpected error from OpenAI API: {exc}.")
            latency_tracker.record_failure("unexpected_api_error")
            continue

        if not response or not response.results:
            print("[moderator_api] No moderation results returned.")
            latency_tracker.record_failure("empty_results")
            continue

        if not await api.is_api_key_working(encrypted_key):
            await api.set_api_key_working(encrypted_key)

        results = response.results[0]
        guild_flagged_categories: list[tuple[str, float]] = []
        summary_categories = {}  # category: score
        flagged_any = False
        parse_timer = latency_tracker.start("response_parse_ms")
        for category, is_flagged in results.categories.__dict__.items():
            normalized_category = category.replace("/", "_").replace("-", "_")
            score = results.category_scores.__dict__.get(category, 0)

            if is_flagged:
                flagged_any = True

            summary_categories[normalized_category] = score

            if is_flagged and not skip_vector_add and clip_vectors.is_available():
                latency_tracker.stop("response_parse_ms", parse_timer)
                vector_started = latency_tracker.start("vector_add_ms")
                await asyncio.to_thread(
                    clip_vectors.add_vector,
                    image,
                    metadata={"category": normalized_category, "score": score},
                )
                latency_tracker.stop("vector_add_ms", vector_started)
                parse_timer = latency_tracker.start("response_parse_ms")

            if score < resolved_threshold:
                continue

            if resolved_allowed_categories and not is_allowed_category(
                category, resolved_allowed_categories
            ):
                continue

            guild_flagged_categories.append((normalized_category, score))

        latency_tracker.stop("response_parse_ms", parse_timer)

        if (
            ADD_SFW_VECTOR
            and image is not None
            and clip_vectors.is_available()
            and _should_add_sfw_vector(flagged_any, skip_vector_add, max_similarity)
        ):
            vector_started = latency_tracker.start("vector_add_ms")
            await asyncio.to_thread(
                clip_vectors.add_vector,
                image,
                metadata={"category": None, "score": 0},
            )
            latency_tracker.stop("vector_add_ms", vector_started)

        if guild_flagged_categories:
            guild_flagged_categories.sort(key=lambda item: item[1], reverse=True)
            best_category, best_score = guild_flagged_categories[0]
            latency_tracker.record_success()
            return _finalize(
                {
                    "is_nsfw": True,
                    "category": best_category,
                    "score": best_score,
                    "reason": "openai_moderation",
                    "threshold": resolved_threshold,
                    "summary_categories": summary_categories,
                }
            )

        latency_tracker.record_success()
        return _finalize(
            {
                "is_nsfw": False,
                "reason": "openai_moderation",
                "flagged_any": flagged_any,
                "threshold": resolved_threshold,
                "summary_categories": summary_categories,
            }
        )

    if had_openai_timeout:
        result["reason"] = "openai_moderation_timeout"
    elif had_http_timeout:
        result["reason"] = "openai_moderation_http_timeout"

    return _finalize(result)
