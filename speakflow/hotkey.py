"""Global hotkey listener for macOS using NSEvent monitors.

Uses NSEvent.addGlobalMonitorForEventsMatchingMask_handler_ for events
in other applications and addLocalMonitorForEventsMatchingMask_handler_
for events in our own app.  Runs on the main thread's run loop — more
stable than pynput's CGEventTap which macOS can silently disable.

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

from AppKit import NSEvent

logger = logging.getLogger(__name__)

# ── NSEvent constants ─────────────────────────────────────────────────

_FLAGS_CHANGED_MASK = 1 << 12   # NSEventMaskFlagsChanged
_KEY_DOWN_MASK = 1 << 10        # NSEventMaskKeyDown

_CTRL_FLAG = 1 << 18            # NSEventModifierFlagControl
_SHIFT_FLAG = 1 << 17           # NSEventModifierFlagShift
_CMD_FLAG = 1 << 20             # NSEventModifierFlagCommand
_OPT_FLAG = 1 << 19             # NSEventModifierFlagOption
_DEVICE_INDEPENDENT_MASK = 0xFFFF0000  # device-independent modifier bits

_MODIFIER_MAP: dict[str, int] = {
    "ctrl": _CTRL_FLAG,
    "shift": _SHIFT_FLAG,
    "cmd": _CMD_FLAG,
    "command": _CMD_FLAG,
    "alt": _OPT_FLAG,
    "option": _OPT_FLAG,
}

# Key-code → name (same map used by the capture UI in app.py)
_KEYCODE_MAP: dict[int, str] = {
    0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z",
    7: "x", 8: "c", 9: "v", 11: "b", 12: "q", 13: "w", 14: "e",
    15: "r", 16: "y", 17: "t", 18: "1", 19: "2", 20: "3",
    21: "4", 22: "6", 23: "5", 24: "=", 25: "9", 26: "7",
    27: "-", 28: "8", 29: "0", 31: "o", 32: "u", 34: "i",
    35: "p", 37: "l", 38: "j", 40: "k", 45: "n", 46: "m",
    49: "space", 36: "enter", 48: "tab", 51: "backspace",
    53: "escape", 123: "left", 124: "right", 125: "down", 126: "up",
    122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5",
    97: "f6", 98: "f7", 100: "f8", 101: "f9", 109: "f10",
    103: "f11", 111: "f12",
}


def is_modifier_only(hotkey_string: str) -> bool:
    """Return True if the hotkey is a single modifier key (hold-to-record)."""
    return hotkey_string.strip().lower() in _MODIFIER_MAP


class HotkeyListener:
    """Listens for a global keyboard shortcut via NSEvent monitors."""

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
        self._global_monitor = None
        self._local_monitor = None

        self._hold_delay: float = 0.3
        self._hold_timer: Optional[threading.Timer] = None
        self._hold_generation: int = 0

        # Parsed hotkey state
        self._is_modifier_only = False
        self._target_flag: int = 0
        self._combo_mod_flags: int = 0
        self._combo_trigger: Optional[str] = None  # key name
        self._prev_flags: int = 0
        self._parse_hotkey()

    # ── Public API ────────────────────────────────────────────────

    def start(self) -> None:
        """Install NSEvent monitors.  Must be called on the main thread."""
        if self._global_monitor is not None:
            logger.warning("Listener already running; ignoring start().")
            return

        mask = _FLAGS_CHANGED_MASK | _KEY_DOWN_MASK

        self._global_monitor = (
            NSEvent.addGlobalMonitorForEventsMatchingMask_handler_(
                mask, self._handle_global))
        self._local_monitor = (
            NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                mask, self._handle_local))

        mode = "hold-to-record" if self._is_modifier_only else "combo"
        logger.info("%s listener started for %r.", mode.capitalize(),
                    self._hotkey_string)

    def stop(self) -> None:
        """Remove NSEvent monitors."""
        if self._global_monitor is not None:
            NSEvent.removeMonitor_(self._global_monitor)
            self._global_monitor = None
        if self._local_monitor is not None:
            NSEvent.removeMonitor_(self._local_monitor)
            self._local_monitor = None
        with self._lock:
            self._cancel_hold_timer_locked()
            self._active = False
            self._prev_flags = 0
        logger.info("Hotkey listener stopped.")

    @property
    def is_listening(self) -> bool:
        return self._global_monitor is not None

    def update_hotkey(self, hotkey_string: str) -> None:
        """Change the target hotkey without removing monitors."""
        with self._lock:
            self._cancel_hold_timer_locked()
            self._active = False
        self._hotkey_string = hotkey_string
        self._parse_hotkey()
        logger.info("Hotkey updated to %r.", hotkey_string)

    # ── Parse ────────────────────────────────────────────────────

    def _parse_hotkey(self) -> None:
        hs = self._hotkey_string.strip().lower()
        if hs in _MODIFIER_MAP:
            self._is_modifier_only = True
            self._target_flag = _MODIFIER_MAP[hs]
            self._combo_mod_flags = 0
            self._combo_trigger = None
        else:
            self._is_modifier_only = False
            self._target_flag = 0
            parts = [p.strip().lower() for p in hs.split("+")]
            flags = 0
            for p in parts[:-1]:
                if p not in _MODIFIER_MAP:
                    raise ValueError(f"Unknown modifier {p!r}.")
                flags |= _MODIFIER_MAP[p]
            self._combo_mod_flags = flags
            self._combo_trigger = parts[-1]

    # ── NSEvent callbacks ────────────────────────────────────────

    def _handle_global(self, event):
        """Global monitor — events from other applications."""
        self._process_event(event)

    def _handle_local(self, event):
        """Local monitor — events in our own app."""
        self._process_event(event)
        return event

    def _process_event(self, event):
        try:
            flags = event.modifierFlags() & _DEVICE_INDEPENDENT_MASK
            event_type = event.type()

            if event_type == 12:  # FlagsChanged
                if self._is_modifier_only:
                    self._handle_modifier(flags)
                self._prev_flags = flags

            elif event_type == 10:  # KeyDown
                if self._is_modifier_only:
                    # Non-modifier key while holding target → cancel
                    with self._lock:
                        if self._hold_timer is not None:
                            self._cancel_hold_timer_locked()
                else:
                    self._handle_combo_keydown(event, flags)
        except Exception:
            logger.exception("Error in hotkey event handler")

    def _handle_modifier(self, flags):
        target = self._target_flag
        was_pressed = bool(self._prev_flags & target)
        is_pressed = bool(flags & target)

        if is_pressed and not was_pressed:
            # Target modifier pressed → start hold timer
            with self._lock:
                self._cancel_hold_timer_locked()
                gen = self._hold_generation
                t = threading.Timer(self._hold_delay, self._hold_expired,
                                    args=(gen,))
                t.daemon = True
                t.start()
                self._hold_timer = t

        elif not is_pressed and was_pressed:
            # Target modifier released → deactivate
            with self._lock:
                self._cancel_hold_timer_locked()
                if self._active:
                    self._active = False
                    cb = self._on_deactivate
                else:
                    cb = None
            self._fire(cb)

        elif is_pressed:
            # Still pressed, but another modifier changed → cancel hold
            # (user doing e.g. Ctrl+C)
            other = flags & ~target & _DEVICE_INDEPENDENT_MASK
            prev_other = self._prev_flags & ~target & _DEVICE_INDEPENDENT_MASK
            if other != prev_other:
                with self._lock:
                    self._cancel_hold_timer_locked()

    def _handle_combo_keydown(self, event, flags):
        if self._combo_trigger is None:
            return
        key_name = _KEYCODE_MAP.get(event.keyCode())
        if key_name is None:
            try:
                ch = event.charactersIgnoringModifiers()
                if ch and len(ch) == 1:
                    key_name = ch.lower()
            except Exception:
                return
        if key_name == self._combo_trigger:
            if (flags & self._combo_mod_flags) == self._combo_mod_flags:
                with self._lock:
                    if self._active:
                        self._active = False
                        cb = self._on_deactivate
                    else:
                        self._active = True
                        cb = self._on_activate
                self._fire(cb)

    # ── Helpers ──────────────────────────────────────────────────

    def _fire(self, cb) -> None:
        if cb is not None:
            threading.Thread(target=self._safe_fire, args=(cb,),
                             daemon=True).start()

    @staticmethod
    def _safe_fire(cb):
        try:
            cb()
        except Exception:
            logger.exception("Hotkey callback error")

    def _hold_expired(self, gen: int) -> None:
        with self._lock:
            if gen != self._hold_generation:
                return
            if not self._active:
                self._active = True
                cb = self._on_activate
            else:
                cb = None
        self._fire(cb)

    def _cancel_hold_timer_locked(self) -> None:
        self._hold_generation += 1
        if self._hold_timer is not None:
            self._hold_timer.cancel()
            self._hold_timer = None
