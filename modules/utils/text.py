import re
import unicodedata
from typing import Dict

from cleantext import clean

# Leetspeak map to help catch obfuscations
LEET_MAP = {
    "!": "i",
    "1": "i",
    "|": "l",
    "3": "e",
    "4": "a",
    "@": "a",
    "0": "o",
    "5": "s",
    "$": "s",
    "7": "t",
    "+": "t",
    "8": "b",
    "9": "g",
    "2": "z",
}

LEET_RE = re.compile("|".join(re.escape(k) for k in sorted(LEET_MAP, key=len, reverse=True)))
RE_REPEATS = re.compile(r"(.)\1{2,}")


def apply_leet(text: str) -> str:
    return LEET_RE.sub(lambda m: LEET_MAP[m.group(0)], text)


_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_MENTION_RE = re.compile(r"(@everyone|@here|<@[!&]?[0-9]+>|<#[0-9]+>)")
_CUSTOM_EMOJI_RE = re.compile(r"<a?:\w+:\d+>")


def _strip_accents_keep_non_ascii(s: str) -> str:
    """Remove diacritics from letters while keeping other non-ASCII (e.g., emojis)."""
    return "".join(ch for ch in unicodedata.normalize("NFKD", s) if not unicodedata.combining(ch))


# Zero-width and formatting characters frequently used to bypass filters
def _remove_format_controls(s: str) -> str:
    # Drop all Unicode "Other, Format" codepoints (category Cf), e.g. ZWJ/ZWNJ/VS/dir marks
    # This also removes U+FE0F/FE0E (emoji variation selectors) which are not needed for matching
    return "".join(ch for ch in s if unicodedata.category(ch) != "Cf")


# Common cross-script confusables to Latin ASCII.
# This is intentionally focused on the most abused characters for moderation bypasses.
_CONFUSABLES_TRANSLATION: Dict[int, str] = {
    # Cyrillic → Latin
    ord("А"): "A", ord("В"): "B", ord("Е"): "E", ord("К"): "K", ord("М"): "M",
    ord("Н"): "H", ord("О"): "O", ord("Р"): "P", ord("С"): "S", ord("Т"): "T",
    ord("Х"): "X", ord("У"): "Y", ord("І"): "I", ord("Ј"): "J", ord("Ѕ"): "S",
    ord("а"): "a", ord("е"): "e", ord("о"): "o", ord("р"): "p", ord("с"): "c",
    ord("т"): "t", ord("х"): "x", ord("у"): "y", ord("і"): "i", ord("ј"): "j",
    ord("ѕ"): "s",
    # Greek → Latin (only clear visual matches)
    ord("Α"): "A", ord("Β"): "B", ord("Ε"): "E", ord("Ζ"): "Z", ord("Η"): "H",
    ord("Ι"): "I", ord("Κ"): "K", ord("Μ"): "M", ord("Ν"): "N", ord("Ο"): "O",
    ord("Ρ"): "P", ord("Τ"): "T", ord("Υ"): "Y", ord("Χ"): "X", ord("Ϲ"): "C",
    ord("α"): "a", ord("ο"): "o", ord("ρ"): "p", ord("ν"): "v", ord("τ"): "t",
    ord("ι"): "i", ord("κ"): "k", ord("χ"): "x", ord("υ"): "y", ord("ϲ"): "c",
    # Latin lookalikes
    ord("ſ"): "s",  # long s
}


def _fold_confusables(s: str) -> str:
    # First, normalize compatibility forms (fullwidth, math alphanumerics) to plain letters
    s = unicodedata.normalize("NFKC", s)
    # Then apply targeted cross-script folding
    return s.translate(_CONFUSABLES_TRANSLATION)


def normalize_text(
    text: str,
    *,
    remove_urls: bool = True,
    remove_mentions: bool = True,
    remove_custom_emojis: bool = True,
    to_ascii: bool = True,
    remove_punct: bool = True,
) -> str:
    """
    Normalize free text for safer matching.

    - remove_urls/remove_mentions/remove_custom_emojis: strip or preserve Discord/URL tokens
    - to_ascii: transliterate to ASCII (drops emojis); if False, keep emojis and non-ASCII
    - remove_punct: drop punctuation (after protecting preserved tokens)
    """
    if not text:
        return ""

    # Protect tokens that we are asked to preserve using ASCII-only placeholders
    placeholders: Dict[str, str] = {}

    def protect(pattern: re.Pattern, kind: str, s: str) -> str:
        idx = 0
        def repl(m: re.Match):
            nonlocal idx
            original = m.group(0)
            ph = f"PLH{kind}{idx}X"
            idx += 1
            # Store by lowercase; we'll lowercase the text later
            placeholders[ph.lower()] = original
            return ph
        return pattern.sub(repl, s)

    # URLs
    if remove_urls:
        text = _URL_RE.sub("", text)
    else:
        text = protect(_URL_RE, "URL", text)

    # Mentions (#channels, @user, @role, @everyone, @here)
    if remove_mentions:
        text = _MENTION_RE.sub("", text)
    else:
        text = protect(_MENTION_RE, "MEN", text)

    # Custom emojis like <:name:id> or <a:name:id>
    if remove_custom_emojis:
        text = _CUSTOM_EMOJI_RE.sub("", text)
    else:
        text = protect(_CUSTOM_EMOJI_RE, "EMO", text)

    # Remove invisible format controls and normalize compatibility equivalents
    text = _remove_format_controls(text)
    text = _fold_confusables(text)

    # Transliteration / accent handling
    if to_ascii:
        # After folding confusables, ASCII-encode to drop any remaining non-ASCII safely
        text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    else:
        # Remove combining marks but keep other non-ASCII (e.g., emojis)
        text = _strip_accents_keep_non_ascii(text)

    # Flatten repeats to a maximum of two
    text = RE_REPEATS.sub(r"\1\1", text)

    # Apply simple leetspeak normalization
    text = apply_leet(text)

    # Clean up
    text = clean(
        text,
        lower=True,
        to_ascii=False,      # already handled above
        no_line_breaks=True,
        no_urls=False,
        no_emails=True,
        no_phone_numbers=True,
        no_digits=False,
        no_currency_symbols=True,
        no_punct=remove_punct,
        lang="en",
    )

    # Restore placeholders (text is lowercased by clean)
    for ph_lower, original in placeholders.items():
        text = text.replace(ph_lower, original)

    # Normalize whitespace
    text = re.sub(r"\s+", " ", text).strip()
    return text
