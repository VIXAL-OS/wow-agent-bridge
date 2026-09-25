import json
from pathlib import Path
import tempfile
import tomllib
import unittest

from companion.agents import AgentConfig, Job, claude_command, codex_command
from companion.extras import configuration, media_tools


class Extras(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.flags = {'coding_agents': True, 'browser': True, 'speech': True, 'vision': True}
        (self.home / 'bridge-tools.json').write_text(json.dumps(self.flags))
        self.cfg = AgentConfig('codex', self.home / 'work space', hermes='hermes-python.exe', hermes_home=self.home)

    def test_extra_tools_follow_access_and_web(self):
        self.assertEqual(set(media_tools('read-only', False, self.flags)), {'vision_analyze', 'text_to_speech'})
        for level in ('read-only', 'workspace-write'):
            allowed = media_tools(level, True, self.flags)
            self.assertIn('browser_navigate', allowed)
            self.assertNotIn('browser_click', allowed)
        self.assertIn('browser_click', media_tools('workspace-write+shell', True, self.flags))
        self.assertIsNone(configuration(AgentConfig('codex', self.home, hermes_home=self.home)))

    def test_claude_gets_exact_allowed_mcp_tools_and_native_features(self):
        command = claude_command(self.cfg, Job('test', 'private prompt', resume='S'))
        allowed = command[command.index('--allowedTools') + 1].split(',')
        self.assertTrue({'Agent', 'Skill', 'mcp__agentbridge__browser_navigate'} <= set(allowed))
        self.assertNotIn('mcp__agentbridge__browser_click', allowed)
        self.assertNotIn('Bash', allowed)
        self.assertEqual(command[-2:], ['--resume', 'S'])
        mcp = json.loads(command[command.index('--mcp-config') + 1])['mcpServers']['agentbridge']
        self.assertEqual(mcp['args'][2], str(self.cfg.project.resolve()))
        self.assertEqual(mcp['args'][-1], 'web')
        self.assertNotIn('private prompt', ' '.join(command))

    def test_codex_configuration_is_valid_toml_for_fresh_and_resumed_runs(self):
        for resume in (None, 'T'):
            command = codex_command(self.cfg, Job('test', 'private prompt', resume=resume))
            values = [command[i+1] for i, value in enumerate(command) if value == '-c']
            parsed = tomllib.loads('\n'.join(values))
            self.assertTrue(parsed['features']['memories'])
            self.assertTrue(parsed['memories']['generate_memories'])
            self.assertEqual(parsed['approvals_reviewer'], 'auto_review')
            self.assertFalse(parsed['approval_policy']['granular']['sandbox_approval'])
            self.assertFalse(parsed['approval_policy']['granular']['request_permissions'])
            self.assertEqual(parsed['agents']['max_concurrent_threads_per_session'], 2)
            server = parsed['mcp_servers']['agentbridge']
            self.assertEqual(server['args'][2], str(self.cfg.project.resolve()))
            self.assertTrue(server['required'])
            self.assertNotIn('browser_click', server['enabled_tools'])
            self.assertNotIn('private prompt', ' '.join(command))
