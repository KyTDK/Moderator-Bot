from __future__ import annotations

from typing import Any

from .moderation_state import ImageModerationState

__all__ = ["build_error_context"]


def build_error_context(
    *,
    exc: Exception,
    attempt_number: int,
    max_attempts: int,
    request_model: str | None,
    has_image_input: bool,
    image_state: ImageModerationState | None,
    payload_metadata: dict[str, Any] | None,
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

    if has_image_input and image_state is not None:
        context_parts.extend(image_state.logging_details())

    if isinstance(payload_metadata, dict):
        guild_id = payload_metadata.get("guild_id")
        if guild_id is not None:
            context_parts.append(f"guild_id={guild_id}")
        channel_id = payload_metadata.get("channel_id")
        if channel_id is not None:
            context_parts.append(f"channel_id={channel_id}")
        user_id = payload_metadata.get("user_id")
        author_id = payload_metadata.get("author_id")
        if user_id is not None:
            context_parts.append(f"user_id={user_id}")
        elif author_id is not None:
            context_parts.append(f"user_id={author_id}")
        if author_id is not None and author_id != user_id:
            context_parts.append(f"author_id={author_id}")
        message_id = payload_metadata.get("message_id")
        if message_id is not None:
            context_parts.append(f"message_id={message_id}")
        message_jump_url = payload_metadata.get("message_jump_url")
        if message_jump_url:
            context_parts.append(f"message_jump_url={message_jump_url}")
        if payload_metadata.get("video_frame"):
            context_parts.append("video_frame=True")
        source_url = payload_metadata.get("source_url")
        if source_url:
            from urllib.parse import urlparse

            context_parts.append(f"source_url={source_url}")
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
        header_request_id = (
            response.headers.get("x-request-id")
            or response.headers.get("request-id")
        )
        if header_request_id:
            context_parts.append(f"header_request_id={header_request_id}")
        error_json = None
        try:
            error_json = response.json()
        except Exception:
            error_json = None
        if isinstance(error_json, dict):
            error_payload = error_json.get("error")
            if isinstance(error_payload, dict):
                error_type = error_payload.get("type")
                if error_type:
                    context_parts.append(f"error_type={error_type}")
                error_code = error_payload.get("code")
                if error_code:
                    context_parts.append(f"error_code={error_code}")
                error_message = error_payload.get("message")
                if error_message:
                    sanitized_message = error_message[:256].replace("\n", " ")
                    context_parts.append(
                        f"error_message={sanitized_message}"
                    )
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
