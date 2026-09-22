"""Desktop reply banner. Never creates a game frame or sends input to WoW."""
import ctypes
import tkinter as tk


class ReplyBanner:
    def __init__(self, root, open_reply):
        self.root, self.open_reply = root, open_reply
        self.window = self.timer = None

    def dismiss(self):
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None
        if self.window is not None:
            self.window.destroy()
            self.window = None

    def show(self, title, reply):
        self.dismiss()
        summary = ' '.join(reply.split()) or 'Open the companion to read the result.'
        window = self.window = tk.Toplevel(self.root)
        window.withdraw()
        window.overrideredirect(True)
        window.configure(bg='#18212c', borderwidth=2, relief='solid')
        window.attributes('-topmost', True)
        width, height = 390, 170
        x = max(0, self.root.winfo_screenwidth() - width - 24)
        y = max(0, self.root.winfo_screenheight() - height - 64)
        window.geometry(f'{width}x{height}+{x}+{y}')
        tk.Label(window, text=title, bg='#18212c', fg='#f3cb76', font=('Segoe UI', 12, 'bold')).pack(anchor='w', padx=12, pady=(10, 4))
        tk.Label(window, text=summary[:180] + ('…' if len(summary) > 180 else ''), bg='#18212c', fg='white',
                 wraplength=360, justify='left', font=('Segoe UI', 10)).pack(anchor='w', padx=12)
        buttons = tk.Frame(window, bg='#18212c')
        buttons.pack(side='bottom', fill='x', padx=12, pady=10)
        tk.Button(buttons, text='Open companion', command=lambda: (self.dismiss(), self.open_reply())).pack(side='left')
        tk.Button(buttons, text='Dismiss', command=self.dismiss).pack(side='right')
        if hasattr(ctypes, 'windll'):
            # WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW: keep keyboard focus in the game.
            from ctypes import wintypes
            window.update_idletasks()
            user32 = ctypes.windll.user32
            user32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
            user32.GetAncestor.restype = wintypes.HWND
            user32.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
            user32.GetWindowLongPtrW.restype = ctypes.c_ssize_t
            user32.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            hwnd = user32.GetAncestor(window.winfo_id(), 2)
            user32.SetWindowLongPtrW(hwnd, -20, user32.GetWindowLongPtrW(hwnd, -20) | 0x08000000 | 0x00000080)
        window.deiconify()
        self.timer = self.root.after(15000, self.dismiss)
