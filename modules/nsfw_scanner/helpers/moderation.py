import asyncio
import base64
import io
import logging
import os
import time
from collections import Counter
from dataclasses import dataclass
from typing import Any, Awaitable
from urllib.parse import urlparse

import httpx
import openai
from PIL import Image

from cogs.nsfw import NSFW_CATEGORY_SETTING
from modules.utils import api, clip_vectors, mysql

from ..constants import ADD_SFW_VECTOR, SFW_VECTOR_MAX_SIMILARITY
from ..utils.categories import is_allowed_category
log = logging.getLogger(__name__)

_RESAMPLING_FILTER = getattr(getattr(Image, "Resampling", Image), "LANCZOS", Image.BICUBIC)

_MAX_IMAGE_EDGE = int(os.getenv("MODBOT_MODERATION_MAX_IMAGE_EDGE", "1536"))
_INLINE_PASSTHROUGH_BYTES = int(os.getenv("MODBOT_MODERATION_INLINE_THRESHOLD", "262144"))
_JPEG_TARGET_BYTES = int(os.getenv("MODBOT_MODERATION_TARGET_BYTES", "1250000"))
_JPEG_INITIAL_QUALITY = int(os.getenv("MODBOT_MODERATION_JPEG_QUALITY", "82"))
_JPEG_MIN_QUALITY = int(os.getenv("MODBOT_MODERATION_MIN_JPEG_QUALITY", "58"))
_VIDEO_FRAME_MAX_EDGE = int(os.getenv("MODBOT_MODERATION_VIDEO_MAX_EDGE", "768"))
_VIDEO_FRAME_TARGET_BYTES = int(os.getenv("MODBOT_MODERATION_VIDEO_TARGET_BYTES", "350000"))

_ALLOW_REMOTE_IMAGES = os.getenv("MODBOT_ENABLE_REMOTE_IMAGE_URLS", "1").lower() not in {
    "0",
    "false",
    "no",
    "off",
}
_REMOTE_ALLOWED_HOSTS = {
    "cdn.discordapp.com",
    "media.discordapp.net",
}
_REMOTE_MIN_BYTES = int(os.getenv("MODBOT_MODERATION_REMOTE_MIN_BYTES", "524288"))


@dataclass(slots=True)
class PreparedImagePayload:
    data: bytes
    mime: str
    width: int
    height: int
    resized: bool
    strategy: str
    quality: int | None
    original_mime: str | None


def _flatten_alpha(image: Image.Image) -> Image.Image:
    """Return an RGB image with alpha composited on white."""
    if image.mode not in {"RGBA", "LA"}:
        return image.convert("RGB") if image.mode != "RGB" else image
    base = Image.new("RGB", image.size, (255, 255, 255))
    rgb = image.convert("RGB")
    alpha = image.split()[-1]
    base.paste(rgb, mask=alpha)
    return base


def _prepare_image_payload_sync(
    *,
    image: Image.Image | None,
    image_bytes: bytes | None,
    image_path: str | None,
    image_mime: str | None,
    original_size: int | None,
    max_image_edge: int | None = None,
    jpeg_target_bytes: int | None = None,
) -> PreparedImagePayload:
    working: Image.Image | None = None
    close_working = False
    original_mime = (image_mime or "").lower() or None

    edge_limit = int(max_image_edge) if max_image_edge else _MAX_IMAGE_EDGE
    if edge_limit <= 0:
        edge_limit = _MAX_IMAGE_EDGE
    target_bytes = int(jpeg_target_bytes) if jpeg_target_bytes else _JPEG_TARGET_BYTES
    if target_bytes <= 0:
        target_bytes = _JPEG_TARGET_BYTES

    if image is not None:
        try:
            working = image.copy()
            working.load()
            close_working = True
        except Exception:
            working = None

    if working is None:
        if image_bytes is not None:
            stream = io.BytesIO(image_bytes)
            loaded = Image.open(stream)
            loaded.load()
            working = loaded
            close_working = True
        elif image_path is not None and os.path.exists(image_path):
            loaded = Image.open(image_path)
            loaded.load()
            working = loaded
            close_working = True
        else:
            raise ValueError("No image data available for moderation payload preparation")

    try:
        width, height = working.size
    except Exception as exc:
        if close_working and working is not None:
            working.close()
        raise RuntimeError("Failed to read image dimensions") from exc

    passthrough_allowed = (
        original_size is not None
        and original_size <= _INLINE_PASSTHROUGH_BYTES
        and max(width, height) <= edge_limit
        and original_mime in {"image/jpeg", "image/jpg"}
        and image_bytes is not None
    )
    if passthrough_allowed:
        data_bytes = image_bytes
        if data_bytes is None and image_path and os.path.exists(image_path):
            with open(image_path, "rb") as file_obj:
                data_bytes = file_obj.read()
        payload = PreparedImagePayload(
            data=data_bytes or b"",
            mime=image_mime or "image/jpeg",
            width=width,
            height=height,
            resized=False,
            strategy="passthrough",
            quality=None,
            original_mime=image_mime,
        )
        if close_working and working is not None:
            working.close()
        return payload

    resized = False
    max_edge = max(width, height)
    if max_edge > edge_limit and working.size[0] > 0 and working.size[1] > 0:
        scale = edge_limit / float(max_edge)
        new_size = (
            max(1, int(round(working.size[0] * scale))),
            max(1, int(round(working.size[1] * scale))),
        )
        working = working.resize(new_size, _RESAMPLING_FILTER)
        width, height = working.size
        resized = True

    prepared = _flatten_alpha(working)

    buffer = io.BytesIO()
    chosen_quality = None
    qualities = [q for q in range(_JPEG_INITIAL_QUALITY, _JPEG_MIN_QUALITY - 1, -10)]
    if qualities[-1] != _JPEG_MIN_QUALITY:
        qualities.append(_JPEG_MIN_QUALITY)
    final_bytes: bytes | None = None

    for quality in qualities:
        try:
            buffer.seek(0)
            buffer.truncate(0)
            prepared.save(
                buffer,
                format="JPEG",
                quality=max(10, min(95, quality)),
                optimize=True,
                progressive=True,
            )
        except OSError:
            buffer.seek(0)
            buffer.truncate(0)
            prepared.convert("RGB").save(
                buffer,
                format="JPEG",
                quality=max(10, min(95, quality)),
            )
        data = buffer.getvalue()
        final_bytes = data
        chosen_quality = quality
        if len(data) <= target_bytes:
            break

    if final_bytes is None:
        raise RuntimeError("Failed to encode moderation payload")

    payload = PreparedImagePayload(
        data=final_bytes,
        mime="image/jpeg",
        width=width,
        height=height,
        resized=resized,
        strategy="compressed_jpeg",
        quality=chosen_quality,
        original_mime=image_mime,
    )

    if close_working and working is not None and working is not prepared:
        working.close()
    prepared.close()
    return payload


async def _prepare_image_payload(
    *,
    image: Image.Image | None,
    image_bytes: bytes | None,
    image_path: str | None,
    image_mime: str | None,
    original_size: int | None,
    max_image_edge: int | None = None,
    jpeg_target_bytes: int | None = None,
) -> PreparedImagePayload:
    return await asyncio.to_thread(
        _prepare_image_payload_sync,
        image=image,
        image_bytes=image_bytes,
        image_path=image_path,
        image_mime=image_mime,
        original_size=original_size,
        max_image_edge=max_image_edge,
        jpeg_target_bytes=jpeg_target_bytes,
    )


def _should_use_remote_source(
    source_url: str | None,
    *,
    payload_size: int | None,
) -> bool:
    if not _ALLOW_REMOTE_IMAGES or not source_url:
        return False
    try:
        parsed = urlparse(source_url)
    except Exception:
        return False
    if parsed.scheme not in {"https"}:
        return False
    if parsed.hostname not in _REMOTE_ALLOWED_HOSTS:
        return False
    if payload_size is not None and payload_size < _REMOTE_MIN_BYTES:
        return False
    return True


def _schedule_background(coro: Awaitable[None]) -> None:
    try:
        task = asyncio.create_task(coro)
    except RuntimeError:
        log.debug("Unable to schedule background task; no running event loop")
        return

    def _suppress(task: asyncio.Task) -> None:
        try:
            task.result()
        except Exception:
            log.debug("Background moderation task raised", exc_info=True)

    task.add_done_callback(_suppress)


def _schedule_vector_add(image: Image.Image, metadata: dict[str, Any]) -> None:
    try:
        image_copy = image.copy()
        image_copy.load()
    except Exception as exc:
        log.debug("Skipping vector insert; unable to copy image: %s", exc, exc_info=True)
        return

    async def _run() -> None:
        try:
            await asyncio.to_thread(clip_vectors.add_vector, image_copy, metadata)
        except Exception:
            log.debug("Vector insert failed in background", exc_info=True)
        finally:
            try:
                image_copy.close()
            except Exception:
                pass

    _schedule_background(_run())



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
        self._payload_details: dict[str, Any] = {}

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

    def merge_payload_details(self, details: dict[str, Any] | None) -> None:
        if not details:
            return
        for key, value in details.items():
            if value is None:
                continue
            self._payload_details[key] = value

    def ensure_payload_detail(self, key: str, value: Any) -> None:
        if value is None or key in self._payload_details:
            return
        self._payload_details[key] = value

    def set_payload_detail(self, key: str, value: Any) -> None:
        if value is None:
            return
        self._payload_details[key] = value

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
        if self._payload_details:
            metadata["payload_info"] = self._payload_details

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
    payload_metadata: dict[str, Any] | None = None,
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
    latency_tracker.merge_payload_details(payload_metadata)
    latency_tracker.ensure_payload_detail("input_kind", "image" if has_image_input else "text")
    latency_tracker.ensure_payload_detail("attempt_limit", max_attempts)

    def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
        return latency_tracker.finalize(payload)

    if text and not has_image_input:
        inputs = text

    original_size = None
    if isinstance(payload_metadata, dict):
        original_size = payload_metadata.get("payload_bytes") or payload_metadata.get("source_bytes")

    image_state: dict[str, Any] | None = None

    def _build_image_inputs(state: dict[str, Any] | None) -> list[dict[str, Any]]:
        if not state:
            return []
        if state.get("use_remote") and state.get("source_url"):
            return [
                {
                    "type": "image_url",
                    "image_url": {"url": state["source_url"]},
                }
            ]

        if state.get("base64_data") is None:
            state["base64_data"] = base64.b64encode(state["payload_bytes"]).decode()
            latency_tracker.set_payload_detail("base64_chars", len(state["base64_data"]))

        return [
            {
                "type": "image_url",
                "image_url": {
                    "url": f"data:{state['payload_mime']};base64,{state['base64_data']}",
                },
            }
        ]

    if has_image_input:
        payload_timer = latency_tracker.start("payload_prepare_ms")
        max_edge_override = None
        target_bytes_override = None
        if isinstance(payload_metadata, dict) and payload_metadata.get("video_frame"):
            max_edge_override = _VIDEO_FRAME_MAX_EDGE
            target_bytes_override = _VIDEO_FRAME_TARGET_BYTES
            latency_tracker.set_payload_detail("video_frame", True)
        try:
            prepared = await _prepare_image_payload(
                image=image,
                image_bytes=image_bytes,
                image_path=image_path,
                image_mime=image_mime,
                original_size=original_size,
                max_image_edge=max_edge_override,
                jpeg_target_bytes=target_bytes_override,
            )
        except Exception as exc:
            latency_tracker.stop("payload_prepare_ms", payload_timer)
            log.warning("Failed to prepare moderation payload: %s", exc, exc_info=True)
            return _finalize(result)

        latency_tracker.stop("payload_prepare_ms", payload_timer)

        payload_bytes = prepared.data
        payload_mime = prepared.mime or "image/jpeg"
        payload_size = len(payload_bytes)

        latency_tracker.set_payload_detail("payload_width", prepared.width)
        latency_tracker.set_payload_detail("payload_height", prepared.height)
        latency_tracker.set_payload_detail("payload_bytes", payload_size)
        latency_tracker.set_payload_detail("payload_strategy", prepared.strategy)
        latency_tracker.set_payload_detail("payload_mime", payload_mime)
        if prepared.quality is not None:
            latency_tracker.set_payload_detail("payload_quality", prepared.quality)
        latency_tracker.set_payload_detail("payload_resized", prepared.resized)

        if isinstance(payload_metadata, dict):
            payload_metadata["moderation_payload_bytes"] = payload_size
            payload_metadata["moderation_payload_mime"] = payload_mime
            payload_metadata["moderation_payload_strategy"] = prepared.strategy
            payload_metadata["moderation_payload_resized"] = prepared.resized
            payload_metadata["moderation_payload_quality"] = prepared.quality

        source_url = None
        if isinstance(payload_metadata, dict):
            source_url = payload_metadata.get("source_url")

        image_state = {
            "payload_bytes": payload_bytes,
            "payload_mime": payload_mime,
            "base64_data": None,
            "use_remote": _should_use_remote_source(source_url, payload_size=payload_size),
            "source_url": source_url,
        }

        if image_state["use_remote"]:
            latency_tracker.set_payload_detail("payload_strategy", "remote_url")
        else:
            latency_tracker.set_payload_detail("payload_strategy", prepared.strategy)

        inputs = _build_image_inputs(image_state)

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
            if has_image_input:
                inputs = _build_image_inputs(image_state)
                if not inputs:
                    print("[moderator_api] Unable to build image payload for moderation request.")
                    latency_tracker.record_failure("no_image_payload")
                    break
            resource_timer = latency_tracker.start("resource_latency_ms")
            moderations_resource = await _get_moderations_resource(client)
            latency_tracker.stop("resource_latency_ms", resource_timer)
            request_model = (
                "omni-moderation-latest" if has_image_input else "text-moderation-latest"
            )
            latency_tracker.ensure_payload_detail("request_model", request_model)
            api_started = latency_tracker.start("api_call_ms")
            response = await moderations_resource.create(
                model=request_model,
                input=inputs,
            )
            latency_tracker.stop("api_call_ms", api_started)
            latency_tracker.set_payload_detail(
                "response_model", getattr(response, "model", None)
            )
            latency_tracker.set_payload_detail(
                "response_id", getattr(response, "id", None)
            )
            latency_tracker.set_payload_detail(
                "response_ms", getattr(response, "response_ms", None)
            )
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
        except openai.BadRequestError as exc:
            latency_tracker.stop("api_call_ms", api_started)
            if image_state and image_state.get("use_remote"):
                log.debug(
                    "Remote image URL moderation failed; falling back to inline payload: %s",
                    exc,
                    exc_info=True,
                )
                image_state["use_remote"] = False
                latency_tracker.record_failure("remote_bad_request")
                latency_tracker.set_payload_detail("remote_fallback", True)
                if isinstance(payload_metadata, dict):
                    payload_metadata["remote_fallback"] = True
                if attempt_index < max_attempts - 1:
                    continue
            print(
                f"[moderator_api] Bad request on attempt {attempt_number}/{max_attempts}: {exc}."
            )
            latency_tracker.record_failure("bad_request_error")
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
            if image_state and image_state.get("use_remote"):
                message = str(exc).lower()
                if "image_url" in message or "fetch" in message or "download" in message:
                    log.debug(
                        "Remote moderation fetch error detected; retrying with inline payload: %s",
                        exc,
                        exc_info=True,
                    )
                    image_state["use_remote"] = False
                    latency_tracker.record_failure("remote_fetch_error")
                    latency_tracker.set_payload_detail("remote_fallback", True)
                    if isinstance(payload_metadata, dict):
                        payload_metadata["remote_fallback"] = True
                    if attempt_index < max_attempts - 1:
                        continue
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

            if (
                is_flagged
                and image is not None
                and not skip_vector_add
                and clip_vectors.is_available()
            ):
                latency_tracker.set_payload_detail("vector_add_async", True)
                _schedule_vector_add(
                    image,
                    {"category": normalized_category, "score": score},
                )

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
            latency_tracker.set_payload_detail("sfw_vector_add_async", True)
            _schedule_vector_add(
                image,
                {"category": None, "score": 0},
            )

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
