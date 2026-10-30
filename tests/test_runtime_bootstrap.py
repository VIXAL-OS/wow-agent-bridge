import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from companion.hermes_runtime import bootstrap


class RuntimeBootstrap(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.config = self.home / 'bridge-runtime.json'

    def test_matching_interpreter_does_not_spawn(self):
        self.config.write_text(json.dumps({'hermes_python': sys.executable}))
        with patch('subprocess.run') as run:
            bootstrap(self.home)
        run.assert_not_called()

    def test_relaunch_preserves_arguments_streams_and_exit_code(self):
        python = self.home / 'new runtime' / 'python.exe'
        python.parent.mkdir()
        python.touch()
        self.config.write_text(json.dumps({'hermes_python': str(python)}))
        with patch.dict(os.environ, {'PYTHONPATH': 'stale', 'PYTHONHOME': 'old'}, clear=True), \
                patch.object(sys, 'argv', ['helper.py', 'folder with spaces', 'web']), \
                patch('companion.browser_lifetime.protect_worker') as protect, \
                patch('subprocess.run', return_value=types.SimpleNamespace(returncode=7)) as run:
            with self.assertRaises(SystemExit) as exit_code:
                bootstrap(self.home)
        self.assertEqual(exit_code.exception.code, 7)
        protect.assert_called_once()
        self.assertEqual(run.call_args.args[0], [str(python.resolve()), 'helper.py', 'folder with spaces', 'web'])
        self.assertNotIn('PYTHONPATH', run.call_args.kwargs['env'])
        self.assertNotIn('PYTHONHOME', run.call_args.kwargs['env'])
        self.assertIs(run.call_args.kwargs['stdin'], sys.stdin)
        self.assertIs(run.call_args.kwargs['stdout'], sys.stdout)
        self.assertIs(run.call_args.kwargs['stderr'], sys.stderr)

    def test_missing_interpreter_fails_closed(self):
        self.config.write_text(json.dumps({'hermes_python': str(self.home / 'missing.exe')}))
        with self.assertRaises(ValueError):
            bootstrap(self.home)

    def test_relaunch_cannot_loop(self):
        python = self.home / 'wrong.exe'
        python.touch()
        self.config.write_text(json.dumps({'hermes_python': str(python)}))
        with patch.dict(os.environ, {'AGENTBRIDGE_RUNTIME_REEXEC': '1'}):
            with self.assertRaises(RuntimeError):
                bootstrap(self.home)
