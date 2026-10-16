"""Small non-activating desktop dictation bubble. Audio and text stay in memory."""
import ctypes
from ctypes import wintypes as w
import json
import os
import sys
import math
import queue
import threading
import time
import tkinter as tk
from pathlib import Path
if not getattr(sys, 'frozen', False):
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'core'))  # shared engine
from dictate import ROOT, clean_text, transcribe
from streaming import StreamingDictation

u = ctypes.WinDLL('user32', use_last_error=True)
u.GetForegroundWindow.restype = w.HWND
u.SetForegroundWindow.argtypes = [w.HWND]
u.GetAncestor.argtypes = [w.HWND, w.UINT]
u.GetAncestor.restype = w.HWND
u.GetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int]
u.GetWindowLongPtrW.restype = ctypes.c_ssize_t
u.SetWindowLongPtrW.argtypes = [w.HWND, ctypes.c_int, ctypes.c_ssize_t]
u.SetWindowLongPtrW.restype = ctypes.c_ssize_t
u.CallWindowProcW.argtypes = [ctypes.c_void_p, w.HWND, w.UINT, w.WPARAM, w.LPARAM]
u.CallWindowProcW.restype = ctypes.c_ssize_t
u.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.UINT]
u.GetAsyncKeyState.argtypes = [ctypes.c_int]
u.GetAsyncKeyState.restype = ctypes.c_short
u.IsWindow.argtypes = [w.HWND]
u.IsIconic.argtypes = [w.HWND]
u.IsWindowVisible.argtypes = [w.HWND]
u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
u.SetWindowDisplayAffinity.argtypes = [w.HWND, w.DWORD]
u.GetWindowDisplayAffinity.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]


def hide_from_capture(hwnd):
    # WDA_EXCLUDEFROMCAPTURE (Windows 10 2004+): visible on the local monitor,
    # absent from screen sharing, recordings and screenshots.
    return bool(u.SetWindowDisplayAffinity(hwnd, 0x11))


class Keyboard(ctypes.Structure):
    _fields_ = [('vk', w.WORD), ('scan', w.WORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('extra', ctypes.c_size_t)]


class Mouse(ctypes.Structure):
    _fields_ = [('dx', w.LONG), ('dy', w.LONG), ('data', w.DWORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('extra', ctypes.c_size_t)]


class Payload(ctypes.Union):
    _fields_ = [('keyboard', Keyboard), ('mouse', Mouse)]


class Input(ctypes.Structure):
    _fields_ = [('type', w.DWORD), ('payload', Payload)]


u.SendInput.argtypes = [w.UINT, ctypes.POINTER(Input), ctypes.c_int]
u.SendInput.restype = w.UINT


def text_events(text):
    raw = clean_text(text).encode('utf-16-le')
    units = [int.from_bytes(raw[n:n + 2], 'little') for n in range(0, len(raw), 2)]
    events = (Input * (len(units) * 2))()
    for n, unit in enumerate(units):
        for release in range(2):
            events[n * 2 + release].type = 1
            events[n * 2 + release].payload.keyboard = Keyboard(0, unit, 4 | release * 2, 0, 0)
    return events


def insert_text(text, target):
    if not target or not u.IsWindow(target) or u.GetForegroundWindow() != target:
        raise RuntimeError('Выберите поле ввода и нажмите шарик ещё раз')
    if any(u.GetAsyncKeyState(key) & 0x8000 for key in (0x10, 0x11, 0x12, 0x5B, 0x5C)):
        raise RuntimeError('Отпустите клавиши и нажмите шарик ещё раз')
    events = text_events(text + ' ')
    count = u.SendInput(len(events), events, ctypes.sizeof(Input))
    if count != len(events):
        raise RuntimeError('Ввод заблокирован приложением')


class Bubble:
    def __init__(self, enable_hotkey=True):
        previous_window = u.GetForegroundWindow()
        import sounddevice as sd
        self.sd = sd
        settings_dir = Path(os.environ.get('LOCALAPPDATA', str(Path.home()))) / 'HerdrVoice'
        settings_dir.mkdir(parents=True, exist_ok=True)
        self.config_path = settings_dir / 'settings.json'
        try:
            self.config = json.loads(self.config_path.read_text('utf-8'))
        except (OSError, ValueError):
            self.config = {}
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title('Herdr Voice')
        self.root.overrideredirect(True)
        self.root.configure(bg='#010101')
        self.root.wm_attributes('-topmost', True)
        self.x = min(max(0, self.config.get('x', self.root.winfo_screenwidth() - 92)), self.root.winfo_screenwidth() - 64)
        self.y = min(max(0, self.config.get('y', self.root.winfo_screenheight() - 170)), self.root.winfo_screenheight() - 64)
        self.root.geometry(f'64x64+{self.x}+{self.y}')
        self.canvas = tk.Canvas(self.root, width=64, height=64, bg='#010101', highlightthickness=0)
        self.canvas.pack(fill='both', expand=True)
        self.root.update_idletasks()
        self.hwnd = u.GetAncestor(self.root.winfo_id(), 2)
        style = u.GetWindowLongPtrW(self.hwnd, -20)
        u.SetWindowLongPtrW(self.hwnd, -20, (style | 0x08000000 | 0x80) & ~0x40000)
        callback = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self.old_proc = u.GetWindowLongPtrW(self.hwnd, -4)
        def procedure(hwnd, message, wp, lp):
            if message == 0x21:  # WM_MOUSEACTIVATE: keep the text editor focused.
                return 3
            return u.CallWindowProcW(self.old_proc, hwnd, message, wp, lp)
        self.procedure = callback(procedure)
        u.SetWindowLongPtrW(self.hwnd, -4, ctypes.cast(self.procedure, ctypes.c_void_p).value)
        hide_from_capture(self.hwnd)
        self.root.deiconify()
        u.SetWindowPos(self.hwnd, w.HWND(-1), self.x, self.y, 64, 64, 0x10 | 0x40)
        if previous_window and u.GetForegroundWindow() == self.hwnd:
            u.SetForegroundWindow(previous_window)
        self.state = 'idle'
        self.stream = None
        self.dictation = None
        self.generation = 0
        self.events = queue.Queue()
        self.pending_text = ''
        self.last_dictation = ''
        self.paste_wait_until = 0
        self.level = 0.0
        self.target = None
        self.press = None
        self.dragged = False
        self.frame = 0
        self.success_until = 0
        self.error_until = 0
        self.last_error = ''
        self.caps = None
        self.caps_enabled = enable_hotkey and self.config.get('hotkey_enabled', True)
        self.hotkey_vk = self.config.get('hotkey_vk', 0x14)
        self.hotkey_modifiers = self.config.get('hotkey_modifiers', 0)
        self.settings_dialog = None
        from mascot import render
        from layered import LayeredSurface
        self.surface = LayeredSurface(self.hwnd, 64, 64)
        self.mascots = {name: render(name) for name in ('open', 'recording')}
        devices = sd.query_devices()
        default = sd.default.device[0]
        self.devices = [(i, d['name']) for i, d in enumerate(devices)
                        if d['max_input_channels'] > 0 and d['hostapi'] == devices[default]['hostapi']]
        self.device_index = next((i for i, name in self.devices if name == self.config.get('microphone')), default)
        self.canvas.bind('<ButtonPress-1>', self.down)
        self.canvas.bind('<B1-Motion>', self.drag)
        self.canvas.bind('<ButtonRelease-1>', self.up)
        self.canvas.bind('<ButtonRelease-3>', self.menu)
        if self.caps_enabled:
            self.enable_caps()
        def warmup():
            try:
                from dictate import warmup
                warmup()
            except Exception:
                self.events.put(('model_error', 'Не удалось загрузить распознавание'))
        threading.Thread(target=warmup, daemon=True).start()
        self.root.after(60, self.tick)
        self.root.after(500, self.keep_on_top)

    def keep_on_top(self):
        # Reassert without activating the bubble or stealing the editor focus.
        if u.IsIconic(self.hwnd) or not u.IsWindowVisible(self.hwnd):
            u.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
        u.SetWindowPos(self.hwnd, w.HWND(-1), 0, 0, 0, 0,
                       0x1 | 0x2 | 0x10 | 0x40 | 0x200)  # no size/move/activation
        self.root.after(500, self.keep_on_top)

    def save(self):
        self.config.update(x=self.x, y=self.y,
                           microphone=next(name for i, name in self.devices if i == self.device_index),
                           hotkey_vk=self.hotkey_vk, hotkey_modifiers=self.hotkey_modifiers,
                           hotkey_enabled=self.caps_enabled)
        self.config_path.write_text(json.dumps(self.config, ensure_ascii=False), 'utf-8')

    def down(self, event):
        self.press = (event.x_root, event.y_root, self.x, self.y)
        self.dragged = False

    def drag(self, event):
        if not self.press:
            return
        px, py, x, y = self.press
        if abs(event.x_root - px) + abs(event.y_root - py) > 5:
            self.dragged = True
            self.x = max(0, min(self.root.winfo_screenwidth() - 64, x + event.x_root - px))
            self.y = max(0, min(self.root.winfo_screenheight() - 64, y + event.y_root - py))
            self.root.geometry(f'64x64+{self.x}+{self.y}')

    def up(self, _):
        if self.dragged:
            self.save()
        else:
            self.activate()
        self.press = None

    def activate(self):
        if self.state == 'idle':
            self.record()
        elif self.state == 'recording':
            # The user may select a different field while dictating.
            self.target = u.GetForegroundWindow()
            self.stop()
        elif self.state == 'ready':
            self.target = u.GetForegroundWindow()
            self.paste()

    def enable_caps(self):
        from capslock import Hotkey
        try:
            self.caps = Hotkey(lambda: self.events.put(('hotkey', None)),
                               self.hotkey_vk, self.hotkey_modifiers)
        except OSError:
            self.caps_enabled = False
            self.hint('Не удалось подключить горячую клавишу')

    def toggle_caps(self):
        if self.caps:
            self.caps.close()
            self.caps = None
            self.caps_enabled = False
        else:
            self.caps_enabled = True
            self.enable_caps()

        self.save()

    def set_hotkey(self, vk, modifiers=0):
        if self.caps:
            self.caps.close()
            self.caps = None
        self.hotkey_vk, self.hotkey_modifiers = vk, modifiers
        self.caps_enabled = True
        self.enable_caps()
        self.save()

    def configure_hotkey(self):
        from capslock import hotkey_label, modifiers_down
        if self.settings_dialog:
            self.settings_dialog.lift()
            return
        was_enabled = self.caps_enabled
        if self.caps:
            self.caps.close()
            self.caps = None
        previous = u.GetForegroundWindow()
        dialog = tk.Toplevel(self.root)
        dialog.withdraw()  # Exclude from capture before it is ever shown.
        self.settings_dialog = dialog
        dialog.title('Горячая клавиша')
        dialog.resizable(False, False)
        dialog.wm_attributes('-topmost', True)
        dialog.configure(bg='#242532')
        candidate = [self.hotkey_vk, self.hotkey_modifiers]
        label = tk.StringVar(master=dialog, value=hotkey_label(*candidate))
        tk.Label(dialog, text='Нажмите клавишу или сочетание', bg='#242532', fg='white').pack(padx=20, pady=(16, 8))
        tk.Label(dialog, textvariable=label, bg='#242532', fg='#5EDDC6', font=('Segoe UI', 15, 'bold')).pack(pady=6)
        tk.Label(dialog, text='Эта клавиша будет занята диктовкой.\nEscape — отмена.', bg='#242532', fg='#bbbbc8').pack(padx=20, pady=8)
        def close(apply=False):
            dialog.destroy()
            self.settings_dialog = None
            if apply:
                self.set_hotkey(*candidate)
            elif was_enabled:
                self.enable_caps()
            if previous:
                u.SetForegroundWindow(previous)
        def capture(event):
            if event.keycode == 0x1B:
                close()
            elif event.keycode not in (0x10, 0x11, 0x12, 0x5B, 0x5C, 0xA0, 0xA1, 0xA2, 0xA3, 0xA4, 0xA5) and 8 <= event.keycode <= 254:
                candidate[:] = [event.keycode, modifiers_down()]
                label.set(hotkey_label(*candidate))
            return 'break'
        dialog.bind('<KeyPress>', capture)
        tk.Button(dialog, text='Сохранить', command=lambda: close(True), takefocus=False).pack(pady=(4, 16))
        dialog.protocol('WM_DELETE_WINDOW', close)
        dialog.update_idletasks()
        hide_from_capture(u.GetAncestor(dialog.winfo_id(), 2))
        dialog.deiconify()
        dialog.focus_force()

    def hint(self, text):
        self.last_error = text
        self.error_until = time.monotonic() + 3

    def record(self):
        self.target = u.GetForegroundWindow()
        if not self.target or self.target == self.hwnd:
            self.hint('Сначала выберите поле ввода')
            return
        try:
            import numpy as np
            self.generation += 1
            self.dictation = StreamingDictation()
            recording = self.dictation
            self.pending_text = ''
            self.success_until = 0
            self.error_until = 0
            def receive(data, frames, timing, status):
                recording.feed(data)
                self.level = min(1.0, float(np.max(np.abs(data))) * 5)
            self.stream = self.sd.InputStream(device=self.device_index, samplerate=16000,
                                              channels=1, dtype='float32', blocksize=1600, callback=receive)
            self.stream.start()
            self.last_dictation = ''
            self.started = time.monotonic()
            self.state = 'recording'
        except Exception:
            self.close_stream()
            if self.dictation:
                self.dictation.cancel()
            self.hint('Нет микрофона · выберите его через ПКМ')

    def close_stream(self):
        if self.stream is not None:
            self.stream.stop()
            self.stream.close()
            self.stream = None

    def stop(self):
        self.close_stream()
        self.state = 'processing'
        recording, generation = self.dictation, self.generation
        def worker():
            try:
                text = recording.finish() if recording else ''
                self.events.put(('text', (generation, text)))
            except Exception:
                self.events.put(('error', (generation, 'Не удалось распознать · повторите запись')))
        threading.Thread(target=worker, daemon=True).start()

    def paste(self):
        if not self.pending_text:
            return
        from capslock import modifiers_down
        if modifiers_down():
            if not self.paste_wait_until:
                self.paste_wait_until = time.monotonic() + 2
            if time.monotonic() < self.paste_wait_until:
                self.root.after(30, self.paste)
                return
        self.paste_wait_until = 0
        try:
            insert_text(self.pending_text, self.target)
            self.pending_text = ''
            self.state = 'idle'
            self.success_until = time.monotonic() + 1.3
        except RuntimeError as error:
            self.state = 'ready'
            self.hint(str(error))

    def paste_last_dictation(self):
        if not self.last_dictation or self.state not in ('idle', 'ready'):
            return
        self.target = u.GetForegroundWindow()
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

    def toggle_autostart(self):
        from autostart import is_enabled, set_enabled
        try:
            set_enabled(not is_enabled())
        except OSError:
            self.hint('Не удалось изменить автозапуск Windows')

    def menu(self, event):
        from autostart import is_enabled
        from capslock import hotkey_label
        from popup import show
        hotkeys = [(hotkey_label(vk), lambda key=vk: self.set_hotkey(key),
                    self.hotkey_vk == vk and self.hotkey_modifiers == 0)
                   for vk in (0x14, 0x77, 0x78, 0x79, 0x13, 0x91)]
        hotkeys.append(('Своя клавиша / сочетание…', self.configure_hotkey, False))
        devices = [(name, lambda i=index: self.choose_device(i), index == self.device_index)
                   for index, name in self.devices]
        items = [(hotkey_label(self.hotkey_vk, self.hotkey_modifiers) + ' / клик: запись и вставка', None, False),
                 ('Горячая клавиша включена', self.toggle_caps, self.caps_enabled),
                 ('Выбрать горячую клавишу', hotkeys, False),
                 ('Микрофон', devices, False),
                 ('Отменить запись', self.cancel, False),
                 ('Запускать вместе с Windows', self.toggle_autostart, is_enabled()),
                 ('Вставить последнюю диктовку',
                  self.paste_last_dictation if self.last_dictation and self.state in ('idle', 'ready') else None, False)]
        if self.last_error:
            items.append((self.last_error, None, False))
        items.extend([(None, None, False), ('Закрыть', self.quit, False)])
        show(self.hwnd, event.x_root, event.y_root, items)

    def choose_device(self, index):
        self.cancel()
        self.device_index = index
        self.save()
        self.hint('Микрофон выбран')

    def draw(self):
        from PIL import Image, ImageDraw
        image = Image.new('RGBA', (64, 64))
        c = ImageDraw.Draw(image)
        recording = self.state == 'recording'
        step = self.frame // (3 if recording else 14)
        expression = 'recording' if recording else 'open'
        dx = (-1, 1, 0, 1)[self.frame % 4] if recording else 0
        dy = (0, 1, -1, 0)[self.frame % 4] if recording else step % 2
        image.alpha_composite(self.mascots[expression], (dx, dy))
        if recording:
            # Pixel recording light and sound-reactive meter.
            c.rectangle((51, 3, 60, 12, ), fill='#FF615B')
            for n in range(3):
                height = 3 + int(self.level * (5 + 4 * math.sin(self.frame / 3 + n)))
                c.rectangle((25 + n * 5, 61 - height, 28 + n * 5, 61,
                                   ), fill='#FF615B')
        elif self.state in ('processing', 'cancelled'):
            for n in range(3):
                c.rectangle((24 + n * 6, 57, 27 + n * 6, 60,
                                   ), fill='#C4A7E7' if step % 3 == n else '#514659')
        elif self.state == 'ready' or time.monotonic() < self.success_until:
            for left, top in ((46, 52), (49, 55), (52, 52), (55, 49), (58, 46)):
                c.rectangle((left, top, left + 3, top + 3, ), fill='#80DEA7')
        elif time.monotonic() < self.error_until:
            c.rectangle((55, 46, 58, 54, ), fill='#FFBE70')
            c.rectangle((55, 57, 58, 60, ), fill='#FFBE70')

        self.surface.present(image)

    def tick(self):
        self.frame += 1
        if self.state == 'recording' and time.monotonic() - self.started >= 300:
            self.stop()
        try:
            kind, value = self.events.get_nowait()
            if kind == 'hotkey':
                self.activate()
            elif kind == 'model_error':
                self.hint(value)
            elif kind in ('text', 'error'):
                generation, result = value
                if generation == self.generation and self.state == 'processing':
                    if kind == 'text' and result:
                        self.last_dictation = result
                        self.pending_text = result
                        self.root.after(30, self.paste)
                    else:
                        self.state = 'idle'
                        self.hint(result if kind == 'error' else 'Речь не обнаружена')
        except queue.Empty:
            pass
        self.draw()
        self.root.after(60, self.tick)

    def quit(self):
        if self.caps:
            self.caps.close()
        self.close_stream()
        if self.dictation:
            self.dictation.cancel()
        self.save()
        u.SetWindowLongPtrW(self.hwnd, -4, self.old_proc)
        self.surface.close()
        self.root.destroy()


def main():
    if len(sys.argv) == 4 and sys.argv[1] == '--diagnose':
        # Optional local diagnostics: no network, no microphone recording.
        import wave
        import numpy as np
        with wave.open(sys.argv[2], 'rb') as audio:
            if audio.getsampwidth() != 2 or audio.getnchannels() != 1:
                raise ValueError('Diagnostic WAV must be mono PCM16')
            samples = np.frombuffer(audio.readframes(audio.getnframes()), np.int16).astype(np.float32) / 32768
            rate = audio.getframerate()
        samples = np.interp(np.arange(0, len(samples), rate / 16000), np.arange(len(samples)), samples).astype(np.float32)
        app = Bubble(enable_hotkey=False)
        app.root.update()
        u.ShowWindow(app.hwnd, 6)  # simulate minimization
        app.keep_on_top()
        visible = bool(u.IsWindowVisible(app.hwnd)) and not u.IsIconic(app.hwnd)
        topmost = bool(u.GetWindowLongPtrW(app.hwnd, -20) & 8)
        recording = StreamingDictation()
        recording.feed(samples)
        text = recording.finish()
        silence = transcribe(np.zeros(16000, dtype=np.float32))
        app.quit()
        Path(sys.argv[3]).write_text(json.dumps(dict(text=text, silence=silence,
            visible=visible, topmost=topmost), ensure_ascii=False), 'utf-8')
        return
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    k.CreateMutexW.restype = ctypes.c_void_p
    mutex = k.CreateMutexW(None, False, 'Local\\HerdrVoiceBubble')
    if ctypes.get_last_error() == 183:
        return
    app = Bubble()
    app.root.mainloop()
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    k.CloseHandle(mutex)


if __name__ == '__main__':
    main()
