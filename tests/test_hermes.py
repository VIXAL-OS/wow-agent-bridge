import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import threading
import types
from unittest.mock import patch

from companion.agents import AgentConfig, Job, run_agent, run_process
from companion.hermes import HermesStream, model_choices, resume_id, session_scope
from companion.hermes_policy import features, install_scratch_policy, scratch_path, tool_names, write_path
from companion.hermes_lock import profile_lock
from tools.configure_hermes import configure, hydra_profiles
from tests.harness import Sim
from tests.test_e2e import agent


class Hermes(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        self.profile = {'provider': 'bridge-test', 'model': 'model-a'}
        self.data = {'default': 'deepseek', 'models': {'deepseek': self.profile,
                                                    'qwen': dict(self.profile, model='model-b')}}
        (self.home / 'models.json').write_text(json.dumps(self.data))
        self.cfg = AgentConfig('hermes', self.home, hermes=sys.executable, hermes_home=self.home)

    def test_jsonl_stream_and_authoritative_completion(self):
        updates, activities = [], []
        parser = HermesStream(updates.append, activities.append)
        parser.feed({'type': 'system', 'subtype': 'init', 'session_id': 's1'})
        parser.feed({'type': 'text', 'text': 'Looking'})
        parser.feed({'type': 'text', 'text': ' now'})
        parser.feed({'type': 'tool_use', 'name': 'web_search', 'input': {'query': 'WoW'}})
        self.assertEqual(parser.partial, 'Looking now')
        self.assertIn('web_search: WoW', activities)
        self.assertTrue(updates)
        self.assertEqual(parser.outcome(0, '', False).state, 'failed')
        parser.feed({'type': 'result', 'exit_code': 0, 'text': 'Answer', 'session_id': 's1'})
        self.assertEqual(parser.outcome(0, '', False).reply, 'Answer')
        self.assertEqual(parser.outcome(0, '', False).agent_session, 's1')
        self.assertEqual(parser.outcome(1, '', False).state, 'failed')
        self.assertEqual(parser.outcome(0, '', True).state, 'failed')
        parser.feed({'type': 'result', 'exit_code': 1, 'error': 'Bad credentials'})
        result = parser.outcome(0, '', False)
        self.assertEqual((result.state, result.agent_session), ('failed', None))
        self.assertIn('Bad credentials', result.reply)

    def test_sessions_are_scoped_to_profile_and_web_access(self):
        scope = session_scope(self.home, self.profile, True)
        saved = 'hermes:' + scope + ':s1'
        self.assertEqual(resume_id(saved, scope), 's1')
        for changed in (session_scope(self.home, self.profile, False),
                        session_scope(self.home, self.data['models']['qwen'], True),
                        session_scope(self.home / 'other', self.profile, True)):
            self.assertIsNone(resume_id(saved, changed))
        self.assertIsNone(resume_id('hermes:' + scope + ':--latest', scope))

    def test_runner_stdin_model_switch_and_native_resume(self):
        seen = []
        def process(command, cwd, prompt, timeout, feed, env=None, kill_tree=False):
            seen.append((command, prompt, env))
            self.assertEqual(Path(cwd), self.home)
            self.assertEqual(timeout, self.cfg.timeout)
            self.assertEqual(env['HERMES_SAFE_MODE'], '1')
            self.assertTrue(kill_tree)
            self.assertNotIn('HERMES_KANBAN_TASK', env)
            self.assertNotIn('HERMES_IGNORE_RULES', env)
            self.assertNotIn('--ignore-rules', command)
            self.assertNotIn('Latest user input', ' '.join(command))
            self.assertIn('Latest user input', prompt)
            feed({'type': 'result', 'exit_code': 0, 'text': 'OK', 'session_id': 's1'})
            return 0, '', False
        with patch('companion.hermes.run_process', process), patch.dict(os.environ, {'HERMES_KANBAN_TASK': 'unrelated'}):
            first = run_agent(self.cfg, Job('1', 'Latest user input', context='Zone: Exodar'))
            self.assertIn('Zone: Exodar', seen[-1][1])
            self.assertIn('Web search and page reading are enabled', seen[-1][1])
            self.assertEqual(first.state, 'done')
            history = [('earlier message', 'earlier reply')]
            run_agent(self.cfg, Job('2', 'Latest user input', resume=first.agent_session, history=history))
            self.assertIn('--resume', seen[-1][0])
            self.assertNotIn('earlier reply', seen[-1][1])
            self.cfg.model, self.cfg.web = 'qwen', False
            run_agent(self.cfg, Job('3', 'Latest user input', resume=first.agent_session, history=history))
            self.assertNotIn('--resume', seen[-1][0])
            self.assertIn('earlier reply', seen[-1][1])
            self.assertEqual(seen[-1][0][2], 'none')
            self.assertIn('model-b', seen[-1][0])
            self.assertIn('Web search and browser tools are disabled', seen[-1][1])

    def test_unknown_models_fail_without_starting_a_process(self):
        self.assertEqual(model_choices(self.home), ['deepseek', 'qwen'])
        self.cfg.model = 'not-configured'
        with patch('companion.hermes.run_process') as process:
            self.assertEqual(run_agent(self.cfg, Job('1', 'test')).state, 'failed')
            process.assert_not_called()
        (self.home / 'models.json').write_text('bad json')
        self.assertEqual(model_choices(self.home), [])
        (self.home / 'models.json').write_text('[]')
        self.assertEqual(model_choices(self.home), [])

    def test_thinking_change_starts_fresh_native_session_with_history(self):
        seen = []
        def process(command, cwd, prompt, timeout, feed, **kwargs):
            seen.append((command, prompt))
            feed({'type': 'result', 'exit_code': 0, 'text': 'OK', 'session_id': 's1'})
            return 0, '', False
        with patch('companion.hermes.run_process', process):
            first = run_agent(self.cfg, Job('1', 'Before thinking'))
            self.data['models']['deepseek']['reasoning'] = 'high'
            (self.home / 'models.json').write_text(json.dumps(self.data))
            second = run_agent(self.cfg, Job('2', 'After thinking', resume=first.agent_session,
                                           history=[('Earlier question', 'Earlier answer')]))
        command, prompt = seen[-1]
        self.assertEqual(command[command.index('--reasoning') + 1], 'high')
        self.assertNotIn('--resume', command)
        self.assertIn('Earlier answer', prompt)
        self.assertNotEqual(first.agent_session, second.agent_session)

    def test_scratch_hint_and_temp_files_obey_workspace_access(self):
        project = self.home / 'project'
        project.mkdir()
        old_hint = 'Host: test\nScratch directory: /outside/cache/scratch (pruned)'
        for level in ('read-only', 'workspace-write', 'workspace-write+shell'):
            with self.subTest(level=level), patch.dict(os.environ), patch('tempfile.tempdir'):
                builder = types.SimpleNamespace(_local_host_hints=lambda: [old_hint, 'Shell hint'])
                agent = types.ModuleType('agent')
                agent.prompt_builder = builder
                with patch.dict(sys.modules, {'agent': agent}):
                    install_scratch_policy(project, level)
                    hints = '\n'.join(builder._local_host_hints())
                self.assertNotIn('/outside/cache/scratch', hints)
                self.assertNotIn('(pruned)', hints)
                self.assertIn('Host: test', hints)
                self.assertIn('Shell hint', hints)
                if level == 'read-only':
                    self.assertIn('do not create temporary helper files', hints)
                    self.assertFalse(scratch_path(project).exists())
                else:
                    scratch = scratch_path(project)
                    self.assertIn(str(scratch), hints)
                    self.assertEqual(Path(tempfile.gettempdir()), scratch)
                    with tempfile.TemporaryFile() as file:
                        file.write(b'probe')
                    target = write_path(project, str(scratch / 'glyph_diff.py'))
                    self.assertEqual(Path(target).parent, scratch)
                    for key in ('TMPDIR', 'TMP', 'TEMP'):
                        self.assertEqual(os.environ[key], str(scratch))
                with self.assertRaises(ValueError):
                    write_path(project, str(self.home / 'cache/scratch/glyph_diff.py'))

    def test_new_deepseek_profile_enables_thinking(self):
        (self.home / 'bot.py').write_text(
            'DEEPSEEK = ModelProvider(id="deepseek", model_id="deepseek-flash", '
            'api_key_env="DEEPSEEK_API_KEY", base_url="https://api.deepseek.com")\n')
        (self.home / 'config.json').write_text('{}')
        fake = types.SimpleNamespace(dotenv_values=lambda _: {'DEEPSEEK_API_KEY': 'test-only'},
                                     set_key=lambda *_: None)
        with patch.dict(sys.modules, {'dotenv': fake}), patch('builtins.print'):
            configure(self.home, self.home / 'new-profile')
        models = json.loads((self.home / 'new-profile/models.json').read_text())
        self.assertEqual(models['models']['deepseek']['reasoning'], 'high')

    def test_tools_follow_access_and_web_settings(self):
        extras = {'browser': True, 'vision': True, 'speech': True}
        read = set(tool_names('read-only', True, extras))
        edit = set(tool_names('workspace-write', True, extras))
        shell = set(tool_names('workspace-write+shell', True, extras))
        self.assertTrue({'memory', 'skill_manage', 'delegate_task', 'read_file', 'search_files'} <= read)
        self.assertFalse({'write_file', 'patch', 'terminal', 'process_manage', 'browser_click'} & read)
        self.assertTrue({'write_file', 'patch'} <= edit)
        self.assertFalse({'terminal', 'browser_type'} & edit)
        self.assertTrue({'terminal', 'process_manage', 'browser_click', 'browser_type'} <= shell)
        self.assertFalse({'cronjob_manage', 'computer_use', 'execute_code', 'browser_console', 'browser_cdp'} & shell)
        offline = tool_names('workspace-write+shell', False, extras)
        self.assertFalse(any(t.startswith(('web_', 'browser_')) for t in offline))
        with self.assertRaises(ValueError):
            tool_names('all', True)

    def test_workspace_write_boundaries(self):
        project = self.home / 'project'
        project.mkdir()
        self.assertEqual(Path(write_path(project, 'a/new.txt')), project / 'a' / 'new.txt')
        for value in ('../escape.txt', str(self.home / 'escape.txt'), '.git/config', 'file.txt:secret'):
            with self.subTest(path=value), self.assertRaises(ValueError):
                write_path(project, value)
        try:
            (project / 'escape').symlink_to(self.home, target_is_directory=True)
        except OSError:
            pass  # Windows can require developer mode for symlinks.
        else:
            with self.assertRaises(ValueError):
                write_path(project, 'escape/outside.txt')

    def test_project_and_access_changes_cannot_reuse_old_session(self):
        scope = session_scope(self.home, self.profile, True, self.home, 'read-only', {})
        saved = 'hermes:' + scope + ':s1'
        for project, level, extras in ((self.home / 'other', 'read-only', {}),
                                       (self.home, 'workspace-write', {}),
                                       (self.home, 'read-only', {'vision': True})):
            changed = session_scope(self.home, self.profile, True, project, level, extras)
            self.assertIsNone(resume_id(saved, changed))

    def test_profile_lock_queues_other_chat_and_releases_on_error(self):
        waiting, acquired = threading.Event(), threading.Event()
        def waiter():
            with profile_lock(self.home, 5, waiting.set):
                acquired.set()
        with self.assertRaises(RuntimeError):
            with profile_lock(self.home, 1):
                thread = threading.Thread(target=waiter)
                thread.start()
                self.assertTrue(waiting.wait(2))
                self.assertFalse(acquired.is_set())
                raise RuntimeError('simulated failed run')
        thread.join(3)
        self.assertTrue(acquired.is_set())
        self.assertFalse(thread.is_alive())

    def test_process_environment_is_passed_without_shell_interpolation(self):
        events = []
        script = ('import os,sys,json; print(json.dumps({"input":sys.stdin.read(),'
                  '"marker":os.environ["BRIDGE_TEST_MARKER"]}))')
        rc, _, timeout = run_process([sys.executable, '-c', script], self.home, 'hello $() & world',
                                     10, events.append, env=dict(os.environ, BRIDGE_TEST_MARKER='present'))
        self.assertEqual((rc, timeout), (0, False))
        self.assertEqual(events, [{'input': 'hello $() & world', 'marker': 'present'}])

    @unittest.skipUnless(sys.platform == 'win32', 'Windows venv process tree')
    def test_timeout_stops_python_descendants(self):
        import ctypes
        import subprocess
        events = []
        script = ('import subprocess,sys,json,time; '
                  'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(60)"]); '
                  'print(json.dumps({"pid":p.pid}),flush=True); time.sleep(60)')
        _, _, timed_out = run_process([sys.executable, '-c', script], self.home, '', 2,
                                     events.append, kill_tree=True)
        self.assertTrue(timed_out)
        self.assertTrue(events)
        kernel = ctypes.windll.kernel32
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, events[0]['pid'])
        if handle:
            code = ctypes.c_ulong()
            try:
                self.assertTrue(kernel.GetExitCodeProcess(ctypes.c_void_p(handle), ctypes.byref(code)))
            finally:
                kernel.CloseHandle(ctypes.c_void_p(handle))
            if code.value == 259:  # clean up our test child even if the assertion fails
                subprocess.run(['taskkill.exe', '/PID', str(events[0]['pid']), '/T', '/F'],
                               capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertNotEqual(code.value, 259, 'child must not keep running after timeout')

    def test_importer_reads_data_and_respects_backend_override(self):
        (self.home / 'bot.py').write_text('raise RuntimeError("must not execute")\n'
            'KIMI = ModelProvider(id="kimi", model_id="old", sdk_type="openai_compatible", '
            'api_key_env="OLD_KEY", base_url="https://old.test/v1", backends={"fireworks": '
            '{"model":"new", "base_url":"https://new.test/v1", "api_key_env":"NEW_KEY"}})\n'
            'SIM = ModelProvider(id="sim", completions_mode=True, api_key_env="OTHER_KEY")\n')
        (self.home / 'config.json').write_text('{"providers":{"kimi":{"backend":"fireworks"}}}')
        routes, keys = hydra_profiles(self.home, {'NEW_KEY': 'private', 'DISCORD_TOKEN': 'private', 'TAVILY_API_KEY': 'private'})
        self.assertEqual(routes['kimi']['model'], 'new')
        self.assertEqual(routes['kimi']['base_url'], 'https://new.test/v1')
        self.assertEqual(keys, {'NEW_KEY', 'TAVILY_API_KEY'})

    def test_game_picker_transmits_hermes_and_model(self):
        sim = Sim(self.tmp.name + '/game', agent(lambda p: 'Echo: ' + p))
        sim.run(2)
        sim.g.AgentBridgeChatMenu.chat = sim.g.AgentBridgeState.chat
        button = next(f for f in sim.g.STUB.frames.values() if f.text == b'Use Hermes')
        button.scripts[b'OnClick'](button)
        sim.g.SlashCmdList.AGENTBRIDGE(b'model qwen')
        sim.send('Hello Hermes')
        self.assertTrue(sim.run(60, until=lambda: sim.last_reply()))
        job = next(iter(sim.jobs.values()))
        self.assertEqual(job['fields']['agent'], ['hermes'])
        self.assertEqual(job['fields']['model'], ['qwen'])
        sim.g.SlashCmdList.AGENTBRIDGE(b'agent codex')
        self.assertIsNone(sim.ns.FindChat(sim.g.AgentBridgeState.chat)[0].model)
