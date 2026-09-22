"""Locate the WoW 3.3.5a window and strip, and keep the bank recycle epoch.

Read-only Win32 queries (window rectangles, process image paths and start
state). Nothing here touches game memory or sends input.
"""
from dataclasses import dataclass
import ctypes
import os
from pathlib import Path
import re
import tempfile
import time

ANCHORS = ('TOP', 'TOPLEFT', 'TOPRIGHT', 'BOTTOM', 'BOTTOMLEFT', 'BOTTOMRIGHT')
STRIP_UNITS = (512, 32)  # 128 x 8 cells of 4 units at effective scale 1
UI_HEIGHT = 768          # UI units spanning the full window height at scale 1


@dataclass(frozen=True)
class GameWindow:
    pid: int
    left: int
    top: int
    width: int
    height: int
    minimized: bool = False
    hwnd: int = 0

    def on_screen(self, box):
        """Client-relative crop -> virtual-screen crop."""
        x, y, w, h = box
        return (self.left + x, self.top + y, w, h)


def game_dir_for(addon):
    """addon = <game>/Interface/AddOns/AgentBridge"""
    return Path(addon).resolve().parents[2]


def gx_aspect(game_dir):
    """Aspect ratio of gxResolution in Config.wtf, if set (render size may differ from the window)."""
    try:
        text = (Path(game_dir) / 'WTF' / 'Config.wtf').read_text(encoding='utf-8', errors='replace')
    except OSError:
        return None
    match = re.search(r'SET\s+gxResolution\s+"(\d+)x(\d+)"', text, re.IGNORECASE)
    if not match or not int(match.group(2)):
        return None
    return int(match.group(1)) / int(match.group(2))


def strip_candidates(window, aspect=None):
    """Client-relative crops where the strip can be, per anchor and aspect model.

    The strip has effective scale 1, so 768 units span the window height. If
    the client renders at gxResolution and stretches to a differently shaped
    window, horizontal pixels per unit differ from vertical ones; try both.
    """
    ry = window.height / UI_HEIGHT
    models = [ry]
    if aspect:
        rx = window.width / (UI_HEIGHT * aspect)
        if abs(rx - ry) > 1e-3:
            models.append(rx)
    result = []
    for rx in models:
        w, h = STRIP_UNITS[0] * rx, STRIP_UNITS[1] * ry
        for anchor in ANCHORS:
            x = {'LEFT': 0, 'RIGHT': window.width - w}.get(anchor.replace('TOP', '').replace('BOTTOM', ''),
                                                           (window.width - w) / 2)
            y = 0 if anchor.startswith('TOP') else window.height - h
            result.append((anchor, (round(x), round(y), round(w), round(h))))
    return result


if os.name == 'nt':
    from ctypes import wintypes
    user32 = ctypes.WinDLL('user32', use_last_error=True)
    kernel32 = ctypes.WinDLL('kernel32', use_last_error=True)
    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
    user32.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.IsIconic.argtypes = [wintypes.HWND]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR,
                                                    ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.K32EnumProcesses.argtypes = [ctypes.POINTER(wintypes.DWORD), wintypes.DWORD,
                                          ctypes.POINTER(wintypes.DWORD)]

    def process_image(pid):
        handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return None
        try:
            size = wintypes.DWORD(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return buffer.value
        finally:
            kernel32.CloseHandle(handle)
        return None

    def game_processes(game_dir):
        """PIDs of executables that live in the game folder (any client exe name)."""
        game_dir = os.path.normcase(str(Path(game_dir).resolve()))
        pids = (wintypes.DWORD * 8192)()
        needed = wintypes.DWORD()
        if not kernel32.K32EnumProcesses(pids, ctypes.sizeof(pids), ctypes.byref(needed)):
            raise OSError(ctypes.get_last_error(), 'EnumProcesses failed')
        found = []
        for pid in pids[:needed.value // ctypes.sizeof(wintypes.DWORD)]:
            image = pid and process_image(pid)
            if image and os.path.normcase(os.path.dirname(image)) == game_dir:
                found.append(pid)
        return found

    def game_windows(pids):
        pids, windows = set(pids), []

        def visit(hwnd, _):
            if not user32.IsWindowVisible(hwnd):
                return True
            name = ctypes.create_unicode_buffer(64)
            user32.GetClassNameW(hwnd, name, 64)
            if not name.value.startswith('GxWindowClass'):
                return True
            pid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value not in pids:
                return True
            rect, origin = wintypes.RECT(), wintypes.POINT(0, 0)
            user32.GetClientRect(hwnd, ctypes.byref(rect))
            user32.ClientToScreen(hwnd, ctypes.byref(origin))
            windows.append(GameWindow(pid.value, origin.x, origin.y, rect.right - rect.left,
                                      rect.bottom - rect.top, bool(user32.IsIconic(hwnd)), int(hwnd)))
            return True
        user32.EnumWindows(WNDENUMPROC(visit), 0)
        return windows
else:  # Non-Windows hosts only run the tests.
    def game_processes(game_dir):
        return []

    def game_windows(pids):
        return []


class EpochKeeper:
    """Rewrite Epoch.lua only while no client from this game folder is running.

    The addon resets its slot counter when it sees a new epoch. Because the
    value can only change while the game is closed, the first UI load that sees
    it belongs to a client process that has not loaded any bank font yet.
    """

    def __init__(self, addon, processes=game_processes, clock=time.time):
        self.path = Path(addon) / 'Epoch.lua'
        self.game_dir = game_dir_for(addon)
        self.processes, self.clock = processes, clock
        self.written_while_closed = False

    def poll(self):
        running = bool(self.processes(self.game_dir))
        if running:
            self.written_while_closed = False
            return False
        if self.written_while_closed:
            return False
        write_epoch(self.path, str(int(self.clock() * 1000)))
        self.written_while_closed = True
        return True


def write_epoch(path, value):
    if not value.isdigit():
        raise ValueError('Epoch must be digits')
    fd, name = tempfile.mkstemp(prefix='.epoch-', suffix='.lua', dir=Path(path).parent)
    with os.fdopen(fd, 'w', encoding='ascii') as file:
        file.write(f'-- Written by the Agent Bridge companion while WoW was closed.\nAgentBridgeEpoch = "{value}"\n')
    os.replace(name, path)


class StripLocator:
    """Find the strip automatically: try every candidate crop until one decodes, then stick."""

    def __init__(self, game_dir, clock=time.monotonic):
        self.game_dir, self.clock = Path(game_dir), clock
        self.windows, self.locked, self.refreshed, self.last_ok = [], None, -1e9, -1e9
        self.aspect = None

    def refresh(self, force=False):
        now = self.clock()
        if not force and now - self.refreshed < 2:
            return
        self.refreshed = now
        self.aspect = gx_aspect(self.game_dir)
        self.windows = [w for w in game_windows(game_processes(self.game_dir)) if not w.minimized and w.height > 0]
        if self.locked and self.locked[0] not in self.windows:
            self.locked = None

    def crops(self):
        if self.locked:
            return [self.locked]
        return [(window, anchor, box) for window in self.windows
                for anchor, box in strip_candidates(window, self.aspect)]

    def success(self, crop):
        self.locked, self.last_ok = crop, self.clock()

    def failure(self):
        # Keep a lock through brief glitches; search again after 3 s without a frame.
        if self.locked and self.clock() - self.last_ok > 3:
            self.locked = None
