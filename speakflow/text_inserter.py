"""Text insertion for macOS -- paste transcribed text at the current cursor position."""

from __future__ import annotations

import logging
import subprocess
import threading
import time

from pynput.keyboard import Controller, Key

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Clipboard helpers (macOS pbcopy / pbpaste)
# ---------------------------------------------------------------------------

def _read_clipboard() -> str:
    """Return the current macOS clipboard contents as a string."""
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            timeout=2,
        )
        return result.stdout.decode("utf-8", errors="replace")
    except Exception:
        logger.warning("Failed to read clipboard via pbpaste", exc_info=True)
        return ""


def _write_clipboard(text: str) -> None:
    """Write *text* to the macOS clipboard via pbcopy."""
    try:
        subprocess.run(
            ["pbcopy"],
            input=text.encode("utf-8"),
            timeout=2,
            check=True,
        )
    except Exception:
        logger.error("Failed to write to clipboard via pbcopy", exc_info=True)


# ---------------------------------------------------------------------------
# TextInserter
# ---------------------------------------------------------------------------

class TextInserter:
    """Insert text at the current cursor position on macOS.

    Two strategies are available:

    * **clipboard** (default) -- copies the text to the system clipboard,
      simulates *Cmd+V* to paste it, then restores the previous clipboard
      contents.  Fast and fully Unicode-safe (Danish ``ae``, ``oe``, ``aa``
      included).
    * **keyboard** -- types the text character-by-character via
      ``pynput.keyboard.Controller``.  Slower but does not touch the
      clipboard.

    Parameters
    ----------
    method:
        ``"clipboard"`` or ``"keyboard"``.
    """

    VALID_METHODS = ("clipboard", "keyboard")

    def __init__(self, method: str = "clipboard") -> None:
        if method not in self.VALID_METHODS:
            raise ValueError(
                f"Invalid insertion method: {method!r}. "
                f"Must be one of {self.VALID_METHODS}."
            )
        self._method = method
        self._keyboard = Controller()
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
        """Insert *text* at the current cursor position.

        The concrete strategy is determined by :pyattr:`method`.
        """
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
        """Copy *text* to the clipboard, paste with Cmd+V, then restore.

        This is the preferred approach: it is fast and handles arbitrary
        Unicode (including Danish characters such as ae, oe, aa) without
        issues.
        """
        original = _read_clipboard()
        try:
            _write_clipboard(text)

            # Small pause so the pasteboard is ready before we fire the
            # keystroke.
            time.sleep(0.05)

            # Simulate Cmd+V
            self._keyboard.press(Key.cmd)
            self._keyboard.press("v")
            self._keyboard.release("v")
            self._keyboard.release(Key.cmd)

            # Give the target application a moment to process the paste.
            time.sleep(0.1)
            logger.debug("Pasted %d characters via clipboard", len(text))
        finally:
            # Always restore the user's original clipboard contents.
            _write_clipboard(original)

    def _insert_via_keyboard(self, text: str) -> None:
        """Type *text* character-by-character using pynput.

        A small delay is inserted between keystrokes so that receiving
        applications do not drop characters.  Newlines are handled by
        pressing the *Return* key.
        """
        delay = 0.02  # seconds between keystrokes

        for char in text:
            if char == "\n":
                self._keyboard.press(Key.enter)
                self._keyboard.release(Key.enter)
            elif char == "\t":
                self._keyboard.press(Key.tab)
                self._keyboard.release(Key.tab)
            else:
                self._keyboard.type(char)

            time.sleep(delay)

        logger.debug("Typed %d characters via keyboard", len(text))
