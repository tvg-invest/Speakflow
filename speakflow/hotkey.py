"""Global hotkey listener for macOS.

Uses a single pynput Listener per instance that is started once and never
recreated.  Hotkey changes update instance variables in-place so no
restart is needed — this avoids a macOS crash where TSMGetInputSourceProperty
requires the main dispatch queue but pynput threads call it on a background
thread.

Supports two modes:
- **Single modifier** (e.g. ``"ctrl"``): hold-to-record.
  Press → activate, release → deactivate.
- **Combo** (e.g. ``"ctrl+shift+z"``): toggle.
  First press → activate, second press → deactivate.
"""

from __future__ import annotations

import logging
import threading
from typing import Callable, Optional

from pynput.keyboard import Key, KeyCode, Listener

logger = logging.getLogger(__name__)

# ── Mapping from human-readable names to pynput Key objects ──────────

_MODIFIER_MAP: dict[str, Key] = {
    "ctrl": Key.ctrl,
    "ctrl_l": Key.ctrl_l,
    "ctrl_r": Key.ctrl_r,
    "shift": Key.shift,
    "shift_l": Key.shift_l,
    "shift_r": Key.shift_r,
    "cmd": Key.cmd,
    "command": Key.cmd,
    "cmd_l": Key.cmd_l,
    "cmd_r": Key.cmd_r,
    "alt": Key.alt,
    "option": Key.alt,
    "alt_l": Key.alt_l,
    "alt_r": Key.alt_r,
}

_SPECIAL_KEY_MAP: dict[str, Key] = {
    "space": Key.space,
    "enter": Key.enter,
    "return": Key.enter,
    "tab": Key.tab,
    "backspace": Key.backspace,
    "delete": Key.delete,
    "escape": Key.esc,
    "esc": Key.esc,
    "up": Key.up,
    "down": Key.down,
    "left": Key.left,
    "right": Key.right,
    "home": Key.home,
    "end": Key.end,
    "page_up": Key.page_up,
    "page_down": Key.page_down,
    "f1": Key.f1, "f2": Key.f2, "f3": Key.f3, "f4": Key.f4,
    "f5": Key.f5, "f6": Key.f6, "f7": Key.f7, "f8": Key.f8,
    "f9": Key.f9, "f10": Key.f10, "f11": Key.f11, "f12": Key.f12,
}

# Normalize left/right modifier variants to the generic key.
_NORMALIZE: dict[Key, Key] = {
    Key.ctrl_l: Key.ctrl, Key.ctrl_r: Key.ctrl,
    Key.shift_l: Key.shift, Key.shift_r: Key.shift,
    Key.cmd_l: Key.cmd, Key.cmd_r: Key.cmd,
    Key.alt_l: Key.alt, Key.alt_r: Key.alt,
}

_ALL_MODS = set(_NORMALIZE.values()) | set(_NORMALIZE.keys())


def is_modifier_only(hotkey_string: str) -> bool:
    """Return True if the hotkey is a single modifier key (hold-to-record)."""
    return hotkey_string.strip().lower() in _MODIFIER_MAP


class HotkeyListener:
    """Listens for a global keyboard shortcut.

    Single modifier (e.g. "ctrl") → hold-to-record.
    Combo (e.g. "ctrl+shift+z") → toggle.

    The underlying pynput Listener is created once by ``start()`` and
    never recreated.  ``update_hotkey()`` swaps the target key in-place.
    """

    def __init__(
        self,
        hotkey_string: str = "ctrl+shift+space",
        on_activate: Optional[Callable[[], None]] = None,
        on_deactivate: Optional[Callable[[], None]] = None,
    ) -> None:
        self._hotkey_string = hotkey_string
        self._on_activate = on_activate
        self._on_deactivate = on_deactivate

        self._active = False
        self._lock = threading.Lock()
        self._listener: Optional[Listener] = None
        self._held_mods: set[Key] = set()

        # Derived from hotkey_string — updated by _parse_hotkey().
        self._is_modifier_only = False
        self._target_mod: Optional[Key] = None
        self._combo_mods: Optional[set[Key]] = None
        self._combo_trigger = None  # Key or KeyCode
        self._parse_hotkey()

    # ── Public API ────────────────────────────────────────────────

    def start(self) -> None:
        """Start listening.  Creates the pynput Listener once."""
        if self._listener is not None:
            logger.warning("Listener already running; ignoring start().")
            return
        self._listener = Listener(
            on_press=self._on_press,
            on_release=self._on_release,
        )
        self._listener.daemon = True
        self._listener.start()
        mode = "hold-to-record" if self._is_modifier_only else "combo"
        logger.info("%s listener started for %r.", mode.capitalize(), self._hotkey_string)

    def stop(self) -> None:
        """Stop the listener permanently (app quit)."""
        if self._listener is None:
            return
        self._listener.stop()
        self._listener = None
        with self._lock:
            self._active = False
        self._held_mods.clear()
        logger.info("Hotkey listener stopped.")

    @property
    def is_listening(self) -> bool:
        return self._listener is not None and self._listener.is_alive()

    def update_hotkey(self, hotkey_string: str) -> None:
        """Change the target hotkey without restarting the listener."""
        self._hotkey_string = hotkey_string
        self._parse_hotkey()
        with self._lock:
            self._active = False
        self._held_mods.clear()
        logger.info("Hotkey updated to %r.", hotkey_string)

    # ── Internal: parse hotkey string ────────────────────────────

    def _parse_hotkey(self) -> None:
        hs = self._hotkey_string.strip().lower()
        if hs in _MODIFIER_MAP:
            self._is_modifier_only = True
            raw = _MODIFIER_MAP[hs]
            self._target_mod = _NORMALIZE.get(raw, raw)
            self._combo_mods = None
            self._combo_trigger = None
        else:
            self._is_modifier_only = False
            self._target_mod = None
            parts = [p.strip().lower() for p in hs.split("+")]
            mods: set[Key] = set()
            for p in parts[:-1]:
                key = _MODIFIER_MAP.get(p)
                if key is None:
                    raise ValueError(f"Unknown modifier {p!r}.")
                mods.add(_NORMALIZE.get(key, key))
            self._combo_mods = mods
            trigger = parts[-1]
            if trigger in _SPECIAL_KEY_MAP:
                self._combo_trigger = _SPECIAL_KEY_MAP[trigger]
            elif len(trigger) == 1:
                self._combo_trigger = trigger  # store as plain char
            else:
                raise ValueError(f"Unknown trigger key {trigger!r}.")

    # ── Internal: key matching helpers ───────────────────────────

    @staticmethod
    def _norm(key) -> Key:
        return _NORMALIZE.get(key, key)

    @staticmethod
    def _is_mod(key) -> bool:
        return key in _ALL_MODS

    def _matches_trigger(self, key) -> bool:
        trigger = self._combo_trigger
        if trigger is None:
            return False
        # Trigger stored as plain char string
        if isinstance(trigger, str):
            if isinstance(key, KeyCode) and key.char:
                return key.char.lower() == trigger
            return False
        # Trigger stored as Key (special key)
        if isinstance(trigger, Key):
            return self._norm(key) == self._norm(trigger)
        return False

    # ── Callbacks ────────────────────────────────────────────────

    def _fire(self, cb) -> None:
        if cb is not None:
            try:
                cb()
            except Exception:
                logger.exception("Hotkey callback error")

    def _on_press(self, key) -> None:
        nk = self._norm(key)

        # Track modifier state
        if self._is_mod(key):
            self._held_mods.add(nk)

        if self._is_modifier_only:
            if nk == self._target_mod:
                with self._lock:
                    if not self._active:
                        self._active = True
                        cb = self._on_activate
                    else:
                        cb = None
                self._fire(cb)
        else:
            # Combo mode: trigger fires when correct mods are held
            if not self._is_mod(key) and self._matches_trigger(key):
                if self._combo_mods is not None and self._combo_mods.issubset(self._held_mods):
                    with self._lock:
                        if self._active:
                            self._active = False
                            cb = self._on_deactivate
                        else:
                            self._active = True
                            cb = self._on_activate
                    self._fire(cb)

    def _on_release(self, key) -> None:
        nk = self._norm(key)

        if self._is_modifier_only:
            if nk == self._target_mod:
                with self._lock:
                    if self._active:
                        self._active = False
                        cb = self._on_deactivate
                    else:
                        cb = None
                self._fire(cb)

        # Release modifier tracking
        if self._is_mod(key):
            self._held_mods.discard(nk)
