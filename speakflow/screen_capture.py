"""Screen capture for macOS — captures the main display as JPEG."""

from __future__ import annotations

import base64
import io
import logging

import Quartz
from AppKit import NSBitmapImageRep, NSJPEGFileType, NSImageCompressionFactor

logger = logging.getLogger(__name__)


def has_screen_recording_permission() -> bool:
    """Check if Screen Recording permission is granted without prompting."""
    try:
        return Quartz.CGPreflightScreenCaptureAccess()
    except AttributeError:
        return True


def request_screen_recording_permission() -> bool:
    """Request Screen Recording permission (shows prompt once)."""
    try:
        return Quartz.CGRequestScreenCaptureAccess()
    except AttributeError:
        return True


def capture_screen_base64() -> str:
    """Capture the main display and return as base64-encoded JPEG.

    Uses Quartz CGWindowListCreateImage directly (no subprocess) so the
    permission is tied to the app bundle and persists once granted.
    Returns empty string on failure.
    """
    if not has_screen_recording_permission():
        request_screen_recording_permission()
        if not has_screen_recording_permission():
            logger.warning("Screen Recording permission not granted")
            return ""

    try:
        image = Quartz.CGWindowListCreateImage(
            Quartz.CGRectInfinite,
            Quartz.kCGWindowListOptionOnScreenOnly,
            Quartz.kCGNullWindowID,
            Quartz.kCGWindowImageDefault,
        )
        if image is None:
            logger.warning("CGWindowListCreateImage returned None")
            return ""

        bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image)
        jpeg_data = bitmap.representationUsingType_properties_(
            NSJPEGFileType, {NSImageCompressionFactor: 0.7})
        if jpeg_data is None:
            logger.warning("JPEG conversion failed")
            return ""

        return base64.b64encode(bytes(jpeg_data)).decode("ascii")
    except Exception:
        logger.warning("Screen capture failed", exc_info=True)
        return ""
