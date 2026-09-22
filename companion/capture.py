"""Copy one small screen rectangle (GDI BitBlt); Pillow's grab copies the whole desktop."""
import ctypes
import os

from PIL import Image, ImageGrab

if os.name == 'nt':
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32')
    gdi32 = ctypes.WinDLL('gdi32')
    user32.GetDC.restype = wintypes.HDC
    user32.GetDC.argtypes = [wintypes.HWND]
    user32.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
    gdi32.CreateCompatibleDC.restype = wintypes.HDC
    gdi32.CreateCompatibleDC.argtypes = [wintypes.HDC]
    gdi32.CreateCompatibleBitmap.restype = wintypes.HBITMAP
    gdi32.CreateCompatibleBitmap.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int]
    gdi32.SelectObject.restype = wintypes.HGDIOBJ
    gdi32.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
    gdi32.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             wintypes.HDC, ctypes.c_int, ctypes.c_int, wintypes.DWORD]
    gdi32.GetDIBits.argtypes = [wintypes.HDC, wintypes.HBITMAP, wintypes.UINT, wintypes.UINT,
                                ctypes.c_void_p, ctypes.c_void_p, wintypes.UINT]
    gdi32.DeleteObject.argtypes = [wintypes.HGDIOBJ]
    gdi32.DeleteDC.argtypes = [wintypes.HDC]

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [('biSize', wintypes.DWORD), ('biWidth', ctypes.c_long), ('biHeight', ctypes.c_long),
                    ('biPlanes', wintypes.WORD), ('biBitCount', wintypes.WORD), ('biCompression', wintypes.DWORD),
                    ('biSizeImage', wintypes.DWORD), ('biXPelsPerMeter', ctypes.c_long),
                    ('biYPelsPerMeter', ctypes.c_long), ('biClrUsed', wintypes.DWORD),
                    ('biClrImportant', wintypes.DWORD)]

    user32.PrintWindow.argtypes = [wintypes.HWND, wintypes.HDC, wintypes.UINT]

    def _render(width, height, draw):
        """Run `draw(memory_dc)` and read the result back as an RGB image."""
        screen = user32.GetDC(None)
        memory = gdi32.CreateCompatibleDC(screen)
        bitmap = gdi32.CreateCompatibleBitmap(screen, width, height)
        previous = gdi32.SelectObject(memory, bitmap)
        try:
            draw(memory)
            header = BITMAPINFOHEADER(ctypes.sizeof(BITMAPINFOHEADER), width, -height, 1, 32, 0, 0, 0, 0, 0, 0)
            buffer = ctypes.create_string_buffer(width * height * 4)
            if gdi32.GetDIBits(memory, bitmap, 0, height, buffer, ctypes.byref(header), 0) != height:
                raise OSError('GetDIBits failed')
            return Image.frombuffer('RGB', (width, height), buffer.raw, 'raw', 'BGRX', 0, 1)
        finally:
            gdi32.SelectObject(memory, previous)
            gdi32.DeleteObject(bitmap)
            gdi32.DeleteDC(memory)
            user32.ReleaseDC(None, screen)

    def grab(box):
        """box = (x, y, w, h) in virtual-screen pixels -> RGB image.

        Cheap, but it reads whatever is on screen: another window on top of the
        game hides the strip. See grab_window for the occluded case.
        """
        x, y, w, h = box

        def draw(memory):
            screen = user32.GetDC(None)
            try:
                if not gdi32.BitBlt(memory, 0, 0, w, h, screen, x, y, 0x00CC0020):  # SRCCOPY
                    raise OSError('BitBlt failed')
            finally:
                user32.ReleaseDC(None, screen)
        return _render(w, h, draw)

    def grab_window(hwnd, width, height):
        """Ask a window to render itself, even while covered or in the background.

        PW_RENDERFULLCONTENT works for this DX9 client under the desktop
        compositor, so prompts still get through while WoW is behind another
        window. It renders the whole window, so it costs more than grab().
        """
        def draw(memory):
            if not user32.PrintWindow(hwnd, memory, 2):  # PW_RENDERFULLCONTENT
                raise OSError('PrintWindow failed')
        return _render(width, height, draw)
else:
    def grab(box):
        x, y, w, h = box
        return ImageGrab.grab(bbox=(x, y, x + w, y + h), all_screens=True)

    def grab_window(hwnd, width, height):
        raise OSError('Window capture is Windows only')
