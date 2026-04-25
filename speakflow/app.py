"""SpeakFlow — native macOS app using PyObjC/Cocoa."""

import logging
import math
import random
import threading
import traceback
from collections import deque
from pathlib import Path

import os
import subprocess
import time as _time
import objc
from AppKit import (
    NSApplication, NSApp, NSApplicationActivationPolicyRegular,
    NSWindow, NSPanel, NSBackingStoreBuffered,
    NSMakeRect, NSTextField, NSButton, NSFont,
    NSColor, NSPopUpButton, NSStatusBar, NSVariableStatusItemLength,
    NSMenu, NSMenuItem, NSObject,
    NSEvent, NSScreen, NSView,
    NSFloatingWindowLevel, NSVisualEffectView,
    NSFontWeightMedium, NSFontWeightSemibold,
    NSWorkspace,
    NSScrollView, NSTextView, NSSlider,
    NSPasteboard,
    NSFontAttributeName, NSForegroundColorAttributeName,
)
from Foundation import NSTimer, NSAttributedString
from PyObjCTools import AppHelper
import ApplicationServices

import openai

from .audio import AudioRecorder
from .config import Config
from . import history
from .hotkey import HotkeyListener, is_modifier_only, _KEYCODE_MAP
from .sounds import play_error_sound, play_start_sound, play_stop_sound, set_volume, warm_up as _warm_up_sounds
from .screen_capture import capture_screen_base64, has_screen_recording_permission
from .text_inserter import TextInserter
from .transcriber import Transcriber

logger = logging.getLogger(__name__)

VERSION = "1.5.1"

LANG_OPTIONS = ["Danish", "English", "Auto-detect"]
LANG_CODES = {"Danish": "da", "English": "en", "Auto-detect": "auto"}

_LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.speakflow.app.plist"
_APP_PATH = Path.home() / "Desktop" / "SpeakFlow.app"

# ── Colour palette (lazy-cached — created once on first access) ──
_color_cache: dict[str, object] = {}

def _c(name: str, r: float, g: float, b: float, a: float = 1.0):
    cached = _color_cache.get(name)
    if cached is not None:
        return cached
    c = NSColor.colorWithCalibratedRed_green_blue_alpha_(r, g, b, a)
    _color_cache[name] = c
    return c

def _BG():        return _c("bg",   0.10, 0.10, 0.13)
def _CARD():      return _c("card", 0.15, 0.15, 0.19)
def _CARD_EDGE(): return _c("ce",   0.22, 0.22, 0.27)
def _ACCENT():    return _c("acc",  0.35, 0.58, 1.0)
def _GREEN():     return _c("grn",  0.30, 0.85, 0.55)
def _RED():       return _c("red",  1.0,  0.32, 0.32)
def _ORANGE():    return _c("org",  1.0,  0.65, 0.20)
def _GOLD():      return _c("gld",  1.0,  0.80, 0.28)
def _PURPLE():    return _c("pur",  0.65, 0.40, 1.0)
def _DIM():       return _c("dim",  0.50, 0.50, 0.56)
def _WHITE():     return _c("wht",  1.0,  1.0,  1.0)
def _SEC_BG():    return _c("sbg",  0.18, 0.18, 0.23)
def _SEC_EDGE():  return _c("sed",  0.30, 0.30, 0.36)
def _TEAL():      return _c("teal", 0.25, 0.78, 0.85)

# ── Mode system ────────────────────────────────────────────────
_BUILTIN_MODES = ["auto", "dictation", "ask", "vision", "vibecode"]
_MODE_NAMES = {
    "auto": "Auto",
    "dictation": "Dictation",
    "ask": "AI Ask",
    "vision": "Screen Vision",
    "vibecode": "VibeCode",
}
_MODE_IDS = {v: k for k, v in _MODE_NAMES.items()}
_MODE_COLORS = {
    "auto": _WHITE,
    "dictation": _ACCENT,
    "ask": _TEAL,
    "vision": _GOLD,
    "vibecode": _PURPLE,
}


class MainThreadDispatcher(NSObject):
    def init(self):
        self = objc.super(MainThreadDispatcher, self).init()
        if self is not None:
            self._queue = deque()
            self._lock = threading.Lock()
        return self

    def enqueue_(self, func):
        with self._lock:
            self._queue.append(func)
        self.performSelectorOnMainThread_withObject_waitUntilDone_("drain:", None, False)

    def drain_(self, _):
        while True:
            with self._lock:
                if not self._queue:
                    break
                func = self._queue.popleft()
            try:
                func()
            except Exception:
                logger.error("Callback error:\n%s", traceback.format_exc())


class AppDelegate(NSObject):
    def applicationDidFinishLaunching_(self, notification):
        self.sf = SpeakFlowUI.alloc().init()

    def applicationShouldHandleReopen_hasVisibleWindows_(self, app, flag):
        if hasattr(self, 'sf'):
            self.sf.show_window()
        return True

    def applicationShouldTerminateAfterLastWindowClosed_(self, app):
        return False

    def applicationWillTerminate_(self, notification):
        if hasattr(self, 'sf'):
            try:
                self.sf.hotkey_listener.stop()
            except Exception:
                pass
            try:
                if self.sf._ax_poll_timer is not None:
                    self.sf._ax_poll_timer.invalidate()
                    self.sf._ax_poll_timer = None
            except Exception:
                pass


class SpeakFlowUI(NSObject):
    def init(self):
        self = objc.super(SpeakFlowUI, self).init()
        if self is None:
            return None
        self._setup()
        return self

    @objc.python_method
    def _setup(self):
        self.config = Config()
        self._recording = False
        self._processing = False
        self._capturing = False
        self._capture_target = "main"
        self._context_mode = False
        self._float_triggered = False
        self._selected_text = ""
        self._before_text = ""
        self._after_text = ""
        self._active_app = ""
        self._target_running_app = None
        self._stop_lock = threading.Lock()
        self._dispatcher = MainThreadDispatcher.alloc().init()
        self._history_win = None
        self._guide_win = None
        self._key_monitor = None
        self._vol_save_timer = None
        self._screenshot_b64 = ""
        self._mode_mgr_win = None
        self._add_mode_win = None
        self._response_panel = None
        self._popup_timer = None
        self._popup_response_text = ""
        self._shortcuts_win = None
        self._add_shortcut_win = None

        # Core components
        self.audio_recorder = AudioRecorder(
            max_duration=self.config.max_recording_seconds,
            silence_timeout=self.config.silence_timeout,
            device=self.config.microphone,
        )
        self.audio_recorder.on_silence_detected = self._on_silence
        self.audio_recorder.on_error = self._on_record_error
        self.audio_recorder.on_max_duration = self._on_silence

        self.transcriber = Transcriber(
            api_key=self.config.openai_api_key,
            model=self.config.model,
            language=self.config.language,
            auto_detect=self.config.auto_language_detect,
            cleanup_model=self.config.ai_cleanup_model,
            editing_strength=self.config.editing_strength,
            personal_dictionary=self.config.personal_dictionary,
        )
        self.text_inserter = TextInserter(method=self.config.text_insertion_method)
        self.hotkey_listener = HotkeyListener(
            hotkey_string=self.config.hotkey,
            on_activate=self._on_activate,
            on_deactivate=self._on_deactivate,
        )

        set_volume(self.config.sound_volume)
        _warm_up_sounds()

        self._build_status_bar()
        self._build_window()
        self._build_floating_indicator()
        self._check_api_key()

        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

        # Start listeners immediately — NSEvent monitors work for modifier
        # keys even without explicit Accessibility trust on some systems.
        self.hotkey_listener.start()

        # Prompt for Accessibility if not yet granted (improves reliability)
        self._ax_poll_timer = None
        NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
            0.5, self, "_checkAccessibility:", None, False)

        # Show first-time onboarding guide
        if self.config.get("first_run", True):
            self.config.set("first_run", False)
            NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.5, self, "showGuide:", None, False)

        logger.info("SpeakFlowApp initialised.")

    @objc.python_method
    def _check_api_key(self):
        if not self.config.openai_api_key:
            self.status_label.setStringValue_("Add your API key below to get started")
            self.status_label.setTextColor_(_ORANGE())
            logger.warning("No OpenAI API key configured.")

    def _checkAccessibility_(self, timer):
        """Prompt for Accessibility if not granted (non-blocking)."""
        trusted = ApplicationServices.AXIsProcessTrustedWithOptions(
            {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        )
        if trusted:
            logger.info("Accessibility granted.")
            if self._ax_poll_timer is not None:
                self._ax_poll_timer.invalidate()
                self._ax_poll_timer = None
        else:
            logger.info("Accessibility not yet granted — prompting user.")
            # Poll until granted so we can clear the status message
            if self._ax_poll_timer is None:
                self._ax_poll_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                    2.0, self, "_axPollTick:", None, True)

    def _axPollTick_(self, timer):
        """Repeating poll — detects when user grants Accessibility."""
        trusted = ApplicationServices.AXIsProcessTrustedWithOptions(
            {ApplicationServices.kAXTrustedCheckOptionPrompt: False}
        )
        if trusted:
            logger.info("Accessibility granted (via poll).")
            self._ax_poll_timer.invalidate()
            self._ax_poll_timer = None

    @objc.python_method
    def show_window(self):
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    # ── Helpers ─────────────────────────────────────────────────

    @objc.python_method
    def _remove_key_monitor(self):
        """Safely remove event monitor and reset capture state."""
        try:
            if self._key_monitor is not None:
                NSEvent.removeMonitor_(self._key_monitor)
        except Exception:
            logger.warning("Failed to remove event monitor", exc_info=True)
        finally:
            self._key_monitor = None
            self._capturing = False

    @objc.python_method
    def _label(self, parent, text, x, y, w, h, font=None, color=None, center=False, selectable=False):
        l = NSTextField.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        l.setStringValue_(text)
        l.setBezeled_(False)
        l.setDrawsBackground_(False)
        l.setEditable_(False)
        l.setSelectable_(selectable)
        if font:
            l.setFont_(font)
        if color:
            l.setTextColor_(color)
        if center:
            l.setAlignment_(1)
        parent.addSubview_(l)
        return l

    @objc.python_method
    def _styled_btn(self, parent, title, x, y, w, h, color=None, font=None):
        """Create a styled button with visible colors on dark background."""
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setButtonType_(0)  # momentaryPushIn
        btn.setBordered_(False)
        btn.setWantsLayer_(True)
        btn.setFocusRingType_(1)  # NSFocusRingTypeNone
        bg = color or _ACCENT()
        btn.layer().setCornerRadius_(8)
        btn.layer().setBackgroundColor_(bg.CGColor())
        self._set_btn_title(btn, title, font)
        parent.addSubview_(btn)
        return btn

    @objc.python_method
    def _set_btn_title(self, btn, title, font=None, color=None):
        """Set button title with custom or white text."""
        attrs = {
            NSFontAttributeName: font or NSFont.systemFontOfSize_weight_(12, NSFontWeightMedium),
            NSForegroundColorAttributeName: color or _WHITE(),
        }
        btn.setAttributedTitle_(
            NSAttributedString.alloc().initWithString_attributes_(title, attrs)
        )

    @objc.python_method
    def _ghost_btn(self, parent, title, x, y, w, h, font=None):
        """Create a subtle secondary button with border, for non-primary actions."""
        btn = NSButton.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        btn.setButtonType_(0)
        btn.setBordered_(False)
        btn.setWantsLayer_(True)
        btn.setFocusRingType_(1)
        btn.layer().setCornerRadius_(8)
        btn.layer().setBackgroundColor_(_SEC_BG().CGColor())
        btn.layer().setBorderWidth_(1)
        btn.layer().setBorderColor_(_SEC_EDGE().CGColor())
        f = font or NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium)
        self._set_btn_title(btn, title, f, color=_DIM())
        parent.addSubview_(btn)
        return btn

    @objc.python_method
    def _card(self, parent, x, y, w, h):
        card = NSView.alloc().initWithFrame_(NSMakeRect(x, y, w, h))
        card.setWantsLayer_(True)
        card.layer().setCornerRadius_(16)
        card.layer().setMasksToBounds_(True)
        card.layer().setBackgroundColor_(_CARD().CGColor())
        card.layer().setBorderWidth_(1)
        card.layer().setBorderColor_(_CARD_EDGE().CGColor())
        parent.addSubview_(card)
        return card

    @objc.python_method
    def _divider(self, parent, x, y, w):
        d = NSView.alloc().initWithFrame_(NSMakeRect(x, y, w, 1))
        d.setWantsLayer_(True)
        d.layer().setBackgroundColor_(_CARD_EDGE().CGColor())
        parent.addSubview_(d)

    @objc.python_method
    def _dot(self, parent, x, y, size, color):
        dot = NSView.alloc().initWithFrame_(NSMakeRect(x, y, size, size))
        dot.setWantsLayer_(True)
        dot.layer().setCornerRadius_(size / 2)
        dot.layer().setBackgroundColor_(color.CGColor())
        parent.addSubview_(dot)
        return dot

    @objc.python_method
    def _get_active_app(self):
        try:
            app = NSWorkspace.sharedWorkspace().frontmostApplication()
            return app.localizedName() if app else ""
        except Exception:
            return ""

    @objc.python_method
    def _get_active_running_app(self):
        """Return the NSRunningApplication for the frontmost app."""
        try:
            return NSWorkspace.sharedWorkspace().frontmostApplication()
        except Exception:
            return None

    @objc.python_method
    def _reactivate_target_app(self):
        """Re-activate the app that was frontmost when recording started.

        Returns True if the target app is (or becomes) frontmost.
        Must NOT be called on the main thread (uses time.sleep).
        """
        target = self._target_running_app
        if target is None:
            return False
        if target.processIdentifier() == os.getpid():
            logger.info("Target is SpeakFlow itself — using clipboard fallback")
            return False
        if target.isTerminated():
            logger.warning("Target app %s terminated, cannot reactivate", self._active_app)
            return False
        # Already frontmost?
        front = NSWorkspace.sharedWorkspace().frontmostApplication()
        if front and front.processIdentifier() == target.processIdentifier():
            return True
        # Activate it
        target.activateWithOptions_(1 << 1)  # NSApplicationActivateIgnoringOtherApps
        # Wait briefly for it to become frontmost (up to 300ms)
        for _ in range(6):
            _time.sleep(0.05)
            front = NSWorkspace.sharedWorkspace().frontmostApplication()
            if front and front.processIdentifier() == target.processIdentifier():
                return True
        logger.warning("Could not reactivate %s before paste", self._active_app)
        return False

    # ── Deliver text (insert or clipboard) ─────────────────────

    @objc.python_method
    def _deliver_text(self, text):
        """Insert text at cursor or copy to clipboard, with history."""
        if self._float_triggered:
            self._run_on_main_sync(lambda: self._set_clipboard(text))
            self._run_on_main(lambda: self._ui_done_clipboard(text))
        else:
            reactivated = self._reactivate_target_app()
            if reactivated:
                _time.sleep(0.15)
                self.text_inserter.insert_text(text)
                self._run_on_main(lambda: self._ui_done(text))
            else:
                self._run_on_main_sync(lambda: self._set_clipboard(text))
                self._run_on_main(lambda: self._ui_done_clipboard(text))
        try:
            history.add(text, app_name=self._active_app, language=self.config.language)
        except Exception:
            logger.warning("Failed to save history", exc_info=True)

    # ── Voice shortcuts ────────────────────────────────────────

    @objc.python_method
    def _rebuild_shortcut_map(self):
        """Build a dict from normalised triggers to expansions."""
        self._shortcut_map = {}
        for sc in self.config.voice_shortcuts:
            trigger = sc.get("trigger", "").lower().strip().rstrip(".,!?;:")
            if trigger:
                self._shortcut_map[trigger] = sc.get("expansion", "")

    @objc.python_method
    def _check_voice_shortcut(self, text):
        """Return expansion if text matches a voice shortcut trigger, else None."""
        if not hasattr(self, "_shortcut_map"):
            self._rebuild_shortcut_map()
        if not self._shortcut_map:
            return None
        normalized = text.lower().strip().rstrip(".,!?;:")
        return self._shortcut_map.get(normalized)

    # ── Rewrite classification ─────────────────────────────────

    @objc.python_method
    def _is_rewrite_instruction(self, text):
        """Heuristic: does this voice instruction want to transform selected text?"""
        t = text.lower().strip().rstrip("?.!")
        for p in ("can you ", "could you ", "please ", "kan du ",
                   "venligst ", "prøv at ", "ville du "):
            if t.startswith(p):
                t = t[len(p):]
        rewrite_starts = (
            "make", "change", "fix", "rewrite", "translate", "shorten",
            "expand", "improve", "convert", "format", "summarize", "simplify",
            "rephrase", "reword", "correct", "edit", "transform", "replace",
            "add", "remove", "delete", "insert", "move", "swap", "merge",
            "split", "combine", "clean", "tidy", "polish", "refine",
            "write", "draft", "compose",
            "gør", "ændr", "ret", "omskriv", "oversæt", "forkort",
            "udvid", "forbedre", "konverter", "opsummer", "forenkl",
            "tilføj", "fjern", "slet", "indsæt", "flyt", "byt",
            "ryd op", "skriv om", "skriv det om", "skriv", "lav",
        )
        for kw in rewrite_starts:
            if t.startswith(kw):
                return True
        return False

    # ── Status bar ──────────────────────────────────────────────

    @objc.python_method
    def _build_status_bar(self):
        self.status_bar = NSStatusBar.systemStatusBar()
        self.status_item = self.status_bar.statusItemWithLength_(NSVariableStatusItemLength)
        self.status_item.setTitle_("SF")
        menu = NSMenu.alloc().init()
        show = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Show SpeakFlow", "showWindow:", "")
        show.setTarget_(self)
        menu.addItem_(show)

        # Mode submenu
        mode_sub = NSMenu.alloc().init()
        mode_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Mode", None, "")
        mode_item.setSubmenu_(mode_sub)
        menu.addItem_(mode_item)
        self._status_mode_menu = mode_sub
        self._populate_status_mode_menu()

        shortcuts_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Voice Shortcuts", "manageShortcuts:", "")
        shortcuts_item.setTarget_(self)
        menu.addItem_(shortcuts_item)

        update_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Check for Updates", "checkForUpdates:", "")
        update_item.setTarget_(self)
        menu.addItem_(update_item)
        menu.addItem_(NSMenuItem.separatorItem())
        quit_item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_("Quit", "terminate:", "q")
        menu.addItem_(quit_item)
        self.status_item.setMenu_(menu)

    def showWindow_(self, sender):
        self.show_window()

    # ── Main window ─────────────────────────────────────────────

    @objc.python_method
    def _build_window(self):
        W, H = 460, 898
        screen = NSScreen.mainScreen().frame()
        cx = (screen.size.width - W) / 2
        cy = (screen.size.height - H) / 2

        mask = 1 | 2 | 4
        self.window = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(cx, cy, W, H), mask, NSBackingStoreBuffered, False)
        self.window.setTitle_("SpeakFlow")
        self.window.setBackgroundColor_(_BG())
        self.window.setReleasedWhenClosed_(False)
        self.window.setTitlebarAppearsTransparent_(True)
        self.window.setTitleVisibility_(1)

        v = self.window.contentView()
        v.setWantsLayer_(True)

        pad = 24
        cw = W - pad * 2
        y = H - 48

        # ── Header ──
        self._label(v, "SpeakFlow", pad, y, cw, 30,
                    NSFont.boldSystemFontOfSize_(26), _WHITE(), True)
        y -= 22
        self._label(v, "Voice to text, effortlessly", pad, y, cw, 18,
                    NSFont.systemFontOfSize_(12), _DIM(), True)
        y -= 40

        # ── Status card ──
        card_h = 146
        sc = self._card(v, pad, y - card_h, cw, card_h)

        self._status_dot = self._dot(sc, 20, card_h - 42, 10, _ACCENT())
        self.status_label = self._label(sc, "Ready", 38, card_h - 48, cw - 56, 24,
                                        NSFont.systemFontOfSize_weight_(15, NSFontWeightSemibold),
                                        _ACCENT(), True)

        # Mode selector
        self._label(sc, "Mode", 20, 58, 50, 24,
                    NSFont.systemFontOfSize_(12), _DIM())
        self.mode_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(70, 56, cw - 160, 26), False)
        self._populate_mode_popup()
        self.mode_popup.setTarget_(self)
        self.mode_popup.setAction_("modeChanged:")
        self.mode_popup.setWantsLayer_(True)
        self.mode_popup.setBordered_(False)
        self.mode_popup.layer().setCornerRadius_(6)
        self.mode_popup.layer().setBackgroundColor_(_SEC_BG().CGColor())
        self.mode_popup.layer().setBorderWidth_(1)
        self.mode_popup.layer().setBorderColor_(_SEC_EDGE().CGColor())
        sc.addSubview_(self.mode_popup)

        self.manage_modes_btn = self._ghost_btn(sc, "Edit", cw - 70, 56, 50, 26)
        self.manage_modes_btn.setTarget_(self)
        self.manage_modes_btn.setAction_("manageModes:")

        bw, bh = 180, 34
        self.rec_button = self._styled_btn(sc, "Start Recording",
                                           (cw - bw) / 2, 14, bw, bh,
                                           color=_GREEN())
        self.rec_button.setTarget_(self)
        self.rec_button.setAction_("toggleRecording:")
        y -= card_h + 16

        # ── Settings card (10 rows) ──
        row_h = 34
        num_rows = 10
        set_h = 44 + num_rows * row_h + 10
        stc = self._card(v, pad, y - set_h, cw, set_h)

        self._label(stc, "Settings", 20, set_h - 34, 150, 20,
                    NSFont.systemFontOfSize_weight_(14, NSFontWeightSemibold), _WHITE())
        self._divider(stc, 16, set_h - 42, cw - 32)

        ry = set_h - 44 - row_h
        lx = 20
        rx = cw - 24

        # Row 0 — API Key
        self._label(stc, "API Key", lx, ry + 6, 80, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.api_key_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(lx + 80, ry + 5, cw - 130, 24))
        key_val = self.config.openai_api_key
        if key_val:
            if len(key_val) <= 8:
                masked = key_val[:2] + "•" * (len(key_val) - 2)
            else:
                masked = key_val[:3] + "•" * (len(key_val) - 7) + key_val[-4:]
            self.api_key_field.setStringValue_(masked)
        else:
            self.api_key_field.setPlaceholderString_("sk-...")
        self.api_key_field.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
        self.api_key_field.setTextColor_(_WHITE())
        self.api_key_field.setDrawsBackground_(False)
        self.api_key_field.setBezeled_(False)
        self.api_key_field.setWantsLayer_(True)
        self.api_key_field.setFocusRingType_(1)
        self.api_key_field.layer().setCornerRadius_(6)
        self.api_key_field.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.16, 1.0).CGColor())
        self.api_key_field.layer().setBorderWidth_(1)
        self.api_key_field.layer().setBorderColor_(_SEC_EDGE().CGColor())
        self.api_key_field.setTarget_(self)
        self.api_key_field.setAction_("apiKeyChanged:")
        stc.addSubview_(self.api_key_field)
        ry -= row_h

        # Row 1 — Hotkey
        self._label(stc, "Hotkey", lx, ry + 6, 80, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        hotkey_text = self.config.hotkey
        if is_modifier_only(hotkey_text):
            hotkey_text += " (hold)"
        self.hotkey_display = self._label(stc, hotkey_text, lx + 80, ry + 6, cw - 230, 24,
                                          NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold),
                                          _GOLD())
        self.hotkey_btn = self._ghost_btn(stc, "Change",
                                         rx - 90, ry + 5, 90, 26)
        self.hotkey_btn.setTarget_(self)
        self.hotkey_btn.setAction_("captureHotkey:")
        ry -= row_h

        # Row 2 — Language
        self._label(stc, "Language", lx, ry + 6, 100, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.lang_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(rx - 160, ry + 5, 160, 26), False)
        for lang in LANG_OPTIONS:
            self.lang_popup.addItemWithTitle_(lang)
        current = {v2: k for k, v2 in LANG_CODES.items()}.get(self.config.language, "Danish")
        self.lang_popup.selectItemWithTitle_(current)
        self.lang_popup.setTarget_(self)
        self.lang_popup.setAction_("languageChanged:")
        self.lang_popup.setWantsLayer_(True)
        self.lang_popup.setBordered_(False)
        self.lang_popup.layer().setCornerRadius_(6)
        self.lang_popup.layer().setBackgroundColor_(_SEC_BG().CGColor())
        self.lang_popup.layer().setBorderWidth_(1)
        self.lang_popup.layer().setBorderColor_(_SEC_EDGE().CGColor())
        stc.addSubview_(self.lang_popup)
        ry -= row_h

        # Row — Microphone
        self._label(stc, "Microphone", lx, ry + 6, 100, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.mic_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(rx - 220, ry + 5, 220, 26), False)
        self.mic_popup.addItemWithTitle_("System Default")
        self._mic_devices = AudioRecorder.list_input_devices()
        saved_mic = self.config.microphone
        for dev in self._mic_devices:
            self.mic_popup.addItemWithTitle_(dev["name"])
            if saved_mic is not None and dev["id"] == saved_mic:
                self.mic_popup.selectItemWithTitle_(dev["name"])
        self.mic_popup.setTarget_(self)
        self.mic_popup.setAction_("micChanged:")
        self.mic_popup.setWantsLayer_(True)
        self.mic_popup.setBordered_(False)
        self.mic_popup.layer().setCornerRadius_(6)
        self.mic_popup.layer().setBackgroundColor_(_SEC_BG().CGColor())
        self.mic_popup.layer().setBorderWidth_(1)
        self.mic_popup.layer().setBorderColor_(_SEC_EDGE().CGColor())
        stc.addSubview_(self.mic_popup)
        ry -= row_h

        # Row — Cleanup Level
        self._label(stc, "Cleanup Level", lx, ry + 6, 120, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.cleanup_popup = NSPopUpButton.alloc().initWithFrame_pullsDown_(
            NSMakeRect(rx - 140, ry + 5, 140, 26), False)
        for lvl in ["Off", "Light", "Medium"]:
            self.cleanup_popup.addItemWithTitle_(lvl)
        _level_titles = {"off": "Off", "light": "Light", "medium": "Medium"}
        self.cleanup_popup.selectItemWithTitle_(
            _level_titles.get(self.config.editing_strength, "Medium"))
        self.cleanup_popup.setTarget_(self)
        self.cleanup_popup.setAction_("cleanupLevelChanged:")
        self.cleanup_popup.setWantsLayer_(True)
        self.cleanup_popup.setBordered_(False)
        self.cleanup_popup.layer().setCornerRadius_(6)
        self.cleanup_popup.layer().setBackgroundColor_(_SEC_BG().CGColor())
        self.cleanup_popup.layer().setBorderWidth_(1)
        self.cleanup_popup.layer().setBorderColor_(_SEC_EDGE().CGColor())
        stc.addSubview_(self.cleanup_popup)
        ry -= row_h

        # Row 4 — Context Cleanup
        self._label(stc, "Smart Context", lx, ry + 6, 140, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.context_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(rx - 24, ry + 7, 22, 22))
        self.context_btn.setButtonType_(3)
        self.context_btn.setTitle_("")
        self.context_btn.setState_(1 if self.config.context_cleanup else 0)
        self.context_btn.setTarget_(self)
        self.context_btn.setAction_("contextToggled:")
        stc.addSubview_(self.context_btn)
        ry -= row_h

        # Row — My Words (personal dictionary)
        self._label(stc, "My Words", lx, ry + 6, 90, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.dict_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(lx + 90, ry + 4, cw - 120, 26))
        self.dict_field.setPlaceholderString_("Names, terms (comma-separated)")
        current_words = ", ".join(self.config.personal_dictionary)
        if current_words:
            self.dict_field.setStringValue_(current_words)
        self.dict_field.setFont_(NSFont.systemFontOfSize_(11))
        self.dict_field.setTextColor_(_WHITE())
        self.dict_field.setDrawsBackground_(False)
        self.dict_field.setBezeled_(False)
        self.dict_field.setWantsLayer_(True)
        self.dict_field.setFocusRingType_(1)
        self.dict_field.layer().setCornerRadius_(6)
        self.dict_field.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(
                0.12, 0.12, 0.16, 1.0).CGColor())
        self.dict_field.layer().setBorderWidth_(1)
        self.dict_field.layer().setBorderColor_(_SEC_EDGE().CGColor())
        self.dict_field.setTarget_(self)
        self.dict_field.setAction_("dictChanged:")
        stc.addSubview_(self.dict_field)
        ry -= row_h

        # Row — Sound
        self._label(stc, "Sound Feedback", lx, ry + 6, 140, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.sound_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(rx - 24, ry + 7, 22, 22))
        self.sound_btn.setButtonType_(3)
        self.sound_btn.setTitle_("")
        self.sound_btn.setState_(1 if self.config.sound_feedback else 0)
        self.sound_btn.setTarget_(self)
        self.sound_btn.setAction_("soundToggled:")
        stc.addSubview_(self.sound_btn)
        ry -= row_h

        # Row 6 — Volume
        self._label(stc, "Volume", lx, ry + 6, 80, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.volume_slider = NSSlider.alloc().initWithFrame_(
            NSMakeRect(rx - 160, ry + 8, 140, 20))
        self.volume_slider.setMinValue_(0.0)
        self.volume_slider.setMaxValue_(1.0)
        self.volume_slider.setFloatValue_(self.config.sound_volume)
        self.volume_slider.setTarget_(self)
        self.volume_slider.setAction_("volumeChanged:")
        self.volume_slider.setContinuous_(True)
        stc.addSubview_(self.volume_slider)
        ry -= row_h

        # Row 7 — Auto-start
        self._label(stc, "Start at Login", lx, ry + 6, 140, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.autostart_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(rx - 24, ry + 7, 22, 22))
        self.autostart_btn.setButtonType_(3)
        self.autostart_btn.setTitle_("")
        self.autostart_btn.setState_(1 if self.config.auto_start else 0)
        self.autostart_btn.setTarget_(self)
        self.autostart_btn.setAction_("autostartToggled:")
        stc.addSubview_(self.autostart_btn)

        y -= set_h + 16

        # ── Last transcription card ──
        lt_h = 86
        ltc = self._card(v, pad, y - lt_h, cw, lt_h)

        self._label(ltc, "Last Transcription", 20, lt_h - 30, 200, 18,
                    NSFont.systemFontOfSize_weight_(12, NSFontWeightSemibold), _DIM())

        self._last_text_label = NSTextField.alloc().initWithFrame_(
            NSMakeRect(20, 8, cw - 40, 44))
        self._last_text_label.setStringValue_("No transcriptions yet")
        self._last_text_label.setBezeled_(False)
        self._last_text_label.setDrawsBackground_(False)
        self._last_text_label.setEditable_(False)
        self._last_text_label.setSelectable_(True)
        self._last_text_label.setFont_(NSFont.systemFontOfSize_(12))
        self._last_text_label.setTextColor_(_WHITE())
        self._last_text_label.setLineBreakMode_(4)  # truncate tail
        self._last_text_label.setMaximumNumberOfLines_(2)
        ltc.addSubview_(self._last_text_label)

        y -= lt_h + 12

        # ── Footer ──
        btn_w = 96
        gap = 8
        total = btn_w * 4 + gap * 3
        bx = (W - total) / 2

        hist_btn = self._ghost_btn(v, "History",
                                   bx, y - 4, btn_w, 26)
        hist_btn.setTarget_(self)
        hist_btn.setAction_("showHistory:")

        sc_btn = self._ghost_btn(v, "Shortcuts",
                                 bx + btn_w + gap, y - 4, btn_w, 26)
        sc_btn.setTarget_(self)
        sc_btn.setAction_("manageShortcuts:")

        help_btn = self._ghost_btn(v, "Help & Guide",
                                   bx + (btn_w + gap) * 2, y - 4, btn_w, 26)
        help_btn.setTarget_(self)
        help_btn.setAction_("showGuide:")

        self._update_btn = self._ghost_btn(v, "Update",
                                          bx + (btn_w + gap) * 3, y - 4, btn_w, 26)
        self._update_btn.setTarget_(self)
        self._update_btn.setAction_("checkForUpdates:")
        y -= 30

        self._label(v, "Switch modes via popup above or right-click the floating bar",
                    pad, y, cw, 14, NSFont.systemFontOfSize_(10), _DIM(), True)
        y -= 16
        self._label(v, f"v{VERSION}", pad, y, cw, 12,
                    NSFont.systemFontOfSize_(9), _DIM(), True)

    # ── Floating waveform indicator ─────────────────────────────

    @objc.python_method
    def _build_floating_indicator(self):
        self._float_num_bars = 5
        self._float_bar_w = 3.5
        self._float_bar_gap = 3
        self._float_min_h = 4
        self._float_max_h = 20
        self._float_mode = None
        self._anim_tick = 0
        self._level_timer = None

        fw, fh = 90, 30
        screen = NSScreen.mainScreen()
        full = screen.frame()
        visible = screen.visibleFrame()
        dock_top = visible.origin.y
        x = (full.size.width - fw) / 2
        y = dock_top + 10

        # NSPanel with non-activating mask (1 << 7) so clicking doesn't
        # bring the main window to front or activate the app.
        self._float_win = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, fw, fh), 1 << 7, NSBackingStoreBuffered, False)
        self._float_win.setLevel_(NSFloatingWindowLevel)
        self._float_win.setOpaque_(False)
        self._float_win.setBackgroundColor_(NSColor.clearColor())
        self._float_win.setIgnoresMouseEvents_(False)
        self._float_win.setMovableByWindowBackground_(True)
        self._float_win.setCollectionBehavior_(1 << 1 | 1 << 4)
        self._float_win.setReleasedWhenClosed_(False)
        self._float_win.setHasShadow_(True)
        self._float_win.setFloatingPanel_(True)
        self._float_win.setBecomesKeyOnlyIfNeeded_(True)
        self._float_win.setHidesOnDeactivate_(False)

        blur = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, fw, fh))
        blur.setMaterial_(11)
        blur.setBlendingMode_(0)
        blur.setState_(1)
        blur.setWantsLayer_(True)
        blur.layer().setCornerRadius_(fh / 2)
        blur.layer().setMasksToBounds_(True)
        blur.layer().setBorderWidth_(0.5)
        blur.layer().setBorderColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.4, 0.4, 0.45, 0.4).CGColor())

        dot_sz = 6
        self._float_dot = self._dot(blur, 12, (fh - dot_sz) / 2, dot_sz, _ACCENT())

        bars_x = 12 + dot_sz + 8
        self._float_bars = []
        for i in range(self._float_num_bars):
            bx = bars_x + i * (self._float_bar_w + self._float_bar_gap)
            h = self._float_min_h
            by = (fh - h) / 2
            bar = NSView.alloc().initWithFrame_(NSMakeRect(bx, by, self._float_bar_w, h))
            bar.setWantsLayer_(True)
            bar.layer().setCornerRadius_(self._float_bar_w / 2)
            bar.layer().setBackgroundColor_(_ACCENT().CGColor())
            blur.addSubview_(bar)
            self._float_bars.append(bar)

        # Clickable button over the entire float
        click_btn = NSButton.alloc().initWithFrame_(NSMakeRect(0, 0, fw, fh))
        click_btn.setTransparent_(True)
        click_btn.setTarget_(self)
        click_btn.setAction_("floatClicked:")
        blur.addSubview_(click_btn)

        # Right-click menu for quick mode switching
        self._float_menu = NSMenu.alloc().init()
        self._populate_float_menu()
        click_btn.setMenu_(self._float_menu)

        self._float_win.contentView().addSubview_(blur)
        self._float_fh = fh

        # Always visible in idle state
        self._float_win.orderFront_(None)

    def floatClicked_(self, sender):
        if self._processing:
            return
        if self._recording:
            self._on_deactivate()
        else:
            self._float_triggered = True
            self._on_activate()

    def updateLevels_(self, timer):
        self._anim_tick += 1
        fh = self._float_fh
        min_h = self._float_min_h
        max_h = self._float_max_h
        bw = self._float_bar_w

        if self._float_mode == "recording":
            rms = self.audio_recorder.current_rms
            level = min(1.0, rms / 0.06)
            for bar in self._float_bars:
                bl = level * (0.3 + 0.7 * random.random())
                h = min_h + bl * (max_h - min_h)
                old_x = bar.frame().origin.x
                bar.setFrame_(NSMakeRect(old_x, (fh - h) / 2, bw, h))
        elif self._float_mode == "transcribing":
            for i, bar in enumerate(self._float_bars):
                phase = self._anim_tick * 0.12 + i * 1.0
                bl = 0.25 + 0.20 * math.sin(phase)
                h = min_h + bl * (max_h - min_h)
                old_x = bar.frame().origin.x
                bar.setFrame_(NSMakeRect(old_x, (fh - h) / 2, bw, h))

    @objc.python_method
    def _set_float_color(self, color):
        self._float_dot.layer().setBackgroundColor_(color.CGColor())
        for bar in self._float_bars:
            bar.layer().setBackgroundColor_(color.CGColor())

    @objc.python_method
    def _show_float(self, mode, color):
        self._float_mode = mode
        self._anim_tick = 0
        self._set_float_color(color)
        self._float_win.orderFront_(None)
        if self._level_timer is None:
            self._level_timer = NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
                0.05, self, "updateLevels:", None, True)

    @objc.python_method
    def _hide_float(self):
        """Reset float to idle state (always visible, mode-coloured)."""
        self._float_mode = None
        if self._level_timer is not None:
            self._level_timer.invalidate()
            self._level_timer = None
        fh = self._float_fh
        bw = self._float_bar_w
        min_h = self._float_min_h
        for bar in self._float_bars:
            old_x = bar.frame().origin.x
            bar.setFrame_(NSMakeRect(old_x, (fh - min_h) / 2, bw, min_h))
        mode_color = _MODE_COLORS.get(self.config.active_mode, _ACCENT)()
        self._set_float_color(mode_color)
        self._float_win.orderFront_(None)

    # ── History window ──────────────────────────────────────────

    def showGuide_(self, sender):
        w, h = 520, 620
        if self._guide_win is None:
            screen = NSScreen.mainScreen().frame()
            self._guide_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect((screen.size.width - w) / 2, (screen.size.height - h) / 2, w, h),
                1 | 2 | 4 | 8, NSBackingStoreBuffered, False)
            self._guide_win.setTitle_("SpeakFlow — Guide")
            self._guide_win.setBackgroundColor_(_BG())
            self._guide_win.setReleasedWhenClosed_(False)
            self._guide_win.setTitlebarAppearsTransparent_(True)
            self._guide_win.setTitleVisibility_(1)

        cv = self._guide_win.contentView()
        for sub in list(cv.subviews()):
            sub.removeFromSuperview()

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setBackgroundColor_(_BG())

        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 20, h))
        tv.setEditable_(False)
        tv.setSelectable_(True)
        tv.setBackgroundColor_(_BG())
        tv.setTextColor_(_WHITE())
        tv.setFont_(NSFont.systemFontOfSize_(13))
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.textContainer().setContainerSize_((w - 20, 1e7))
        tv.textContainer().setWidthTracksTextView_(True)

        hotkey_str = self.config.hotkey
        if is_modifier_only(hotkey_str):
            hotkey_desc = f"Hold  {hotkey_str.upper()}  to record, release to stop"
        else:
            hotkey_desc = f"Press  {hotkey_str.upper()}  to toggle recording"

        guide = f"""Welcome to SpeakFlow

Voice-to-text that types what you say, right where your cursor is.


QUICK START

1.  Make sure your API key is set (Settings → API Key)
2.  Grant Accessibility and Microphone permissions when prompted
3.  Choose a mode and use the hotkey to start


MODES

SpeakFlow has multiple modes — switch in the main window or right-click
the floating indicator:

  Dictation (default) — transcribes your speech and types it at your cursor.
      With text selected, it enters Context mode: your speech becomes an AI
      instruction about the selection (e.g. "make this more formal").

  AI Ask — ask any question by voice. The answer is copied to your clipboard.
      Select text first to give the AI context for your question.

  Screen Vision — captures your screen, then listens to your voice instruction.
      The AI analyzes what it sees and responds. Great for "what does this
      error mean?" or "summarize what's on my screen".
      Requires Screen Recording permission.

  VibeCode — describe what you want to build. Your voice is converted into
      a precise, optimized prompt for AI coding tools (Claude Code, Cursor, etc.).
      Select existing code to include it as context.

  Custom Modes — create your own! Click "Edit" next to the mode selector to
      add custom modes with your own system prompts (e.g. "Translate to English",
      "Summarize", "Fix grammar").


HOW IT WORKS

{hotkey_desc}.

In Dictation mode, SpeakFlow automatically detects context:

  No text selected — your speech is transcribed and typed at your cursor.

  Text selected — SpeakFlow grabs the selection, listens to your voice
  instruction, and uses AI to respond. The result is copied to your clipboard.

All other modes record your voice, process it with AI, and copy the result
to your clipboard.


FLOATING INDICATOR

The small floating bar at the bottom of your screen shows the active mode
and recording status. Click to start a quick recording (copies to clipboard).
Right-click to switch modes.

  Colour shows active mode  ·  Red = recording  ·  Orange = processing


SETTINGS

  • Hotkey — change the keyboard shortcut
  • Language — set your dictation language or use auto-detect
  • Cleanup Level — controls how much AI cleans up your speech:
      Off = raw transcription, Light = punctuation and filler words only,
      Medium = full cleanup with grammar and clarity fixes
  • Smart Context — reads text around your cursor for better results:
      names, terms, and writing style are matched automatically
  • My Words — add names, terms, or jargon (comma-separated) that
      should always be spelled correctly in transcriptions
  • Sound Feedback — plays a sound when recording starts/stops
  • Start at Login — launch SpeakFlow automatically


TIPS

  • Recording stops automatically after a silence pause
  • Maximum recording length is 2 hours
  • Check "View History" to see and copy previous transcriptions
  • Add your name, company, and common terms to "My Words" for
    much better transcription accuracy
  • After an update, you may need to re-enable Accessibility permissions
    in System Settings → Privacy & Security → Accessibility
"""

        tv.setString_(guide)
        scroll.setDocumentView_(tv)
        cv.addSubview_(scroll)

        self._guide_win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def showHistory_(self, sender):
        entries = history.load()
        w, h = 480, 500
        if self._history_win is None:
            screen = NSScreen.mainScreen().frame()
            self._history_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect((screen.size.width - w) / 2, (screen.size.height - h) / 2, w, h),
                1 | 2 | 4 | 8, NSBackingStoreBuffered, False)
            self._history_win.setTitle_("Transcription History")
            self._history_win.setBackgroundColor_(_BG())
            self._history_win.setReleasedWhenClosed_(False)
            self._history_win.setTitlebarAppearsTransparent_(True)
            self._history_win.setTitleVisibility_(1)

        # Build content
        cv = self._history_win.contentView()
        for sub in list(cv.subviews()):
            sub.removeFromSuperview()

        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(0, 0, w, h))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setBackgroundColor_(_BG())

        tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, w - 20, h))
        tv.setEditable_(False)
        tv.setSelectable_(True)
        tv.setBackgroundColor_(_BG())
        tv.setTextColor_(_WHITE())
        tv.setFont_(NSFont.systemFontOfSize_(12))
        tv.setVerticallyResizable_(True)
        tv.setHorizontallyResizable_(False)
        tv.textContainer().setContainerSize_((w - 20, 1e7))
        tv.textContainer().setWidthTracksTextView_(True)

        if not entries:
            tv.setString_("No transcriptions yet.")
        else:
            lines = []
            for e in entries:
                ts = e.get("timestamp", "")
                app_name = e.get("app", "")
                text = e.get("text", "")
                header = ts
                if app_name:
                    header += f"  ·  {app_name}"
                lines.append(f"{header}\n{text}\n")
            tv.setString_("\n".join(lines))

        scroll.setDocumentView_(tv)
        cv.addSubview_(scroll)

        self._history_win.makeKeyAndOrderFront_(None)

    # ── Actions ─────────────────────────────────────────────────

    def toggleRecording_(self, sender):
        if self._recording:
            self._on_deactivate()
        else:
            self._on_activate()

    def captureHotkey_(self, sender):
        self._capture_target = "main"
        self._start_hotkey_capture()


    @objc.python_method
    def _start_hotkey_capture(self):
        try:
            if self._capturing:
                return
            self._capturing = True
            self._capture_single_mod = None
            # Listeners stay alive — _capturing flag suppresses their callbacks.

            self.hotkey_display.setStringValue_("Press a key...")
            self.hotkey_display.setTextColor_(_ACCENT())
            self._set_btn_title(self.hotkey_btn, "Listening...",
                                NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium),
                                color=_ACCENT())
            self.hotkey_btn.setEnabled_(False)

            _STANDALONE_KEYS = {
                "f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8", "f9",
                "f10", "f11", "f12", "escape",
            }

            def _get_mods(flags):
                mods = []
                if flags & (1 << 18): mods.append("ctrl")
                if flags & (1 << 20): mods.append("cmd")
                if flags & (1 << 19): mods.append("alt")
                if flags & (1 << 17): mods.append("shift")
                return mods

            def handler(event):
                if not self._capturing:
                    return event
                event_type = event.type()

                if event_type == 12:
                    mods = _get_mods(event.modifierFlags())
                    if len(mods) == 1 and self._capture_single_mod is None:
                        self._capture_single_mod = mods[0]
                    elif len(mods) == 0 and self._capture_single_mod is not None:
                        mod = self._capture_single_mod
                        self._capture_single_mod = None
                        self._remove_key_monitor()
                        self._finish_capture(mod)
                        return None
                    elif len(mods) > 1:
                        self._capture_single_mod = None
                    return event

                if event_type == 10:
                    self._capture_single_mod = None
                    keycode = event.keyCode()
                    key_name = _KEYCODE_MAP.get(keycode)

                    # Escape cancels capture
                    if key_name == "escape":
                        self._remove_key_monitor()
                        self._cancel_capture()
                        return None

                    mods = _get_mods(event.modifierFlags())
                    if key_name and (mods or key_name in _STANDALONE_KEYS):
                        new_hotkey = "+".join(mods + [key_name])
                        self._remove_key_monitor()
                        self._finish_capture(new_hotkey)
                        return None
                    return event

                return event

            self._key_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                (1 << 10) | (1 << 12), handler)
        except Exception:
            logger.error("captureHotkey error:\n%s", traceback.format_exc())
            self._capturing = False

    @objc.python_method
    def _cancel_capture(self):
        """Cancel hotkey capture and restore UI."""
        self._capturing = False
        hotkey_text = self.config.hotkey
        if is_modifier_only(hotkey_text):
            hotkey_text += " (hold)"
        self.hotkey_display.setStringValue_(hotkey_text)
        self.hotkey_display.setTextColor_(_GOLD())
        self._set_btn_title(self.hotkey_btn, "Change",
                            NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium),
                            color=_DIM())
        self.hotkey_btn.setEnabled_(True)

    @objc.python_method
    def _finish_capture(self, new_hotkey):
        self._capturing = False
        try:
            self.config.hotkey = new_hotkey
            self.hotkey_listener.update_hotkey(new_hotkey)
            display = f"{new_hotkey} (hold)" if is_modifier_only(new_hotkey) else new_hotkey
            self.hotkey_display.setStringValue_(display)
            self.hotkey_display.setTextColor_(_GOLD())
            logger.info("Hotkey changed to %s.", new_hotkey)
        except Exception:
            logger.error("Hotkey apply error:\n%s", traceback.format_exc())
        finally:
            self._set_btn_title(self.hotkey_btn, "Change",
                                NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium),
                                color=_DIM())
            self.hotkey_btn.setEnabled_(True)

    def apiKeyChanged_(self, sender):
        raw = sender.stringValue().strip()
        if not raw or "•" in raw:
            # User didn't actually change it (masked value or empty)
            return
        self.config.openai_api_key = raw
        self.transcriber.client = openai.OpenAI(api_key=raw, max_retries=5)
        if len(raw) <= 8:
            masked = raw[:2] + "•" * (len(raw) - 2)
        else:
            masked = raw[:3] + "•" * (len(raw) - 7) + raw[-4:]
        sender.setStringValue_(masked)
        self.status_label.setStringValue_("API key saved")
        self.status_label.setTextColor_(_GREEN())
        logger.info("API key updated.")
        self._auto_clear_after(2.0)

    def micChanged_(self, sender):
        idx = sender.indexOfSelectedItem()
        if idx == 0:
            # System Default
            self.config.microphone = None
            self.audio_recorder.device = None
        else:
            dev = self._mic_devices[idx - 1]
            self.config.microphone = dev["id"]
            self.audio_recorder.device = dev["id"]

    def languageChanged_(self, sender):
        name = sender.titleOfSelectedItem()
        code = LANG_CODES.get(name, "da")
        self.config.language = code
        self.config.auto_language_detect = (code == "auto")
        self.transcriber.language = code
        self.transcriber.auto_detect = (code == "auto")

    def cleanupLevelChanged_(self, sender):
        title = sender.titleOfSelectedItem()
        _level_map = {"Off": "off", "Light": "light", "Medium": "medium"}
        level = _level_map.get(title, "medium")
        self.config.editing_strength = level
        self.transcriber.editing_strength = level

    def dictChanged_(self, sender):
        raw = sender.stringValue()
        words = [w.strip() for w in raw.split(",") if w.strip()]
        self.config.personal_dictionary = words
        self.transcriber.personal_dictionary = words

    def contextToggled_(self, sender):
        self.config.context_cleanup = bool(sender.state())

    def soundToggled_(self, sender):
        self.config.sound_feedback = bool(sender.state())

    def volumeChanged_(self, sender):
        vol = sender.floatValue()
        set_volume(vol)
        if self._vol_save_timer is not None:
            self._vol_save_timer.cancel()
        self._vol_save_timer = threading.Timer(
            0.5, lambda: self.config.set("sound_volume", max(0.0, min(1.0, vol))))
        self._vol_save_timer.start()

    def autostartToggled_(self, sender):
        enabled = bool(sender.state())
        self.config.auto_start = enabled
        self._set_auto_start(enabled)

    # ── Mode management ──────────────────────────────────────────

    @objc.python_method
    def _populate_mode_popup(self):
        self.mode_popup.removeAllItems()
        for mode_id in _BUILTIN_MODES:
            self.mode_popup.addItemWithTitle_(_MODE_NAMES[mode_id])
        custom = self.config.custom_modes
        if custom:
            self.mode_popup.menu().addItem_(NSMenuItem.separatorItem())
            for cm in custom:
                self.mode_popup.addItemWithTitle_(cm["name"])
        current = self.config.active_mode
        if current in _MODE_NAMES:
            self.mode_popup.selectItemWithTitle_(_MODE_NAMES[current])
        else:
            self.mode_popup.selectItemWithTitle_(current)

    @objc.python_method
    def _populate_status_mode_menu(self):
        self._status_mode_menu.removeAllItems()
        current = self.config.active_mode
        for mode_id in _BUILTIN_MODES:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                _MODE_NAMES[mode_id], "statusBarModeSelected:", "")
            item.setTarget_(self)
            if current == mode_id:
                item.setState_(1)
            self._status_mode_menu.addItem_(item)
        custom = self.config.custom_modes
        if custom:
            self._status_mode_menu.addItem_(NSMenuItem.separatorItem())
            for cm in custom:
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    cm["name"], "statusBarModeSelected:", "")
                item.setTarget_(self)
                if current == cm["name"]:
                    item.setState_(1)
                self._status_mode_menu.addItem_(item)

    @objc.python_method
    def _populate_float_menu(self):
        self._float_menu.removeAllItems()
        for mode_id in _BUILTIN_MODES:
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                _MODE_NAMES[mode_id], "floatModeSelected:", "")
            item.setTarget_(self)
            if self.config.active_mode == mode_id:
                item.setState_(1)
            self._float_menu.addItem_(item)
        custom = self.config.custom_modes
        if custom:
            self._float_menu.addItem_(NSMenuItem.separatorItem())
            for cm in custom:
                item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(
                    cm["name"], "floatModeSelected:", "")
                item.setTarget_(self)
                if self.config.active_mode == cm["name"]:
                    item.setState_(1)
                self._float_menu.addItem_(item)

    @objc.python_method
    def _update_mode_idle_color(self):
        if not self._recording and not self._processing:
            color = _MODE_COLORS.get(self.config.active_mode, _ACCENT)()
            self._set_float_color(color)
            self._status_dot.layer().setBackgroundColor_(color.CGColor())
            self.status_label.setTextColor_(color)

    def modeChanged_(self, sender):
        title = sender.titleOfSelectedItem()
        mode_id = _MODE_IDS.get(title)
        self.config.active_mode = mode_id if mode_id else title
        self._populate_float_menu()
        self._populate_status_mode_menu()
        self._update_mode_idle_color()
        logger.info("Mode changed to: %s", self.config.active_mode)

    def floatModeSelected_(self, sender):
        title = sender.title()
        mode_id = _MODE_IDS.get(title)
        self.config.active_mode = mode_id if mode_id else title
        self._populate_float_menu()
        self._populate_status_mode_menu()
        self._populate_mode_popup()
        current = self.config.active_mode
        if current in _MODE_NAMES:
            self.mode_popup.selectItemWithTitle_(_MODE_NAMES[current])
        else:
            self.mode_popup.selectItemWithTitle_(current)
        self._update_mode_idle_color()
        logger.info("Mode changed via float: %s", self.config.active_mode)

    def statusBarModeSelected_(self, sender):
        title = sender.title()
        mode_id = _MODE_IDS.get(title)
        self.config.active_mode = mode_id if mode_id else title
        self._populate_float_menu()
        self._populate_status_mode_menu()
        self._populate_mode_popup()
        current = self.config.active_mode
        if current in _MODE_NAMES:
            self.mode_popup.selectItemWithTitle_(_MODE_NAMES[current])
        else:
            self.mode_popup.selectItemWithTitle_(current)
        self._update_mode_idle_color()

    def manageModes_(self, sender):
        self._build_mode_manager()

    @objc.python_method
    def _build_mode_manager(self):
        w, h = 420, 400
        if self._mode_mgr_win is None:
            screen = NSScreen.mainScreen().frame()
            self._mode_mgr_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect((screen.size.width - w) / 2, (screen.size.height - h) / 2, w, h),
                1 | 2 | 4 | 8, NSBackingStoreBuffered, False)
            self._mode_mgr_win.setTitle_("Custom Modes")
            self._mode_mgr_win.setBackgroundColor_(_BG())
            self._mode_mgr_win.setReleasedWhenClosed_(False)
            self._mode_mgr_win.setTitlebarAppearsTransparent_(True)
            self._mode_mgr_win.setTitleVisibility_(1)
        cv = self._mode_mgr_win.contentView()
        for sub in list(cv.subviews()):
            sub.removeFromSuperview()

        self._label(cv, "Custom Modes", 20, h - 48, 200, 24,
                    NSFont.systemFontOfSize_weight_(16, NSFontWeightSemibold), _WHITE())
        add_btn = self._styled_btn(cv, "Add New Mode", w - 170, h - 48, 140, 30, color=_ACCENT())
        add_btn.setTarget_(self)
        add_btn.setAction_("addCustomMode:")

        modes = self.config.custom_modes
        y_pos = h - 90
        if not modes:
            self._label(cv, "No custom modes yet. Click 'Add New Mode' to create one.",
                        20, y_pos, w - 40, 44, NSFont.systemFontOfSize_(13), _DIM())
        else:
            for i, cm in enumerate(modes):
                mc = self._card(cv, 16, y_pos - 66, w - 32, 62)
                self._label(mc, cm["name"], 16, 30, 220, 24,
                            NSFont.systemFontOfSize_weight_(14, NSFontWeightSemibold), _WHITE())
                preview = cm["prompt"][:70] + ("..." if len(cm["prompt"]) > 70 else "")
                self._label(mc, preview, 16, 8, w - 130, 20,
                            NSFont.systemFontOfSize_(11), _DIM())
                del_btn = self._ghost_btn(mc, "Delete", w - 32 - 84, 18, 64, 26)
                del_btn.setTag_(i)
                del_btn.setTarget_(self)
                del_btn.setAction_("deleteCustomMode:")
                y_pos -= 74

        self._mode_mgr_win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def addCustomMode_(self, sender):
        w, h = 400, 320
        if self._add_mode_win is None:
            screen = NSScreen.mainScreen().frame()
            self._add_mode_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect((screen.size.width - w) / 2, (screen.size.height - h) / 2, w, h),
                1 | 2 | 4, NSBackingStoreBuffered, False)
            self._add_mode_win.setTitle_("Add Custom Mode")
            self._add_mode_win.setBackgroundColor_(_BG())
            self._add_mode_win.setReleasedWhenClosed_(False)
            self._add_mode_win.setTitlebarAppearsTransparent_(True)
            self._add_mode_win.setTitleVisibility_(1)
        cv = self._add_mode_win.contentView()
        for sub in list(cv.subviews()):
            sub.removeFromSuperview()

        self._label(cv, "Mode Name", 20, h - 54, 120, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self._mode_name_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(20, h - 82, w - 40, 28))
        self._mode_name_field.setPlaceholderString_("e.g., Translate to English")
        self._mode_name_field.setFont_(NSFont.systemFontOfSize_(13))
        self._mode_name_field.setTextColor_(_WHITE())
        self._mode_name_field.setDrawsBackground_(False)
        self._mode_name_field.setBezeled_(False)
        self._mode_name_field.setWantsLayer_(True)
        self._mode_name_field.layer().setCornerRadius_(6)
        self._mode_name_field.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.16, 1.0).CGColor())
        self._mode_name_field.layer().setBorderWidth_(1)
        self._mode_name_field.layer().setBorderColor_(_SEC_EDGE().CGColor())
        cv.addSubview_(self._mode_name_field)

        self._label(cv, "System Prompt", 20, h - 112, 150, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(20, 60, w - 40, h - 130))
        scroll.setHasVerticalScroller_(True)
        scroll.setBorderType_(0)
        scroll.setWantsLayer_(True)
        scroll.layer().setCornerRadius_(6)
        scroll.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.16, 1.0).CGColor())
        scroll.layer().setBorderWidth_(1)
        scroll.layer().setBorderColor_(_SEC_EDGE().CGColor())
        self._mode_prompt_tv = NSTextView.alloc().initWithFrame_(
            NSMakeRect(0, 0, w - 60, h - 130))
        self._mode_prompt_tv.setEditable_(True)
        self._mode_prompt_tv.setSelectable_(True)
        self._mode_prompt_tv.setBackgroundColor_(NSColor.clearColor())
        self._mode_prompt_tv.setTextColor_(_WHITE())
        self._mode_prompt_tv.setFont_(NSFont.systemFontOfSize_(12))
        self._mode_prompt_tv.setString_("")
        scroll.setDocumentView_(self._mode_prompt_tv)
        cv.addSubview_(scroll)

        cancel_btn = self._ghost_btn(cv, "Cancel", w - 210, 18, 90, 32)
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_("cancelAddMode:")
        save_btn = self._styled_btn(cv, "Save", w - 110, 18, 90, 32, color=_GREEN())
        save_btn.setTarget_(self)
        save_btn.setAction_("saveCustomMode:")

        self._add_mode_win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def cancelAddMode_(self, sender):
        self._add_mode_win.close()

    def saveCustomMode_(self, sender):
        name = self._mode_name_field.stringValue().strip()
        prompt = self._mode_prompt_tv.string().strip()
        if not name or not prompt:
            return
        modes = list(self.config.custom_modes)
        for m in modes:
            if m["name"] == name:
                m["prompt"] = prompt
                self.config.custom_modes = modes
                self._populate_mode_popup()
                self._populate_float_menu()
                self._populate_status_mode_menu()
                self._add_mode_win.close()
                return
        modes.append({"name": name, "prompt": prompt})
        self.config.custom_modes = modes
        self.config.active_mode = name
        self._populate_mode_popup()
        self.mode_popup.selectItemWithTitle_(name)
        self._populate_float_menu()
        self._populate_status_mode_menu()
        self._update_mode_idle_color()
        self._add_mode_win.close()
        if self._mode_mgr_win is not None and self._mode_mgr_win.isVisible():
            self._build_mode_manager()
        logger.info("Custom mode added: %s", name)

    def deleteCustomMode_(self, sender):
        idx = sender.tag()
        modes = list(self.config.custom_modes)
        if 0 <= idx < len(modes):
            deleted = modes[idx]["name"]
            modes.pop(idx)
            self.config.custom_modes = modes
            self._populate_mode_popup()
            self._populate_float_menu()
            self._populate_status_mode_menu()
            if self.config.active_mode == deleted:
                self.config.active_mode = "dictation"
                self.mode_popup.selectItemWithTitle_("Dictation")
                self._update_mode_idle_color()
            self._build_mode_manager()
            logger.info("Custom mode deleted: %s", deleted)

    # ── Voice shortcuts management ────────────────────────────

    def manageShortcuts_(self, sender):
        self._build_shortcuts_manager()

    @objc.python_method
    def _build_shortcuts_manager(self):
        w, h = 420, 400
        if self._shortcuts_win is None:
            screen = NSScreen.mainScreen().frame()
            self._shortcuts_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect((screen.size.width - w) / 2, (screen.size.height - h) / 2, w, h),
                1 | 2 | 4 | 8, NSBackingStoreBuffered, False)
            self._shortcuts_win.setTitle_("Voice Shortcuts")
            self._shortcuts_win.setBackgroundColor_(_BG())
            self._shortcuts_win.setReleasedWhenClosed_(False)
            self._shortcuts_win.setTitlebarAppearsTransparent_(True)
            self._shortcuts_win.setTitleVisibility_(1)
        cv = self._shortcuts_win.contentView()
        for sub in list(cv.subviews()):
            sub.removeFromSuperview()

        self._label(cv, "Voice Shortcuts", 20, h - 48, 200, 24,
                    NSFont.systemFontOfSize_weight_(16, NSFontWeightSemibold), _WHITE())
        add_btn = self._styled_btn(cv, "Add New", w - 140, h - 48, 110, 30, color=_ACCENT())
        add_btn.setTarget_(self)
        add_btn.setAction_("addShortcut:")

        shortcuts = self.config.voice_shortcuts
        y_pos = h - 90
        if not shortcuts:
            self._label(cv, "No shortcuts yet. Say a trigger phrase and SpeakFlow\n"
                        "will expand it to the full text automatically.",
                        20, y_pos - 30, w - 40, 44, NSFont.systemFontOfSize_(13), _DIM())
        else:
            for i, sc in enumerate(shortcuts):
                mc = self._card(cv, 16, y_pos - 66, w - 32, 62)
                self._label(mc, sc["trigger"], 16, 30, 220, 24,
                            NSFont.systemFontOfSize_weight_(14, NSFontWeightSemibold), _WHITE())
                preview = sc["expansion"][:60] + ("..." if len(sc["expansion"]) > 60 else "")
                self._label(mc, preview, 16, 8, w - 130, 20,
                            NSFont.systemFontOfSize_(11), _DIM())
                del_btn = self._ghost_btn(mc, "Delete", w - 32 - 84, 18, 64, 26)
                del_btn.setTag_(i)
                del_btn.setTarget_(self)
                del_btn.setAction_("deleteShortcut:")
                y_pos -= 74

        self._shortcuts_win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def addShortcut_(self, sender):
        w, h = 400, 240
        if self._add_shortcut_win is None:
            screen = NSScreen.mainScreen().frame()
            self._add_shortcut_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
                NSMakeRect((screen.size.width - w) / 2, (screen.size.height - h) / 2, w, h),
                1 | 2 | 4, NSBackingStoreBuffered, False)
            self._add_shortcut_win.setTitle_("Add Voice Shortcut")
            self._add_shortcut_win.setBackgroundColor_(_BG())
            self._add_shortcut_win.setReleasedWhenClosed_(False)
            self._add_shortcut_win.setTitlebarAppearsTransparent_(True)
            self._add_shortcut_win.setTitleVisibility_(1)
        cv = self._add_shortcut_win.contentView()
        for sub in list(cv.subviews()):
            sub.removeFromSuperview()

        self._label(cv, "Trigger Phrase", 20, h - 54, 200, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self._shortcut_trigger_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(20, h - 82, w - 40, 28))
        self._shortcut_trigger_field.setPlaceholderString_("e.g., book meeting")
        self._shortcut_trigger_field.setFont_(NSFont.systemFontOfSize_(13))
        self._shortcut_trigger_field.setTextColor_(_WHITE())
        self._shortcut_trigger_field.setDrawsBackground_(False)
        self._shortcut_trigger_field.setBezeled_(False)
        self._shortcut_trigger_field.setWantsLayer_(True)
        self._shortcut_trigger_field.layer().setCornerRadius_(6)
        self._shortcut_trigger_field.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.16, 1.0).CGColor())
        self._shortcut_trigger_field.layer().setBorderWidth_(1)
        self._shortcut_trigger_field.layer().setBorderColor_(_SEC_EDGE().CGColor())
        cv.addSubview_(self._shortcut_trigger_field)

        self._label(cv, "Expands To", 20, h - 112, 200, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self._shortcut_expansion_field = NSTextField.alloc().initWithFrame_(
            NSMakeRect(20, 60, w - 40, h - 130))
        self._shortcut_expansion_field.setPlaceholderString_(
            "e.g., Book a meeting: https://cal.com/me/30min")
        self._shortcut_expansion_field.setFont_(NSFont.systemFontOfSize_(13))
        self._shortcut_expansion_field.setTextColor_(_WHITE())
        self._shortcut_expansion_field.setDrawsBackground_(False)
        self._shortcut_expansion_field.setBezeled_(False)
        self._shortcut_expansion_field.setWantsLayer_(True)
        self._shortcut_expansion_field.layer().setCornerRadius_(6)
        self._shortcut_expansion_field.layer().setBackgroundColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.16, 1.0).CGColor())
        self._shortcut_expansion_field.layer().setBorderWidth_(1)
        self._shortcut_expansion_field.layer().setBorderColor_(_SEC_EDGE().CGColor())
        cv.addSubview_(self._shortcut_expansion_field)

        cancel_btn = self._ghost_btn(cv, "Cancel", w - 210, 18, 90, 32)
        cancel_btn.setTarget_(self)
        cancel_btn.setAction_("cancelAddShortcut:")
        save_btn = self._styled_btn(cv, "Save", w - 110, 18, 90, 32, color=_GREEN())
        save_btn.setTarget_(self)
        save_btn.setAction_("saveShortcut:")

        self._add_shortcut_win.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    def cancelAddShortcut_(self, sender):
        self._add_shortcut_win.close()

    def saveShortcut_(self, sender):
        trigger = self._shortcut_trigger_field.stringValue().strip()
        expansion = self._shortcut_expansion_field.stringValue().strip()
        if not trigger or not expansion:
            return
        shortcuts = list(self.config.voice_shortcuts)
        for sc in shortcuts:
            if sc["trigger"].lower() == trigger.lower():
                sc["expansion"] = expansion
                self.config.voice_shortcuts = shortcuts
                self._rebuild_shortcut_map()
                self._add_shortcut_win.close()
                if self._shortcuts_win is not None and self._shortcuts_win.isVisible():
                    self._build_shortcuts_manager()
                return
        shortcuts.append({"trigger": trigger, "expansion": expansion})
        self.config.voice_shortcuts = shortcuts
        self._rebuild_shortcut_map()
        self._add_shortcut_win.close()
        if self._shortcuts_win is not None and self._shortcuts_win.isVisible():
            self._build_shortcuts_manager()
        logger.info("Voice shortcut added: %s", trigger)

    def deleteShortcut_(self, sender):
        idx = sender.tag()
        shortcuts = list(self.config.voice_shortcuts)
        if 0 <= idx < len(shortcuts):
            deleted = shortcuts[idx]["trigger"]
            shortcuts.pop(idx)
            self.config.voice_shortcuts = shortcuts
            self._rebuild_shortcut_map()
            self._build_shortcuts_manager()
            logger.info("Voice shortcut deleted: %s", deleted)

    @objc.python_method
    def _set_auto_start(self, enabled):
        if enabled:
            plist = f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.speakflow.app</string>
    <key>ProgramArguments</key>
    <array>
        <string>/usr/bin/open</string>
        <string>{_APP_PATH}</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>"""
            _LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
            _LAUNCH_AGENT.write_text(plist)
            logger.info("Auto-start enabled.")
        else:
            if _LAUNCH_AGENT.exists():
                _LAUNCH_AGENT.unlink()
            logger.info("Auto-start disabled.")

    # ── Update ─────────────────────────────────────────────────

    def checkForUpdates_(self, sender):
        self._set_btn_title(self._update_btn, "Checking...",
                            NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium),
                            color=_DIM())
        self._update_btn.setEnabled_(False)
        threading.Thread(target=self._do_update, daemon=True).start()

    @objc.python_method
    def _do_update(self):
        install_dir = Path.home() / ".speakflow"
        try:
            # Check if it's a git repo
            if not (install_dir / ".git").exists():
                self._run_on_main(lambda: self._show_update_result(
                    "Manual install — use install.sh to update"))
                return

            # Fetch latest
            result = subprocess.run(
                ["git", "fetch"], cwd=str(install_dir),
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                self._run_on_main(lambda: self._show_update_result(
                    "No internet connection"))
                return

            # Check if behind
            status = subprocess.run(
                ["git", "status", "-uno"], cwd=str(install_dir),
                capture_output=True, text=True, timeout=5,
            )
            if "behind" not in status.stdout:
                self._run_on_main(lambda: self._show_update_result(
                    "You're on the latest version", is_success=True))
                return

            # Stash local changes (if any) before pulling
            subprocess.run(
                ["git", "stash"], cwd=str(install_dir),
                capture_output=True, text=True, timeout=10,
            )

            # Pull
            pull = subprocess.run(
                ["git", "pull", "--quiet"], cwd=str(install_dir),
                capture_output=True, text=True, timeout=30,
            )
            if pull.returncode != 0:
                self._run_on_main(lambda: self._show_update_result(
                    "Update failed — run update.sh manually"))
                return

            # Reinstall deps
            venv_pip = str(install_dir / "venv" / "bin" / "pip")
            subprocess.run(
                [venv_pip, "install", "--quiet", "-r", str(install_dir / "requirements.txt")],
                capture_output=True, timeout=60,
            )

            # Rebuild launcher, re-embed Python, re-sign bundle
            if _APP_PATH.exists():
                launcher_c = install_dir / "launcher.c"
                if launcher_c.exists():
                    app_bin = _APP_PATH / "Contents" / "MacOS"
                    subprocess.run(
                        ["cc", "-o", str(app_bin / "SpeakFlow"), str(launcher_c)],
                        capture_output=True, timeout=30,
                    )
                    import os as _os, sys as _sys, shutil
                    real_py = _os.path.realpath(_sys.executable)
                    if real_py and _os.path.isfile(real_py):
                        embedded = str(app_bin / "python3")
                        if _os.path.realpath(embedded) != real_py:
                            shutil.copy2(real_py, embedded)
                        # Fix dylib path so embedded python finds its framework
                        fw_dir = _os.path.dirname(_os.path.dirname(real_py))
                        dylib = _os.path.join(fw_dir, "Python3")
                        if _os.path.isfile(dylib):
                            subprocess.run(
                                ["install_name_tool", "-change",
                                 "@executable_path/../Python3", dylib, embedded],
                                capture_output=True, timeout=10,
                            )
                            subprocess.run(
                                ["install_name_tool", "-change",
                                 "@rpath/Python3.framework/Versions/3.9/Python3",
                                 dylib, embedded],
                                capture_output=True, timeout=10,
                            )
                    subprocess.run(
                        ["codesign", "--force", "--deep", "--sign", "-", str(_APP_PATH)],
                        capture_output=True, timeout=30,
                    )
                    logger.info("Rebuilt and re-signed .app bundle.")

            logger.info("Update completed — restarting.")
            self._run_on_main(self._restart_app)

        except Exception as exc:
            logger.error("Update failed: %s", exc)
            self._run_on_main(lambda: self._show_update_result(
                f"Update error — try again later"))

    @objc.python_method
    def _restart_app(self):
        """Restart the application after a successful update."""
        import sys
        self.status_label.setStringValue_("Restarting...")
        self.status_label.setTextColor_(_GREEN())
        install_dir = Path.home() / ".speakflow"
        if _APP_PATH.exists():
            subprocess.Popen(
                ["bash", "-c", "sleep 1 && open \"$1\"", "_", str(_APP_PATH)],
                start_new_session=True,
            )
        else:
            script = sys.argv[0] if sys.argv else str(install_dir / "run.py")
            subprocess.Popen(
                ["bash", "-c", "sleep 1 && \"$1\" \"$2\"", "_", sys.executable, script],
                start_new_session=True,
            )
        NSApp.terminate_(None)

    @objc.python_method
    def _show_update_result(self, msg, is_success=False):
        self._set_btn_title(self._update_btn, "Update",
                            NSFont.systemFontOfSize_weight_(11, NSFontWeightMedium),
                            color=_DIM())
        self._update_btn.setEnabled_(True)
        self.status_label.setStringValue_(msg)
        self.status_label.setTextColor_(_GREEN() if is_success else _ACCENT())
        self._auto_clear_after(4.0)

    # ── Recording ───────────────────────────────────────────────

    @objc.python_method
    def _on_activate(self):
        if self._capturing:
            return
        if not self.config.openai_api_key:
            self._run_on_main(lambda: self._ui_error("Add your API key first"))
            return
        with self._stop_lock:
            if self._recording or self._processing:
                return
            self._recording = True
        self._active_app = self._run_on_main_sync(self._get_active_app)
        self._target_running_app = self._run_on_main_sync(self._get_active_running_app)

        mode = self.config.active_mode

        self._selected_text = ""
        self._before_text = ""
        self._after_text = ""
        self._context_mode = False
        self._screenshot_b64 = ""

        if not self._float_triggered:
            # Vision / Auto: capture screen before recording
            if mode == "vision":
                self._screenshot_b64 = capture_screen_base64()
                if not self._screenshot_b64:
                    self._recording = False
                    self._float_triggered = False
                    self._run_on_main(lambda: self._ui_error(
                        "Screen capture failed — grant Screen Recording permission"))
                    return
            elif mode == "auto":
                if has_screen_recording_permission():
                    self._screenshot_b64 = capture_screen_base64()

            # Grab selection + surrounding text in one AX round-trip
            sel, ctx_before, ctx_after = self._run_on_main_sync(self._grab_text_context)

            if self.config.context_cleanup:
                self._before_text, self._after_text = ctx_before, ctx_after

            if mode in ("dictation", "auto"):
                if sel and sel.strip():
                    self._selected_text = sel
                    self._context_mode = True
                    logger.info("Context mode: grabbed %d chars from %s.",
                                len(sel), self._active_app)
            elif mode in ("ask", "vibecode") or mode not in _BUILTIN_MODES:
                if sel and sel.strip():
                    self._selected_text = sel

        # Re-check: deactivate may have raced us during _grab_selection
        if not self._recording:
            self._context_mode = False
            self._processing = False
            self._float_triggered = False
            self._screenshot_b64 = ""
            self._run_on_main(self._ui_ready)
            return

        if self._context_mode:
            self._run_on_main(lambda: self._show_float("recording", _PURPLE()))
            self._run_on_main(self._ui_context_recording)
        else:
            self._run_on_main(self._ui_recording)

        try:
            if self.config.sound_feedback:
                play_start_sound()
            if self.audio_recorder.is_recording:
                try:
                    self.audio_recorder.stop_recording()
                except Exception:
                    pass
            if not self._recording:
                self._context_mode = False
                self._float_triggered = False
                self._processing = False
                self._screenshot_b64 = ""
                self._run_on_main(self._ui_ready)
                return
            self.audio_recorder.start_recording()
            if not self._recording:
                try:
                    self.audio_recorder.stop_recording()
                except Exception:
                    pass
                self._context_mode = False
                self._float_triggered = False
                self._processing = False
                self._screenshot_b64 = ""
                self._run_on_main(self._ui_ready)
                return
            logger.info("Recording started (mode=%s, app=%s).", mode, self._active_app)
        except Exception:
            logger.error("Record start failed:\n%s", traceback.format_exc())
            self._recording = False
            self._context_mode = False
            self._processing = False
            self._float_triggered = False
            self._screenshot_b64 = ""
            self._run_on_main(self._ui_ready)

    @objc.python_method
    def _on_deactivate(self):
        if self._capturing or not self._recording:
            return
        if self._context_mode:
            threading.Thread(target=self._context_stop_and_process, daemon=True).start()
        else:
            threading.Thread(target=self._stop_and_transcribe, daemon=True).start()

    @objc.python_method
    def _on_silence(self):
        if not self._recording:
            return
        if self._context_mode:
            threading.Thread(target=self._context_stop_and_process, daemon=True).start()
        else:
            threading.Thread(target=self._stop_and_transcribe, daemon=True).start()

    @objc.python_method
    def _on_record_error(self, msg):
        """Called from the recording thread when it crashes."""
        logger.error("Recording thread error: %s", msg)
        with self._stop_lock:
            self._recording = False
            self._processing = False
            self._context_mode = False
            self._float_triggered = False
            self._screenshot_b64 = ""
        self._run_on_main(lambda: self._ui_error(msg))

    # ── Context mode (select + voice → AI response) ────────────

    @objc.python_method
    def _grab_text_context(self):
        """Grab selection and surrounding text in a single AX round-trip.

        Returns (selected_text, before_text, after_text).
        """
        try:
            system_wide = ApplicationServices.AXUIElementCreateSystemWide()
            err, focused = ApplicationServices.AXUIElementCopyAttributeValue(
                system_wide, "AXFocusedUIElement", None)
            if err != 0 or focused is None:
                return ("", "", "")

            # Selected text
            selected = ""
            err, sel_val = ApplicationServices.AXUIElementCopyAttributeValue(
                focused, "AXSelectedText", None)
            if err == 0 and sel_val:
                selected = str(sel_val).strip()

            # Surrounding text
            before, after = "", ""
            err, value = ApplicationServices.AXUIElementCopyAttributeValue(
                focused, "AXValue", None)
            if err == 0 and value:
                full_text = str(value)
                err, range_val = ApplicationServices.AXUIElementCopyAttributeValue(
                    focused, "AXSelectedTextRange", None)
                if err == 0 and range_val is not None:
                    try:
                        r = range_val.rangeValue()
                        before = full_text[:r.location][-300:]
                        after = full_text[r.location + r.length:][:200]
                    except Exception:
                        before = full_text[-300:]
                else:
                    before = full_text[-300:]

            return (selected, before, after)
        except Exception:
            logger.debug("_grab_text_context failed", exc_info=True)
            return ("", "", "")

    @objc.python_method
    def _set_clipboard(self, text):
        """Put text on the clipboard."""
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, "public.utf8-plain-text")

    @objc.python_method
    def _context_stop_and_process(self):
        with self._stop_lock:
            if not self._recording and not self.audio_recorder.is_recording:
                return
            self._recording = False
            self._processing = True
        self._run_on_main(lambda: self._show_float("transcribing", _PURPLE()))
        self._run_on_main(self._ui_context_thinking)
        try:
            if self.config.sound_feedback:
                play_stop_sound()
            audio_data = self.audio_recorder.stop_recording()
            if audio_data is None or len(audio_data) < 16044:
                logger.debug("Context audio too short, discarding.")
                self._processing = False
                self._context_mode = False
                self._float_triggered = False
                self._run_on_main(self._ui_ready)
                return
            threading.Thread(
                target=self._context_transcribe_and_query,
                args=(audio_data,), daemon=True,
            ).start()
        except Exception:
            logger.warning("Context stop: %s", traceback.format_exc().splitlines()[-1])
            self._processing = False
            self._context_mode = False
            self._float_triggered = False
            self._run_on_main(self._ui_ready)

    @objc.python_method
    def _context_transcribe_and_query(self, audio_data):
        try:
            voice_text = self.transcriber.transcribe(audio_data, skip_cleanup=True)
            if not voice_text or not voice_text.strip():
                self._run_on_main(lambda: self._ui_error("No speech detected."))
                return
            logger.info("Context instruction: %s", voice_text[:100])

            app_ctx = (self._active_app or "") if self.config.context_cleanup else ""
            response = self.transcriber.context_query(
                selected_text=self._selected_text,
                voice_instruction=voice_text,
                model=self.config.context_model,
                app_context=app_ctx,
                before_text=self._before_text, after_text=self._after_text,
            )
            if not response or not response.strip():
                self._run_on_main(lambda: self._ui_error("No response generated."))
                return

            is_rewrite = self._is_rewrite_instruction(voice_text)
            if is_rewrite and not self._float_triggered:
                reactivated = self._reactivate_target_app()
                if reactivated:
                    _time.sleep(0.15)
                    self.text_inserter.insert_text(response)
                    logger.info("Context rewrite: replaced selection with %d chars.", len(response))
                    self._run_on_main(lambda: self._ui_done(response))
                else:
                    self._run_on_main_sync(lambda: self._set_clipboard(response))
                    self._run_on_main(lambda: self._ui_done_clipboard(response))
            else:
                self._run_on_main_sync(lambda: self._set_clipboard(response))
                logger.info("Context response copied: %d chars.", len(response))
                self._run_on_main(lambda: self._ui_ai_response(response))

            history.add(
                f"[Context] {voice_text}\n→ {response}",
                app_name=self._active_app,
                language=self.config.language,
            )
        except RuntimeError as exc:
            logger.error("Context query failed: %s", exc)
            msg = str(exc)
            self._run_on_main(lambda: self._ui_error(msg))
        except Exception:
            logger.error("Context query failed:\n%s", traceback.format_exc())
            self._run_on_main(lambda: self._ui_error("Context query failed."))
        finally:
            self._processing = False
            self._context_mode = False
            self._float_triggered = False
            self._screenshot_b64 = ""

    # ── Regular recording ──────────────────────────────────────

    @objc.python_method
    def _stop_and_transcribe(self):
        with self._stop_lock:
            if not self._recording and not self.audio_recorder.is_recording:
                return  # Already stopped (race with silence detection)
            self._recording = False
            self._processing = True
        self._run_on_main(self._ui_transcribing)
        try:
            if self.config.sound_feedback:
                play_stop_sound()
            audio_data = self.audio_recorder.stop_recording()
            if audio_data is None or len(audio_data) < 16044:
                logger.debug("Audio too short, discarding.")
                self._processing = False
                self._float_triggered = False
                self._run_on_main(self._ui_ready)
                return
            threading.Thread(target=self._transcribe_and_insert, args=(audio_data,), daemon=True).start()
        except Exception:
            logger.warning("Stop recording: %s", traceback.format_exc().splitlines()[-1])
            self._processing = False
            self._float_triggered = False
            self._run_on_main(self._ui_ready)

    @objc.python_method
    def _transcribe_and_insert(self, audio_data):
        mode = self.config.active_mode
        try:
            if mode == "dictation":
                self._process_dictation(audio_data)
            elif mode == "auto":
                self._process_auto_mode(audio_data)
            else:
                self._process_ai_mode(audio_data, mode)
        except RuntimeError as exc:
            logger.error("Processing failed: %s", exc)
            msg = str(exc)
            self._run_on_main(lambda: self._ui_error(msg))
        except Exception:
            logger.error("Processing failed:\n%s", traceback.format_exc())
            self._run_on_main(lambda: self._ui_error("Processing failed."))
        finally:
            self._processing = False
            self._float_triggered = False
            self._target_running_app = None
            self._screenshot_b64 = ""

    @objc.python_method
    def _process_dictation(self, audio_data):
        app_ctx = (self._active_app or "") if self.config.context_cleanup else ""
        text = self.transcriber.transcribe(
            audio_data, app_context=app_ctx,
            before_text=self._before_text, after_text=self._after_text,
        )
        if not text or not text.strip():
            self._run_on_main(lambda: self._ui_error("No speech detected."))
            return

        expansion = self._check_voice_shortcut(text)
        if expansion is not None:
            text = expansion
            logger.info("Voice shortcut matched: %d chars.", len(text))
        else:
            logger.info("Transcription: %d chars.", len(text))

        self._deliver_text(text)

    @objc.python_method
    def _process_ai_mode(self, audio_data, mode):
        raw = self.transcriber.transcribe(audio_data, skip_cleanup=True)
        if not raw or not raw.strip():
            self._run_on_main(lambda: self._ui_error("No speech detected."))
            return
        logger.info("AI mode '%s' instruction: %s", mode, raw[:100])
        self._run_on_main(self._ui_mode_thinking)
        app_ctx = (self._active_app or "") if self.config.context_cleanup else ""

        if mode == "ask":
            question = raw
            if self._selected_text:
                question = f"{raw}\n\nSelected text:\n---\n{self._selected_text}\n---"
            response = self.transcriber.ask_question(
                question, model=self.config.context_model, app_context=app_ctx)
            label = "AI Ask"
        elif mode == "vision":
            response = self.transcriber.vision_query(
                self._screenshot_b64, raw,
                model=self.config.context_model, app_context=app_ctx)
            label = "Vision"
        elif mode == "vibecode":
            description = raw
            if self._selected_text:
                description = f"{raw}\n\nExisting code:\n```\n{self._selected_text}\n```"
            response = self.transcriber.vibecode_prompt(
                description, model=self.config.context_model)
            label = "VibeCode"
        else:
            mode_config = None
            for cm in self.config.custom_modes:
                if cm["name"] == mode:
                    mode_config = cm
                    break
            if mode_config is None:
                self._run_on_main(lambda: self._ui_error(f"Mode not found"))
                return
            user_input = raw
            if self._selected_text:
                user_input = f"{raw}\n\nSelected text:\n---\n{self._selected_text}\n---"
            response = self.transcriber.custom_mode_query(
                user_input, mode_config["prompt"], model=self.config.context_model)
            label = mode

        if not response or not response.strip():
            self._run_on_main(lambda: self._ui_error("No response generated."))
            return
        self._run_on_main_sync(lambda: self._set_clipboard(response))
        history.add(f"[{label}] {raw}\n→ {response}",
                    app_name=self._active_app, language=self.config.language)
        self._run_on_main(lambda: self._ui_ai_response(response))

    @objc.python_method
    def _process_auto_mode(self, audio_data):
        """Auto mode: classify intent then route."""
        app_ctx = (self._active_app or "") if self.config.context_cleanup else ""

        raw = self.transcriber.transcribe(audio_data, skip_cleanup=True)
        if not raw or not raw.strip():
            self._run_on_main(lambda: self._ui_error("No speech detected."))
            return

        expansion = self._check_voice_shortcut(raw)
        if expansion is not None:
            logger.info("Auto→shortcut: %d chars.", len(expansion))
            self._deliver_text(expansion)
            return

        intent = self.transcriber.classify_intent(
            raw, app_context=app_ctx, language=self.config.language)
        logger.info("Auto classified '%s...' → %s", raw[:40], intent)

        if intent == "dictation":
            text = raw
            if self.transcriber.editing_strength != "off":
                try:
                    text = self.transcriber.cleanup_text(
                        raw, self.transcriber.language, app_ctx,
                        self._before_text, self._after_text) or raw
                except Exception:
                    pass
            logger.info("Auto→dictation: %d chars.", len(text))
            self._deliver_text(text)
        else:
            # AI mode — route to the right handler
            self._run_on_main(self._ui_mode_thinking)
            if intent == "ask":
                response = self.transcriber.ask_question(
                    raw, model=self.config.context_model, app_context=app_ctx)
                label = "AI Ask"
            elif intent == "vision" and self._screenshot_b64:
                response = self.transcriber.vision_query(
                    self._screenshot_b64, raw,
                    model=self.config.context_model, app_context=app_ctx)
                label = "Vision"
            elif intent == "vision":
                logger.warning("Auto→vision but no screenshot, falling back to ask")
                response = self.transcriber.ask_question(
                    raw, model=self.config.context_model, app_context=app_ctx)
                label = "AI Ask"
            elif intent == "vibecode":
                response = self.transcriber.vibecode_prompt(
                    raw, model=self.config.context_model)
                label = "VibeCode"
            else:
                response = self.transcriber.ask_question(
                    raw, model=self.config.context_model, app_context=app_ctx)
                label = "AI Ask"
            if not response or not response.strip():
                self._run_on_main(lambda: self._ui_error("No response generated."))
                return
            self._run_on_main_sync(lambda: self._set_clipboard(response))
            history.add(f"[{label}] {raw}\n→ {response}",
                        app_name=self._active_app, language=self.config.language)
            self._run_on_main(lambda: self._ui_ai_response(response))

    # ── UI state updates ────────────────────────────────────────

    @objc.python_method
    def _ui_recording(self):
        self.status_label.setStringValue_("Recording...")
        self.status_label.setTextColor_(_RED())
        self._status_dot.layer().setBackgroundColor_(_RED().CGColor())
        self._set_btn_title(self.rec_button, "Stop Recording")
        self.rec_button.layer().setBackgroundColor_(_RED().CGColor())
        self.status_item.setTitle_("REC")
        self._show_float("recording", _RED())

    @objc.python_method
    def _ui_transcribing(self):
        self.status_label.setStringValue_("Transcribing...")
        self.status_label.setTextColor_(_ORANGE())
        self._status_dot.layer().setBackgroundColor_(_ORANGE().CGColor())
        self._set_btn_title(self.rec_button, "Processing...")
        self.rec_button.layer().setBackgroundColor_(_ORANGE().CGColor())
        self.rec_button.setEnabled_(False)
        self.status_item.setTitle_("...")
        self._show_float("transcribing", _ORANGE())

    @objc.python_method
    def _ui_done(self, text):
        """Transition to ready + show last transcription + popup."""
        self._ui_ready()
        self._last_text_label.setStringValue_(text)
        self._show_response_popup(text)

    @objc.python_method
    def _ui_done_clipboard(self, text):
        """Show 'copied' feedback when triggered from float button + popup."""
        self.status_label.setStringValue_("Copied to clipboard")
        self.status_label.setTextColor_(_GREEN())
        self._status_dot.layer().setBackgroundColor_(_GREEN().CGColor())
        self._set_btn_title(self.rec_button, "Start Recording")
        self.rec_button.layer().setBackgroundColor_(_GREEN().CGColor())
        self.rec_button.setEnabled_(True)
        self.status_item.setTitle_("SF")
        self._float_mode = None
        if self._level_timer is not None:
            self._level_timer.invalidate()
            self._level_timer = None
        self._set_float_color(_GREEN())
        self._last_text_label.setStringValue_(text)
        self._show_response_popup(text)
        self._auto_clear_after(2.5)

    @objc.python_method
    def _ui_context_recording(self):
        self.status_label.setStringValue_("Context — Listening...")
        self.status_label.setTextColor_(_PURPLE())
        self._status_dot.layer().setBackgroundColor_(_PURPLE().CGColor())
        self._set_btn_title(self.rec_button, "Stop Recording")
        self.rec_button.layer().setBackgroundColor_(_PURPLE().CGColor())
        self.status_item.setTitle_("CTX")

    @objc.python_method
    def _ui_context_thinking(self):
        self.status_label.setStringValue_("Context — Thinking...")
        self.status_label.setTextColor_(_PURPLE())
        self._status_dot.layer().setBackgroundColor_(_PURPLE().CGColor())
        self._set_btn_title(self.rec_button, "Processing...")
        self.rec_button.layer().setBackgroundColor_(_PURPLE().CGColor())
        self.rec_button.setEnabled_(False)
        self.status_item.setTitle_("...")

    # ── Response popup ──────────────────────────────────────────

    @objc.python_method
    def _build_response_panel(self):
        """Create the popup panel and its cached subviews once."""
        pw, ph = 340, 400
        screen = NSScreen.mainScreen()
        visible = screen.visibleFrame()
        x = visible.origin.x + visible.size.width - pw - 16
        y = visible.origin.y + visible.size.height - ph - 8

        self._response_panel = NSPanel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, pw, ph), 1 << 7, NSBackingStoreBuffered, False)
        self._response_panel.setLevel_(NSFloatingWindowLevel)
        self._response_panel.setOpaque_(False)
        self._response_panel.setBackgroundColor_(NSColor.clearColor())
        self._response_panel.setIgnoresMouseEvents_(False)
        self._response_panel.setMovableByWindowBackground_(True)
        self._response_panel.setCollectionBehavior_(1 << 1 | 1 << 4)
        self._response_panel.setReleasedWhenClosed_(False)
        self._response_panel.setHasShadow_(True)
        self._response_panel.setFloatingPanel_(True)
        self._response_panel.setBecomesKeyOnlyIfNeeded_(True)
        self._response_panel.setHidesOnDeactivate_(False)

        cv = self._response_panel.contentView()

        self._popup_blur = NSVisualEffectView.alloc().initWithFrame_(NSMakeRect(0, 0, pw, ph))
        self._popup_blur.setMaterial_(11)
        self._popup_blur.setBlendingMode_(0)
        self._popup_blur.setState_(1)
        self._popup_blur.setWantsLayer_(True)
        self._popup_blur.layer().setCornerRadius_(14)
        self._popup_blur.layer().setMasksToBounds_(True)
        self._popup_blur.layer().setBorderWidth_(0.5)
        self._popup_blur.layer().setBorderColor_(
            NSColor.colorWithCalibratedRed_green_blue_alpha_(0.4, 0.4, 0.45, 0.4).CGColor())

        self._popup_header = self._label(self._popup_blur, "SPEAKFLOW", 16, ph - 30, 120, 16,
                    NSFont.systemFontOfSize_weight_(11, NSFontWeightSemibold), _DIM())

        copy_btn = NSButton.alloc().initWithFrame_(NSMakeRect(pw - 62, ph - 32, 26, 24))
        copy_btn.setButtonType_(0)
        copy_btn.setBordered_(False)
        copy_btn.setWantsLayer_(True)
        copy_btn.setFocusRingType_(1)
        copy_btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            "⧉", {NSFontAttributeName: NSFont.systemFontOfSize_(14),
                      NSForegroundColorAttributeName: _DIM()}))
        copy_btn.setTarget_(self)
        copy_btn.setAction_("popupCopy:")
        self._popup_blur.addSubview_(copy_btn)
        self._popup_copy_btn = copy_btn

        close_btn = NSButton.alloc().initWithFrame_(NSMakeRect(pw - 32, ph - 32, 24, 24))
        close_btn.setButtonType_(0)
        close_btn.setBordered_(False)
        close_btn.setWantsLayer_(True)
        close_btn.setFocusRingType_(1)
        close_btn.setAttributedTitle_(NSAttributedString.alloc().initWithString_attributes_(
            "✕", {NSFontAttributeName: NSFont.systemFontOfSize_(13),
                      NSForegroundColorAttributeName: _DIM()}))
        close_btn.setTarget_(self)
        close_btn.setAction_("popupClose:")
        self._popup_blur.addSubview_(close_btn)
        self._popup_close_btn = close_btn

        cv.addSubview_(self._popup_blur)

        text_bg = NSColor.colorWithCalibratedRed_green_blue_alpha_(0.13, 0.13, 0.16, 1.0)
        text_fg = NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 1.0, 1.0, 1.0)
        self._popup_scroll = NSScrollView.alloc().initWithFrame_(NSMakeRect(12, 14, pw - 24, 350))
        self._popup_scroll.setHasVerticalScroller_(True)
        self._popup_scroll.setAutohidesScrollers_(True)
        self._popup_scroll.setBorderType_(0)
        self._popup_scroll.setDrawsBackground_(True)
        self._popup_scroll.setBackgroundColor_(text_bg)
        self._popup_scroll.setWantsLayer_(True)
        self._popup_scroll.layer().setCornerRadius_(8)
        self._popup_scroll.layer().setMasksToBounds_(True)
        self._popup_tv = NSTextView.alloc().initWithFrame_(NSMakeRect(0, 0, pw - 24, 350))
        self._popup_tv.setEditable_(False)
        self._popup_tv.setSelectable_(True)
        self._popup_tv.setDrawsBackground_(True)
        self._popup_tv.setBackgroundColor_(text_bg)
        self._popup_tv.setTextColor_(text_fg)
        self._popup_tv.setFont_(NSFont.systemFontOfSize_(13.5))
        self._popup_tv.setVerticallyResizable_(True)
        self._popup_tv.setHorizontallyResizable_(False)
        self._popup_tv.textContainer().setContainerSize_((pw - 24, 1e7))
        self._popup_tv.textContainer().setWidthTracksTextView_(True)
        self._popup_scroll.setDocumentView_(self._popup_tv)
        cv.addSubview_(self._popup_scroll)

    @objc.python_method
    def _show_response_popup(self, text):
        """Show a floating frosted-glass popup with the AI response."""
        if self._response_panel is None:
            self._build_response_panel()

        pw = 340
        line_h = 18
        chars_per_line = 42
        est_lines = 0
        for para in text.split('\n'):
            est_lines += max(1, (len(para) + chars_per_line - 1) // chars_per_line)
        text_h = min(350, max(30, est_lines * line_h + 8))
        header_h = 36
        pad_bottom = 14
        ph = header_h + text_h + pad_bottom

        screen = NSScreen.mainScreen()
        visible = screen.visibleFrame()
        x = visible.origin.x + visible.size.width - pw - 16
        y = visible.origin.y + visible.size.height - ph - 8

        self._response_panel.setFrame_display_(NSMakeRect(x, y, pw, ph), True)
        self._popup_blur.setFrame_(NSMakeRect(0, 0, pw, ph))
        self._popup_header.setFrame_(NSMakeRect(16, ph - 30, 120, 16))
        self._popup_copy_btn.setFrame_(NSMakeRect(pw - 62, ph - 32, 26, 24))
        self._popup_close_btn.setFrame_(NSMakeRect(pw - 32, ph - 32, 24, 24))
        self._popup_scroll.setFrame_(NSMakeRect(12, pad_bottom, pw - 24, text_h))
        self._popup_tv.setString_(text)
        self._response_panel.orderFront_(None)
        self._popup_response_text = text

        if self._popup_timer is not None:
            self._popup_timer.cancel()
        dismiss_delay = min(20.0, max(4.0, len(text) * 0.06))
        self._popup_timer = threading.Timer(
            dismiss_delay, lambda: self._run_on_main(self._dismiss_response_popup))
        self._popup_timer.start()

    @objc.python_method
    def _dismiss_response_popup(self):
        if self._response_panel is not None:
            self._response_panel.orderOut_(None)
        if self._popup_timer is not None:
            self._popup_timer.cancel()
            self._popup_timer = None

    def popupCopy_(self, sender):
        if self._popup_response_text:
            self._set_clipboard(self._popup_response_text)

    def popupClose_(self, sender):
        self._dismiss_response_popup()

    @objc.python_method
    def _ui_ai_response(self, text):
        """Show AI response popup, update status to ready."""
        self.status_label.setStringValue_("Response copied")
        self.status_label.setTextColor_(_GREEN())
        self._status_dot.layer().setBackgroundColor_(_GREEN().CGColor())
        self._set_btn_title(self.rec_button, "Start Recording")
        self.rec_button.layer().setBackgroundColor_(_GREEN().CGColor())
        self.rec_button.setEnabled_(True)
        self.status_item.setTitle_("SF")
        self._float_mode = None
        if self._level_timer is not None:
            self._level_timer.invalidate()
            self._level_timer = None
        self._set_float_color(_GREEN())
        self._last_text_label.setStringValue_(text)
        self._show_response_popup(text)
        self._auto_clear_after(2.5)

    @objc.python_method
    def _ui_mode_thinking(self):
        mode = self.config.active_mode
        labels = {
            "auto": "Processing...",
            "ask": "AI thinking...",
            "vision": "Analyzing screen...",
            "vibecode": "Generating prompt...",
        }
        label = labels.get(mode, "Processing...")
        self.status_label.setStringValue_(label)
        self.status_label.setTextColor_(_ORANGE())
        self._status_dot.layer().setBackgroundColor_(_ORANGE().CGColor())
        self._set_btn_title(self.rec_button, "Processing...")
        self.rec_button.layer().setBackgroundColor_(_ORANGE().CGColor())
        self.rec_button.setEnabled_(False)
        self.status_item.setTitle_("...")
        self._show_float("transcribing", _ORANGE())

    @objc.python_method
    def _ui_ready(self):
        mode_color = _MODE_COLORS.get(self.config.active_mode, _ACCENT)()
        self.status_label.setStringValue_("Ready")
        self.status_label.setTextColor_(mode_color)
        self._status_dot.layer().setBackgroundColor_(mode_color.CGColor())
        self._set_btn_title(self.rec_button, "Start Recording")
        self.rec_button.layer().setBackgroundColor_(_GREEN().CGColor())
        self.rec_button.setEnabled_(True)
        self.status_item.setTitle_("SF")
        self._hide_float()

    @objc.python_method
    def _ui_error(self, msg):
        self.status_label.setStringValue_(msg)
        self.status_label.setTextColor_(_RED())
        self._status_dot.layer().setBackgroundColor_(_RED().CGColor())
        self._set_btn_title(self.rec_button, "Start Recording")
        self.rec_button.layer().setBackgroundColor_(_GREEN().CGColor())
        self.rec_button.setEnabled_(True)
        self.status_item.setTitle_("SF")
        if self.config.sound_feedback:
            play_error_sound()
        # Show error color on float, stop animation
        self._float_mode = None
        if self._level_timer is not None:
            self._level_timer.invalidate()
            self._level_timer = None
        fh = self._float_fh
        bw = self._float_bar_w
        min_h = self._float_min_h
        for bar in self._float_bars:
            old_x = bar.frame().origin.x
            bar.setFrame_(NSMakeRect(old_x, (fh - min_h) / 2, bw, min_h))
        self._set_float_color(_RED())
        self._auto_clear_after(3.0)

    @objc.python_method
    def _auto_clear_after(self, seconds: float):
        """Reset UI to ready after a delay, unless a new recording started."""
        def _check():
            if not self._recording and not self._processing:
                self._run_on_main(self._ui_ready)
        threading.Timer(seconds, _check).start()

    @objc.python_method
    def _run_on_main(self, func):
        self._dispatcher.enqueue_(func)

    @objc.python_method
    def _run_on_main_sync(self, func):
        """Run func on the main thread and block until it returns. Returns the result."""
        if threading.current_thread() is threading.main_thread():
            return func()
        event = threading.Event()
        result = [None]
        error = [None]
        def wrapper():
            try:
                result[0] = func()
            except Exception as e:
                error[0] = e
            finally:
                event.set()
        d = self._dispatcher
        with d._lock:
            d._queue.append(wrapper)
        d.performSelectorOnMainThread_withObject_waitUntilDone_("drain:", None, False)
        if not event.wait(timeout=5.0):
            logger.error("_run_on_main_sync timed out after 5s — main thread may be blocked")
            return None
        if error[0] is not None:
            raise error[0]
        return result[0]


class SpeakFlowApp:
    """Entry point — sets up NSApplication then hands off to SpeakFlowUI."""
    def run(self):
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        delegate = AppDelegate.alloc().init()
        app.setDelegate_(delegate)
        AppHelper.runEventLoop()
