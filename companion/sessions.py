"""Find existing agent conversations so an in-game thread can continue one.

Claude Code keeps one JSONL transcript per session under
~/.claude/projects/<escaped working folder>/<session id>.jsonl; Codex keeps
rollout files under ~/.codex/sessions/<year>/<month>/<day>/. Everything read
here is display data: a folder, a timestamp and the opening message.
"""
from dataclasses import dataclass
import json
from pathlib import Path

PROJECTS = Path.home() / '.claude' / 'projects'
ROLLOUTS = Path.home() / '.codex' / 'sessions'


@dataclass(frozen=True)
class Session:
    id: str
    cwd: str
    modified: float
    summary: str

    def label(self):
        folder = Path(self.cwd).name or self.cwd
        return f'{folder}  ·  {self.summary or "(no opening message)"}'


INJECTED = ('# agents.md instructions', '<instructions>', 'caveat:', '# claude.md')


def _is_injected(text):
    """Both CLIs prepend context (rules files, caveats) as user messages."""
    head = text.lstrip()[:200].lower()
    return not head or head.startswith('<') or any(marker in head for marker in INJECTED)


def _text_of(entry):
    content = entry.get('content')
    if isinstance(content, str):
        return content
    message = entry.get('message')
    if isinstance(message, dict):
        body = message.get('content')
        if isinstance(body, str):
            return body
        if isinstance(body, list):
            return ' '.join(part.get('text', '') for part in body
                            if isinstance(part, dict) and part.get('type') == 'text')
    return ''


def read_session(path, probe_lines=60):
    """First working folder and opening message of a transcript."""
    cwd, summary = '', ''
    try:
        with Path(path).open(encoding='utf-8', errors='replace') as file:
            for index, line in enumerate(file):
                if index >= probe_lines or (cwd and summary):
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(entry, dict):
                    continue
                cwd = cwd or str(entry.get('cwd') or '')
                if not summary and entry.get('type') in ('queue-operation', 'user'):
                    candidate = _text_of(entry)
                    summary = '' if _is_injected(candidate) else candidate
    except OSError:
        pass
    return cwd, ' '.join(summary.split())[:110]


def read_rollout(path, probe_lines=80):
    """Session id, working folder and first real prompt of a Codex rollout."""
    identity, cwd, summary = '', '', ''
    try:
        with Path(path).open(encoding='utf-8', errors='replace') as file:
            for index, line in enumerate(file):
                if index >= probe_lines or (identity and summary):
                    break
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                payload = entry.get('payload') if isinstance(entry, dict) else None
                if not isinstance(payload, dict):
                    continue
                if entry.get('type') == 'session_meta':
                    identity = str(payload.get('session_id') or '')
                    cwd = str(payload.get('cwd') or '')
                elif not summary and payload.get('role') == 'user':
                    for part in payload.get('content') or []:
                        text = part.get('text', '') if isinstance(part, dict) else ''
                        if not _is_injected(text):
                            summary = text
                            break
    except OSError:
        pass
    return identity, cwd, ' '.join(summary.split())[:110]


def list_sessions(limit=25, root=None, min_bytes=2048, backend='claude'):
    """Recent conversations, newest first. Transcripts too small to hold a real
    exchange are skipped."""
    codex = backend == 'codex'
    root = Path(root) if root else (ROLLOUTS if codex else PROJECTS)
    if not root.is_dir():
        return []
    pattern = '**/rollout-*.jsonl' if codex else '*/*.jsonl'
    files = [p for p in root.glob(pattern) if p.is_file() and p.stat().st_size >= min_bytes]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    sessions = []
    for path in files[:limit]:
        if codex:
            identity, cwd, summary = read_rollout(path)
            identity = identity or path.stem[-36:]
        else:
            identity, (cwd, summary) = path.stem, read_session(path)
        sessions.append(Session(identity, cwd or str(path.parent), path.stat().st_mtime, summary))
    return sessions
