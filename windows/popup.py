"""Native context menu: Windows owns dismissal, capture and submenu lifetime."""
import ctypes
from ctypes import wintypes as w

u = ctypes.WinDLL('user32', use_last_error=True)
k = ctypes.WinDLL('kernel32', use_last_error=True)
k.GetModuleHandleW.argtypes = [w.LPCWSTR]
k.GetModuleHandleW.restype = w.HMODULE
class WindowClass(ctypes.Structure):
    _fields_ = [('style', w.UINT), ('procedure', ctypes.c_void_p),
                ('class_extra', ctypes.c_int), ('window_extra', ctypes.c_int),
                ('instance', w.HINSTANCE), ('icon', w.HICON), ('cursor', w.HANDLE),
                ('background', w.HBRUSH), ('menu_name', w.LPCWSTR), ('class_name', w.LPCWSTR)]
u.RegisterClassW.argtypes = [ctypes.POINTER(WindowClass)]
_owner_class = WindowClass()
_owner_class.procedure = ctypes.cast(u.DefWindowProcW, ctypes.c_void_p).value
_owner_class.instance = k.GetModuleHandleW(None)
_owner_class.class_name = 'HerdrVoicePopupOwner'
u.RegisterClassW(ctypes.byref(_owner_class))
u.CreatePopupMenu.restype = w.HMENU
u.AppendMenuW.argtypes = [w.HMENU, w.UINT, ctypes.c_size_t, w.LPCWSTR]
u.DestroyMenu.argtypes = [w.HMENU]
u.TrackPopupMenuEx.argtypes = [w.HMENU, w.UINT, ctypes.c_int, ctypes.c_int, w.HWND, ctypes.c_void_p]
u.TrackPopupMenuEx.restype = w.UINT
u.SetForegroundWindow.argtypes = [w.HWND]
u.GetForegroundWindow.restype = w.HWND
u.CreateWindowExW.argtypes = [w.DWORD, w.LPCWSTR, w.LPCWSTR, w.DWORD,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU, w.HINSTANCE, ctypes.c_void_p]
u.CreateWindowExW.restype = w.HWND
u.DestroyWindow.argtypes = [w.HWND]
u.ShowWindow.argtypes = [w.HWND, ctypes.c_int]
u.PostMessageW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
u.SetWindowDisplayAffinity.argtypes = [w.HWND, w.DWORD]
HookProc = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, w.WPARAM, w.LPARAM)
u.SetWindowsHookExW.argtypes = [ctypes.c_int, HookProc, w.HINSTANCE, w.DWORD]
u.SetWindowsHookExW.restype = w.HANDLE
u.CallNextHookEx.argtypes = [w.HANDLE, ctypes.c_int, w.WPARAM, w.LPARAM]
u.CallNextHookEx.restype = ctypes.c_ssize_t
u.UnhookWindowsHookEx.argtypes = [w.HANDLE]


class Message(ctypes.Structure):
    _fields_ = [('lp', w.LPARAM), ('wp', w.WPARAM), ('message', w.UINT), ('hwnd', w.HWND)]


@HookProc
def _hide_menu(code, wp, lp):
    # Menu windows (#32768) are created on this thread. WM_CREATE arrives
    # before they are shown, so screen capture never sees a single frame.
    message = ctypes.cast(lp, ctypes.POINTER(Message)).contents
    if code >= 0 and message.message == 0x0001:
        u.SetWindowDisplayAffinity(message.hwnd, 0x11)
    return u.CallNextHookEx(None, code, wp, lp)


def show(owner, x, y, items):
    callbacks = {}
    def build(entries):
        menu = u.CreatePopupMenu()
        if not menu:
            raise ctypes.WinError(ctypes.get_last_error())
        for label, action, checked in entries:
            if label is None:
                u.AppendMenuW(menu, 0x800, 0, None)
            elif isinstance(action, list):
                u.AppendMenuW(menu, 0x10, build(action), label)
            else:
                index = len(callbacks) + 1
                callbacks[index] = action
                flags = (8 if checked else 0) | (3 if action is None else 0)
                u.AppendMenuW(menu, flags, index, label)
        return menu
    menu = build(items)
    previous = u.GetForegroundWindow()
    # Tk's window procedure assumes menus contain Tk bookkeeping. Use a native
    # owner so Windows menu messages never reach Tk's WM_MENUSELECT handler.
    native_owner = u.CreateWindowExW(0x88, 'HerdrVoicePopupOwner', 'HerdrVoice Menu',
                                    0x80000000, x, y, 1, 1, None, None, None, None)
    if not native_owner:
        u.DestroyMenu(menu)
        raise ctypes.WinError(ctypes.get_last_error())
    hook = u.SetWindowsHookExW(4, _hide_menu, None, k.GetCurrentThreadId())  # WH_CALLWNDPROC
    try:
        u.ShowWindow(native_owner, 4)
        u.SetForegroundWindow(native_owner)
        ctypes.set_last_error(0)
        command = u.TrackPopupMenuEx(menu, 0x182, x, y, native_owner, None)


    finally:
        if hook:
            u.UnhookWindowsHookEx(hook)
        restore = u.GetForegroundWindow() == native_owner
        u.PostMessageW(native_owner, 0, 0, 0)
        u.DestroyMenu(menu)
        u.DestroyWindow(native_owner)
        # Outside clicks may select another application; preserve that selection.
        if restore and previous:
            u.SetForegroundWindow(previous)
    callback = callbacks.get(command)
    if callback:
        callback()
