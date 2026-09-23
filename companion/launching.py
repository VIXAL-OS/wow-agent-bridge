"""Find native agent CLIs and the models they offer on this machine."""
import json
import os
from pathlib import Path
import re
import shutil

CLAUDE_ALIASES = ['opus', 'sonnet', 'haiku']  # each means "the latest of that family"


def claude_models(config=Path.home() / '.claude.json'):
    """Model IDs Claude Code has seen here, newest version first, then the aliases."""
    try:
        text = Path(config).read_text(encoding='utf-8', errors='replace')
    except OSError:
        text = ''
    found = {name.rstrip('-') for name in re.findall(r'claude-(?:opus|sonnet|haiku|fable)-\d[\w-]*', text)}

    def version(name):
        return tuple(int(n) for n in re.findall(r'\d+', name)[:2])
    return sorted(found, key=lambda name: (version(name), name), reverse=True) + CLAUDE_ALIASES


def codex_models(cache=Path.home() / '.codex' / 'models_cache.json'):
    """Models Codex lists for this account, in its own order; hidden ones are skipped."""
    try:
        data = json.loads(Path(cache).read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return []
    return [m['slug'] for m in data.get('models', [])
            if isinstance(m, dict) and m.get('slug') and m.get('visibility', 'list') == 'list']


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
