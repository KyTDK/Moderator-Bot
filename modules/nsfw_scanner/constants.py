import os
from tempfile import gettempdir

from dotenv import load_dotenv

load_dotenv()

GUILD_ID = int(os.getenv("GUILD_ID", 0))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 0))

TMP_DIR = os.path.join(gettempdir(), "modbot")
os.makedirs(TMP_DIR, exist_ok=True)

CLIP_THRESHOLD = 0.80  # Threshold for similarity search
HIGH_ACCURACY_SIMILARITY = 0.90  # Min similarity to skip API when high-accuracy is enabled
# Max frames per video
MAX_FRAMES_PER_VIDEO = 5
ACCELERATED_MAX_FRAMES_PER_VIDEO = 100
ACCELERATED_PRO_MAX_FRAMES_PER_VIDEO = 300
ACCELERATED_ULTRA_MAX_FRAMES_PER_VIDEO = None
# Make concurrent frames
MAX_CONCURRENT_FRAMES = 5
ACCELERATED_MAX_CONCURRENT_FRAMES = 10
ACCELERATED_PRO_CONCURRENT_FRAMES = 20
ACCELERATED_ULTRA_CONCURRENT_FRAMES = 50
ADD_SFW_VECTOR = False  # Add SFW vectors to the index
try:
    VECTOR_REFRESH_DIVISOR = int(os.getenv("VECTOR_REFRESH_DIVISOR", "0"))
except (TypeError, ValueError):
    VECTOR_REFRESH_DIVISOR = 0
