"""Shared process-local setup for the Hermes worker and media MCP server."""
import json
import os
from pathlib import Path


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
        if runtime.get('browser_executable'):
            os.environ['AGENT_BROWSER_EXECUTABLE_PATH'] = runtime['browser_executable']
    os.environ['PATH'] = str(home / 'runtime' / 'node_modules' / '.bin') + os.pathsep + os.environ.get('PATH', '')
