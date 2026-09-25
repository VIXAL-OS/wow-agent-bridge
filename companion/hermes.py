"""Hermes CLI adapter: project tools, persistent memory, streaming and sessions."""
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import re
import time

from .agents import Result, codex_prompt, describe_tool, run_process
from .hermes_lock import profile_lock
from .hermes_policy import POLICY_VERSION, features, scratch_path, tool_names


def hermes_home(configured=None):
    return Path(configured) if configured else Path.home() / '.hermes' / 'agentbridge'


def profiles(home):
    """Only configured aliases can route requests; never guess a billing provider."""
    data = json.loads((Path(home) / 'models.json').read_text(encoding='utf-8'))
    if not isinstance(data, dict):
        raise ValueError('models.json must contain an object')
    choices = data.get('models')
    if not isinstance(choices, dict) or not choices or data.get('default') not in choices:
        raise ValueError('models.json needs a default alias and a models mapping')
    for alias, profile in choices.items():
        if not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,63}', alias):
            raise ValueError('Invalid Hermes model alias')
        if not isinstance(profile, dict) or any(not isinstance(profile.get(k), str) or
                                               not profile[k].strip() or profile[k].startswith('-')
                                               for k in ('provider', 'model')):
            raise ValueError(f'Invalid Hermes profile: {alias}')
        if profile.get('reasoning', 'none') not in ('none', 'minimal', 'low', 'medium', 'high'):
            raise ValueError(f'Invalid Hermes reasoning setting: {alias}')
    return data


def model_choices(home=None):
    try:
        return list(profiles(hermes_home(home))['models'])
    except (OSError, ValueError, TypeError):
        return []


def session_scope(home, profile, web, project=None, level='read-only', extras=None):
    # A model/access change starts a new native session using the bridge history.
    identity = [POLICY_VERSION, str(Path(home).resolve()), profile, bool(web),
                str(Path(project).resolve()) if project else None, level, extras or {}]
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()[:16]


def resume_id(saved, scope):
    if not isinstance(saved, str):
        return None
    parts = saved.split(':')
    if len(parts) == 3 and parts[:2] == ['hermes', scope] and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]*', parts[2]):
        return parts[2]
    return None


class HermesStream:
    def __init__(self, on_update=None, on_activity=None):
        self.on_update = on_update or (lambda text: None)
        self.on_activity = on_activity or (lambda text: None)
        self.session, self.result, self.partial, self.last_emit = None, None, '', 0.0

    def feed(self, event):
        kind = event.get('type')
        if kind == 'system' and event.get('subtype') == 'init':
            self.session = event.get('session_id')
        elif kind == 'text' and isinstance(event.get('text'), str):
            self.partial += event['text']
            now = time.monotonic()
            if self.partial.strip() and now - self.last_emit >= .5:
                self.on_update(self.partial)
                self.last_emit = now
        elif kind == 'tool_use':
            self.on_activity(describe_tool(str(event.get('name') or 'Tool'), event.get('input')))
        elif kind == 'result':
            self.result = event
            self.session = event.get('session_id') or self.session

    def outcome(self, returncode, tail, timed_out):
        if timed_out:
            return Result('failed', 'Hermes timed out. Send a shorter request or try again.')
        event = self.result
        if event is None:
            return Result('failed', 'Hermes exited without a completed reply.\n' +
                          (tail or self.partial or 'Check the Hermes installation.'))
        text = event.get('text') if isinstance(event.get('text'), str) else ''
        if returncode != 0 or event.get('exit_code') != 0 or event.get('error'):
            return Result('failed', str(event.get('error') or text or 'Hermes failed.'))
        return Result('done', text.strip() or self.partial.strip() or '(No text response.)', self.session)


def run_hermes(cfg, job, on_update, on_activity):
    if not cfg.hermes:
        return Result('failed', 'Hermes Python runtime not found. Install Hermes and set --hermes to its venv Python.')
    home = hermes_home(cfg.hermes_home)
    try:
        data = profiles(home)
        alias = cfg.model or data['default']
        profile = data['models'].get(alias)
        if profile is None:
            return Result('failed', f'Unknown Hermes model "{alias}". Choose: ' + ', '.join(data['models']))
    except (OSError, ValueError, TypeError) as error:
        return Result('failed', f'Hermes bridge profile is not configured: {error}')
    try:
        extras = features(home)
        tool_names(cfg.sandbox, cfg.web, extras)  # validate before launching a process
        if not cfg.project.is_dir():
            raise ValueError('The companion work folder does not exist')
    except (OSError, ValueError) as error:
        return Result('failed', str(error))
    scope = session_scope(home, profile, cfg.web, cfg.project, cfg.sandbox, extras)
    resume = resume_id(job.resume, scope)
    job = replace(job, resume=resume, fork=False)
    timeout = cfg.timeout
    command = [cfg.hermes, str(Path(__file__).with_name('hermes_worker.py')),
               'web' if cfg.web else 'none', cfg.sandbox, str(cfg.project.resolve()),
               'chat', '--cli', '--oneshot', '--query-file', '-',
               '--format', 'stream-json', '--toolsets', 'agentbridge', '--in', str(cfg.project.resolve()),
               '--provider', profile['provider'], '--model', profile['model'],
               '--max-turns', '40', '--run-budget', str(max(1, timeout - 30)),
               '--reasoning', profile.get('reasoning', 'none')]
    if resume:
        command += ['--resume', resume, '--no-restore-cwd']
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith('HERMES_')}
    env.update(HERMES_HOME=str(home.resolve()), HERMES_SAFE_MODE='1',
               PYTHONIOENCODING='utf-8', PYTHONUTF8='1')
    parser = HermesStream(on_update, on_activity)
    try:
        with profile_lock(home, timeout, lambda: on_activity('Waiting for another Hermes chat to finish')):
            on_activity(f'Hermes / {alias}: {cfg.sandbox}; thinking {profile.get("reasoning", "none")}; memory and skills enabled')
            instructions = ('Agent Bridge project folder: ' + str(cfg.project.resolve()) +
                            '. Project access: ' + cfg.sandbox + '. Persistent memory and skills live in your Hermes profile. '
                            'Only claim a file change, memory save, or completed command after the tool succeeds. '
                            'Do not control the WoW client or send game input. ')
            if cfg.sandbox == 'read-only':
                instructions += 'Do not create temporary helper files or scripts at read-only project access. '
            else:
                instructions += ('Put temporary helper files and scripts in ' + str(scratch_path(cfg.project)) +
                                 ', inside the work folder. File tools cannot write to the Hermes profile cache. ')
            if cfg.sandbox != 'workspace-write+shell':
                instructions += 'Shell commands and browser form interactions are disabled at this access level. '
            if cfg.web:
                instructions += 'Web search and page reading are enabled, including at read-only project access. '
                if extras.get('browser'):
                    instructions += 'Use browser_navigate and browser_snapshot to open and read websites. '
            else:
                instructions += 'Web search and browser tools are disabled. '
            prompt = instructions + '\n\n' + codex_prompt(job)
            outcome = parser.outcome(*run_process(command, cfg.project, prompt, timeout, parser.feed,
                                                 env=env, kill_tree=True))
    except (OSError, TimeoutError) as error:
        return Result('failed', str(error))
    if outcome.state == 'done' and outcome.agent_session:
        outcome.agent_session = f'hermes:{scope}:{outcome.agent_session}'
    return outcome
