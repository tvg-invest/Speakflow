"""SpeakFlow — native macOS app using PyObjC/Cocoa."""

import logging
import math
import random
import threading
import traceback
from collections import deque
from pathlib import Path

import subprocess
import time as _time
import objc
import Quartz
from AppKit import (
    NSApplication, NSApp, NSApplicationActivationPolicyRegular,
    NSWindow, NSBackingStoreBuffered,
    NSMakeRect, NSTextField, NSButton, NSFont,
    NSColor, NSPopUpButton, NSStatusBar, NSVariableStatusItemLength,
    NSMenu, NSMenuItem, NSObject,
    NSBezelStyleRounded, NSBezierPath,
    NSEvent, NSScreen, NSView,
    NSFloatingWindowLevel, NSVisualEffectView,
    NSFontWeightMedium, NSFontWeightSemibold,
    NSImage, NSImageView, NSWorkspace,
    NSScrollView, NSTextView, NSSlider,
    NSPasteboard,
)
from Foundation import NSTimer
from PyObjCTools import AppHelper
from pynput import keyboard as kb
import ApplicationServices

import openai

from .audio import AudioRecorder
from .config import Config
from . import history
from .hotkey import HotkeyListener, is_modifier_only
from .sounds import play_error_sound, play_start_sound, play_stop_sound, set_volume
from .text_inserter import TextInserter
from .transcriber import Transcriber

logger = logging.getLogger(__name__)

_MOD_NAMES = {
    kb.Key.ctrl: "ctrl", kb.Key.ctrl_l: "ctrl", kb.Key.ctrl_r: "ctrl",
    kb.Key.shift: "shift", kb.Key.shift_l: "shift", kb.Key.shift_r: "shift",
    kb.Key.cmd: "cmd", kb.Key.cmd_l: "cmd", kb.Key.cmd_r: "cmd",
    kb.Key.alt: "alt", kb.Key.alt_l: "alt", kb.Key.alt_r: "alt",
}
_SPECIAL_NAMES = {
    kb.Key.space: "space", kb.Key.enter: "enter", kb.Key.tab: "tab",
    kb.Key.backspace: "backspace", kb.Key.delete: "delete", kb.Key.esc: "escape",
    kb.Key.f1: "f1", kb.Key.f2: "f2", kb.Key.f3: "f3", kb.Key.f4: "f4",
    kb.Key.f5: "f5", kb.Key.f6: "f6", kb.Key.f7: "f7", kb.Key.f8: "f8",
    kb.Key.f9: "f9", kb.Key.f10: "f10", kb.Key.f11: "f11", kb.Key.f12: "f12",
}
LANG_OPTIONS = ["Danish", "English", "Auto-detect"]
LANG_CODES = {"Danish": "da", "English": "en", "Auto-detect": "auto"}

_LAUNCH_AGENT = Path.home() / "Library" / "LaunchAgents" / "com.speakflow.app.plist"
_APP_PATH = Path.home() / "Desktop" / "SpeakFlow.app"

# ── Colour palette ──────────────────────────────────────────────
_BG        = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.10, 0.10, 0.13, 1.0)
_CARD      = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.15, 0.15, 0.19, 1.0)
_CARD_EDGE = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.22, 0.22, 0.27, 1.0)
_ACCENT    = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.35, 0.58, 1.0, 1.0)
_GREEN     = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.30, 0.85, 0.55, 1.0)
_RED       = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.32, 0.32, 1.0)
_ORANGE    = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.65, 0.20, 1.0)
_GOLD      = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.80, 0.28, 1.0)
_PURPLE    = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.65, 0.40, 1.0, 1.0)
_DIM       = lambda: NSColor.colorWithCalibratedRed_green_blue_alpha_(0.50, 0.50, 0.56, 1.0)
_WHITE     = lambda: NSColor.whiteColor()


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
        self._selected_text = ""
        self._active_app = ""
        self._stop_lock = threading.Lock()
        self._dispatcher = MainThreadDispatcher.alloc().init()
        self._history_win = None

        # Core components
        self.audio_recorder = AudioRecorder(
            max_duration=self.config.max_recording_seconds,
            silence_timeout=self.config.silence_timeout,
        )
        self.audio_recorder.on_silence_detected = self._on_silence
        self.audio_recorder.on_error = self._on_record_error

        self.transcriber = Transcriber(
            api_key=self.config.openai_api_key,
            model=self.config.model,
            language=self.config.language,
            auto_detect=self.config.auto_language_detect,
            ai_cleanup=self.config.ai_cleanup,
            cleanup_model=self.config.ai_cleanup_model,
        )
        self.text_inserter = TextInserter(method=self.config.text_insertion_method)
        self.hotkey_listener = HotkeyListener(
            hotkey_string=self.config.hotkey,
            on_activate=self._on_activate,
            on_deactivate=self._on_deactivate,
        )
        self.context_listener = HotkeyListener(
            hotkey_string=self.config.context_hotkey,
            on_activate=self._on_context_activate,
            on_deactivate=self._on_context_deactivate,
        )

        set_volume(self.config.sound_volume)

        self._build_status_bar()
        self._build_window()
        self._build_floating_indicator()
        self._check_api_key()
        self._check_permissions()
        self.hotkey_listener.start()
        self.context_listener.start()

        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)
        logger.info("SpeakFlowApp initialised.")

    @objc.python_method
    def _check_permissions(self):
        trusted = ApplicationServices.AXIsProcessTrustedWithOptions(
            {ApplicationServices.kAXTrustedCheckOptionPrompt: True}
        )
        if not trusted:
            logger.warning("Accessibility not granted — prompting user.")
            self.status_label.setStringValue_("Giv Accessibility-tilladelse i System Settings")
            self.status_label.setTextColor_(_ORANGE())
            return
        AVCaptureDevice = objc.lookUpClass('AVCaptureDevice')
        mic_status = AVCaptureDevice.authorizationStatusForMediaType_('soun')
        if mic_status == 0:
            logger.info("Requesting microphone permission.")
            AVCaptureDevice.requestAccessForMediaType_completionHandler_('soun', lambda granted: None)
        elif mic_status == 2:
            logger.warning("Microphone access denied.")
            self.status_label.setStringValue_("Giv mikrofon-tilladelse i System Settings")
            self.status_label.setTextColor_(_ORANGE())

    @objc.python_method
    def _check_api_key(self):
        if not self.config.openai_api_key:
            self.status_label.setStringValue_("Enter your OpenAI API key in Settings ↓")
            self.status_label.setTextColor_(_ORANGE())
            logger.warning("No OpenAI API key configured.")

    @objc.python_method
    def show_window(self):
        self.window.makeKeyAndOrderFront_(None)
        NSApp.activateIgnoringOtherApps_(True)

    # ── Helpers ─────────────────────────────────────────────────

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
        W, H = 460, 794
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
        card_h = 110
        sc = self._card(v, pad, y - card_h, cw, card_h)

        self._status_dot = self._dot(sc, cw / 2 - 52, card_h - 42, 10, _ACCENT())
        self.status_label = self._label(sc, "Ready", cw / 2 - 38, card_h - 48, 130, 24,
                                        NSFont.systemFontOfSize_weight_(17, NSFontWeightSemibold),
                                        _ACCENT(), False)

        bw, bh = 170, 32
        self.rec_button = NSButton.alloc().initWithFrame_(
            NSMakeRect((cw - bw) / 2, 16, bw, bh))
        self.rec_button.setTitle_("Start Recording")
        self.rec_button.setBezelStyle_(NSBezelStyleRounded)
        self.rec_button.setTarget_(self)
        self.rec_button.setAction_("toggleRecording:")
        self.rec_button.setFont_(NSFont.systemFontOfSize_weight_(12, NSFontWeightMedium))
        sc.addSubview_(self.rec_button)
        y -= card_h + 16

        # ── Settings card (9 rows) ──
        row_h = 34
        num_rows = 9
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
            masked = key_val[:3] + "•" * max(0, len(key_val) - 7) + key_val[-4:]
            self.api_key_field.setStringValue_(masked)
        else:
            self.api_key_field.setPlaceholderString_("sk-...")
        self.api_key_field.setFont_(NSFont.monospacedSystemFontOfSize_weight_(11, 0.0))
        self.api_key_field.setTextColor_(_WHITE())
        self.api_key_field.setBackgroundColor_(NSColor.colorWithCalibratedRed_green_blue_alpha_(0.12, 0.12, 0.16, 1.0))
        self.api_key_field.setBezeled_(True)
        self.api_key_field.setBezelStyle_(1)
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
        self.hotkey_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(rx - 90, ry + 5, 90, 26))
        self.hotkey_btn.setTitle_("Change")
        self.hotkey_btn.setBezelStyle_(NSBezelStyleRounded)
        self.hotkey_btn.setTarget_(self)
        self.hotkey_btn.setAction_("captureHotkey:")
        self.hotkey_btn.setFont_(NSFont.systemFontOfSize_(11))
        stc.addSubview_(self.hotkey_btn)
        ry -= row_h

        # Row 2 — Context Hotkey (modifier-only)
        self._label(stc, "Context Key", lx, ry + 6, 100, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        ctx_text = self.config.context_hotkey + " (hold)"
        self.ctx_hotkey_display = self._label(stc, ctx_text, lx + 100, ry + 6, cw - 250, 24,
                                              NSFont.systemFontOfSize_weight_(13, NSFontWeightSemibold),
                                              _PURPLE())
        self.ctx_hotkey_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(rx - 90, ry + 5, 90, 26))
        self.ctx_hotkey_btn.setTitle_("Change")
        self.ctx_hotkey_btn.setBezelStyle_(NSBezelStyleRounded)
        self.ctx_hotkey_btn.setTarget_(self)
        self.ctx_hotkey_btn.setAction_("captureContextHotkey:")
        self.ctx_hotkey_btn.setFont_(NSFont.systemFontOfSize_(11))
        stc.addSubview_(self.ctx_hotkey_btn)
        ry -= row_h

        # Row 3 — Language
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
        stc.addSubview_(self.lang_popup)
        ry -= row_h

        # Row 3 — AI Cleanup
        self._label(stc, "AI Cleanup", lx, ry + 6, 120, 24,
                    NSFont.systemFontOfSize_(13), _DIM())
        self.cleanup_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(rx - 24, ry + 7, 22, 22))
        self.cleanup_btn.setButtonType_(3)
        self.cleanup_btn.setTitle_("")
        self.cleanup_btn.setState_(1 if self.config.ai_cleanup else 0)
        self.cleanup_btn.setTarget_(self)
        self.cleanup_btn.setAction_("cleanupToggled:")
        stc.addSubview_(self.cleanup_btn)
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

        # Row 5 — Sound
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
        btn_w = 130
        gap = 12
        total = btn_w * 2 + gap
        bx = (W - total) / 2

        hist_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(bx, y - 4, btn_w, 26))
        hist_btn.setTitle_("View History")
        hist_btn.setBezelStyle_(NSBezelStyleRounded)
        hist_btn.setTarget_(self)
        hist_btn.setAction_("showHistory:")
        hist_btn.setFont_(NSFont.systemFontOfSize_(11))
        v.addSubview_(hist_btn)

        self._update_btn = NSButton.alloc().initWithFrame_(
            NSMakeRect(bx + btn_w + gap, y - 4, btn_w, 26))
        self._update_btn.setTitle_("Check for Updates")
        self._update_btn.setBezelStyle_(NSBezelStyleRounded)
        self._update_btn.setTarget_(self)
        self._update_btn.setAction_("checkForUpdates:")
        self._update_btn.setFont_(NSFont.systemFontOfSize_(11))
        v.addSubview_(self._update_btn)
        y -= 30

        self._label(v, "Hotkey = dictate  ·  Context Key = select + AI query",
                    pad, y, cw, 14, NSFont.systemFontOfSize_(10), _DIM(), True)

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

        self._float_win = NSWindow.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, fw, fh), 0, NSBackingStoreBuffered, False)
        self._float_win.setLevel_(NSFloatingWindowLevel)
        self._float_win.setOpaque_(False)
        self._float_win.setBackgroundColor_(NSColor.clearColor())
        self._float_win.setIgnoresMouseEvents_(True)
        self._float_win.setCollectionBehavior_(1 << 1 | 1 << 4)
        self._float_win.setReleasedWhenClosed_(False)
        self._float_win.setHasShadow_(True)

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
        self._float_dot = self._dot(blur, 12, (fh - dot_sz) / 2, dot_sz, _RED())

        bars_x = 12 + dot_sz + 8
        self._float_bars = []
        for i in range(self._float_num_bars):
            bx = bars_x + i * (self._float_bar_w + self._float_bar_gap)
            h = self._float_min_h
            by = (fh - h) / 2
            bar = NSView.alloc().initWithFrame_(NSMakeRect(bx, by, self._float_bar_w, h))
            bar.setWantsLayer_(True)
            bar.layer().setCornerRadius_(self._float_bar_w / 2)
            bar.layer().setBackgroundColor_(_RED().CGColor())
            blur.addSubview_(bar)
            self._float_bars.append(bar)

        self._float_win.contentView().addSubview_(blur)
        self._float_fh = fh

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
        self._float_win.orderOut_(None)

    # ── History window ──────────────────────────────────────────

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
            if self._context_mode:
                self._on_context_deactivate()
            else:
                self._on_deactivate()
        else:
            self._on_activate()

    def captureHotkey_(self, sender):
        self._capture_target = "main"
        self._start_hotkey_capture()

    def captureContextHotkey_(self, sender):
        self._capture_target = "context"
        self._start_modifier_only_capture()

    @objc.python_method
    def _start_modifier_only_capture(self):
        """Capture a single modifier key (ctrl/alt/cmd/shift) for context hotkey."""
        try:
            if self._capturing:
                return
            self._capturing = True
            self._capture_single_mod = None
            self.ctx_hotkey_display.setStringValue_("Press a modifier...")
            self.ctx_hotkey_display.setTextColor_(_ACCENT())
            self.ctx_hotkey_btn.setTitle_("Listening...")
            self.ctx_hotkey_btn.setEnabled_(False)

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
                if event.type() != 12:  # Only NSFlagsChanged
                    return event
                mods = _get_mods(event.modifierFlags())
                if len(mods) == 1 and self._capture_single_mod is None:
                    self._capture_single_mod = mods[0]
                elif len(mods) == 0 and self._capture_single_mod is not None:
                    mod = self._capture_single_mod
                    self._capture_single_mod = None
                    self._capturing = False
                    NSEvent.removeMonitor_(self._key_monitor)
                    self._key_monitor = None
                    self._finish_capture(mod)
                    return None
                elif len(mods) > 1:
                    self._capture_single_mod = None
                return event

            self._key_monitor = NSEvent.addLocalMonitorForEventsMatchingMask_handler_(
                1 << 12, handler)  # NSFlagsChangedMask only
        except Exception:
            logger.error("Modifier capture error:\n%s", traceback.format_exc())
            self._capturing = False

    @objc.python_method
    def _start_hotkey_capture(self):
        try:
            if self._capturing:
                return
            self._capturing = True
            self._capture_single_mod = None
            # Listeners stay alive — _capturing flag suppresses their callbacks.

            if self._capture_target == "main":
                self.hotkey_display.setStringValue_("Press a key...")
                self.hotkey_display.setTextColor_(_ACCENT())
                self.hotkey_btn.setTitle_("Listening...")
                self.hotkey_btn.setEnabled_(False)
            else:
                self.ctx_hotkey_display.setStringValue_("Press a key...")
                self.ctx_hotkey_display.setTextColor_(_ACCENT())
                self.ctx_hotkey_btn.setTitle_("Listening...")
                self.ctx_hotkey_btn.setEnabled_(False)

            _KEYCODE_MAP = {
                0: "a", 1: "s", 2: "d", 3: "f", 4: "h", 5: "g", 6: "z",
                7: "x", 8: "c", 9: "v", 11: "b", 12: "q", 13: "w", 14: "e",
                15: "r", 16: "y", 17: "t", 18: "1", 19: "2", 20: "3",
                21: "4", 22: "6", 23: "5", 24: "=", 25: "9", 26: "7",
                27: "-", 28: "8", 29: "0", 31: "o", 32: "u", 34: "i",
                35: "p", 37: "l", 38: "j", 40: "k", 45: "n", 46: "m",
                49: "space", 36: "enter", 48: "tab", 51: "backspace",
                53: "escape", 123: "left", 124: "right", 125: "down",
                126: "up",
                122: "f1", 120: "f2", 99: "f3", 118: "f4", 96: "f5",
                97: "f6", 98: "f7", 100: "f8", 101: "f9", 109: "f10",
                103: "f11", 111: "f12",
            }
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
                        self._capturing = False
                        NSEvent.removeMonitor_(self._key_monitor)
                        self._key_monitor = None
                        self._finish_capture(mod)
                        return None
                    elif len(mods) > 1:
                        self._capture_single_mod = None
                    return event

                if event_type == 10:
                    self._capture_single_mod = None
                    mods = _get_mods(event.modifierFlags())
                    keycode = event.keyCode()
                    key_name = _KEYCODE_MAP.get(keycode)

                    if key_name and (mods or key_name in _STANDALONE_KEYS):
                        new_hotkey = "+".join(mods + [key_name])
                        self._capturing = False
                        NSEvent.removeMonitor_(self._key_monitor)
                        self._key_monitor = None
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
    def _finish_capture(self, new_hotkey):
        self._capturing = False
        try:
            if self._capture_target == "main":
                self.config.hotkey = new_hotkey
                self.hotkey_listener.update_hotkey(new_hotkey)
                display = f"{new_hotkey} (hold)" if is_modifier_only(new_hotkey) else new_hotkey
                self.hotkey_display.setStringValue_(display)
                self.hotkey_display.setTextColor_(_GOLD())
                self.hotkey_btn.setTitle_("Change")
                self.hotkey_btn.setEnabled_(True)
                logger.info("Hotkey changed to %s.", new_hotkey)
            else:
                self.config.context_hotkey = new_hotkey
                self.context_listener.update_hotkey(new_hotkey)
                self.ctx_hotkey_display.setStringValue_(f"{new_hotkey} (hold)")
                self.ctx_hotkey_display.setTextColor_(_PURPLE())
                self.ctx_hotkey_btn.setTitle_("Change")
                self.ctx_hotkey_btn.setEnabled_(True)
                logger.info("Context hotkey changed to %s.", new_hotkey)
        except Exception:
            logger.error("Hotkey apply error:\n%s", traceback.format_exc())

    def apiKeyChanged_(self, sender):
        raw = sender.stringValue().strip()
        if not raw or "•" in raw:
            # User didn't actually change it (masked value or empty)
            return
        self.config.openai_api_key = raw
        self.transcriber.client = openai.OpenAI(api_key=raw)
        # Mask the displayed key
        masked = raw[:3] + "•" * max(0, len(raw) - 7) + raw[-4:]
        sender.setStringValue_(masked)
        self.status_label.setStringValue_("API key saved!")
        self.status_label.setTextColor_(_GREEN())
        logger.info("API key updated.")
        threading.Timer(2.0, lambda: self._run_on_main(self._ui_ready)).start()

    def languageChanged_(self, sender):
        name = sender.titleOfSelectedItem()
        code = LANG_CODES.get(name, "da")
        self.config.language = code
        self.config.auto_language_detect = (code == "auto")
        self.transcriber.language = code
        self.transcriber.auto_detect = (code == "auto")

    def cleanupToggled_(self, sender):
        self.config.ai_cleanup = bool(sender.state())
        self.transcriber.ai_cleanup = bool(sender.state())

    def contextToggled_(self, sender):
        self.config.context_cleanup = bool(sender.state())

    def soundToggled_(self, sender):
        self.config.sound_feedback = bool(sender.state())

    def volumeChanged_(self, sender):
        vol = sender.floatValue()
        self.config.sound_volume = vol
        set_volume(vol)

    def autostartToggled_(self, sender):
        enabled = bool(sender.state())
        self.config.auto_start = enabled
        self._set_auto_start(enabled)

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
        self._update_btn.setTitle_("Checking...")
        self._update_btn.setEnabled_(False)
        threading.Thread(target=self._do_update, daemon=True).start()

    @objc.python_method
    def _do_update(self):
        install_dir = Path.home() / ".speakflow"
        try:
            # Check if it's a git repo
            if not (install_dir / ".git").exists():
                self._run_on_main(lambda: self._show_update_result("Not a git install — update manually."))
                return

            # Fetch latest
            result = subprocess.run(
                ["git", "fetch"], cwd=str(install_dir),
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode != 0:
                self._run_on_main(lambda: self._show_update_result("Could not reach GitHub."))
                return

            # Check if behind
            status = subprocess.run(
                ["git", "status", "-uno"], cwd=str(install_dir),
                capture_output=True, text=True, timeout=5,
            )
            if "behind" not in status.stdout:
                self._run_on_main(lambda: self._show_update_result("Already up to date!"))
                return

            # Pull
            pull = subprocess.run(
                ["git", "pull", "--quiet"], cwd=str(install_dir),
                capture_output=True, text=True, timeout=30,
            )
            if pull.returncode != 0:
                self._run_on_main(lambda: self._show_update_result("Update failed. Run update.sh manually."))
                return

            # Reinstall deps
            venv_pip = str(install_dir / "venv" / "bin" / "pip")
            subprocess.run(
                [venv_pip, "install", "--quiet", "-r", str(install_dir / "requirements.txt")],
                capture_output=True, timeout=60,
            )

            self._run_on_main(lambda: self._show_update_result("Updated! Restart SpeakFlow to apply."))
            logger.info("Update completed successfully.")

        except Exception as exc:
            logger.error("Update failed: %s", exc)
            self._run_on_main(lambda: self._show_update_result(f"Error: {exc}"))

    @objc.python_method
    def _show_update_result(self, msg):
        self._update_btn.setTitle_("Check for Updates")
        self._update_btn.setEnabled_(True)
        self.status_label.setStringValue_(msg)
        self.status_label.setTextColor_(_ACCENT())

        def _clear():
            if not self._recording and not self._processing:
                self._run_on_main(self._ui_ready)

        threading.Timer(4.0, _clear).start()

    # ── Recording ───────────────────────────────────────────────

    @objc.python_method
    def _on_activate(self):
        if self._capturing:
            return
        if not self.config.openai_api_key:
            self._run_on_main(lambda: self._ui_error("Enter your API key in Settings first"))
            return
        with self._stop_lock:
            if self._recording or self._processing:
                return
            self._recording = True
        self._active_app = self._get_active_app()
        self._run_on_main(self._ui_recording)
        try:
            if self.config.sound_feedback:
                play_start_sound()
            self.audio_recorder.start_recording()
            logger.info("Recording started (app: %s).", self._active_app)
        except Exception:
            logger.error("Record start failed:\n%s", traceback.format_exc())
            self._recording = False
            self._processing = False
            self._run_on_main(self._ui_ready)

    @objc.python_method
    def _on_deactivate(self):
        if self._capturing or not self._recording:
            return
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
        self._recording = False
        self._processing = False
        self._context_mode = False
        self._run_on_main(lambda: self._ui_error(msg))

    # ── Context mode (select + voice → AI response) ────────────

    @objc.python_method
    def _grab_selection(self):
        """Simulate Cmd+C and return clipboard contents."""
        src = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStatePrivate)
        c_down = Quartz.CGEventCreateKeyboardEvent(src, 8, True)
        Quartz.CGEventSetFlags(c_down, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, c_down)
        c_up = Quartz.CGEventCreateKeyboardEvent(src, 8, False)
        Quartz.CGEventSetFlags(c_up, Quartz.kCGEventFlagMaskCommand)
        Quartz.CGEventPost(Quartz.kCGAnnotatedSessionEventTap, c_up)
        _time.sleep(0.15)
        pb = NSPasteboard.generalPasteboard()
        return pb.stringForType_("public.utf8-plain-text") or ""

    @objc.python_method
    def _set_clipboard(self, text):
        """Put text on the clipboard."""
        pb = NSPasteboard.generalPasteboard()
        pb.clearContents()
        pb.setString_forType_(text, "public.utf8-plain-text")

    @objc.python_method
    def _on_context_activate(self):
        if self._capturing:
            return
        if not self.config.openai_api_key:
            self._run_on_main(lambda: self._ui_error("Enter your API key in Settings first"))
            return
        with self._stop_lock:
            if self._recording or self._processing:
                return
            self._recording = True
            self._context_mode = True
        self._active_app = self._get_active_app()
        self._selected_text = self._grab_selection()
        logger.info("Context mode: grabbed %d chars from %s.",
                    len(self._selected_text), self._active_app)
        self._run_on_main(lambda: self._show_float("recording", _PURPLE()))
        self._run_on_main(self._ui_context_recording)
        try:
            if self.config.sound_feedback:
                play_start_sound()
            self.audio_recorder.start_recording()
        except Exception:
            logger.error("Context record start failed:\n%s", traceback.format_exc())
            self._recording = False
            self._context_mode = False
            self._run_on_main(self._ui_ready)

    @objc.python_method
    def _on_context_deactivate(self):
        if self._capturing or not self._recording or not self._context_mode:
            return
        threading.Thread(target=self._context_stop_and_process, daemon=True).start()

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
            self._run_on_main(self._ui_ready)

    @objc.python_method
    def _context_transcribe_and_query(self, audio_data):
        try:
            voice_text = self.transcriber.transcribe(audio_data)
            if not voice_text or not voice_text.strip():
                self._run_on_main(lambda: self._ui_error("No speech detected."))
                return
            logger.info("Context instruction: %s", voice_text[:100])

            app_ctx = self._active_app if self.config.context_cleanup else ""
            response = self.transcriber.context_query(
                selected_text=self._selected_text,
                voice_instruction=voice_text,
                model=self.config.context_model,
                app_context=app_ctx,
            )
            if not response or not response.strip():
                self._run_on_main(lambda: self._ui_error("No response generated."))
                return

            self._set_clipboard(response)
            logger.info("Context response copied: %d chars.", len(response))
            history.add(
                f"[Context] {voice_text}\n→ {response}",
                app_name=self._active_app,
                language=self.config.language,
            )
            self._run_on_main(lambda: self._ui_context_done(response))
        except Exception:
            logger.error("Context query failed:\n%s", traceback.format_exc())
            self._run_on_main(lambda: self._ui_error("Context query failed."))
        finally:
            self._processing = False
            self._context_mode = False

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
                self._run_on_main(self._ui_ready)
                self._processing = False
                return
            threading.Thread(target=self._transcribe_and_insert, args=(audio_data,), daemon=True).start()
        except Exception:
            logger.warning("Stop recording: %s", traceback.format_exc().splitlines()[-1])
            self._processing = False
            self._run_on_main(self._ui_ready)

    @objc.python_method
    def _transcribe_and_insert(self, audio_data):
        try:
            app_ctx = self._active_app if self.config.context_cleanup else ""
            text = self.transcriber.transcribe(audio_data, app_context=app_ctx)
            if not text or not text.strip():
                self._run_on_main(lambda: self._ui_error("No speech detected."))
                return
            logger.info("Transcription: %d chars.", len(text))

            # Save to history
            history.add(text, app_name=self._active_app, language=self.config.language)

            # Insert text at cursor
            self.text_inserter.insert_text(text)

            # Update UI with last transcription
            self._run_on_main(lambda: self._ui_done(text))
        except Exception:
            logger.error("Transcribe failed:\n%s", traceback.format_exc())
            self._run_on_main(lambda: self._ui_error("Transcription failed."))
        finally:
            self._processing = False

    # ── UI state updates ────────────────────────────────────────

    @objc.python_method
    def _ui_recording(self):
        self.status_label.setStringValue_("Recording...")
        self.status_label.setTextColor_(_RED())
        self._status_dot.layer().setBackgroundColor_(_RED().CGColor())
        self.rec_button.setTitle_("Stop Recording")
        self.status_item.setTitle_("REC")
        self._show_float("recording", _RED())

    @objc.python_method
    def _ui_transcribing(self):
        self.status_label.setStringValue_("Transcribing...")
        self.status_label.setTextColor_(_ORANGE())
        self._status_dot.layer().setBackgroundColor_(_ORANGE().CGColor())
        self.rec_button.setTitle_("Processing...")
        self.status_item.setTitle_("...")
        self._show_float("transcribing", _ORANGE())

    @objc.python_method
    def _ui_done(self, text):
        """Transition to ready + show last transcription."""
        self._ui_ready()
        self._last_text_label.setStringValue_(text)

    @objc.python_method
    def _ui_context_recording(self):
        self.status_label.setStringValue_("Context — Listening...")
        self.status_label.setTextColor_(_PURPLE())
        self._status_dot.layer().setBackgroundColor_(_PURPLE().CGColor())
        self.rec_button.setTitle_("Stop Recording")
        self.status_item.setTitle_("CTX")

    @objc.python_method
    def _ui_context_thinking(self):
        self.status_label.setStringValue_("Thinking...")
        self.status_label.setTextColor_(_PURPLE())
        self._status_dot.layer().setBackgroundColor_(_PURPLE().CGColor())
        self.rec_button.setTitle_("Processing...")
        self.status_item.setTitle_("...")

    @objc.python_method
    def _ui_context_done(self, response):
        self.status_label.setStringValue_("Copied to clipboard!")
        self.status_label.setTextColor_(_GREEN())
        self._status_dot.layer().setBackgroundColor_(_GREEN().CGColor())
        self.rec_button.setTitle_("Start Recording")
        self.status_item.setTitle_("SF")
        self._set_float_color(_GREEN())
        self._last_text_label.setStringValue_(response)

        def _auto_clear():
            if not self._recording and not self._processing:
                self._run_on_main(self._ui_ready)

        threading.Timer(2.5, _auto_clear).start()

    @objc.python_method
    def _ui_ready(self):
        self.status_label.setStringValue_("Ready")
        self.status_label.setTextColor_(_ACCENT())
        self._status_dot.layer().setBackgroundColor_(_ACCENT().CGColor())
        self.rec_button.setTitle_("Start Recording")
        self.status_item.setTitle_("SF")
        self._hide_float()

    @objc.python_method
    def _ui_error(self, msg):
        self.status_label.setStringValue_(msg)
        self.status_label.setTextColor_(_RED())
        self._status_dot.layer().setBackgroundColor_(_RED().CGColor())
        if self.config.sound_feedback:
            play_error_sound()
        self._set_float_color(_RED())
        self._float_win.orderFront_(None)

        def _auto_clear():
            # Only clear the error if no new recording/processing has started.
            if not self._recording and not self._processing:
                self._run_on_main(self._ui_ready)

        threading.Timer(3.0, _auto_clear).start()

    @objc.python_method
    def _run_on_main(self, func):
        self._dispatcher.enqueue_(func)


class SpeakFlowApp:
    """Entry point — sets up NSApplication then hands off to SpeakFlowUI."""
    def run(self):
        app = NSApplication.sharedApplication()
        app.setActivationPolicy_(NSApplicationActivationPolicyRegular)
        delegate = AppDelegate.alloc().init()
        app.setDelegate_(delegate)
        AppHelper.runEventLoop()
