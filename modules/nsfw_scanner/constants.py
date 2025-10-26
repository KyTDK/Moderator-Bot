import os
from tempfile import gettempdir

from dotenv import load_dotenv

from modules.config.premium_plans import PLAN_CORE, PLAN_FREE, PLAN_PRO, PLAN_ULTRA

load_dotenv()

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


def _int_env(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default

# Threshold for similarity search
_CLIP_THRESHOLD_RAW = os.getenv("CLIP_THRESHOLD")
try:
    CLIP_THRESHOLD = float(_CLIP_THRESHOLD_RAW) if _CLIP_THRESHOLD_RAW is not None else 0.85
except (TypeError, ValueError):
    CLIP_THRESHOLD = 0.85
HIGH_ACCURACY_SIMILARITY = 0.90  # Min similarity to skip API when high-accuracy is enabled
# Max frames per video
MAX_FRAMES_PER_VIDEO = 15
ACCELERATED_MAX_FRAMES_PER_VIDEO = 240
ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO = 480
ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO = None
# Download caps
DEFAULT_DOWNLOAD_CAP_BYTES = 256 * 1024 * 1024  # 256 MiB
ACCELERATED_DOWNLOAD_CAP_BYTES = 512 * 1024 * 1024  # 512 MiB
ACCELERATED_PRO_DOWNLOAD_CAP_BYTES = 1024 * 1024 * 1024  # 1 GiB
ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES = None  # Unlimited
# Make concurrent frames
MAX_CONCURRENT_FRAMES = 12
ACCELERATED_MAX_CONCURRENT_FRAMES = 32
ACCELERATED_PRO_CONCURRENT_FRAMES = 44
ACCELERATED_ULTRA_CONCURRENT_FRAMES = 56
ADD_SFW_VECTOR = True  # Add SFW vectors to the index
SFW_VECTOR_MAX_SIMILARITY = 0.7  # Only add SFW vectors when similarity is low
try:
    VECTOR_REFRESH_DIVISOR = int(os.getenv("VECTOR_REFRESH_DIVISOR", "0"))
except (TypeError, ValueError):
    VECTOR_REFRESH_DIVISOR = 0
try:
    MOD_API_MAX_CONCURRENCY = int(os.getenv("MOD_API_MAX_CONCURRENCY", "6"))
except (TypeError, ValueError):
    MOD_API_MAX_CONCURRENCY = 6
MOD_API_MAX_CONCURRENCY = max(1, MOD_API_MAX_CONCURRENCY)

_free_default = max(3, MOD_API_MAX_CONCURRENCY // 2)
_core_default = max(4, MOD_API_MAX_CONCURRENCY - 2)
_pro_default = max(5, MOD_API_MAX_CONCURRENCY - 1)
_ultra_default = max(_pro_default + 2, MOD_API_MAX_CONCURRENCY + 4)

MOD_API_CONCURRENCY_FREE = max(1, _int_env("MOD_API_CONCURRENCY_FREE", _free_default))
MOD_API_CONCURRENCY_CORE = max(MOD_API_CONCURRENCY_FREE, _int_env("MOD_API_CONCURRENCY_CORE", _core_default))
MOD_API_CONCURRENCY_PRO = max(MOD_API_CONCURRENCY_CORE, _int_env("MOD_API_CONCURRENCY_PRO", _pro_default))
MOD_API_CONCURRENCY_ULTRA = max(MOD_API_CONCURRENCY_PRO, _int_env("MOD_API_CONCURRENCY_ULTRA", _ultra_default))

MOD_API_CONCURRENCY_BY_PLAN = {
    PLAN_FREE: MOD_API_CONCURRENCY_FREE,
    PLAN_CORE: MOD_API_CONCURRENCY_CORE,
    PLAN_PRO: MOD_API_CONCURRENCY_PRO,
    PLAN_ULTRA: MOD_API_CONCURRENCY_ULTRA,
}

__all__ = [
    "GUILD_ID",
    "LOG_CHANNEL_ID",
    "ALLOWED_USER_IDS",
    "TMP_DIR",
    "CLIP_THRESHOLD",
    "HIGH_ACCURACY_SIMILARITY",
    "MAX_FRAMES_PER_VIDEO",
    "ACCELERATED_MAX_FRAMES_PER_VIDEO",
    "ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO",
    "ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO",
    "DEFAULT_DOWNLOAD_CAP_BYTES",
    "ACCELERATED_DOWNLOAD_CAP_BYTES",
    "ACCELERATED_PRO_DOWNLOAD_CAP_BYTES",
    "ACCELERATED_ULTRA_DOWNLOAD_CAP_BYTES",
    "MAX_CONCURRENT_FRAMES",
    "ACCELERATED_MAX_CONCURRENT_FRAMES",
    "ACCELERATED_PRO_CONCURRENT_FRAMES",
    "ACCELERATED_ULTRA_CONCURRENT_FRAMES",
    "ADD_SFW_VECTOR",
    "SFW_VECTOR_MAX_SIMILARITY",
    "VECTOR_REFRESH_DIVISOR",
    "MOD_API_MAX_CONCURRENCY",
    "MOD_API_CONCURRENCY_FREE",
    "MOD_API_CONCURRENCY_CORE",
    "MOD_API_CONCURRENCY_PRO",
    "MOD_API_CONCURRENCY_ULTRA",
    "MOD_API_CONCURRENCY_BY_PLAN",
]
