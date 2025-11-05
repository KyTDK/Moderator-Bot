"""FAQ feature package providing storage, vector search, and service helpers."""

from .config import FAQStreamConfig
from .constants import DEFAULT_FAQ_SIMILARITY_THRESHOLD
from .models import FAQEntry, FAQSearchResult
from .service import (
    FAQLimitError,
    FAQEntryNotFoundError,
    add_faq_entry,
    delete_faq_entry,
    list_faq_entries,
    find_best_faq_answer,
    configure_developer_logging,
)
from .settings_keys import FAQ_ENABLED_SETTING, FAQ_THRESHOLD_SETTING, FAQ_DIRECT_REPLY_SETTING
from .stream import FAQStreamProcessor

__all__ = [
    "FAQEntry",
    "FAQSearchResult",
    "FAQLimitError",
    "FAQEntryNotFoundError",
    "add_faq_entry",
    "delete_faq_entry",
    "list_faq_entries",
    "find_best_faq_answer",
    "configure_developer_logging",
    "FAQ_ENABLED_SETTING",
    "FAQ_THRESHOLD_SETTING",
    "FAQ_DIRECT_REPLY_SETTING",
    "DEFAULT_FAQ_SIMILARITY_THRESHOLD",
    "FAQStreamConfig",
    "FAQStreamProcessor",
]
