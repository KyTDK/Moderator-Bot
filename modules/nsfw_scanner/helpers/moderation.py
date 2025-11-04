import asyncio
import logging
import time
from typing import Any, Callable, Optional

import httpx
import openai
from PIL import Image

from modules.utils import api, clip_vectors, text_vectors

from ..constants import ADD_SFW_VECTOR, MOD_API_MAX_CONCURRENCY
from ..utils.categories import is_allowed_category
from .latency import ModeratorLatencyTracker
from .moderation_context import RemoteFallbackContext
from .moderation_errors import build_error_context, format_exception_for_log
from .moderation_logging import report_moderation_fallback_to_log
from .moderation_state import ImageModerationState
from .moderation_utils import (
    resolve_moderation_settings,
    should_add_sfw_vector,
)
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


_MODERATION_API_SEMAPHORE = asyncio.Semaphore(max(1, MOD_API_MAX_CONCURRENCY))

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

    image_state: ImageModerationState | None = None
    payload_dict: dict[str, Any] | None = (
        payload_metadata if isinstance(payload_metadata, dict) else None
    )
    metadata_dict: dict[str, Any] | None = payload_dict
    if metadata_dict is not None and guild_id is not None:
        metadata_dict.setdefault("guild_id", guild_id)
    max_edge_override = None
    target_bytes_override = None

    fallback_ctx = RemoteFallbackContext(
        scanner=scanner,
        has_image_input=has_image_input,
        image_state=image_state,
        latency_tracker=latency_tracker,
        payload_metadata=payload_dict,
        metadata_dict=metadata_dict,
        max_attempts=max_attempts,
    )

    if has_image_input:
        payload_timer = latency_tracker.start("payload_prepare_ms")
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

        source_url = payload_metadata.get("source_url") if isinstance(payload_metadata, dict) else None
        quality_label = "passthrough" if prepared.strategy == "passthrough" else None
        image_state = ImageModerationState.from_prepared_payload(
            prepared,
            latency_tracker=latency_tracker,
            payload_metadata=metadata_dict,
            source_url=source_url,
            quality_label=quality_label,
        )
        fallback_ctx.image_state = image_state

        inputs = image_state.build_inputs(latency_tracker)

    if not inputs:
        print("[moderator_api] No inputs were provided")
        return _finalize(result)

    resolved_allowed_categories, resolved_threshold = await resolve_moderation_settings(
        guild_id=guild_id,
        use_text_settings=use_text_settings,
        allowed_categories=allowed_categories,
        threshold=threshold,
    )

    had_openai_timeout = False
    had_http_timeout = False
    had_connection_error = False
    had_internal_error = False

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
                if not isinstance(image_state, ImageModerationState):
                    print("[moderator_api] Unable to build image payload for moderation request.")
                    latency_tracker.record_failure("no_image_payload")
                    break
                inputs = image_state.build_inputs(latency_tracker)
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
            error_message = format_exception_for_log(exc)
            penalty = await api.mark_api_key_rate_limited(
                encrypted_key,
                cooldown=None,
            )
            if penalty is not None:
                cooldown_seconds = penalty.cooldown_seconds
                cooldown_fragment = (
                    "Putting API key on cooldown for "
                    f"{int(round(cooldown_seconds))}s "
                    f"(strike {penalty.strike_count})."
                )
            else:
                cooldown_fragment = "Putting API key on cooldown."
            print(
                "[moderator_api] Rate limit error on attempt "
                f"{attempt_number}/{max_attempts}: {error_message}. "
                f"{cooldown_fragment}"
            )
            latency_tracker.record_failure("rate_limit_error")
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except openai.BadRequestError as exc:
            latency_tracker.stop("api_call_ms", api_started)
            error_message = format_exception_for_log(exc)
            latency_tracker.record_failure("bad_request_error")
            remote_handled = await fallback_ctx.handle_remote_inline_fallback(
                label="remote_bad_request",
                error_message=error_message,
                attempt_number=attempt_number,
            )
            if remote_handled:
                print(
                    "[moderator_api] Bad request on attempt "
                    f"{attempt_number}/{max_attempts}: {error_message}. "
                    "Retrying with inline payload."
                )
                continue
            print(
                "[moderator_api] Bad request on attempt "
                f"{attempt_number}/{max_attempts}: {error_message}."
            )
            continue
        except (openai.APIConnectionError, httpx.RemoteProtocolError) as exc:
            latency_tracker.stop("api_call_ms", api_started)
            context_summary = build_error_context(
                exc=exc,
                attempt_number=attempt_number,
                max_attempts=max_attempts,
                request_model=request_model,
                has_image_input=has_image_input,
                image_state=image_state if isinstance(image_state, ImageModerationState) else None,
                payload_metadata=payload_metadata if isinstance(payload_metadata, dict) else None,
            )
            error_message = format_exception_for_log(exc)
            had_connection_error = True
            latency_tracker.record_failure("api_connection_error")
            remote_handled = await fallback_ctx.handle_remote_inline_fallback(
                label="remote_connection_error",
                error_message=f"{error_message}. Context: {context_summary}",
                attempt_number=attempt_number,
                context_summary=context_summary,
            )
            if remote_handled:
                print(
                    "[moderator_api] Connection error during moderation request on attempt "
                    f"{attempt_number}/{max_attempts}: {error_message}. "
                    "Retrying with inline payload."
                )
                continue
            print(
                "[moderator_api] Connection error during moderation request on attempt "
                f"{attempt_number}/{max_attempts}: {error_message}. Context: {context_summary}"
            )
            log.debug(
                "Connection error during moderation request. Context: %s",
                context_summary,
                exc_info=True,
            )
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except openai.InternalServerError as exc:
            latency_tracker.stop("api_call_ms", api_started)
            context_summary = build_error_context(
                exc=exc,
                attempt_number=attempt_number,
                max_attempts=max_attempts,
                request_model=request_model,
                has_image_input=has_image_input,
                image_state=image_state if isinstance(image_state, ImageModerationState) else None,
                payload_metadata=payload_metadata if isinstance(payload_metadata, dict) else None,
            )
            fallback_ctx.record_fallback_context("internal_server_error", context_summary)
            had_internal_error = True
            error_message = format_exception_for_log(exc)
            remote_handled = await fallback_ctx.handle_remote_inline_fallback(
                label="remote_internal_error",
                error_message=f"{error_message}. Context: {context_summary}",
                attempt_number=attempt_number,
                context_summary=context_summary,
                failure_reason="internal_server_error",
            )
            if remote_handled:
                print(
                    "[moderator_api] Internal server error from remote payload on attempt "
                    f"{attempt_number}/{max_attempts}: {error_message}. "
                    "Retrying with inline payload."
                )
                continue
            current_payload_mime = None
            if isinstance(image_state, ImageModerationState):
                current_payload_mime = image_state.payload_mime
            can_retry_with_png = (
                has_image_input
                and isinstance(image_state, ImageModerationState)
                and not image_state.png_retry_attempted
                and attempt_index < max_attempts - 1
                and current_payload_mime != "image/png"
            )
            if (
                current_payload_mime == "image/png"
                and attempt_index < max_attempts - 1
            ):
                latency_tracker.record_failure("internal_server_error")
                latency_tracker.set_payload_detail("internal_retry", True)
                if isinstance(payload_metadata, dict):
                    payload_metadata["internal_retry"] = True
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
                continue
            if can_retry_with_png:
                log.debug(
                    "Inline moderation payload triggered internal server error; retrying with PNG payload. Context: %s",
                    context_summary,
                    exc_info=True,
                )
                image_state.png_retry_attempted = True
                image_state.mark_fallback("png_retry")
                fallback_ctx.record_fallback_context("png_retry", context_summary)
                try:
                    png_prepared = await prepare_image_payload(
                        image=image,
                        image_bytes=image_bytes,
                        image_path=image_path,
                        image_mime=image_mime,
                        original_size=original_size,
                        max_image_edge=max_edge_override,
                        jpeg_target_bytes=target_bytes_override,
                        target_format="png",
                    )
                except Exception as png_exc:
                    log.debug(
                        "PNG fallback payload preparation failed: %s",
                        png_exc,
                        exc_info=True,
                    )
                else:
                    image_state.refresh_payload(
                        png_prepared,
                        latency_tracker=latency_tracker,
                        payload_metadata=metadata_dict,
                        quality_label="png",
                    )
                    latency_tracker.set_payload_detail("png_retry_due_to_internal_error", True)
                    if isinstance(payload_metadata, dict):
                        payload_metadata["png_retry_due_to_internal_error"] = True
                    latency_tracker.record_failure("internal_server_error")
                    continue
            print(
                "[moderator_api] OpenAI internal server error: "
                f"{error_message}. Context: {context_summary}."
            )
            log.error(
                "Internal server error from OpenAI moderation API (%s). Context: %s",
                error_message,
                context_summary,
                exc_info=True,
            )
            latency_tracker.record_failure("internal_server_error")
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except openai.APITimeoutError as exc:
            latency_tracker.stop("api_call_ms", api_started)
            error_message = format_exception_for_log(exc)
            had_openai_timeout = True
            latency_tracker.record_failure("openai_timeout")
            remote_handled = await fallback_ctx.handle_remote_inline_fallback(
                label="remote_openai_timeout",
                error_message=error_message,
                attempt_number=attempt_number,
            )
            if remote_handled:
                print(
                    f"[moderator_api] Moderation request timed out on attempt "
                    f"{attempt_number}/{max_attempts}: {error_message}. "
                    "Retrying with inline payload."
                )
                continue
            print(
                f"[moderator_api] Moderation request timed out on attempt "
                f"{attempt_number}/{max_attempts}: {error_message}."
            )
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except httpx.TimeoutException as exc:
            latency_tracker.stop("api_call_ms", api_started)
            error_message = format_exception_for_log(exc)
            had_http_timeout = True
            latency_tracker.record_failure("http_timeout")
            remote_handled = await fallback_ctx.handle_remote_inline_fallback(
                label="remote_http_timeout",
                error_message=error_message,
                attempt_number=attempt_number,
            )
            if remote_handled:
                print(
                    f"[moderator_api] HTTP timeout during moderation request on attempt "
                    f"{attempt_number}/{max_attempts}: {error_message}. "
                    "Retrying with inline payload."
                )
                continue
            print(
                f"[moderator_api] HTTP timeout during moderation request on attempt "
                f"{attempt_number}/{max_attempts}: {error_message}."
            )
            if attempt_index < max_attempts - 1:
                await asyncio.sleep(min(2 ** attempt_index, 5.0))
            continue
        except Exception as exc:
            latency_tracker.stop("api_call_ms", api_started)
            context_summary = build_error_context(
                exc=exc,
                attempt_number=attempt_number,
                max_attempts=max_attempts,
                request_model=request_model,
                has_image_input=has_image_input,
                image_state=image_state,
                payload_metadata=payload_metadata if isinstance(payload_metadata, dict) else None,
            )
            error_message = format_exception_for_log(exc)
            latency_tracker.record_failure("unexpected_api_error")
            remote_handled = await fallback_ctx.handle_remote_inline_fallback(
                label="remote_unexpected_error",
                error_message=f"{error_message}. Context: {context_summary}",
                attempt_number=attempt_number,
                context_summary=context_summary,
            )
            if remote_handled:
                print(
                    "[moderator_api] Unexpected error from OpenAI API for remote payload on attempt "
                    f"{attempt_number}/{max_attempts}: {error_message}. "
                    "Retrying with inline payload."
                )
                continue
            print(
                "[moderator_api] Unexpected error from OpenAI API: "
                f"{error_message}. Context: {context_summary}."
            )
            log.exception(
                "Unexpected error from OpenAI moderation API (%s). Context: %s",
                error_message,
                context_summary,
            )
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
                    latency_tracker.set_payload_detail("text_vector_added", True)

            if score < resolved_threshold:
                continue

            if resolved_allowed_categories and not is_allowed_category(
                category, resolved_allowed_categories
            ):
                continue

            guild_flagged_categories.append((normalized_category, score))

        latency_tracker.stop("response_parse_ms", parse_timer)

        fallback_notice: str | None = None
        if has_image_input and isinstance(image_state, ImageModerationState):
            fallback_notice = image_state.fallback_message()
            if fallback_notice:
                log.info("[moderator_api] %s", fallback_notice)
                latency_tracker.set_payload_detail("fallback_notice", fallback_notice)
                if isinstance(payload_metadata, dict):
                    payload_metadata["fallback_notice"] = fallback_notice
                if isinstance(metadata_dict, dict):
                    metadata_dict["moderation_tracker"] = latency_tracker.snapshot()
                try:
                    await report_moderation_fallback_to_log(
                        scanner,
                        fallback_notice=fallback_notice,
                        image_state=image_state,
                        payload_metadata=metadata_dict,
                    )
                except Exception:
                    guild_for_log = None
                    if isinstance(metadata_dict, dict):
                        guild_for_log = metadata_dict.get("guild_id")
                    log.debug(
                        "Failed to schedule moderation fallback log for guild %s",
                        guild_for_log,
                        exc_info=True,
                    )

        if ADD_SFW_VECTOR and not flagged_any and not skip_vector_add:
            if (
                image is not None
                and clip_vectors.is_available()
                and should_add_sfw_vector(flagged_any, skip_vector_add, max_similarity)
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
                and should_add_sfw_vector(flagged_any, skip_vector_add, max_similarity)
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
                latency_tracker.set_payload_detail("text_vector_added", True)

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
                    **({"fallback_notice": fallback_notice} if fallback_notice else {}),
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
                **({"fallback_notice": fallback_notice} if fallback_notice else {}),
            }
        )

    if had_openai_timeout:
        result["reason"] = "openai_moderation_timeout"
    elif had_http_timeout:
        result["reason"] = "openai_moderation_http_timeout"
    elif had_connection_error:
        result["reason"] = "openai_moderation_connection_error"
    elif had_internal_error:
        result["reason"] = "openai_moderation_internal_error"

    return _finalize(result)
