"""Find native agent CLIs even when Explorer's PATH differs from a terminal's."""
import os
from pathlib import Path
import shutil


def _first(paths):
    for path in paths:
        if path and Path(path).is_file():
            return str(Path(path).resolve())
    return ''


def _npm_root():
    appdata = os.environ.get('APPDATA')
    return Path(appdata) / 'npm' if appdata else None


def find_claude(remembered=''):
    """Prefer a real executable: npm's claude.cmd would route arguments through cmd.exe."""
    npm = _npm_root()
    home = Path.home()
    return _first([
        remembered,
        shutil.which('claude.exe'),
        home / '.local' / 'bin' / 'claude.exe',
        npm and npm / 'node_modules' / '@anthropic-ai' / 'claude-code' / 'bin' / 'claude.exe',
        shutil.which('claude.cmd') and Path(shutil.which('claude.cmd')).parent / 'node_modules' / '@anthropic-ai'
        / 'claude-code' / 'bin' / 'claude.exe',
    ])


def find_codex(remembered=''):
    found = _first([remembered, shutil.which('codex.exe')])
    if found:
        return found
    candidates = []
    local = os.environ.get('LOCALAPPDATA')
    if local:
        candidates += (Path(local) / 'OpenAI' / 'Codex' / 'bin').glob('*/codex.exe')
    npm = _npm_root()
    if npm:
        candidates += (npm / 'node_modules' / '@openai' / 'codex').glob('**/codex.exe')
    candidates = [p for p in candidates if p.is_file()]
    if candidates:
        return str(max(candidates, key=lambda p: p.stat().st_mtime).resolve())
    # A cmd shim is acceptable here: Codex arguments are fixed flags and paths.
    return _first([shutil.which('codex.cmd'), shutil.which('codex')])
