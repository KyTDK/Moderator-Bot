import os
import types

import pytest

from modules.nsfw_scanner.utils import frames


class _NoisyCapture:
    def __init__(self, *_args, **_kwargs):
        self._pos = 0
        self._released = False

    def get(self, prop):
        if prop == frames.cv2.CAP_PROP_FRAME_COUNT:
            return 0
        if prop == frames.cv2.CAP_PROP_FPS:
            return 0
        if prop == frames.cv2.CAP_PROP_POS_FRAMES:
            return self._pos
        return 0

    def set(self, prop, value):
        if prop == frames.cv2.CAP_PROP_POS_FRAMES:
            self._pos = int(value)
            return True
        return True

    def grab(self):
        os.write(2, b"mmco: unref short failure\n")
        return False

    def read(self):
        self._pos += 1
        os.write(2, b"mmco: unref short failure\n")
        return False, None

    def release(self):
        self._released = True


@pytest.mark.parametrize("wanted", [None, 3])
def test_iter_extracted_frames_suppresses_ffmpeg_noise(monkeypatch, tmp_path, capfd, wanted):
    video_path = tmp_path / "sample.mp4"
    video_path.write_bytes(b"test")

    cv2_stub = types.SimpleNamespace(
        VideoCapture=_NoisyCapture,
        CAP_PROP_FRAME_COUNT=1,
        CAP_PROP_FPS=2,
        CAP_PROP_POS_FRAMES=3,
        CAP_PROP_HW_ACCELERATION=4,
        VIDEO_ACCELERATION_ANY=5,
        CAP_PROP_BUFFERSIZE=6,
        IMWRITE_JPEG_QUALITY=95,
    )

    monkeypatch.setattr(frames, "cv2", cv2_stub, raising=False)

    extracted = list(frames.iter_extracted_frames(str(video_path), wanted))
    assert extracted == []

    captured = capfd.readouterr()
    assert "mmco: unref short failure" not in captured.err
