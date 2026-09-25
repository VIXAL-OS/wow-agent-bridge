"""Opt-in Windows smoke check; one disposable about:blank browser at a time.

Run with Hermes's venv Python from the repository:
  python -m tools.check_browser_cleanup
Requires its psutil dependency. Does not use agent models or provider keys.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import uuid

from companion.browser_lifetime import browser_limits, protect_worker


def worker(home, root, mode):
    protect_worker()  # before the CLI can create even a failed browser session
    browser_limits()
    config = json.loads((home / 'bridge-runtime.json').read_text(encoding='utf-8'))
    executable = Path(config['browser_executable'])
    if not executable.is_file() or executable.name.lower() != 'chrome.exe':
        raise RuntimeError('Configure dedicated Chrome for Testing; this check will not fall back to Edge')
    cli = home / 'runtime/node_modules/agent-browser/bin/agent-browser-win32-x64.exe'
    env = {k: v for k, v in os.environ.items() if not k.upper().startswith(('AGENT_BROWSER_', 'BROWSER_'))}
    env.update(AGENT_BROWSER_EXECUTABLE_PATH=str(executable), AGENT_BROWSER_ARGS='--disable-gpu',
               AGENT_BROWSER_SOCKET_DIR=str(root), AGENT_BROWSER_IDLE_TIMEOUT_MS='3000' if mode == 'idle' else '300000')
    command = [str(cli), '--session', 'ab-cleanup-' + uuid.uuid4().hex[:12], '--profile', str(root / 'profile')]

    def call(action):
        args = ['open', 'about:blank'] if action == 'open' else [action]
        log_path = root / (action + '.log')
        # Files avoid a failed daemon inheriting a PIPE and hanging communicate()
        # even after the CLI's timeout has expired.
        with log_path.open('w', encoding='utf-8') as log:
            result = subprocess.run(command + args, env=env, stdout=log, stderr=log, timeout=30,
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        if result.returncode:
            raise RuntimeError(f'Browser {action} failed: ' + log_path.read_text(encoding='utf-8')[-1200:])

    opened, closed = False, False
    try:
        call('open')
        opened = True
        (root / 'ready').write_text('ready')
        sys.stdin.readline()  # parent records the exact disposable processes first
        if mode == 'normal':
            call('close')
            closed = True
            (root / 'closed').write_text('closed')
        # Keep the owner alive while the parent checks polite/idle shutdown.
        sys.stdin.readline()
    finally:
        if opened and not closed and mode != 'idle':
            call('close')


def check(home):
    import psutil
    if sys.platform != 'win32':
        raise RuntimeError('This process cleanup check is for Windows')
    python = getattr(sys, '_base_executable', sys.executable)
    results = []
    for mode in ('normal', 'forced', 'idle'):
        with tempfile.TemporaryDirectory(prefix='ab-cleanup-', ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            process = subprocess.Popen([python, '-m', 'tools.check_browser_cleanup', '--worker', mode,
                                        '--home', str(home), '--root', str(root)],
                                       stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       creationflags=subprocess.CREATE_NO_WINDOW)
            owned = []
            try:
                deadline = time.monotonic() + 40
                while not (root / 'ready').exists() and process.poll() is None and time.monotonic() < deadline:
                    time.sleep(.1)
                if not (root / 'ready').exists():
                    log = root / 'open.log'
                    raise RuntimeError('Browser probe did not become ready: ' +
                                       (log.read_text(encoding='utf-8', errors='replace')[-1500:] if log.exists() else 'no browser log'))
                # Match only the unique test profile/socket directory. Retain psutil's
                # PID+creation-time identity, never kill a browser by executable name.
                for candidate in psutil.process_iter(['name', 'cmdline']):
                    name = (candidate.info['name'] or '').lower()
                    if ('chrome' in name or 'agent-browser' in name) and any(
                            str(root).lower() in arg.lower() for arg in candidate.info['cmdline'] or []):
                        owned.append(candidate)
                for pid_file in root.glob('*.pid'):
                    candidate = psutil.Process(int(pid_file.read_text().strip()))
                    if ('agent-browser' in candidate.name().lower()
                            and candidate.create_time() >= psutil.Process(process.pid).create_time() - 1):
                        owned.append(candidate)
                for candidate in list(owned):
                    owned.extend(candidate.children(recursive=True))
                owned = list({candidate.pid: candidate for candidate in owned}.values())
                if not any(p.name().lower() == 'chrome.exe' for p in owned):
                    raise RuntimeError('Could not identify the disposable browser')
                process.stdin.write(b'continue\n'); process.stdin.flush()
                if mode == 'forced':
                    process.kill()  # deliberately bypass Python finally/atexit
                    process.wait(timeout=10)
                deadline = time.monotonic() + 15
                while any(p.is_running() for p in owned) and time.monotonic() < deadline:
                    time.sleep(.1)
                survivors = [p.pid for p in owned if p.is_running()]
                if survivors:
                    raise RuntimeError(f'{mode} left disposable processes alive: {survivors}')
                if mode != 'forced':
                    if process.poll() is not None:
                        raise RuntimeError('Worker exited before polite/idle cleanup could be verified')
                    process.stdin.write(b'exit\n'); process.stdin.flush()
                    process.wait(timeout=15)
                    if process.returncode:
                        raise RuntimeError('Worker cleanup failed')
                result = {'mode': mode, 'observed_processes': len(owned), 'survivors': 0}
                results.append(result)
                print(json.dumps(result), flush=True)
            finally:
                if process.poll() is None:
                    process.kill()
                out, err = process.communicate(timeout=10)
                # Emergency fallback limited to exact test processes, even if the guard broke.
                for candidate in owned:
                    if candidate.is_running():
                        candidate.kill()
                psutil.wait_procs(owned, timeout=5)
                if process.returncode and mode != 'forced' and err:
                    print(err.decode('utf-8', errors='replace')[-2000:], file=sys.stderr)
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--home', type=Path, default=Path.home() / '.hermes/agentbridge')
    parser.add_argument('--worker', choices=['normal', 'forced', 'idle'])
    parser.add_argument('--root', type=Path)
    args = parser.parse_args()
    if args.worker:
        worker(args.home.resolve(), args.root.resolve(), args.worker)
    else:
        check(args.home.resolve())
