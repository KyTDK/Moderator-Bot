"""Centralized cost and budget constants for AI moderation.

Keep all pricing knobs and default limits here so budgeting logic stays consistent.
"""

# Default monthly budget limits (USD)
ACCELERATED_BUDGET_LIMIT_USD: float = 2.00 # Base budget for accelerated plan
ACCELERATED_PRO_BUDGET_LIMIT_USD: float = 4.00 # x2 the base
ACCELERATED_ULTRA_BUDGET_LIMIT_USD: float = 10.00 # x5 the base

# Model pricing per million tokens (USD/MTok)
PRICES_PER_MTOK: dict[str, float] = {
    "gpt-5-nano": 0.45,
    "gpt-5-mini": 2.25,
}

# Transcription pricing (USD per minute of audio)
TRANSCRIPTION_PRICE_PER_MINUTE_USD: float = 0.003
LOCAL_TRANSCRIPTION_PRICE_PER_MINUTE_USD: float = 0.0003

# Fraction of the model context window we allow to use for prompts
MAX_CONTEXT_USAGE_FRACTION: float = 0.9

