"""Desktop entry point: asks for a work folder once, then starts the companion."""
from pathlib import Path
import json
import sys
import tkinter as tk
from tkinter import filedialog, messagebox

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE))
STATE = BASE / 'state'


def launch():
    settings_path = STATE / 'settings.json'
    try:
        settings = json.loads(settings_path.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        settings = {}
    if not settings.get('project') or not Path(settings['project']).is_dir():
        root = tk.Tk(); root.withdraw()
        project = filedialog.askdirectory(parent=root, title='Choose the folder the agent should work in')
        root.destroy()
        if not project:
            return
        settings['project'] = project
        STATE.mkdir(exist_ok=True)
        settings_path.write_text(json.dumps(settings, indent=2), encoding='utf-8')
    from companion.app import main
    main(['--state', str(STATE)] + [a for a in sys.argv[1:] if a in ('--start-capture', '--minimized')])


if __name__ == '__main__':
    try:
        launch()
    except Exception as exc:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror('Agent Bridge could not start', str(exc), parent=root)
        root.destroy()
