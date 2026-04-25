"""Screen capture for macOS — captures the main display as JPEG."""

from __future__ import annotations

import base64
import logging

import Quartz
from AppKit import (
    NSBitmapImageRep, NSJPEGFileType, NSImageCompressionFactor,
    NSImage, NSSize, NSCompositingOperationCopy, NSGraphicsContext,
    NSImageInterpolationHigh,
)
from Foundation import NSRect, NSPoint

logger = logging.getLogger(__name__)

_MAX_DIMENSION = 1920


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

    Downscales to max 1920px on the longest side to reduce API payload.
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

        src_w = Quartz.CGImageGetWidth(image)
        src_h = Quartz.CGImageGetHeight(image)

        scale = min(1.0, _MAX_DIMENSION / max(src_w, src_h))
        dst_w = int(src_w * scale)
        dst_h = int(src_h * scale)

        if scale < 1.0:
            ns_image = NSImage.alloc().initWithSize_(NSSize(dst_w, dst_h))
            ns_image.lockFocus()
            ctx = NSGraphicsContext.currentContext()
            ctx.setImageInterpolation_(NSImageInterpolationHigh)
            src_bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image)
            src_bitmap.drawInRect_fromRect_operation_fraction_respectFlipped_hints_(
                NSRect(NSPoint(0, 0), NSSize(dst_w, dst_h)),
                NSRect(NSPoint(0, 0), NSSize(src_w, src_h)),
                NSCompositingOperationCopy, 1.0, True, None,
            )
            ns_image.unlockFocus()
            bitmap = NSBitmapImageRep.alloc().initWithData_(ns_image.TIFFRepresentation())
        else:
            bitmap = NSBitmapImageRep.alloc().initWithCGImage_(image)

        jpeg_data = bitmap.representationUsingType_properties_(
            NSJPEGFileType, {NSImageCompressionFactor: 0.6})
        if jpeg_data is None:
            logger.warning("JPEG conversion failed")
            return ""

        result = base64.b64encode(bytes(jpeg_data)).decode("ascii")
        logger.debug("Screenshot: %dx%d → %dx%d, %d KB",
                     src_w, src_h, dst_w, dst_h, len(result) * 3 // 4 // 1024)
        return result
    except Exception:
        logger.warning("Screen capture failed", exc_info=True)
        return ""
