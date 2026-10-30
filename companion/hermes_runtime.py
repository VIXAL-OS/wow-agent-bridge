"""Shared process-local setup for the Hermes worker and media MCP server."""
import json
import os
from pathlib import Path
import sys
import subprocess


def bootstrap(home):
    """Start this helper in the profile's matched interpreter before any imports.

    The companion may already be running with an older discovered Python. Keep
    its stdin/stdout transport intact and bind the replacement to this worker's
    job, so cancellation also terminates the replacement and its browsers.
    """
    runtime_file = Path(home).resolve() / 'bridge-runtime.json'
    if not runtime_file.exists():
        return
    runtime = json.loads(runtime_file.read_text(encoding='utf-8'))
    value = runtime.get('hermes_python')
    if not value:
        return
    python = Path(value)
    if not python.is_absolute() or not python.is_file():
        raise ValueError('Hermes Python must name an existing absolute interpreter path')
    python = python.resolve()
    if python == Path(sys.executable).resolve():
        return
    if os.environ.get('AGENTBRIDGE_RUNTIME_REEXEC'):
        raise RuntimeError('Hermes interpreter did not select the requested environment')
    if __package__:
        from .browser_lifetime import protect_worker
    else:
        from browser_lifetime import protect_worker
    protect_worker()
    env = dict(os.environ, AGENTBRIDGE_RUNTIME_REEXEC='1')
    # Never mix another environment's Python search paths into this generation.
    for name in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV'):
        env.pop(name, None)
    result = subprocess.run([str(python), *sys.argv], env=env,
                            stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                            creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    raise SystemExit(result.returncode)


def select_source(value):
    """Use a bridge-owned compatible checkout without changing the shared venv.

    An editable install can retain an obsolete module map after upstream updates.
    Upstream may also remove tools that this adapter deliberately exposes. Pinning
    source in the bridge profile avoids mixing those versions in a fresh worker.
    """
    source = Path(value)
    if not source.is_absolute():
        raise ValueError('Hermes source must be an absolute path')
    source = source.resolve()
    required = ('hermes_cli/__init__.py', 'model_tools.py', 'toolsets.py', 'pyproject.toml')
    if not all((source / name).is_file() for name in required):
        raise ValueError('Hermes source is missing required runtime files: ' + str(source))
    path = str(source)
    sys.path.insert(0, path)
    # Hermes can spawn Python helpers; keep their imports on the same source.
    inherited = os.environ.get('PYTHONPATH', '')
    os.environ['PYTHONPATH'] = path + (os.pathsep + inherited if inherited else '')


def prepare(home, project):
    if __package__:
        from .browser_lifetime import protect_worker, browser_limits
    else:
        from browser_lifetime import protect_worker, browser_limits
    home, project = Path(home).resolve(), Path(project).resolve()
    if not project.is_dir():
        raise ValueError('The companion work folder does not exist')
    protect_worker()
    os.chdir(project)
    for key in list(os.environ):
        if key.upper().startswith(('HERMES_', 'AGENT_BROWSER_')) or key == 'BROWSER_CDP_URL':
            os.environ.pop(key, None)
    os.environ.update(HERMES_HOME=str(home), HERMES_SAFE_MODE='1', TERMINAL_CWD=str(project),
                      AGENT_BROWSER_ARGS='--disable-gpu')
    from dotenv import load_dotenv
    load_dotenv(home / '.env', override=True)
    browser_limits()
    runtime_file = home / 'bridge-runtime.json'
    if runtime_file.exists():
        runtime = json.loads(runtime_file.read_text(encoding='utf-8'))
        if runtime.get('hermes_source'):
            select_source(runtime['hermes_source'])
        if runtime.get('browser_executable'):
            os.environ['AGENT_BROWSER_EXECUTABLE_PATH'] = runtime['browser_executable']
    os.environ['PATH'] = str(home / 'runtime' / 'node_modules' / '.bin') + os.pathsep + os.environ.get('PATH', '')
