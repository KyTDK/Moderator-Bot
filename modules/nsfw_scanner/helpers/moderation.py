import asyncio
import base64
import logging
import os
import time
from typing import Any, Callable, Optional
from urllib.parse import urlparse

import httpx
import openai
from PIL import Image

from modules.nsfw_scanner.settings_keys import (
    NSFW_HIGH_ACCURACY_SETTING,
    NSFW_IMAGE_CATEGORY_SETTING,
    NSFW_TEXT_CATEGORY_SETTING,
    NSFW_TEXT_ENABLED_SETTING,
    NSFW_TEXT_THRESHOLD_SETTING,
    NSFW_THRESHOLD_SETTING,
)
from modules.utils import api, clip_vectors, mysql, text_vectors

from ..constants import ADD_SFW_VECTOR, MOD_API_MAX_CONCURRENCY, SFW_VECTOR_MAX_SIMILARITY
from ..utils.categories import is_allowed_category
from .latency import ModeratorLatencyTracker
from .payloads import (
    VIDEO_FRAME_MAX_EDGE,
    VIDEO_FRAME_TARGET_BYTES,
    prepare_image_payload,
)
from .vector_tasks import (
    schedule_text_vector_add,
    schedule_vector_add,
)
log = logging.getLogger(__name__)

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


_MODERATION_API_SEMAPHORE = asyncio.Semaphore(max(1, MOD_API_MAX_CONCURRENCY))


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
    on_rate_limiter_acquire: Optional[Callable[[float], None]] = None,
    on_rate_limiter_release: Optional[Callable[[float], None]] = None,
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
    if text:
        latency_tracker.ensure_payload_detail("text_chars", len(text))
    latency_tracker.ensure_payload_detail("attempt_limit", max_attempts)

    def _finalize(payload: dict[str, Any]) -> dict[str, Any]:
        return latency_tracker.finalize(payload)

    text_preview = None
    truncated_preview = None
    use_text_settings = bool(text and not has_image_input)
    if use_text_settings:
        inputs = text
        text_preview = text[:256]
        truncated_preview = text_preview
        if len(text) > len(text_preview):
            truncated_preview = text_preview.rstrip() + "..."

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
            max_edge_override = VIDEO_FRAME_MAX_EDGE
            target_bytes_override = VIDEO_FRAME_TARGET_BYTES
            latency_tracker.set_payload_detail("video_frame", True)
        try:
            prepared = await prepare_image_payload(
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
        requested_settings = [NSFW_IMAGE_CATEGORY_SETTING, NSFW_THRESHOLD_SETTING]
        if use_text_settings:
            requested_settings.extend(
                [NSFW_TEXT_CATEGORY_SETTING, NSFW_TEXT_THRESHOLD_SETTING, NSFW_TEXT_ENABLED_SETTING]
            )

        settings_map = await mysql.get_settings(guild_id, requested_settings)

    if resolved_allowed_categories is None:
        candidate_categories = []
        if use_text_settings:
            candidate_categories = (settings_map or {}).get(
                NSFW_TEXT_CATEGORY_SETTING, []
            ) or []
        if not candidate_categories:
            candidate_categories = (settings_map or {}).get(
                NSFW_IMAGE_CATEGORY_SETTING, []
            ) or []
        resolved_allowed_categories = candidate_categories

    if resolved_threshold is None:
        threshold_value = None
        if use_text_settings:
            threshold_value = (settings_map or {}).get(NSFW_TEXT_THRESHOLD_SETTING)
        if threshold_value is None:
            threshold_value = (settings_map or {}).get(NSFW_THRESHOLD_SETTING, 0.7)
        try:
            resolved_threshold = float(threshold_value)
        except (TypeError, ValueError):
            resolved_threshold = 0.7

    if resolved_allowed_categories is None:
        resolved_allowed_categories = []
    if resolved_threshold is None:
        resolved_threshold = 0.7

    had_openai_timeout = False
    had_http_timeout = False
    had_connection_error = False

    def _build_error_context(
        *,
        exc: Exception,
        attempt_number: int,
        request_model: str | None,
    ) -> str:
        context_parts: list[str] = [
            f"attempt={attempt_number}/{max_attempts}",
            f"exception_type={type(exc).__name__}",
        ]
        status_code = getattr(exc, "status_code", None)
        if not status_code:
            response = getattr(exc, "response", None)
            status_code = getattr(response, "status_code", None)
        if status_code:
            context_parts.append(f"status_code={status_code}")
        if request_model:
            context_parts.append(f"model={request_model}")
        context_parts.append(f"has_image_input={has_image_input}")
        if has_image_input and image_state:
            context_parts.append(f"image_remote={bool(image_state.get('use_remote'))}")
            payload_bytes = image_state.get("payload_bytes")
            if isinstance(payload_bytes, (bytes, bytearray)):
                context_parts.append(f"image_payload_bytes={len(payload_bytes)}")
            payload_mime = image_state.get("payload_mime")
            if payload_mime:
                context_parts.append(f"image_payload_mime={payload_mime}")
            source_url = image_state.get("source_url")
            if source_url:
                context_parts.append(
                    f"image_source_host={urlparse(source_url).netloc or 'unknown'}"
                )
        if isinstance(payload_metadata, dict):
            message_id = payload_metadata.get("message_id")
            if message_id is not None:
                context_parts.append(f"message_id={message_id}")
            if payload_metadata.get("video_frame"):
                context_parts.append("video_frame=True")
            source_url = payload_metadata.get("source_url")
            if source_url:
                context_parts.append(
                    f"payload_source_host={urlparse(source_url).netloc or 'unknown'}"
                )
        request_id = getattr(exc, "request_id", None)
        if request_id:
            context_parts.append(f"request_id={request_id}")
        response = getattr(exc, "response", None)
        if response is not None:
            retry_after = response.headers.get("retry-after")
            if retry_after:
                context_parts.append(f"retry_after={retry_after}")
            try:
                body_preview = response.text
            except Exception:
                body_preview = None
            if body_preview:
                sanitized_preview = body_preview[:256].replace("\n", " ")
                context_parts.append(
                    f"response_body_preview={sanitized_preview}"
                )
        return ", ".join(context_parts)

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
        request_model: str | None = None
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
            request_model = "omni-moderation-latest"
            latency_tracker.ensure_payload_detail("request_model", request_model)
            response = None
            limiter_wait_started = time.perf_counter()
            async with _MODERATION_API_SEMAPHORE:
                limiter_acquired_at = time.perf_counter()
                if on_rate_limiter_acquire is not None:
                    try:
                        on_rate_limiter_acquire(limiter_acquired_at - limiter_wait_started)
                    except Exception:
                        log.debug("Rate limiter acquire callback failed", exc_info=True)
                api_started = latency_tracker.start("api_call_ms")
                try:
                    response = await moderations_resource.create(
                        model=request_model,
                        input=inputs,
                    )
                finally:
                    latency_tracker.stop("api_call_ms", api_started)
                    if on_rate_limiter_release is not None:
                        try:
                            on_rate_limiter_release(time.perf_counter() - limiter_acquired_at)
                        except Exception:
                            log.debug("Rate limiter release callback failed", exc_info=True)
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
        except (openai.APIConnectionError, httpx.RemoteProtocolError) as exc:
            latency_tracker.stop("api_call_ms", api_started)
            print(
                "[moderator_api] Connection error during moderation request on attempt "
                f"{attempt_number}/{max_attempts}: {exc}."
            )
            had_connection_error = True
            latency_tracker.record_failure("api_connection_error")
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except openai.InternalServerError as exc:
            latency_tracker.stop("api_call_ms", api_started)
            context_summary = _build_error_context(
                exc=exc,
                attempt_number=attempt_number,
                request_model=request_model,
            )
            if (
                image_state
                and not image_state.get("use_remote")
                and image_state.get("source_url")
                and _ALLOW_REMOTE_IMAGES
                and attempt_index < max_attempts - 1
            ):
                log.debug(
                    "Inline moderation payload triggered internal server error; retrying with remote URL. Context: %s",
                    context_summary,
                    exc_info=True,
                )
                image_state["use_remote"] = True
                latency_tracker.record_failure("inline_internal_server_error")
                latency_tracker.set_payload_detail("remote_retry_due_to_internal_error", True)
                if isinstance(payload_metadata, dict):
                    payload_metadata["remote_retry_due_to_internal_error"] = True
                continue
            print(
                "[moderator_api] OpenAI internal server error: "
                f"{exc}. Context: {context_summary}."
            )
            log.error(
                "Internal server error from OpenAI moderation API. Context: %s",
                context_summary,
                exc_info=True,
            )
            latency_tracker.record_failure("internal_server_error")
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
            context_summary = _build_error_context(
                exc=exc,
                attempt_number=attempt_number,
                request_model=request_model,
            )
            print(
                "[moderator_api] Unexpected error from OpenAI API: "
                f"{exc}. Context: {context_summary}."
            )
            log.exception(
                "Unexpected error from OpenAI moderation API. Context: %s",
                context_summary,
            )
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

            if is_flagged and not skip_vector_add:
                if image is not None and clip_vectors.is_available():
                    latency_tracker.set_payload_detail("vector_add_async", True)
                    schedule_vector_add(
                        image,
                        {"category": normalized_category, "score": score},
                        logger=log,
                    )
                elif text and text_vectors.is_available():
                    latency_tracker.set_payload_detail("text_vector_add_async", True)
                    text_metadata = {
                        "category": normalized_category,
                        "score": score,
                    }
                    if truncated_preview:
                        text_metadata["preview"] = truncated_preview
                    if guild_id is not None:
                        text_metadata["guild_id"] = guild_id
                    if isinstance(payload_metadata, dict):
                        message_id = payload_metadata.get("message_id")
                        if message_id is not None:
                            text_metadata["message_id"] = message_id
                    schedule_text_vector_add(text, text_metadata, logger=log)

            if score < resolved_threshold:
                continue

            if resolved_allowed_categories and not is_allowed_category(
                category, resolved_allowed_categories
            ):
                continue

            guild_flagged_categories.append((normalized_category, score))

        latency_tracker.stop("response_parse_ms", parse_timer)

        if ADD_SFW_VECTOR and not flagged_any and not skip_vector_add:
            if (
                image is not None
                and clip_vectors.is_available()
                and _should_add_sfw_vector(flagged_any, skip_vector_add, max_similarity)
            ):
                latency_tracker.set_payload_detail("sfw_vector_add_async", True)
                schedule_vector_add(
                    image,
                    {"category": None, "score": 0},
                    logger=log,
                )
            elif (
                text
                and text_vectors.is_available()
                and _should_add_sfw_vector(flagged_any, skip_vector_add, max_similarity)
            ):
                latency_tracker.set_payload_detail("text_sfw_vector_add_async", True)
                sfw_metadata = {"category": None, "score": 0}
                if truncated_preview:
                    sfw_metadata["preview"] = truncated_preview
                if guild_id is not None:
                    sfw_metadata["guild_id"] = guild_id
                if isinstance(payload_metadata, dict):
                    message_id = payload_metadata.get("message_id")
                    if message_id is not None:
                        sfw_metadata["message_id"] = message_id
                schedule_text_vector_add(text, sfw_metadata, logger=log)

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
    elif had_connection_error:
        result["reason"] = "openai_moderation_connection_error"

    return _finalize(result)
