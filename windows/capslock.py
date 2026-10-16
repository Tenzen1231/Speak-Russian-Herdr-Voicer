"""Consume a configured hotkey only; no typed text is stored."""
import ctypes
from ctypes import wintypes as w

u = ctypes.WinDLL('user32', use_last_error=True)
k = ctypes.WinDLL('kernel32', use_last_error=True)
Proc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)


class Key(ctypes.Structure):
    _fields_ = [('vk', w.DWORD), ('scan', w.DWORD), ('flags', w.DWORD),
                ('time', w.DWORD), ('extra', ctypes.c_size_t)]


u.SetWindowsHookExW.argtypes = [ctypes.c_int, Proc, w.HINSTANCE, w.DWORD]
u.SetWindowsHookExW.restype = w.HANDLE
u.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
u.CallNextHookEx.restype = ctypes.c_ssize_t
u.UnhookWindowsHookEx.argtypes = [w.HANDLE]
k.GetModuleHandleW.argtypes = [w.LPCWSTR]
k.GetModuleHandleW.restype = w.HMODULE


def modifiers_down():
    return sum(bit for bit, keys in ((1, (0x11,)), (2, (0x12,)),
               (4, (0x10,)), (8, (0x5B, 0x5C)))
               if any(u.GetAsyncKeyState(key) & 0x8000 for key in keys))


def hotkey_label(vk, modifiers=0):
    names = {0x14: 'Caps Lock', 0x91: 'Scroll Lock', 0x13: 'Pause',
             0x20: 'Space', 0x2D: 'Insert', 0x2E: 'Delete', 0x09: 'Tab'}
    key = names.get(vk, chr(vk) if 0x30 <= vk <= 0x5A else
                    f'F{vk - 0x6F}' if 0x70 <= vk <= 0x87 else f'Клавиша {vk}')
    return '+'.join([name for bit, name in ((1, 'Ctrl'), (2, 'Alt'), (4, 'Shift'), (8, 'Win'))
                     if modifiers & bit] + [key])


class Hotkey:
    def __init__(self, notify, vk=0x14, modifiers=0):
        self.pressed = False
        self.handle = None
        def callback(code, message, data):
            if code >= 0 and ctypes.cast(data, ctypes.POINTER(Key)).contents.vk == vk:
                if message in (0x100, 0x104):
                    if self.pressed:
                        return 1
                    if modifiers_down() == modifiers:
                        self.pressed = True
                        notify()  # Queue only; keep the keyboard hook responsive.
                        return 1
                elif message in (0x101, 0x105) and self.pressed:
                    self.pressed = False
                    return 1
            return u.CallNextHookEx(self.handle, code, message, data)
        self.callback = Proc(callback)
        self.handle = u.SetWindowsHookExW(13, self.callback, k.GetModuleHandleW(None), 0)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            u.UnhookWindowsHookEx(self.handle)
            self.handle = None

# Compatibility with the original Caps Lock default.
CapsLock = Hotkey
