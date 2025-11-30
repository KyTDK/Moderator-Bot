import os
from tempfile import gettempdir

from dotenv import load_dotenv

load_dotenv()


def _int_from_env(name: str, default: int | None, *, allow_none: bool = False) -> int | None:
    raw = os.getenv(name)
    if raw is None:
        return default
    cleaned = raw.strip().lower()
    if allow_none and cleaned in {"none", "null", "unlimited"}:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


GUILD_ID = int(os.getenv("GUILD_ID", 0))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 0))


def _parse_allowed_user_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    result: list[int] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.append(int(part))
        except ValueError:
            continue
    return result


ALLOWED_USER_IDS = _parse_allowed_user_ids(os.getenv("ALLOWED_USER_IDS"))

TMP_DIR = os.path.join(gettempdir(), "modbot")
os.makedirs(TMP_DIR, exist_ok=True)

_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "ModeratorBot/1.0 Chrome/124.0.0.0 Safari/537.36"
)


def _resolve_user_agent(raw: str | None) -> str:
    if not raw:
        return _DEFAULT_USER_AGENT
    cleaned = raw.strip()
    return cleaned or _DEFAULT_USER_AGENT


NSFW_SCANNER_USER_AGENT = _resolve_user_agent(os.getenv("NSFW_SCANNER_USER_AGENT"))
NSFW_SCANNER_DEFAULT_HEADERS = {
    "User-Agent": NSFW_SCANNER_USER_AGENT,
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
}

# Threshold for similarity search
_CLIP_THRESHOLD_RAW = os.getenv("CLIP_THRESHOLD")
try:
    CLIP_THRESHOLD = float(_CLIP_THRESHOLD_RAW) if _CLIP_THRESHOLD_RAW is not None else 0.90
except (TypeError, ValueError):
    CLIP_THRESHOLD = 0.90

_TEXT_THRESHOLD_RAW = os.getenv("TEXT_SIMILARITY_THRESHOLD")
try:
    TEXT_SIMILARITY_THRESHOLD = (
        float(_TEXT_THRESHOLD_RAW) if _TEXT_THRESHOLD_RAW is not None else 0.75
    )
except (TypeError, ValueError):
    TEXT_SIMILARITY_THRESHOLD = 0.75
HIGH_ACCURACY_SIMILARITY = 0.95  # Min similarity to skip API when high-accuracy is enabled
# Max frames per video
MAX_FRAMES_PER_VIDEO = _int_from_env("MAX_FRAMES_PER_VIDEO", 5)
ACCELERATED_MAX_FRAMES_PER_VIDEO = _int_from_env("ACCELERATED_MAX_FRAMES_PER_VIDEO", 100)
ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO = _int_from_env("ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO", 300)
ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO = _int_from_env("ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO", None, allow_none=True)
# Download caps
DEFAULT_DOWNLOAD_CAP_BYTES = _int_from_env("DEFAULT_DOWNLOAD_CAP_BYTES", 128 * 1024 * 1024)  # 128 MiB
ACCELERATED_DOWNLOAD_CAP_BYTES = _int_from_env("ACCELERATED_DOWNLOAD_CAP_BYTES", 256 * 1024 * 1024)  # 256 MiB
ACCELERATED_PRO_DOWNLOAD_CAP_BYTES = _int_from_env("ACCELERATED_PRO_DOWNLOAD_CAP_BYTES", 512 * 1024 * 1024)  # 512 MiB
ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES = _int_from_env("ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES", None, allow_none=True)  # Unlimited
# Make concurrent frames
MAX_CONCURRENT_FRAMES = _int_from_env("MAX_CONCURRENT_FRAMES", 5)
ACCELERATED_MAX_CONCURRENT_FRAMES = _int_from_env("ACCELERATED_MAX_CONCURRENT_FRAMES", 24)
ACCELERATED_PRO_CONCURRENT_FRAMES = _int_from_env("ACCELERATED_PRO_CONCURRENT_FRAMES", 28)
ACCELERATED_ULTRA_CONCURRENT_FRAMES = _int_from_env("ACCELERATED_ULTRA_CONCURRENT_FRAMES", 32)
# Queue/batch safety caps
FREE_VIDEO_MAX_CONCURRENCY_CAP = _int_from_env("FREE_VIDEO_MAX_CONCURRENCY_CAP", 4)
FREE_VIDEO_BATCH_CAP = _int_from_env("FREE_VIDEO_BATCH_CAP", 4)
ACCELERATED_VIDEO_BATCH_CAP = _int_from_env("ACCELERATED_VIDEO_BATCH_CAP", 16)
VIDEO_MAX_BATCH_CAP = _int_from_env("VIDEO_MAX_BATCH_CAP", 16)
ADD_SFW_VECTOR = True  # Add SFW vectors to the index
SFW_VECTOR_MAX_SIMILARITY = 0.7  # Only add SFW vectors when similarity is low
try:
    VECTOR_REFRESH_DIVISOR = int(os.getenv("VECTOR_REFRESH_DIVISOR", "0"))
except (TypeError, ValueError):
    VECTOR_REFRESH_DIVISOR = 0
try:
    MOD_API_MAX_CONCURRENCY = int(os.getenv("MOD_API_MAX_CONCURRENCY", "10"))
except (TypeError, ValueError):
    MOD_API_MAX_CONCURRENCY = 10
try:
    _ACCEL_CONCURRENCY_RAW = os.getenv("ACCELERATED_MOD_API_MAX_CONCURRENCY")
    if _ACCEL_CONCURRENCY_RAW is None:
        ACCELERATED_MOD_API_MAX_CONCURRENCY = max(24, MOD_API_MAX_CONCURRENCY * 4)
    else:
        ACCELERATED_MOD_API_MAX_CONCURRENCY = int(_ACCEL_CONCURRENCY_RAW)
except (TypeError, ValueError):
    ACCELERATED_MOD_API_MAX_CONCURRENCY = max(24, MOD_API_MAX_CONCURRENCY * 4)
