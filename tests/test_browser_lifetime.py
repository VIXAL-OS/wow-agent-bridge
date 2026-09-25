import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import types
import unittest
from unittest.mock import patch

from companion.browser_lifetime import browser_limits, cleanup_browsers
from companion.hermes_runtime import prepare


class BrowserLifetime(unittest.TestCase):
    def test_profile_cannot_disable_daemon_deadline(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            before = Path.cwd()
            self.addCleanup(os.chdir, before)
            fake = types.SimpleNamespace(load_dotenv=lambda *_a, **_k: os.environ.update(AGENT_BROWSER_IDLE_TIMEOUT_MS='0'))
            with patch.dict(sys.modules, {'dotenv': fake}), patch('companion.browser_lifetime.protect_worker') as guard:
                prepare(directory, directory)
                guard.assert_called_once()
                self.assertEqual(os.environ['AGENT_BROWSER_IDLE_TIMEOUT_MS'], '300000')
            os.chdir(before)

    def test_cleanup_failure_does_not_mask_original_error(self):
        def fail():
            raise RuntimeError('private browser state')
        with patch.dict(sys.modules, {'tools.browser_tool_lifecycle': types.SimpleNamespace(cleanup_all_browsers=fail)}):
            with patch('sys.stderr') as output:
                cleanup_browsers()
            text = ''.join(str(call.args[0]) for call in output.write.call_args_list)
            self.assertIn('RuntimeError', text)
            self.assertNotIn('private browser state', text)

    @unittest.skipUnless(sys.platform == 'win32', 'Windows process-lifetime regression')
    def test_detached_descendants_exit_on_success_exception_and_kill(self):
        import ctypes
        from ctypes import wintypes as w
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [w.DWORD, w.BOOL, w.DWORD]
        kernel.OpenProcess.restype = w.HANDLE
        kernel.WaitForSingleObject.argtypes = [w.HANDLE, w.DWORD]
        kernel.TerminateProcess.argtypes = [w.HANDLE, w.UINT]
        kernel.CloseHandle.argtypes = [w.HANDLE]
        python = getattr(sys, '_base_executable', sys.executable)
        sentinel = subprocess.Popen([python, '-c', 'import time; time.sleep(60)'],
                                    creationflags=subprocess.CREATE_NO_WINDOW)
        self.addCleanup(sentinel.wait, 5)
        self.addCleanup(sentinel.kill)
        child_code = ("import subprocess,sys,json,os,time; "
                      "p=subprocess.Popen([sys.executable,'-c','import time; time.sleep(60)'],creationflags=0x8); "
                      # Rename into place: ready.json never exists without its contents.
                      "f=open(sys.argv[1]+'.tmp','w'); f.write(json.dumps([os.getpid(),p.pid])); f.close(); "
                      "os.replace(sys.argv[1]+'.tmp',sys.argv[1]); time.sleep(60)")
        worker_code = ("from companion.browser_lifetime import protect_worker; "
                       "protect_worker(); protect_worker(); import subprocess,sys; "
                       "subprocess.Popen([sys.executable,'-c',sys.argv[2],sys.argv[1]],creationflags=0x8); "
                       "mode=sys.stdin.readline().strip(); "
                       "raise SystemExit(1 if mode=='exception' else 0)")
        for mode in ('success', 'exception', 'kill'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                ready = Path(directory) / 'ready.json'
                process = subprocess.Popen([python, '-c', worker_code, str(ready), child_code],
                                           stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                           creationflags=subprocess.CREATE_NO_WINDOW)
                handles = []
                try:
                    deadline = time.monotonic() + 10
                    while not ready.exists() and process.poll() is None and time.monotonic() < deadline:
                        time.sleep(.05)
                    self.assertTrue(ready.exists(), 'Protected worker failed before creating test children')
                    # Open stable kernel handles while the children are alive; no PID-reuse ambiguity.
                    for pid in json.loads(ready.read_text()):
                        handle = kernel.OpenProcess(0x100001, False, pid)  # SYNCHRONIZE | TERMINATE
                        self.assertTrue(handle)
                        handles.append(handle)
                    if mode == 'kill':
                        process.kill()
                    else:
                        process.stdin.write((mode + '\n').encode()); process.stdin.flush()
                    process.wait(timeout=10)
                    for handle in handles:
                        self.assertEqual(kernel.WaitForSingleObject(handle, 10000), 0, 'Worker leaked a descendant')
                    self.assertIsNone(sentinel.poll(), 'An unrelated process was affected')
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
                    process.stdin.close(); process.stderr.close()
                    for handle in handles:
                        if kernel.WaitForSingleObject(handle, 0) != 0:
                            kernel.TerminateProcess(handle, 1)
                        kernel.CloseHandle(handle)
