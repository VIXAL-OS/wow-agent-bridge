"""Bridge-owned Hermes tool policy, also importable by Hermes's Python 3.11."""
from copy import deepcopy
import json
import inspect
import os
from pathlib import Path
import tempfile
import threading

LEVELS = ('read-only', 'workspace-write', 'workspace-write+shell')
BASE_TOOLS = ['read_file', 'search_files', 'memory', 'session_search', 'todo_list',
              'skills_list', 'skill_view', 'skill_manage', 'delegate_task']
BROWSER_READ = ['browser_navigate', 'browser_snapshot', 'browser_scroll', 'browser_back', 'browser_get_images']
BROWSER_WRITE = ['browser_click', 'browser_type', 'browser_press']
POLICY_VERSION = 3


def features(home):
    path = Path(home) / 'bridge-tools.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(data, dict) or any(type(value) is not bool for value in data.values()):
        raise ValueError('bridge-tools.json must map feature names to true/false')
    return data


def tool_names(level, web, extras=None):
    if level not in LEVELS:
        raise ValueError('Unknown Hermes access level: ' + str(level))
    extras = extras or {}
    names = list(BASE_TOOLS)
    if web:
        names += ['web_search', 'web_extract']
        if extras.get('browser'):
            names += BROWSER_READ
            if level == 'workspace-write+shell':
                names += BROWSER_WRITE
    if level != 'read-only':
        names += ['write_file', 'patch']
    if level == 'workspace-write+shell':
        names += ['terminal', 'process_manage']
    if extras.get('vision'):
        names += ['vision_analyze']
    if extras.get('speech'):
        names += ['text_to_speech']
    return names


def write_path(project, value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError('A file path is required')
    root = Path(project).resolve()
    path = Path(value).expanduser()
    path = (path if path.is_absolute() else root / path).resolve()
    if not path.is_relative_to(root) or path == root:
        raise ValueError('File edits must stay inside the companion work folder: ' + str(root))
    relative = path.relative_to(root)
    if any(':' in part or part.lower() == '.git' for part in relative.parts):
        raise ValueError('Git metadata and alternate data streams are not editable through file tools')
    return str(path)


def scratch_path(project):
    return Path(write_path(project, 'state/hermes-scratch'))


def install_scratch_policy(project, level):
    """Align Hermes's system prompt and process temp directory with file guards.

    Hermes otherwise advertises its profile cache as a place to write helper
    scripts, which our workspace-only file tools must reject. Adapt the host
    hints in this worker only; never widen write access or patch the installation.
    """
    if level not in LEVELS:
        raise ValueError('Unknown Hermes access level: ' + str(level))
    from agent import prompt_builder
    original = prompt_builder._local_host_hints
    if level == 'read-only':
        hint = 'Project access is read-only; do not create temporary helper files or scripts.'
    else:
        scratch = scratch_path(project)
        scratch.mkdir(parents=True, exist_ok=True)
        for key in ('TMPDIR', 'TMP', 'TEMP'):
            os.environ[key] = str(scratch)
        tempfile.tempdir = None
        hint = (f'Scratch directory: {scratch} (inside the companion work folder; '
                'TMPDIR, TMP and TEMP point here). Write temporary helper files and scripts here. '
                'These files are not automatically deleted.')

    def host_hints():
        hints = ['\n'.join(line for line in block.splitlines()
                           if not line.startswith('Scratch directory:'))
                 for block in original()]
        return hints + [hint]

    prompt_builder._local_host_hints = host_hints


def install_guards(project, level):
    """Wrap Hermes's real handlers; child agents dispatch through the same registry.

    File-edit modes enforce resolved paths, including symlinks/junctions. Shell
    mode is explicitly NOT an OS sandbox: commands have the user's permissions.
    """
    from tools.registry import registry
    lock = threading.RLock()
    for name in ('write_file', 'patch', 'memory', 'skill_manage', 'text_to_speech'):
        entry = registry.get_entry(name)
        if entry is None or entry.is_async:
            raise RuntimeError('Unsupported Hermes tool registration: ' + name)
        original = entry.handler
        def guarded(args, _name=name, _original=original, **kwargs):
            args = dict(args)
            if _name == 'text_to_speech' and args.get('output_path'):
                try:
                    if level == 'read-only':
                        raise ValueError('Use the default Hermes audio cache at read-only access.')
                    args['output_path'] = write_path(project, args['output_path'])
                except (OSError, ValueError) as error:
                    return json.dumps({'error': str(error)})
            if _name in ('write_file', 'patch'):
                if level == 'read-only':
                    return json.dumps({'error': 'Project access is read-only.'})
                try:
                    if _name == 'patch' and args.get('mode', 'replace') != 'replace':
                        raise ValueError('Use patch mode=replace with an explicit path in the work folder.')
                    args['path'] = write_path(project, args.get('path'))
                except (OSError, ValueError) as error:
                    return json.dumps({'error': str(error)})
            with lock:
                parameters = inspect.signature(_original).parameters
                if not any(p.kind == inspect.Parameter.VAR_KEYWORD for p in parameters.values()):
                    kwargs = {key: value for key, value in kwargs.items() if key in parameters}
                return _original(args, **kwargs)
        schema = deepcopy(entry.schema)
        if name in ('write_file', 'patch'):
            schema['description'] += ' Agent Bridge limits this tool to the companion work folder.'
        registry.register(name=name, toolset=entry.toolset, schema=schema, handler=guarded,
                          check_fn=entry.check_fn, requires_env=entry.requires_env,
                          description=entry.description, emoji=entry.emoji,
                          max_result_size_chars=entry.max_result_size_chars,
                          dynamic_schema_overrides=None if name == 'patch' else entry.dynamic_schema_overrides,
                          override=True)
