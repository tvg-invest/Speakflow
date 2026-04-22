"""Screen capture for macOS — captures the main display as JPEG."""

from __future__ import annotations

import base64
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)


def capture_screen_base64() -> str:
    """Capture the main display and return as base64-encoded JPEG.

    Returns empty string on failure (e.g. missing Screen Recording permission).
    """
    fd, path = tempfile.mkstemp(suffix=".jpg")
    os.close(fd)
    try:
        result = subprocess.run(
            ["/usr/sbin/screencapture", "-m", "-x", "-t", "jpg", path],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0:
            logger.warning("screencapture failed (rc=%d)", result.returncode)
            return ""
        with open(path, "rb") as f:
            data = f.read()
        if not data:
            return ""
        return base64.b64encode(data).decode("ascii")
    except Exception:
        logger.warning("Screen capture failed", exc_info=True)
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass
