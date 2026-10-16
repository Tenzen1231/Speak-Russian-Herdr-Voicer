"""Per-pixel alpha presentation for the small, non-activating Tk window."""
import ctypes as c
from ctypes import wintypes as w

u = c.WinDLL('user32', use_last_error=True)
g = c.WinDLL('gdi32', use_last_error=True)
class Header(c.Structure):
    _fields_ = [('size',w.DWORD),('width',w.LONG),('height',w.LONG),('planes',w.WORD),('bits',w.WORD),('compression',w.DWORD),('image_size',w.DWORD),('x',w.LONG),('y',w.LONG),('used',w.DWORD),('important',w.DWORD)]
class Blend(c.Structure):
    _fields_ = [('operation',w.BYTE),('flags',w.BYTE),('alpha',w.BYTE),('format',w.BYTE)]
g.CreateCompatibleDC.argtypes=[w.HDC];g.CreateCompatibleDC.restype=w.HDC
g.CreateDIBSection.argtypes=[w.HDC,c.POINTER(Header),w.UINT,c.POINTER(c.c_void_p),w.HANDLE,w.DWORD];g.CreateDIBSection.restype=w.HBITMAP
g.SelectObject.argtypes=[w.HDC,w.HANDLE];g.SelectObject.restype=w.HANDLE
g.DeleteObject.argtypes=[w.HANDLE];g.DeleteDC.argtypes=[w.HDC]
u.GetWindowLongPtrW.argtypes=[w.HWND,c.c_int];u.GetWindowLongPtrW.restype=c.c_ssize_t
u.SetWindowLongPtrW.argtypes=[w.HWND,c.c_int,c.c_ssize_t];u.SetWindowLongPtrW.restype=c.c_ssize_t
u.UpdateLayeredWindow.argtypes=[w.HWND,w.HDC,c.POINTER(w.POINT),c.POINTER(w.SIZE),w.HDC,c.POINTER(w.POINT),w.DWORD,c.POINTER(Blend),w.DWORD]
u.UpdateLayeredWindow.restype=w.BOOL

class LayeredSurface:
    def __init__(self, hwnd, width, height):
        self.hwnd=hwnd
        self.size=w.SIZE(width,height)
        self.dc=g.CreateCompatibleDC(None)
        self.bits=c.c_void_p()
        header=Header(c.sizeof(Header),width,-height,1,32,0,width*height*4,0,0,0,0)
        self.bitmap=g.CreateDIBSection(self.dc,c.byref(header),0,c.byref(self.bits),None,0)
        if not self.bitmap:
            g.DeleteDC(self.dc)
            raise c.WinError(c.get_last_error())
        self.old=g.SelectObject(self.dc,self.bitmap)
        style=u.GetWindowLongPtrW(hwnd,-20)
        u.SetWindowLongPtrW(hwnd,-20,style & ~0x80000)
        u.SetWindowLongPtrW(hwnd,-20,style | 0x80000)

    def present(self, image):
        # Windows expects premultiplied BGRA, not straight RGBA or a color key.
        raw=image.convert('RGBa').tobytes('raw','BGRa')
        c.memmove(self.bits,raw,len(raw))
        origin=w.POINT(0,0)
        blend=Blend(0,0,255,1)
        if not u.UpdateLayeredWindow(self.hwnd,None,None,c.byref(self.size),self.dc,c.byref(origin),0,c.byref(blend),2):
            raise c.WinError(c.get_last_error())

    def close(self):
        g.SelectObject(self.dc,self.old)
        g.DeleteObject(self.bitmap)
        g.DeleteDC(self.dc)
