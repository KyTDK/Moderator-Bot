import os
from tempfile import gettempdir

from dotenv import load_dotenv

load_dotenv()

GUILD_ID = int(os.getenv("GUILD_ID", 0))
LOG_CHANNEL_ID = int(os.getenv("LOG_CHANNEL_ID", 0))

TMP_DIR = os.path.join(gettempdir(), "modbot")
os.makedirs(TMP_DIR, exist_ok=True)

CLIP_THRESHOLD = 0.80  # Threshold for similarity search
MAX_FRAMES_PER_VIDEO = 5
ACCELERATED_MAX_FRAMES_PER_VIDEO = 100
MAX_CONCURRENT_FRAMES = 2
ACCELERATED_MAX_CONCURRENT_FRAMES = 10
MISMATCH_DETECTION = False  # Enable mismatch detection between vector search and OpenAI API
ADD_SFW_VECTOR = True  # Add SFW vectors to the index
