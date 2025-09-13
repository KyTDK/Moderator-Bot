"""AI utilities package

Public surface re-exports the most used helpers so callers can do:

    from modules.ai import (
        get_model_limit, pick_model, budget_allows,
        get_price_per_mtok, run_parsed_ai,
    )
"""

from .mod_utils import (
    get_price_per_mtok,
    get_model_limit,
    pick_model,
    budget_allows,
)
from .engine import run_parsed_ai

__all__ = [
    "get_price_per_mtok",
    "get_model_limit",
    "pick_model",
    "budget_allows",
    "run_parsed_ai",
]

