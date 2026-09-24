"""Agent Bridge companion: strip capture, durable inbox, agent runner, font publisher.

Only the strip region is decoded and captures are never saved. Nothing is sent
to the game process; replies travel through addon font files.
"""
import argparse
from contextlib import closing
import ctypes
from dataclasses import dataclass
import json
import os
from pathlib import Path
import queue
import re
import sqlite3
import threading
import time
import tkinter as tk
from tkinter import filedialog, scrolledtext, ttk

from .agents import BACKENDS, GUIDANCE, AgentConfig, Job, run_agent
from .browser import BrowserRequests
from .capture import grab, grab_window
from .launching import claude_models, codex_models, find_claude, find_codex
from .native import NativeBridge, validate_addon
from .notifications import ReplyBanner
from .protocol import Assembler, decode_image, frame_kind, parse_control, parse_envelope, parse_url_frame
from .sessions import list_sessions
from .wow import EpochKeeper, StripLocator, game_dir_for

LABELS = {'queued': 'Queued', 'working': 'Working', 'streaming': 'Writing', 'done': 'Done',
          'failed': 'Failed', 'interrupted': 'Interrupted'}


WORKERS, MAX_OPEN = 3, 16  # agents running at once; prompts accepted but not finished


def conversation_of(key):
    return key.split(':', 1)[0][:8]


def chat_of(key, fields):
    """The chat a prompt belongs to, as 8 hex digits.

    The addon names it in the prompt's envelope. Older addons put the conversation
    number at the start of the session instead, and it is the same number, so a
    conversation from before chats existed carries on as its chat.
    """
    value = (fields.get('chat') or [''])[0]
    if value.isdigit() and int(value) < 2 ** 32:
        return f'{int(value):08x}'
    return conversation_of(key)


@dataclass
class Request:
    key: str
    prompt: str
    chat: str
    name: str = ''
    context: str = ''
    agent: str = 'claude'
    model: str = ''  # '' = the CLI's own default


# Passed as one argv item, never through a shell; this also keeps it from
# reading as a flag.
MODEL_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._:/\[\]-]{0,63}')


def resolve_agent(fields, last_backend, default_backend, default_models):
    """Which agent and model answer a prompt, plus notes on anything refused.

    A chat's own choice (sent by the addon) wins. Otherwise a chat keeps the agent
    it last used, since only that agent can resume its session; a new chat takes
    the companion's selection. The model is the chat's own, or the companion's
    default for that agent.
    """
    notes = []
    agent = (fields.get('agent') or [''])[0]
    if agent and agent not in BACKENDS:
        notes.append(f'unknown agent {agent!r} ignored')
        agent = ''
    agent = agent or last_backend or default_backend
    model = (fields.get('model') or [''])[0]
    if model and not MODEL_NAME.fullmatch(model):
        notes.append(f'model name {model!r} refused')
        model = ''
    return agent, model or default_models.get(agent, ''), notes


class Scheduler:
    """Several agents at once, in arrival order, but one at a time per chat: a
    chat's next turn resumes the session its previous turn produced."""

    def __init__(self):
        self.cond = threading.Condition()
        self.waiting, self.running = [], {}  # running: key -> chat

    def put(self, request):
        with self.cond:
            if len(self.waiting) + len(self.running) >= MAX_OPEN:
                return False
            self.waiting.append(request)
            self.cond.notify_all()
            return True

    def take(self):
        with self.cond:
            while True:
                busy = set(self.running.values())
                for index, request in enumerate(self.waiting):
                    if request.chat not in busy:
                        del self.waiting[index]
                        self.running[request.key] = request.chat
                        return request
                self.cond.wait()

    def done(self, key):
        with self.cond:
            self.running.pop(key, None)
            self.cond.notify_all()

    def open(self):
        with self.cond:
            return len(self.waiting) + len(self.running)

    def describe(self, key):
        """What a queued prompt is waiting for, as shown under it in game."""
        with self.cond:
            busy = set(self.running.values())
            for index, request in enumerate(self.waiting):
                if request.key == key:
                    if request.chat in busy:
                        return 'Queued behind the previous prompt in this chat.'
                    ahead = sum(1 for other in self.waiting[:index] if other.chat not in busy)
                    return f'Queued: {len(self.running)} running, {ahead} ahead of this one.'
            return 'Starting.'


class Inbox:
    def __init__(self, path):
        self.path = path
        self.db = sqlite3.connect(path)
        self.db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, prompt TEXT, state TEXT, reply TEXT, '
                        'backend TEXT, agent_session TEXT, created REAL, chat TEXT, meta TEXT)')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(jobs)')}
        if 'chat' not in columns:
            # Inboxes from before chats: each conversation becomes its chat.
            self.db.execute('ALTER TABLE jobs ADD COLUMN chat TEXT')
            self.db.execute('UPDATE jobs SET chat=substr(id, 1, 8)')
        if 'meta' not in columns:
            self.db.execute('ALTER TABLE jobs ADD COLUMN meta TEXT')
        # A crash may have happened after the agent ran. Never replay automatically.
        self.db.execute("UPDATE jobs SET state='interrupted' WHERE state IN ('queued','working','streaming')")
        self.db.commit()

    def add(self, request):
        meta = json.dumps({'name': request.name, 'context': request.context, 'model': request.model},
                          ensure_ascii=False)
        with self.db:
            cursor = self.db.execute('INSERT OR IGNORE INTO jobs (id, prompt, state, reply, backend, agent_session, '
                                     'created, chat, meta) VALUES (?,?,?,?,?,?,?,?,?)',
                                     (request.key, request.prompt, 'queued', '', request.agent, None, time.time(),
                                      request.chat, meta))
        return cursor.rowcount == 1

    def last_backend(self, chat):
        row = self.db.execute('SELECT backend FROM jobs WHERE chat=? ORDER BY rowid DESC LIMIT 1', (chat,)).fetchone()
        return row[0] if row else None

    def update(self, key, state, reply, agent_session=None):
        with self.db:
            self.db.execute('UPDATE jobs SET state=?, reply=?, agent_session=COALESCE(?, agent_session) WHERE id=?',
                            (state, reply, agent_session, key))

    def get(self, key):
        row = self.db.execute('SELECT id,prompt,state,reply,backend,meta FROM jobs WHERE id=?', (key,)).fetchone()
        if not row:
            return None
        try:
            meta = json.loads(row[5] or '{}')
        except ValueError:
            meta = {}
        return {'id': row[0], 'prompt': row[1], 'state': row[2], 'reply': row[3], 'agent': row[4],
                'model': meta.get('model', '') if isinstance(meta, dict) else ''}

    def recent(self, n=12):
        return self.db.execute('SELECT id,prompt,state,reply FROM jobs ORDER BY rowid DESC LIMIT ?', (n,)).fetchall()[::-1]


class Context:
    """Worker-side view of earlier turns. Recent results are kept in memory because
    the next job can start before the UI thread has committed the previous one."""

    def __init__(self, path):
        self.path, self.recent, self.lock = path, {}, threading.Lock()

    def remember(self, key, backend, result):
        with self.lock:
            self.recent[key] = (result.state, result.reply, backend, result.agent_session)
            while len(self.recent) > 32:
                self.recent.pop(next(iter(self.recent)))

    def job(self, request, backend):
        """Earlier turns of this chat, and the session to resume if the last one
        ran on the same agent."""
        # closing(): a connection's own `with` commits but leaves it open.
        with closing(sqlite3.connect(self.path)) as db:
            rows = db.execute('SELECT id,prompt,state,reply,backend,agent_session FROM jobs WHERE chat=? '
                              'AND rowid < (SELECT rowid FROM jobs WHERE id=?) ORDER BY rowid DESC LIMIT 8',
                              (request.chat, request.key)).fetchall()
        with self.lock:
            recent = dict(self.recent)
        turns = []
        for old, user, state, reply, old_backend, session in reversed(rows):
            if old in recent:
                state, reply, old_backend, session = recent[old]
            if state == 'done':
                turns.append((user, reply[:8000], old_backend, session))
        resume = None
        if turns and turns[-1][2] == backend and turns[-1][3]:
            resume = turns[-1][3]
        return Job(request.key, request.prompt, resume, [(u, r) for u, r, _, _ in turns], context=request.context)


class App:
    def __init__(self, root, args):
        self.root, self.args = root, args
        args.state.mkdir(parents=True, exist_ok=True)
        self.settings_path = args.state / 'settings.json'
        try:
            self.settings = json.loads(self.settings_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            self.settings = {}
        for name in ('backend', 'project', 'addon', 'sandbox', 'claude', 'codex'):
            value = getattr(args, name, None)
            if value:
                self.settings[name] = str(value)
        self.settings.setdefault('backend', 'claude' if find_claude(self.settings.get('claude', '')) else 'codex')
        # A default model per agent: a Claude model name means nothing to Codex.
        # Earlier versions kept one model, which belonged to the selected agent.
        models = self.settings.get('models')
        if not isinstance(models, dict):
            models = {self.settings['backend']: self.settings.get('model', '')}
        if getattr(args, 'model', None):
            models[self.settings['backend']] = args.model
        self.settings['models'] = models
        self.settings.pop('model', None)
        self.guidance = args.state / 'guidance.txt'
        self.guidance.write_text(GUIDANCE, encoding='utf-8')
        self.inbox = Inbox(args.state / 'inbox.sqlite3')
        self.assembler = Assembler()
        self.url_assembler = Assembler(parser=parse_url_frame)
        self.browser = BrowserRequests(self.inbox.db)
        self.scheduler, self.context, self.events = Scheduler(), Context(self.inbox.path), queue.Queue()
        self.activity, self.names, self.capturing, self.closed = {}, {}, False, False
        # (session id, agent, branch): continues on the next prompt that agent answers.
        resume = getattr(args, 'resume_session', None)
        self.pending_resume = (resume, self.settings['backend'], True) if resume else None
        self.picker_lock = threading.Lock()
        self.frames, self.retry_at, self.last_status = 0, 0.0, None
        self.last_frame_at = self.last_attempt = self.window_at = -1e9
        self.capture_mode = 'screen'
        self.native = self.epoch = self.locator = None
        self.banner = ReplyBanner(root, self.open_window)
        self.build()
        self.attach_addon(self.settings.get('addon'))
        for key, prompt, state, reply in self.inbox.recent():
            self.write(f'[{state}] {key}\nYou: {prompt}\n{reply}\n')
        self.write('Only the strip region is decoded; screenshots are never saved.')
        self.pending_publish, self.publish_wanted = None, threading.Event()
        for _ in range(WORKERS):
            threading.Thread(target=self.worker, daemon=True).start()
        threading.Thread(target=self.publisher, daemon=True).start()
        root.protocol('WM_DELETE_WINDOW', self.close)
        if args.start_capture or self.settings.get('capture', True):
            self.toggle_capture(True)
        if args.minimized:
            root.after(100, root.iconify)
        root.after(70, self.tick)

    # ---- settings and UI ------------------------------------------------
    def save(self, **changes):
        self.settings.update(changes)
        temporary = self.settings_path.with_suffix('.next.json')
        temporary.write_text(json.dumps(self.settings, indent=2), encoding='utf-8')
        temporary.replace(self.settings_path)

    def build(self):
        root, s = self.root, self.settings
        root.title('Agent Bridge companion')
        root.minsize(720, 520)
        pad = {'padx': 12, 'pady': 3}
        self.job_status = tk.StringVar(value='Ready for a prompt from the game')
        ttk.Label(root, textvariable=self.job_status, font=('Segoe UI', 14, 'bold')).pack(anchor='w', padx=12, pady=(10, 2))
        self.capture_status = tk.StringVar(value='Capture paused')
        ttk.Label(root, textvariable=self.capture_status).pack(anchor='w', **pad)

        row = ttk.Frame(root); row.pack(fill='x', **pad)
        # The agent a new chat starts with; a chat keeps its own after that, and
        # can pick one in game with /ab agent.
        ttk.Label(row, text='New chats use:').pack(side='left')
        self.backend = tk.StringVar(value=s.get('backend', 'claude'))
        for key, label in BACKENDS.items():
            ttk.Radiobutton(row, text=label, value=key, variable=self.backend,
                            command=self.on_backend).pack(side='left', padx=4)
        ttk.Label(row, text='   Access:').pack(side='left')
        self.sandbox = tk.StringVar(value=s.get('sandbox', 'read-only'))
        box = ttk.Combobox(row, textvariable=self.sandbox, values=('read-only', 'workspace-write', 'workspace-write+shell'), width=20, state='readonly')
        box.pack(side='left', padx=4)
        box.bind('<<ComboboxSelected>>', lambda _: self.save(sandbox=self.sandbox.get()))
        ttk.Label(row, text='   Model:').pack(side='left')
        # The default model of the agent selected above; a chat can set its own
        # with /ab model. The box follows the agent buttons.
        self.model_backend = self.backend.get()
        self.model = tk.StringVar(value=self.default_model(self.model_backend))
        # Editable: the lists are shortcuts, any name the CLI accepts works.
        self.model_box = ttk.Combobox(row, textvariable=self.model, width=16, values=self.model_choices())
        self.model_box.configure(postcommand=lambda: self.model_box.configure(values=self.model_choices()))
        self.model_box.pack(side='left', padx=4)
        self.model_box.bind('<<ComboboxSelected>>', lambda _: self.save_model())
        self.model_box.bind('<FocusOut>', lambda _: self.save_model())
        ttk.Label(row, text='(blank = default)').pack(side='left')

        row = ttk.Frame(root); row.pack(fill='x', **pad)
        self.project_label = tk.StringVar()
        ttk.Label(row, textvariable=self.project_label).pack(side='left')
        ttk.Button(row, text='Change work folder…', command=self.choose_project).pack(side='right')
        self.refresh_project()

        row = ttk.Frame(root); row.pack(fill='x', **pad)
        ttk.Button(row, text='Continue a conversation…', command=self.choose_session).pack(side='left')
        ttk.Button(row, text='Saved replies', command=self.open_replies).pack(side='right')
        self.conversation = tk.StringVar(value='In-game conversation: continuing the last one')
        ttk.Label(row, textvariable=self.conversation, wraplength=560, justify='left').pack(side='left', padx=8)

        row = ttk.Frame(root); row.pack(fill='x', **pad)
        self.capture_button = ttk.Button(row, text='Start capture', command=self.toggle_capture)
        self.capture_button.pack(side='left')
        self.manual = tk.BooleanVar(value=s.get('use_manual_crop', False))
        ttk.Checkbutton(row, text='Manual crop (x,y,w,h):', variable=self.manual,
                        command=lambda: self.save(use_manual_crop=self.manual.get())).pack(side='left', padx=(12, 4))
        self.crop = tk.StringVar(value=s.get('manual_crop', '600,0,720,22'))
        ttk.Entry(row, textvariable=self.crop, width=18).pack(side='left')
        self.web = tk.BooleanVar(value=s.get('web', True))
        ttk.Checkbutton(row, text='Web search', variable=self.web,
                        command=lambda: self.save(web=self.web.get())).pack(side='right', padx=(8, 0))
        self.notify_enabled = tk.BooleanVar(value=s.get('notifications', True))
        ttk.Checkbutton(row, text='Desktop banner', variable=self.notify_enabled,
                        command=lambda: self.save(notifications=self.notify_enabled.get())).pack(side='right')

        # Fixed width: agents reply with tables, diffs and code.
        self.log = scrolledtext.ScrolledText(root, width=100, height=24, state='disabled', wrap='word',
                                             font=('Consolas', 10))
        self.log.pack(fill='both', expand=True, padx=12, pady=8)

    def write(self, text):
        self.log.configure(state='normal')
        self.log.insert('end', text + '\n')
        self.log.see('end')
        self.log.configure(state='disabled')

    def save_reply(self, key, state, prompt, reply):
        """Keep the full reply as a file: the panel shows a reformatted view."""
        folder = self.args.state / 'replies'
        folder.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime('%Y%m%d-%H%M%S')
        path = folder / f'{stamp}-{key.replace(":", "-")}.md'
        body = f'# {state}: {key}\n\n## Prompt\n\n{prompt}\n\n## Reply\n\n{reply}\n'
        path.write_text(body, encoding='utf-8')
        return path

    def open_replies(self):
        folder = self.args.state / 'replies'
        folder.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(folder)
        except (OSError, AttributeError):
            self.write(f'Saved replies are in {folder}')

    def open_window(self):
        self.root.deiconify(); self.root.lift()

    def model_choices(self):
        # Read fresh each time, so a model added to either CLI shows up here.
        backend = self.backend.get()
        found = claude_models() if backend == 'claude' else codex_models() if backend == 'codex' else []
        return tuple([''] + found)

    def default_model(self, backend):
        return (self.settings.get('models') or {}).get(backend, '')

    def save_model(self):
        models = dict(self.settings.get('models') or {})
        models[self.model_backend] = self.model.get().strip()
        self.save(models=models)

    def on_backend(self):
        # Keep what was typed for the previous agent, then show this one's.
        self.save_model()
        self.model_backend = self.backend.get()
        self.model.set(self.default_model(self.model_backend))
        self.save(backend=self.backend.get())
        self.model_box.configure(values=self.model_choices())

    def project(self):
        return Path(self.settings.get('project') or Path.home())

    def refresh_project(self):
        self.project_label.set(f'Work folder: {self.project()}')

    def choose_project(self):
        if self.scheduler.open():
            self.write('Wait for running jobs to finish before changing work folders.')
            return
        chosen = filedialog.askdirectory(parent=self.root, title='Folder the agent works in', initialdir=str(self.project()))
        if chosen:
            self.save(project=str(Path(chosen).resolve()))
            self.refresh_project()

    def choose_session(self):
        """Attach the next in-game prompt to an existing Claude Code conversation."""
        backend = self.backend.get()
        if backend not in ('claude', 'codex'):
            self.write('Pick Claude Code or Codex first; the mock agent has no conversations.')
            return
        found = list_sessions(backend=backend)
        if not found:
            self.write(f'No {BACKENDS[backend]} conversations found on this machine.')
            return
        window = tk.Toplevel(self.root)
        window.title(f'Continue a {BACKENDS[backend]} conversation in game')
        window.geometry('840x430')
        ttk.Label(window, text=f'The next prompt {BACKENDS[backend]} answers continues the conversation you '
                               f'pick. Send it from a new chat in game, or one set to {BACKENDS[backend]}.',
                  justify='left').pack(anchor='w', padx=12, pady=8)
        branch = tk.BooleanVar(value=True)
        if backend == 'claude':
            ttk.Checkbutton(window, variable=branch, text='Branch off, leaving the original untouched. Clear this '
                            'to add in-game turns to that same conversation instead.').pack(anchor='w', padx=12)
        else:
            ttk.Label(window, text='Codex cannot branch: in-game turns are added to that thread.').pack(anchor='w', padx=12)
        listbox = tk.Listbox(window, activestyle='dotbox')
        listbox.pack(fill='both', expand=True, padx=12)
        for session in found:
            stamp = time.strftime('%d %b %H:%M', time.localtime(session.modified))
            listbox.insert('end', f'{stamp}   {session.label()}')
        listbox.selection_set(0)

        def use():
            selection = listbox.curselection()
            if not selection:
                return
            session = found[selection[0]]
            with self.picker_lock:
                self.pending_resume = (session.id, backend, bool(branch.get()) and backend == 'claude')
            folder = Path(session.cwd)
            if folder.is_dir() and folder != self.project():
                # --resume only finds a session from its own working folder.
                self.save(project=str(folder))
                self.refresh_project()
                self.write(f'Work folder switched to {folder} to match that conversation.')
            self.conversation.set(f'Next prompt continues: {session.summary[:60] or session.id}')
            self.write(f'Next in-game prompt continues conversation {session.id}.')
            window.destroy()
        buttons = ttk.Frame(window)
        buttons.pack(fill='x', padx=12, pady=10)
        ttk.Button(buttons, text='Use for next prompt', command=use).pack(side='left')
        ttk.Button(buttons, text='Cancel', command=window.destroy).pack(side='right')
        listbox.bind('<Double-Button-1>', lambda _: use())

    def attach_addon(self, addon):
        if not addon:
            self.write('No addon path set. Run tools.install_addon, or pass --addon.')
            return
        try:
            addon = validate_addon(addon)
            self.native = NativeBridge(addon, agent_label=lambda: BACKENDS.get(self.backend.get(), 'The agent'))
            self.epoch = EpochKeeper(addon)
            self.locator = StripLocator(game_dir_for(addon))
            self.write(f'Addon: {addon}')
        except (OSError, ValueError) as exc:
            self.write(f'Reply channel unavailable: {exc}')

    def toggle_capture(self, value=None):
        self.capturing = (not self.capturing) if value is None else value
        self.capture_button.configure(text='Pause capture' if self.capturing else 'Start capture')
        self.capture_status.set('Looking for the strip…' if self.capturing else 'Capture paused; running jobs continue')
        self.save(capture=self.capturing)

    # ---- agent worker ---------------------------------------------------
    def agent_config(self, request):
        """The agent and model were settled when the prompt arrived (see resolve_agent)."""
        return AgentConfig(backend=request.agent, project=self.project(), sandbox=self.sandbox.get(),
                           model=request.model, claude=find_claude(self.settings.get('claude', '')),
                           codex=find_codex(self.settings.get('codex', '')), timeout=self.args.timeout,
                           guidance_file=self.guidance, web=self.web.get())

    def worker(self):
        while True:
            request = self.scheduler.take()
            key = request.key
            cfg = self.agent_config(request)
            label = BACKENDS.get(cfg.backend, 'The agent')
            self.events.put((key, 'working', f'{label} is working ({cfg.sandbox}).', None))
            actions = 0

            def activity(what):
                # Shown under the prompt in game: how far along, and the latest step.
                nonlocal actions
                actions += 1
                count = f'{actions} action' + ('' if actions == 1 else 's')
                self.events.put((key, 'activity', f'{label} is working · {count} · {what}', None))
            try:
                job = self.context.job(request, cfg.backend)
                # A conversation picked in the companion waits for a prompt its agent answers.
                with self.picker_lock:
                    chosen = self.pending_resume
                    if chosen and chosen[1] == cfg.backend:
                        self.pending_resume = None
                    else:
                        chosen = None
                if chosen:
                    job = Job(key, request.prompt, resume=chosen[0], history=job.history,
                              fork=chosen[2] and cfg.backend == 'claude', context=request.context)
                    self.root.after(0, lambda: self.conversation.set('In-game conversation: continuing the last one'))
                result = run_agent(cfg, job, on_update=lambda text: self.events.put((key, 'streaming', text, None)),
                                   on_activity=activity)
                self.context.remember(key, cfg.backend, result)
                self.events.put((key, result.state, result.reply, result.agent_session))
            except Exception as exc:  # Report, never crash the worker.
                self.events.put((key, 'failed', f'Companion error: {exc}', None))
            finally:
                self.scheduler.done(key)

    # ---- main loop ------------------------------------------------------
    def snapshot(self, key):
        browser = self.browser.get(key)
        if browser:
            return browser
        row = self.inbox.get(key) or {'id': key, 'state': 'waiting', 'reply': ''}
        if row['state'] == 'working' and key in self.activity:
            row['reply'] = self.activity[key]
        if row['state'] == 'queued':
            row['reply'] = self.scheduler.describe(key)
        if row.get('agent'):
            row['label'] = BACKENDS.get(row['agent'], 'The agent')  # the reply header names it too
        return row

    def name_of(self, key):
        return f'{self.names.get(key) or "chat"} #{key.rsplit(":", 1)[-1]}'

    def handle_events(self):
        while not self.events.empty():
            key, state, text, session = self.events.get_nowait()
            if state == 'notice':
                self.write(text)
                continue
            if state == 'activity':
                self.activity[key] = text
                continue
            if state == 'working':
                self.activity[key] = text
                self.inbox.update(key, state, '')
            else:
                self.inbox.update(key, state, text, session)
            self.job_status.set(f'{LABELS.get(state, state)}: {self.name_of(key)}')
            if state in ('done', 'failed'):
                self.activity.pop(key, None)
                row = self.inbox.get(key) or {}
                saved = self.save_reply(key, state, row.get('prompt', ''), text)
                self.write(f'[{state}] {self.name_of(key)}  (saved as {saved.name})\n{text}\n')
                if self.notify_enabled.get():
                    self.root.bell()
                    self.banner.show('Agent replied' if state == 'done' else 'Agent needs attention', text)

    def grab_frame(self):
        if self.manual.get():
            try:
                x, y, w, h = map(int, self.crop.get().split(','))
                crops = [(None, 'manual', (x, y, w, h))]
            except ValueError:
                self.capture_status.set('Manual crop must be x,y,w,h'); return None
            self.save(manual_crop=self.crop.get())
        elif self.locator:
            self.locator.refresh()
            crops = self.locator.crops()
            if not crops:
                self.capture_status.set('WoW window not found (running, visible, not minimized?)'); return None
        else:
            return None
        found = self.decode_from_screen(crops)
        if found:
            self.capture_mode = 'screen'
            return found
        # Nothing on screen: the game may be behind another window, so ask it to
        # render itself. That costs more, hence the slower cadence.
        now = time.monotonic()
        window = crops[0][0]
        interval = 0.25 if now - self.last_frame_at < 5 else 0.5
        if window is not None and window.hwnd and now - self.window_at >= interval:
            self.window_at = now
            found = self.decode_from_window(window, crops)
            if found:
                self.capture_mode = 'window'
                return found
        if self.locator and not self.manual.get():
            self.locator.failure()
        return None

    def decode_from_screen(self, crops):
        """One screen copy per horizontal band serves every candidate in it."""
        bands = {}
        for crop in crops:
            window, _, box = crop
            x, y, w, h = box if window is None else window.on_screen(box)
            bands.setdefault((y, h), []).append((crop, x))
        for (y, h), members in bands.items():
            left = min(x for _, x in members)
            right = max(x + c[2][2] for c, x in members)
            try:
                image = grab((left, y, right - left, h))
            except OSError:
                continue
            for crop, x in members:
                w = crop[2][2]
                try:
                    frame = decode_image(image.crop((x - left, 0, x - left + w, h)))
                except ValueError:
                    continue
                if crop[0] is not None:
                    self.locator.success(crop)
                return frame, crop
        return None

    def decode_from_window(self, window, crops):
        try:
            image = grab_window(window.hwnd, window.width, window.height)
        except OSError:
            return None
        for crop in crops:
            if crop[0] is not window:
                continue
            x, y, w, h = crop[2]
            try:
                frame = decode_image(image.crop((x, y, x + w, y + h)))
            except ValueError:
                continue
            self.locator.success(crop)
            return frame, crop
        return None

    def publish(self, control):
        """Hand the newest control frame to the publisher thread.

        Building a font takes a few hundred milliseconds, which would otherwise
        stall capture; only the latest frame matters, so an older pending one is
        simply replaced. Deadlines are clock-based and still enforced there.
        """
        if self.native is None or time.monotonic() < self.retry_at:
            return
        self.pending_publish = (control, self.snapshot(control.key))
        self.publish_wanted.set()

    def publisher(self):
        while True:
            self.publish_wanted.wait()
            self.publish_wanted.clear()
            item, self.pending_publish = self.pending_publish, None
            if item is None or self.native is None:
                continue
            control, snapshot = item
            try:
                if self.native.accept(control, snapshot):
                    self.last_status = f'wrote slot {control.slot} (part {control.part}) for prompt {control.request}'
            except OSError:
                # A sharing violation while the game reads a file is transient.
                self.retry_at = time.monotonic() + 1
            except ValueError as exc:
                self.native = None
                self.events.put((None, 'notice', f'Reply channel stopped: {exc}. Prompts and replies still work here.', None))

    def tick(self):
        try:
            self.handle_events()
            if self.epoch:
                self.epoch_tick()
            if self.capturing:
                self.capture_tick()
        except Exception as exc:  # Keep the loop alive; show what went wrong.
            self.capture_status.set(f'Capture error: {exc}')
        if not self.closed:
            self.root.after(70, self.tick)

    def epoch_tick(self):
        now = time.monotonic()
        if now - getattr(self, '_epoch_at', -1e9) < 3:
            return
        self._epoch_at = now
        if self.epoch.poll():
            self.write('WoW is closed: the reply channel will recycle on the next game start.')

    def capture_tick(self):
        # The strip only shows during an exchange; look less often while it is absent.
        now = time.monotonic()
        if now - self.last_frame_at > 2 and now - self.last_attempt < 0.3:
            return
        self.last_attempt = now
        found = self.grab_frame()
        if not found:
            if self.frames:
                self.capture_status.set('Strip not visible (idle between prompts, or covered). Waiting…')
            return
        frame, crop = found
        self.frames, self.last_frame_at = self.frames + 1, now
        where = crop[1] if crop[0] is None else f'{crop[1]} of WoW window'
        how = 'reading the window directly (WoW is covered)' if self.capture_mode == 'window' else 'on screen'
        self.capture_status.set(f'Strip at {where}, {how}; {self.frames} frames. {self.last_status or ""}')
        if frame_kind(frame) == 'control':
            self.publish(parse_control(frame))
            return
        if frame_kind(frame) == 'browser':
            result = self.url_assembler.accept(frame)
            if result:
                key, url = result
                previous = self.browser.get(key)
                outcome = self.browser.accept(key, url)
                if not previous:
                    self.write(outcome['reply'])
            return
        result = self.assembler.accept(frame)
        # When full, the prompt is not taken: the addon repeats it until acknowledged.
        if result and self.scheduler.open() < MAX_OPEN:
            key, blob = result
            fields, body = parse_envelope(blob)
            name, chat = (fields.get('name') or [''])[0], chat_of(key, fields)
            agent, model, notes = resolve_agent(fields, self.inbox.last_backend(chat), self.backend.get(),
                                                self.settings.get('models') or {})
            request = Request(key, body, chat, name, '\n'.join(fields.get('ctx', [])), agent, model)
            if self.inbox.add(request):
                self.names[key] = name
                for note in notes:
                    self.write(f'Note: {note}; using the default instead.')
                who = BACKENDS.get(agent, agent) + (f', {model}' if model else '')
                self.write(f'[queued] {self.name_of(key)} ({who})\nYou: {body}')
                self.job_status.set(f'Queued: {self.name_of(key)}')
                self.scheduler.put(request)

    def close(self):
        if self.scheduler.open():
            self.toggle_capture(False)
            self.capture_status.set('Capture paused. Close again after running jobs finish.')
            if getattr(self, '_close_warned', False):
                self._shutdown()
            self._close_warned = True
            return
        self._shutdown()

    def _shutdown(self):
        self.closed = True
        self.banner.dismiss()
        self.inbox.db.close()
        self.root.destroy()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--backend', choices=list(BACKENDS))
    parser.add_argument('--project', type=Path, help='Existing folder the agent works in')
    parser.add_argument('--addon', type=Path, help='Installed Interface/AddOns/AgentBridge folder')
    parser.add_argument('--state', type=Path, default=Path(__file__).resolve().parents[1] / 'state')
    parser.add_argument('--sandbox', choices=['read-only', 'workspace-write'])
    parser.add_argument('--model')
    parser.add_argument('--claude', help='Path to claude.exe')
    parser.add_argument('--codex', help='Path to codex.exe')
    parser.add_argument('--timeout', type=int, default=1800)
    parser.add_argument('--resume-session', help='Continue this agent session on the next in-game prompt')
    parser.add_argument('--start-capture', action='store_true')
    parser.add_argument('--minimized', action='store_true')
    args = parser.parse_args(argv)
    if args.project and not args.project.is_dir():
        parser.error('--project must be an existing folder')
    if hasattr(ctypes, 'windll'):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # physical pixels for screen coordinates
        except OSError:
            pass
    root = tk.Tk()
    App(root, args)
    root.mainloop()


if __name__ == '__main__':
    main()
