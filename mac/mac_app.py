"""macOS dictation bubble: menu-bar app, non-activating panel. Audio and text stay in memory."""
import fcntl
import io
import json
import math
import os
import plistlib
import queue
import sys
import threading
import time
from pathlib import Path

import objc
from AppKit import (NSApplication, NSApplicationActivationPolicyAccessory, NSAlert, NSBackingStoreBuffered,
                    NSColor, NSCompositingOperationClear, NSEvent, NSGraphicsContext, NSImage,
                    NSImageInterpolationNone, NSMenu, NSMenuItem, NSPanel, NSPasteboard,
                    NSPasteboardTypeString, NSRectFillUsingOperation, NSScreen, NSStatusBar, NSView,
                    NSVariableStatusItemLength, NSWorkspace)
from Foundation import NSData, NSMakeRect, NSObject, NSRunLoop, NSRunLoopCommonModes, NSTimer, NSURL
import Quartz as Q

if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))  # shared engine
from dictate import clean_text
from streaming import StreamingDictation

APP_SUPPORT = Path.home() / 'Library/Application Support/HerdrVoice'
LAUNCH_AGENT = Path.home() / 'Library/LaunchAgents/com.herdr.voice.plist'
SIZE = 64
# (label, keycode, is_modifier). Modifiers trigger on a lone press-and-release.
HOTKEYS = [('Правый Option ⌥', 61, True), ('Правый Command ⌘', 54, True),
           ('F8', 100, False), ('F9', 101, False), ('F10', 109, False)]
MODIFIER_FLAGS = (Q.kCGEventFlagMaskCommand | Q.kCGEventFlagMaskAlternate |
                  Q.kCGEventFlagMaskControl | Q.kCGEventFlagMaskShift)
# Device-dependent bits tell the right-hand key from the left-hand one.
SIDE_FLAG = {61: 0x40, 54: 0x10}  # NX_DEVICERALTKEYMASK, NX_DEVICERCMDKEYMASK
FAMILY_FLAG = {61: Q.kCGEventFlagMaskAlternate, 54: Q.kCGEventFlagMaskCommand}
LEFT_FLAG = {61: 0x20, 54: 0x08}  # left Option / left Command held as well


def ns_image(image, points=SIZE):
    buffer = io.BytesIO()
    image.save(buffer, 'PNG')
    raw = buffer.getvalue()
    result = NSImage.alloc().initWithData_(NSData.dataWithBytes_length_(raw, len(raw)))
    result.setSize_((points, points))  # 2x pixels per point: crisp on Retina
    return result


def accessibility_trusted(prompt=False):
    from ApplicationServices import AXIsProcessTrustedWithOptions, kAXTrustedCheckOptionPrompt
    return bool(AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: prompt}))


def type_text(text):
    """Type Unicode text into the focused field without touching the clipboard."""
    units = text.encode('utf-16-le')
    source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
    # Applications accept at most 20 UTF-16 units (40 bytes) per synthetic key event.
    position = 0
    while position < len(units):
        end = min(len(units), position + 40)
        if end < len(units) and 0xDC00 <= int.from_bytes(units[end:end + 2], 'little') <= 0xDFFF:
            end -= 2  # never split a surrogate pair
        chunk = units[position:end].decode('utf-16-le')
        for down in (True, False):
            event = Q.CGEventCreateKeyboardEvent(source, 0, down)
            Q.CGEventSetFlags(event, 0)
            Q.CGEventKeyboardSetUnicodeString(event, (end - position) // 2, chunk)
            Q.CGEventPost(Q.kCGHIDEventTap, event)
        time.sleep(0.004)
        position = end


def paste_text(text):
    """Clipboard fallback for applications that ignore synthetic Unicode keys."""
    board = NSPasteboard.generalPasteboard()
    previous = board.stringForType_(NSPasteboardTypeString)
    board.clearContents()
    board.setString_forType_(text, NSPasteboardTypeString)
    source = Q.CGEventSourceCreate(Q.kCGEventSourceStateHIDSystemState)
    for down in (True, False):
        event = Q.CGEventCreateKeyboardEvent(source, 9, down)  # V
        Q.CGEventSetFlags(event, Q.kCGEventFlagMaskCommand)
        Q.CGEventPost(Q.kCGHIDEventTap, event)
    def restore():
        time.sleep(0.6)
        if previous is not None:
            board.clearContents()
            board.setString_forType_(previous, NSPasteboardTypeString)
    threading.Thread(target=restore, daemon=True).start()


class Panel(NSPanel):
    def canBecomeKeyWindow(self):
        return False

    def canBecomeMainWindow(self):
        return False


class BubbleView(NSView):
    def acceptsFirstMouse_(self, event):
        return True

    def isOpaque(self):
        return False

    def drawRect_(self, rect):
        NSColor.clearColor().set()
        NSRectFillUsingOperation(self.bounds(), NSCompositingOperationClear)
        image = getattr(self, 'image', None)
        if image is not None:
            NSGraphicsContext.currentContext().setImageInterpolation_(NSImageInterpolationNone)
            image.drawInRect_(self.bounds())

    def mouseDown_(self, event):
        self.app.mouse_down()

    def mouseDragged_(self, event):
        self.app.mouse_dragged()

    def mouseUp_(self, event):
        self.app.mouse_up()

    def rightMouseDown_(self, event):
        NSMenu.popUpContextMenu_withEvent_forView_(self.app.build_menu(), event, self)


class Target(NSObject):
    """Objective-C target for menu items and timers; forwards to Python."""
    def menuAction_(self, sender):
        callback = self.callbacks.get(sender.tag())
        if callback:
            callback()

    def tick_(self, timer):
        self.app.tick()

    def menuNeedsUpdate_(self, menu):
        self.app.fill_menu(menu)


class App:
    def __init__(self, interactive=True):
        import sounddevice as sd
        self.sd = sd
        APP_SUPPORT.mkdir(parents=True, exist_ok=True)
        self.config_path = APP_SUPPORT / 'settings.json'
        try:
            self.config = json.loads(self.config_path.read_text('utf-8'))
        except (OSError, ValueError):
            self.config = {}
        self.ns = NSApplication.sharedApplication()
        self.ns.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
        self.target = Target.alloc().init()
        self.target.app = self
        self.target.callbacks = {}
        self.state = 'idle'
        self.stream = None
        self.dictation = None
        self.generation = 0
        self.events = queue.Queue()
        self.pending_text = ''
        self.last_dictation = ''
        self.level = 0.0
        self.peak = 0.0
        self.frame = 0
        self.success_until = self.error_until = 0
        self.last_error = ''
        self.press = None
        self.dragged = False
        self.tap = None
        self.hotkey_down = False
        self.hotkey_spoiled = False
        self.hotkey = self.config.get('hotkey', 61)
        self.hotkey_enabled = self.config.get('hotkey_enabled', True)
        self.paste_mode = self.config.get('paste_mode', 'type')
        self.bubble_visible = self.config.get('bubble_visible', True)
        from mascot import render
        self.mascots = {name: render(name) for name in ('open', 'recording')}
        self.refresh_devices()
        self.make_panel()
        self.make_status_item()
        self.timer = NSTimer.timerWithTimeInterval_target_selector_userInfo_repeats_(
            0.06, self.target, 'tick:', None, True)
        NSRunLoop.currentRunLoop().addTimer_forMode_(self.timer, NSRunLoopCommonModes)
        def warmup():
            try:
                from dictate import warmup
                warmup()
            except Exception:
                self.events.put(('model_error', 'Не удалось загрузить распознавание'))
        threading.Thread(target=warmup, daemon=True).start()
        if interactive:
            self.request_microphone()
            if self.hotkey_enabled:
                self.enable_hotkey(prompt=True)

    # ---------- windows ----------
    def make_panel(self):
        frame = NSScreen.mainScreen().visibleFrame()
        x = self.config.get('mac_x', frame.origin.x + frame.size.width - SIZE - 28)
        y = self.config.get('mac_y', frame.origin.y + 110)
        self.panel = Panel.alloc().initWithContentRect_styleMask_backing_defer_(
            NSMakeRect(x, y, SIZE, SIZE), 0 | (1 << 7), NSBackingStoreBuffered, False)  # borderless, non-activating
        self.panel.setOpaque_(False)
        self.panel.setBackgroundColor_(NSColor.clearColor())
        self.panel.setHasShadow_(False)
        self.panel.setHidesOnDeactivate_(False)
        self.panel.setFloatingPanel_(True)
        # After setFloatingPanel_, which resets the level to NSFloatingWindowLevel.
        self.panel.setLevel_(Q.CGWindowLevelForKey(Q.kCGStatusWindowLevelKey))
        self.panel.setBecomesKeyOnlyIfNeeded_(True)
        # all Spaces | stationary | ignored by Cmd+Tab/Exposé cycling | over full-screen apps
        self.panel.setCollectionBehavior_((1 << 0) | (1 << 4) | (1 << 6) | (1 << 8))
        self.panel.setSharingType_(0)  # NSWindowSharingNone: excluded from screen capture
        self.view = BubbleView.alloc().initWithFrame_(NSMakeRect(0, 0, SIZE, SIZE))
        self.view.app = self
        self.view.image = None
        self.panel.setContentView_(self.view)
        self.keep_on_screen()
        if self.bubble_visible:
            self.panel.orderFrontRegardless()

    def keep_on_screen(self):
        frame = self.panel.frame()
        screens = [s.frame() for s in NSScreen.screens()]
        if not any(s.origin.x <= frame.origin.x <= s.origin.x + s.size.width - 16 and
                   s.origin.y <= frame.origin.y <= s.origin.y + s.size.height - 16 for s in screens):
            visible = NSScreen.mainScreen().visibleFrame()
            self.panel.setFrameOrigin_((visible.origin.x + visible.size.width - SIZE - 28, visible.origin.y + 110))

    def make_status_item(self):
        self.status = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        self.status_symbol = None
        self.set_status_symbol('mic')
        self.menu = NSMenu.alloc().init()
        self.menu.setDelegate_(self.target)
        self.menu.setAutoenablesItems_(False)
        self.status.setMenu_(self.menu)
        try:
            self.status.button().window().setSharingType_(0)
        except Exception:
            pass

    def set_status_symbol(self, name):
        if name == self.status_symbol:
            return
        self.status_symbol = name
        image = NSImage.imageWithSystemSymbolName_accessibilityDescription_(name, 'Herdr Voice')
        if image is not None:
            image.setTemplate_(True)
            self.status.button().setImage_(image)
        else:
            self.status.button().setTitle_('HV')

    # ---------- menu ----------
    def build_menu(self):
        menu = NSMenu.alloc().init()
        menu.setAutoenablesItems_(False)
        self.fill_menu(menu)
        return menu

    def fill_menu(self, menu):
        menu.removeAllItems()
        callbacks = self.target.callbacks
        callbacks.clear()
        def add(parent, title, action=None, checked=False, enabled=True):
            item = NSMenuItem.alloc().initWithTitle_action_keyEquivalent_(title, 'menuAction:' if action else None, '')
            if action:
                tag = len(callbacks) + 1
                callbacks[tag] = action
                item.setTag_(tag)
                item.setTarget_(self.target)
            item.setEnabled_(bool(action) and enabled)
            item.setState_(1 if checked else 0)
            parent.addItem_(item)
            return item
        def submenu(parent, title):
            item = add(parent, title)
            item.setEnabled_(True)
            child = NSMenu.alloc().initWithTitle_(title)
            child.setAutoenablesItems_(False)
            item.setSubmenu_(child)
            return child
        label = next(name for name, code, _ in HOTKEYS if code == self.hotkey)
        actions = {'idle': 'Начать запись', 'recording': 'Остановить и вставить',
                   'processing': 'Распознаю…', 'ready': 'Вставить текст'}
        add(menu, f'{actions.get(self.state, "Начать запись")}   ({label})', self.activate,
            enabled=self.state != 'processing')
        if self.state in ('recording', 'processing', 'ready'):
            add(menu, 'Отменить запись', self.cancel)
        add(menu, 'Вставить последнюю диктовку',
            self.paste_last_dictation if self.last_dictation and self.state in ('idle', 'ready') else None)
        menu.addItem_(NSMenuItem.separatorItem())
        if not accessibility_trusted():
            add(menu, 'Разрешить управление (Универсальный доступ)…', self.open_accessibility_settings)
        if self.last_error:
            add(menu, self.last_error)
        add(menu, 'Горячая клавиша включена', self.toggle_hotkey, self.hotkey_enabled)
        keys = submenu(menu, 'Горячая клавиша')
        for name, code, _ in HOTKEYS:
            add(keys, name, lambda code=code: self.set_hotkey(code), code == self.hotkey)
        mics = submenu(menu, 'Микрофон')
        for index, name in self.devices:
            add(mics, name, lambda index=index: self.choose_device(index), index == self.device_index)
        add(menu, 'Показывать жучка', self.toggle_bubble, self.bubble_visible)
        add(menu, 'Вставлять через буфер обмена', self.toggle_paste_mode, self.paste_mode == 'paste')
        add(menu, 'Запускать при входе в систему', self.toggle_autostart, LAUNCH_AGENT.exists())
        menu.addItem_(NSMenuItem.separatorItem())
        add(menu, 'Выйти', self.quit)

    # ---------- settings ----------
    def save(self):
        origin = self.panel.frame().origin
        self.config.update(mac_x=origin.x, mac_y=origin.y, hotkey=self.hotkey,
                           hotkey_enabled=self.hotkey_enabled, paste_mode=self.paste_mode,
                           bubble_visible=self.bubble_visible,
                           microphone=next((name for i, name in self.devices if i == self.device_index), None))
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False), 'utf-8')

    def refresh_devices(self):
        devices = self.sd.query_devices()
        self.devices = [(i, d['name']) for i, d in enumerate(devices) if d['max_input_channels'] > 0]
        default = self.sd.default.device[0]
        wanted = self.config.get('microphone')
        self.device_index = next((i for i, name in self.devices if name == wanted),
                                 default if default is not None and default >= 0 else None)

    def choose_device(self, index):
        self.cancel()
        self.device_index = index
        self.save()
        self.hint('Микрофон выбран')

    def toggle_bubble(self):
        self.bubble_visible = not self.bubble_visible
        if self.bubble_visible:
            self.panel.orderFrontRegardless()
        else:
            self.panel.orderOut_(None)
        self.save()

    def toggle_paste_mode(self):
        self.paste_mode = 'type' if self.paste_mode == 'paste' else 'paste'
        self.save()

    def toggle_autostart(self):
        try:
            if LAUNCH_AGENT.exists():
                LAUNCH_AGENT.unlink()
            else:
                set_autostart(app_bundle())
        except (OSError, RuntimeError):
            self.hint('Не удалось изменить автозапуск')

    def open_accessibility_settings(self):
        accessibility_trusted(prompt=True)
        NSWorkspace.sharedWorkspace().openURL_(NSURL.URLWithString_(
            'x-apple.systempreferences:com.apple.preference.security?Privacy_Accessibility'))

    def request_microphone(self):
        try:
            from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
            if AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio) == 0:  # not determined
                AVCaptureDevice.requestAccessForMediaType_completionHandler_(AVMediaTypeAudio, lambda granted: None)
        except Exception:
            pass

    def microphone_denied(self):
        try:
            from AVFoundation import AVCaptureDevice, AVMediaTypeAudio
            return AVCaptureDevice.authorizationStatusForMediaType_(AVMediaTypeAudio) in (1, 2)
        except Exception:
            return False

    # ---------- hotkey ----------
    def enable_hotkey(self, prompt=False):
        self.hotkey_enabled = True
        if self.tap is not None:
            return
        if not accessibility_trusted():
            if prompt:
                self.explain_accessibility()
            self.hint('Нужен доступ: Универсальный доступ')
            return  # tick() retries once the user grants access
        mask = (Q.CGEventMaskBit(Q.kCGEventKeyDown) | Q.CGEventMaskBit(Q.kCGEventKeyUp) |
                Q.CGEventMaskBit(Q.kCGEventFlagsChanged))
        self.tap_callback = self.on_key_event
        self.tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGHeadInsertEventTap,
                                      Q.kCGEventTapOptionDefault, mask, self.tap_callback, None)
        if self.tap is None:
            self.hint('Не удалось подключить горячую клавишу')
            return
        self.tap_source = Q.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), self.tap_source, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self.tap, True)

    def disable_hotkey(self):
        self.hotkey_enabled = False
        if self.tap is not None:
            Q.CGEventTapEnable(self.tap, False)
            Q.CFRunLoopRemoveSource(Q.CFRunLoopGetCurrent(), self.tap_source, Q.kCFRunLoopCommonModes)
            self.tap = self.tap_source = None

    def toggle_hotkey(self):
        if self.hotkey_enabled:
            self.disable_hotkey()
        else:
            self.enable_hotkey(prompt=True)
        self.save()

    def set_hotkey(self, code):
        self.hotkey = code
        self.hotkey_down = False
        self.enable_hotkey(prompt=True)
        self.save()

    def explain_accessibility(self):
        alert = NSAlert.alloc().init()
        alert.setMessageText_('Herdr Voice: нужен «Универсальный доступ»')
        alert.setInformativeText_(
            'Без него не работают горячая клавиша и вставка текста.\n\n'
            'Откроются Системные настройки → Конфиденциальность и безопасность → '
            'Универсальный доступ. Включите переключатель у HerdrVoice. '
            'Перезапускать программу не нужно.')
        alert.addButtonWithTitle_('Открыть настройки')
        alert.addButtonWithTitle_('Позже')
        self.ns.activateIgnoringOtherApps_(True)
        if alert.runModal() == 1000:
            self.open_accessibility_settings()

    def on_key_event(self, proxy, kind, event, refcon):
        # Runs on the main run loop: queue only, keep the keyboard responsive.
        if kind in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
            if self.tap is not None:
                Q.CGEventTapEnable(self.tap, True)
            return event
        code = Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventKeycode)
        flags = Q.CGEventGetFlags(event)
        if self.hotkey in SIDE_FLAG:
            if kind == Q.kCGEventFlagsChanged and code == self.hotkey:
                if flags & SIDE_FLAG[self.hotkey]:
                    self.hotkey_down = True
                    self.hotkey_spoiled = bool(flags & (MODIFIER_FLAGS & ~FAMILY_FLAG[self.hotkey])
                                               or flags & LEFT_FLAG[self.hotkey])
                elif self.hotkey_down:
                    self.hotkey_down = False
                    if not self.hotkey_spoiled:
                        self.events.put(('hotkey', None))
            elif self.hotkey_down:
                self.hotkey_spoiled = True  # used as a modifier: Option+letter etc.
            return event
        if kind in (Q.kCGEventKeyDown, Q.kCGEventKeyUp) and code == self.hotkey and not flags & MODIFIER_FLAGS:
            if kind == Q.kCGEventKeyDown and not Q.CGEventGetIntegerValueField(event, Q.kCGKeyboardEventAutorepeat):
                self.events.put(('hotkey', None))
            return None  # consumed
        return event

    # ---------- bubble mouse ----------
    def mouse_down(self):
        location = NSEvent.mouseLocation()
        origin = self.panel.frame().origin
        self.press = (location.x, location.y, origin.x, origin.y)
        self.dragged = False

    def mouse_dragged(self):
        if not self.press:
            return
        location = NSEvent.mouseLocation()
        px, py, x, y = self.press
        if abs(location.x - px) + abs(location.y - py) > 5:
            self.dragged = True
            self.panel.setFrameOrigin_((x + location.x - px, y + location.y - py))

    def mouse_up(self):
        if self.dragged:
            self.keep_on_screen()
            self.save()
        else:
            self.activate()
        self.press = None

    # ---------- dictation ----------
    def hint(self, text):
        self.last_error = text
        self.error_until = time.monotonic() + 3

    def activate(self):
        if self.state == 'idle':
            self.record()
        elif self.state == 'recording':
            self.stop()
        elif self.state == 'ready':
            self.paste()

    def record(self):
        import numpy as np
        if self.microphone_denied():
            self.hint('Нет доступа к микрофону · Настройки → Конфиденциальность')
            return
        try:
            self.generation += 1
            self.dictation = StreamingDictation()
            recording = self.dictation
            self.pending_text = ''
            self.success_until = self.error_until = 0
            self.peak = 0.0
            def receive(data, frames, timing, status):
                recording.feed(data)
                level = float(np.max(np.abs(data)))
                self.peak = max(self.peak, level)
                self.level = min(1.0, level * 5)
            self.stream = self.sd.InputStream(device=self.device_index, samplerate=16000, channels=1,
                                              dtype='float32', blocksize=1600, callback=receive)
            self.stream.start()
            self.last_dictation = ''
            self.started = time.monotonic()
            self.state = 'recording'
        except Exception:
            self.close_stream()
            if self.dictation:
                self.dictation.cancel()
            self.state = 'idle'
            self.hint('Нет микрофона · выберите его в меню')

    def close_stream(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def stop(self):
        self.close_stream()
        self.state = 'processing'
        recording, generation = self.dictation, self.generation
        silent = self.peak == 0.0
        def worker():
            try:
                text = recording.finish() if recording else ''
                self.events.put(('text', (generation, text, silent)))
            except Exception:
                self.events.put(('error', (generation, 'Не удалось распознать · повторите запись', silent)))
        threading.Thread(target=worker, daemon=True).start()

    def paste(self):
        if not self.pending_text:
            return
        if not accessibility_trusted():
            self.state = 'ready'
            self.hint('Нужен Универсальный доступ для вставки')
            return
        try:
            (paste_text if self.paste_mode == 'paste' else type_text)(clean_text(self.pending_text) + ' ')
            self.pending_text = ''
            self.state = 'idle'
            self.success_until = time.monotonic() + 1.3
        except Exception:
            self.state = 'ready'
            self.hint('Не удалось вставить · выберите поле и нажмите ещё раз')

    def paste_last_dictation(self):
        if self.last_dictation and self.state in ('idle', 'ready'):
            self.pending_text = self.last_dictation
            self.paste()

    def cancel(self):
        self.close_stream()
        self.generation += 1
        if self.dictation:
            self.dictation.cancel()
        self.dictation = None
        self.pending_text = ''
        self.state = 'idle'

    # ---------- loop ----------
    def draw(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGBA', (SIZE, SIZE))
        c = ImageDraw.Draw(image)
        recording = self.state == 'recording'
        step = self.frame // (3 if recording else 14)
        dx = (-1, 1, 0, 1)[self.frame % 4] if recording else 0
        dy = (0, 1, -1, 0)[self.frame % 4] if recording else step % 2
        image.alpha_composite(self.mascots['recording' if recording else 'open'], (dx, dy))
        if recording:
            c.rectangle((51, 3, 60, 12), fill='#FF615B')
            for n in range(3):
                height = 3 + int(self.level * (5 + 4 * math.sin(self.frame / 3 + n)))
                c.rectangle((25 + n * 5, 61 - height, 28 + n * 5, 61), fill='#FF615B')
        elif self.state == 'processing':
            for n in range(3):
                c.rectangle((24 + n * 6, 57, 27 + n * 6, 60), fill='#C4A7E7' if step % 3 == n else '#514659')
        elif self.state == 'ready' or time.monotonic() < self.success_until:
            for left, top in ((46, 52), (49, 55), (52, 52), (55, 49), (58, 46)):
                c.rectangle((left, top, left + 3, top + 3), fill='#80DEA7')
        elif time.monotonic() < self.error_until:
            c.rectangle((55, 46, 58, 54), fill='#FFBE70')
            c.rectangle((55, 57, 58, 60), fill='#FFBE70')
        self.view.image = ns_image(image.resize((SIZE * 2, SIZE * 2), Image.Resampling.NEAREST))
        self.view.setNeedsDisplay_(True)
        self.set_status_symbol({'recording': 'record.circle', 'processing': 'ellipsis.circle'}.get(
            self.state, 'exclamationmark.triangle' if time.monotonic() < self.error_until else 'mic'))

    def tick(self):
        self.frame += 1
        if self.state == 'recording' and time.monotonic() - self.started >= 300:
            self.stop()
        if self.hotkey_enabled and self.tap is None and self.frame % 30 == 0 and accessibility_trusted():
            self.enable_hotkey()  # access was just granted in System Settings
            self.last_error = ''
        try:
            kind, value = self.events.get_nowait()
            if kind == 'hotkey':
                self.activate()
            elif kind == 'model_error':
                self.hint(value)
            elif kind in ('text', 'error'):
                generation, result, silent = value
                if generation == self.generation and self.state == 'processing':
                    if kind == 'text' and result:
                        self.last_dictation = self.pending_text = result
                        self.paste()
                    else:
                        self.state = 'idle'
                        self.hint(result if kind == 'error' else
                                  'Микрофон молчит · проверьте доступ к микрофону' if silent else
                                  'Речь не обнаружена')
        except queue.Empty:
            pass
        if self.bubble_visible or self.frame % 5 == 0:
            self.draw()

    def quit(self):
        self.disable_hotkey()
        self.close_stream()
        if self.dictation:
            self.dictation.cancel()
        self.save()
        self.ns.terminate_(None)


def app_bundle():
    # .../HerdrVoice.app/Contents/MacOS/HerdrVoice -> .../HerdrVoice.app
    executable = Path(sys.executable).resolve()
    if executable.parent.name == 'MacOS' and executable.parents[1].name == 'Contents':
        return executable.parents[2]
    raise RuntimeError('Автозапуск доступен только для HerdrVoice.app')


def set_autostart(bundle):
    LAUNCH_AGENT.parent.mkdir(parents=True, exist_ok=True)
    LAUNCH_AGENT.write_bytes(plistlib.dumps({
        'Label': 'com.herdr.voice',
        'ProgramArguments': ['/usr/bin/open', '-a', str(bundle)],
        'RunAtLoad': True,
    }))


def diagnose(wav_path, out_path):
    """Headless check for CI: windows, capture exclusion and recognition."""
    import wave
    import numpy as np
    with wave.open(wav_path, 'rb') as audio:
        samples = np.frombuffer(audio.readframes(audio.getnframes()), np.int16).astype(np.float32) / 32768
        rate = audio.getframerate()
    samples = np.interp(np.arange(0, len(samples), rate / 16000), np.arange(len(samples)), samples).astype(np.float32)
    app = App(interactive=False)
    app.tick()
    menu = app.build_menu()
    recording = StreamingDictation()
    recording.feed(samples)
    text = recording.finish()
    from dictate import transcribe
    result = dict(text=text, silence=transcribe(np.zeros(16000, dtype=np.float32)),
                  sharing_type=int(app.panel.sharingType()), level=int(app.panel.level()),
                  menu_items=int(menu.numberOfItems()), devices=len(app.devices))
    Path(out_path).write_text(json.dumps(result, ensure_ascii=False), 'utf-8')


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--diagnose':
        diagnose(sys.argv[2], sys.argv[3])
        return
    APP_SUPPORT.mkdir(parents=True, exist_ok=True)
    lock = open(APP_SUPPORT / 'instance.lock', 'w')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return  # already running
    app = App()
    app.ns.run()


if __name__ == '__main__':
    main()
