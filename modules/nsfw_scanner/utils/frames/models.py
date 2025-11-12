from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import numpy as np


@dataclass(slots=True)
class ExtractedFrame:
    name: str
    data: bytes
    mime_type: str
    signature: Optional[np.ndarray]
    total_frames: Optional[int] = None
