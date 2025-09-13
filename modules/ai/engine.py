from __future__ import annotations

from typing import Any, Optional, Type


try:
    from openai import AsyncOpenAI
except Exception:  # pragma: no cover
    AsyncOpenAI = None  # type: ignore


async def run_parsed_ai(
    *,
    api_key: str,
    model: str,
    system_prompt: str,
    user_prompt: str,
    text_format: Type[Any],
):
    """Call OpenAI Responses API with parse, returning the parsed object or None on error.

    Adds minimal reasoning when using gpt-5 family models.
    """
    if AsyncOpenAI is None:
        raise RuntimeError("OpenAI Async client unavailable")

    client = AsyncOpenAI(api_key=api_key)
    kwargs = {
        "model": model,
        "instructions": system_prompt,
        "input": user_prompt,
        "text_format": text_format,
    }
    if model.startswith("gpt-5"):
        kwargs["reasoning"] = {"effort": "minimal"}

    completion = await client.responses.parse(**kwargs)
    # Typed at call site
    return completion.output_parsed

