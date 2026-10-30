"""An isolated Hermes source must win over stale editable-install mappings."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

from companion.hermes_runtime import prepare


class RuntimeSource(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'profile'
        self.home.mkdir()
        self.source = self.root / 'pinned source'
        self.source.mkdir()
        for name in ('hermes_cli/__init__.py', 'model_tools.py', 'toolsets.py', 'pyproject.toml'):
            target = self.source / name
            target.parent.mkdir(exist_ok=True)
            target.write_text('')
        self.source.joinpath('bridge_source_probe.py').write_text('VERSION = "pinned"\n')
        self.home.joinpath('bridge-runtime.json').write_text(json.dumps({'hermes_source': str(self.source)}))
        self.addCleanup(os.chdir, Path.cwd())
        self.enterContext(patch.dict(os.environ))
        self.enterContext(patch.object(sys, 'path', list(sys.path)))
        fake = types.SimpleNamespace(load_dotenv=lambda *_a, **_k: None)
        self.enterContext(patch.dict(sys.modules, {'dotenv': fake}))
        self.enterContext(patch('companion.browser_lifetime.protect_worker'))

    def test_pinned_source_wins_in_worker_and_python_children(self):
        stale = self.root / 'stale'
        stale.mkdir()
        stale.joinpath('bridge_source_probe.py').write_text('VERSION = "stale"\n')
        os.environ['PYTHONPATH'] = str(stale)
        sys.path.insert(0, str(stale))
        prepare(self.home, self.root)
        namespace = {}
        exec('from bridge_source_probe import VERSION', namespace)
        self.addCleanup(sys.modules.pop, 'bridge_source_probe', None)
        self.assertEqual(namespace['VERSION'], 'pinned')
        python = getattr(sys, '_base_executable', sys.executable)
        child = subprocess.run([python, '-c', 'from bridge_source_probe import VERSION; print(VERSION)'],
                               cwd=self.root, capture_output=True, text=True, timeout=15,
                               creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(child.stdout.strip(), 'pinned')

    def test_invalid_source_fails_before_hermes_import(self):
        self.source.joinpath('model_tools.py').unlink()
        with self.assertRaisesRegex(ValueError, 'Hermes source'):
            prepare(self.home, self.root)

    def test_relative_source_is_rejected(self):
        self.home.joinpath('bridge-runtime.json').write_text('{"hermes_source": "pinned source"}')
        with self.assertRaisesRegex(ValueError, 'absolute'):
            prepare(self.home, self.root)
