"""Text insertion for macOS -- paste transcribed text at the current cursor position."""

from __future__ import annotations

import logging
import threading
import time

from AppKit import NSPasteboard
import Quartz

logger = logging.getLogger(__name__)

_PASTEBOARD_TYPE = "public.utf8-plain-text"

# ---------------------------------------------------------------------------
# Clipboard helpers (NSPasteboard — synchronous, no subprocess overhead)
# ---------------------------------------------------------------------------

def _read_clipboard() -> str:
    """Return the current macOS clipboard contents as a string."""
    try:
        pb = NSPasteboard.generalPasteboard()
        text = pb.stringForType_(_PASTEBOARD_TYPE)
        return text if text else ""
    except Exception:
        logger.warning("Failed to read clipboard", exc_info=True)
        return ""


def _write_clipboard(text: str) -> None:
    """Write *text* to the macOS clipboard."""
    try:
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, _PASTEBOARD_TYPE)
    except Exception:
        logger.error("Failed to write to clipboard", exc_info=True)


# ---------------------------------------------------------------------------
# Quartz keyboard simulation
# ---------------------------------------------------------------------------

_kVK_V = 0x09  # macOS virtual keycode for "v"
_kCGEventFlagMaskCommand = 1 << 20


def _simulate_cmd_v() -> None:
    """Simulate Cmd+V using Quartz CGEventPost.

    Uses a private event source so physical modifier keys (Caps Lock,
    Shift held by the user, etc.) do not contaminate the synthetic event.
    """
    src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)

    cmd_v_down = Quartz.CGEventCreateKeyboardEvent(src, _kVK_V, True)
    Quartz.CGEventSetFlags(cmd_v_down, _kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, cmd_v_down)

    time.sleep(0.01)

    cmd_v_up = Quartz.CGEventCreateKeyboardEvent(src, _kVK_V, False)
    Quartz.CGEventSetFlags(cmd_v_up, _kCGEventFlagMaskCommand)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, cmd_v_up)


# ---------------------------------------------------------------------------
# TextInserter
# ---------------------------------------------------------------------------

class TextInserter:
    """Insert text at the current cursor position on macOS.

    Two strategies are available:

    * **clipboard** (default) -- copies the text to the system clipboard,
      simulates *Cmd+V* to paste it, then restores the previous clipboard
      contents.  Fast and fully Unicode-safe.
    * **keyboard** -- types the text character-by-character via
      Quartz CGEvent.  Slower but does not touch the clipboard.
    """

    VALID_METHODS = ("clipboard", "keyboard")

    def __init__(self, method: str = "clipboard") -> None:
        if method not in self.VALID_METHODS:
            raise ValueError(
                f"Invalid insertion method: {method!r}. "
                f"Must be one of {self.VALID_METHODS}."
            )
        self._method = method
        self._lock = threading.Lock()
        logger.debug("TextInserter initialised (method=%s)", method)

    # -- public API ---------------------------------------------------------

    @property
    def method(self) -> str:
        """The active insertion method."""
        return self._method

    @method.setter
    def method(self, value: str) -> None:
        if value not in self.VALID_METHODS:
            raise ValueError(
                f"Invalid insertion method: {value!r}. "
                f"Must be one of {self.VALID_METHODS}."
            )
        self._method = value
        logger.info("Text insertion method changed to %s", value)

    def insert_text(self, text: str) -> None:
        """Insert *text* at the current cursor position."""
        if not text:
            logger.debug("insert_text called with empty string -- nothing to do")
            return

        with self._lock:
            if self._method == "clipboard":
                self._insert_via_clipboard(text)
            else:
                self._insert_via_keyboard(text)

    # -- private strategies -------------------------------------------------

    def _insert_via_clipboard(self, text: str) -> None:
        """Copy *text* to the clipboard, paste with Cmd+V, then restore."""
        original = _read_clipboard()
        try:
            _write_clipboard(text)
            time.sleep(0.02)

            logger.info("Firing Cmd+V paste (%d chars)", len(text))
            _simulate_cmd_v()

            time.sleep(0.15)
            logger.info("Paste completed")
        finally:
            _write_clipboard(original)

    def _insert_via_keyboard(self, text: str) -> None:
        """Type *text* character-by-character using Quartz CGEvent."""
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
        delay = 0.005

        for char in text:
            if char == "\n":
                ev_down = Quartz.CGEventCreateKeyboardEvent(src, 36, True)
                ev_up = Quartz.CGEventCreateKeyboardEvent(src, 36, False)
            elif char == "\t":
                ev_down = Quartz.CGEventCreateKeyboardEvent(src, 48, True)
                ev_up = Quartz.CGEventCreateKeyboardEvent(src, 48, False)
            else:
                ev_down = Quartz.CGEventCreateKeyboardEvent(src, 0, True)
                Quartz.CGEventKeyboardSetUnicodeString(ev_down, len(char), char)
                ev_up = Quartz.CGEventCreateKeyboardEvent(src, 0, False)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_down)
            Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev_up)
            time.sleep(delay)

        logger.debug("Typed %d characters via keyboard", len(text))
