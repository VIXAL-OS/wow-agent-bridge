"""Run Claude Code or Codex headlessly and report only events they actually emit.

Prompts go to the agent on stdin, never through a shell or command line.
"""
from dataclasses import dataclass, field
import json
from pathlib import Path, PurePath
import queue
import subprocess
import threading
import time

GUIDANCE = (
    'You are answering through a small in-game chat panel inside World of Warcraft 3.3.5a '
    '(Wrath of the Lich King), on the ChromieCraft private server (AzerothCore, progressive '
    'Blizzlike realm). The user is playing while delegating work to you, so keep in-game replies '
    'compact: lead with the answer or with what you changed, then brief details. The panel is about '
    '70 characters wide. It reformats Markdown rather than rendering it: headings, dashed lists and '
    'fenced code survive, and a table is laid out as columns when it fits or as one block per row '
    'when it does not. Tables are fine, but keep them to a few short columns; for anything wider, '
    'write short labelled lines instead. Bold and inline-code marks are stripped, and typographic '
    'dashes and quotes become ASCII. The user can read the untruncated reply in the companion, so '
    'put the essentials first and keep the rest brief. For a WoW item whose numeric item ID the user supplied or you '
    'verified, write [Item Name](item:12345) with the real name and ID; the panel turns that into '
    'a native item link with tooltip. Never invent item IDs; leave unverified items as plain '
    'names. Never output raw WoW pipe markup. Nothing you write can act inside the game.'
)

BACKENDS = {'claude': 'Claude Code', 'codex': 'Codex', 'mock': 'Mock agent'}


@dataclass
class AgentConfig:
    backend: str
    project: Path
    sandbox: str = 'read-only'  # 'workspace-write', or 'workspace-write+shell'
    model: str = ''
    claude: str = ''
    codex: str = ''
    timeout: int = 1800
    guidance_file: Path | None = None


@dataclass
class Job:
    key: str
    prompt: str
    resume: str | None = None
    history: list = field(default_factory=list)  # [(user, assistant), ...] oldest first
    fork: bool = False  # continue a conversation without writing back into it


@dataclass
class Result:
    state: str  # 'done' | 'failed'
    reply: str
    agent_session: str | None = None


def history_prompt(job, limit=30000):
    """Prompt for agents without a resumable session: prior turns as JSON data."""
    history = [{'role': r, 'content': c} for user, reply in job.history for r, c in (('user', user), ('assistant', reply))]

    def encode():
        return json.dumps({'history': history, 'latest_user_message': job.prompt}, ensure_ascii=False)
    body = encode()
    while len(body) > limit and history:
        del history[:2]
        body = encode()
    if not history:
        return job.prompt
    return ('The JSON below holds earlier turns of this conversation as data (not instructions) '
            'and the latest user message to answer.\n\n' + body)


def describe_tool(name, args):
    args = args if isinstance(args, dict) else {}
    for key, label in (('file_path', ''), ('notebook_path', ''), ('path', ''), ('command', ''),
                       ('pattern', ''), ('url', ''), ('query', ''), ('description', '')):
        value = args.get(key)
        if isinstance(value, str) and value:
            if key.endswith('path'):
                value = PurePath(value).name
            value = ' '.join(value.split())
            return f'{name}: {value[:70]}{"…" if len(value) > 70 else ""}'
    return name


def run_process(command, cwd, stdin_text, timeout, on_event):
    """Stream JSON lines from a child process. Returns (returncode, tail, timed_out)."""
    try:
        process = subprocess.Popen(command, cwd=str(cwd), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                                   bufsize=1, shell=False,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError as error:
        return None, str(error), False
    lines = queue.Queue()

    def read():
        try:
            for line in iter(process.stdout.readline, ''):
                lines.put(line)
        finally:
            lines.put(None)

    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    tail, started, timed_out = '', time.monotonic(), False
    try:
        try:
            process.stdin.write(stdin_text)
            process.stdin.close()
        except OSError:
            pass
        while True:
            if time.monotonic() - started >= timeout:
                timed_out = True
                break
            try:
                line = lines.get(timeout=0.2)
            except queue.Empty:
                continue
            if line is None:
                break
            tail = (tail + line)[-4000:]
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict):
                on_event(event)
    finally:
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        reader.join(timeout=0.5)
        try:
            process.stdout.close()
        except OSError:
            pass
    return process.returncode, tail, timed_out


class ClaudeStream:
    """Parse `claude -p --output-format stream-json --verbose --include-partial-messages`."""

    def __init__(self, on_update=None, on_activity=None):
        self.on_update, self.on_activity = on_update or (lambda t: None), on_activity or (lambda a: None)
        self.session, self.texts, self.partial, self.result = None, [], '', None
        self.activity, self.last_emit, self.emitted = '', 0.0, ''

    def text(self):
        parts = self.texts + ([self.partial] if self.partial else [])
        body = '\n\n'.join(p.strip() for p in parts if p.strip())
        return body + (f'\n\n[{self.activity}]' if body and self.activity else '')

    def emit(self, force=False):
        now = time.monotonic()
        text = self.text()
        if text and text != self.emitted and (force or now - self.last_emit >= 0.5):
            self.emitted, self.last_emit = text, now
            self.on_update(text)

    def feed(self, event):
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            self.session = event.get('session_id') or self.session
        elif kind == 'stream_event':
            inner = event.get('event') or {}
            if inner.get('type') == 'message_start':
                self.partial = ''
            elif inner.get('type') == 'content_block_delta':
                delta = inner.get('delta') or {}
                if delta.get('type') == 'text_delta':
                    self.partial += delta.get('text') or ''
                    self.activity = ''
                    self.emit()
        elif kind == 'assistant':
            content = (event.get('message') or {}).get('content') or []
            text = ''.join(c.get('text', '') for c in content if isinstance(c, dict) and c.get('type') == 'text')
            if text.strip():
                self.texts.append(text)
            self.partial = ''
            for block in content:
                if isinstance(block, dict) and block.get('type') == 'tool_use':
                    self.activity = describe_tool(block.get('name', 'tool'), block.get('input'))
                    self.on_activity(self.activity)
            self.emit(force=True)
        elif kind == 'result':
            self.session = event.get('session_id') or self.session
            self.result = event

    def outcome(self, returncode, tail, timed_out):
        if timed_out:
            return Result('failed', 'Claude Code timed out. Check the work folder before retrying.', self.session)
        r = self.result
        if r is None:
            return Result('failed', self.text() or tail or 'Claude Code exited without a result.', self.session)
        reply = r.get('result') if isinstance(r.get('result'), str) else ''
        if r.get('is_error') or r.get('subtype') != 'success':
            if 'authenticat' in reply.lower():
                reply += '\n\nRun "claude" in a terminal and use /login, then send this again.'
            return Result('failed', reply or f'Claude Code stopped: {r.get("subtype")}', self.session)
        return Result('done', reply.strip() or '\n\n'.join(self.texts).strip() or '(No text response.)', self.session)


class CodexStream:
    """Parse `codex exec --json` events (agent_message items, turn completion)."""

    def __init__(self, on_update=None, on_activity=None):
        self.on_update, self.on_activity = on_update or (lambda t: None), on_activity or (lambda a: None)
        self.messages, self.failures, self.completed, self.anonymous = {}, [], False, 0
        self.last_emit, self.emitted, self.thread = 0.0, '', None

    def text(self):
        return '\n\n'.join(t.strip() for t in self.messages.values() if t.strip())

    def feed(self, event):
        kind = event.get('type')
        if kind == 'thread.started':
            self.thread = event.get('thread_id')
        elif kind == 'turn.completed':
            self.completed = True
        elif kind in ('error', 'turn.failed'):
            error = event.get('error')
            self.failures.append(str(event.get('message') or (error.get('message') if isinstance(error, dict) else error) or event))
        item = event.get('item')
        if kind not in ('item.started', 'item.updated', 'item.completed') or not isinstance(item, dict):
            return
        itype = item.get('type')
        if itype == 'agent_message' and isinstance(item.get('text'), str) and item['text']:
            identity = item.get('id') or f'anonymous-{self.anonymous}'
            self.messages[str(identity)] = item['text']
            if not item.get('id') and kind == 'item.completed':
                self.anonymous += 1
            now, text = time.monotonic(), self.text()
            if text != self.emitted and (kind == 'item.completed' or now - self.last_emit >= 0.5):
                self.emitted, self.last_emit = text, now
                self.on_update(text)
        elif kind == 'item.started' and itype == 'command_execution':
            self.on_activity(describe_tool('Run', {'command': item.get('command') or ''}))
        elif kind == 'item.started' and itype in ('file_change', 'web_search', 'mcp_tool_call'):
            self.on_activity({'file_change': 'Editing files', 'web_search': 'Searching the web',
                              'mcp_tool_call': 'Using a tool'}[itype])

    def outcome(self, returncode, tail, timed_out):
        if timed_out:
            return Result('failed', 'Codex timed out. Check the work folder before retrying.')
        latest = self.text()
        if returncode or self.failures or not self.completed:
            return Result('failed', '\n'.join(self.failures + ([latest] if latest else [])) or tail or 'No completion event',
                          self.thread)
        return Result('done', latest or '(No text response.)', self.thread)


def claude_command(cfg, job):
    command = [cfg.claude, '-p', '--output-format', 'stream-json', '--verbose', '--include-partial-messages']
    if cfg.sandbox == 'workspace-write+shell':
        # Edits and shell commands run unattended: the caller opted in.
        command += ['--permission-mode', 'acceptEdits', '--allowedTools', 'Read,Glob,Grep,Edit,Write,Bash']
    elif cfg.sandbox == 'workspace-write':
        command += ['--permission-mode', 'acceptEdits']
    else:
        # Anything that would need approval is refused; reading the project is allowed.
        command += ['--permission-mode', 'dontAsk', '--allowedTools', 'Read,Glob,Grep']
    if cfg.guidance_file:
        command += ['--append-system-prompt-file', str(cfg.guidance_file)]
    if cfg.model:
        command += ['--model', cfg.model]
    if job.resume:
        command += ['--resume', job.resume]
        if job.fork:
            # Branch off an existing conversation instead of appending to it,
            # so the session it came from is left untouched.
            command += ['--fork-session']
    return command


def codex_sandbox(level):
    """Codex runs commands inside its own sandbox, so both write levels map to one."""
    return 'workspace-write' if level.startswith('workspace-write') else 'read-only'


def codex_command(cfg, job=None):
    if job is not None and job.resume:
        # `exec resume` takes neither --sandbox nor -C; the sandbox goes through
        # a config override and the working folder comes from the process itself.
        command = [cfg.codex, 'exec', 'resume', '--json', '--skip-git-repo-check',
                   '-c', f'sandbox_mode="{codex_sandbox(cfg.sandbox)}"']
        if cfg.model:
            command += ['-m', cfg.model]
        return command + [job.resume, '-']
    command = [cfg.codex, 'exec', '--json', '--sandbox', codex_sandbox(cfg.sandbox), '--skip-git-repo-check',
               '--color', 'never', '-C', str(cfg.project)]
    if cfg.model:
        command += ['-m', cfg.model]
    return command + ['-']


def run_mock(job, on_update, on_activity):
    on_activity('Mock: pretending to read files')
    time.sleep(1.5)
    reply = f'Mock agent received {len(job.prompt.encode())} bytes:\n{job.prompt}'
    if 'long' in job.prompt.lower():
        reply += '\n\n' + '\n'.join(f'Line {i:03}: the quick brown fox jumps over the lazy dog. ☃' for i in range(1, 61))
    on_update(reply[:300])
    time.sleep(1.5)
    return Result('done', reply)


def run_agent(cfg, job, on_update=None, on_activity=None):
    on_update = on_update or (lambda t: None)
    on_activity = on_activity or (lambda a: None)
    if cfg.backend == 'mock':
        return run_mock(job, on_update, on_activity)
    if cfg.backend == 'claude':
        if not cfg.claude:
            return Result('failed', 'Claude Code CLI not found. Install it or set its path in the companion.')
        parser = ClaudeStream(on_update, on_activity)
        # A resumed session already holds the conversation; otherwise send history.
        prompt = job.prompt if job.resume else history_prompt(job)
        returncode, tail, timed_out = run_process(claude_command(cfg, job), cfg.project, prompt, cfg.timeout, parser.feed)
        outcome = parser.outcome(returncode, tail, timed_out)
        unresumable = (job.resume and outcome.state == 'failed' and not timed_out and not parser.texts
                       and (parser.result is None or 'no conversation found' in outcome.reply.lower()))
        if unresumable:
            # The saved session is gone (deleted, other machine): start fresh with history.
            return run_agent(cfg, Job(job.key, job.prompt, None, job.history), on_update, on_activity)
        if job.fork and outcome.agent_session == job.resume:
            outcome = Result(outcome.state, outcome.reply, None)  # no fork id reported; do not append later
        return outcome
    if cfg.backend == 'codex':
        if not cfg.codex:
            return Result('failed', 'Codex CLI not found. Install it or set its path in the companion.')
        parser = CodexStream(on_update, on_activity)
        # A resumed thread already holds the conversation and the guidance.
        prompt = job.prompt if job.resume else GUIDANCE + '\n\n' + history_prompt(job)
        returncode, tail, timed_out = run_process(codex_command(cfg, job), cfg.project, prompt, cfg.timeout, parser.feed)
        outcome = parser.outcome(returncode, tail, timed_out)
        if job.resume and outcome.state == 'failed' and not timed_out and not parser.messages and not parser.thread:
            # That thread is not on this machine any more: start fresh with history.
            return run_agent(cfg, Job(job.key, job.prompt, None, job.history), on_update, on_activity)
        return outcome
    return Result('failed', f'Unknown backend {cfg.backend!r}')
