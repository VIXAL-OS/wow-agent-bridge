"""Retry through the actual Lua UI, transport and saved-variable lifecycle."""
import tempfile
import unittest

from tests.harness import Sim
from tests.test_e2e import agent
from tests.test_character import FIXTURES


class Retry(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sim = Sim(self.tmp.name, agent(lambda p: 'Echo: ' + p))
        self.sim.run(2)

    def enabled(self):
        button = self.sim.ns.RetryButton
        return button.IsEnabled(button)

    def click(self):
        button = self.sim.ns.RetryButton
        button.scripts[b'OnClick'](button)

    def test_agent_failure_retry_preserves_links_draft_and_uses_new_id(self):
        ns, g = self.sim.ns, self.sim.g
        self.assertFalse(self.enabled())
        raw = ('Compare |cff0070dd|Hitem:36942:0:0:0:0:0:0:0:80|h[Frostmourne]|h|r '
               'with |Hspell:48441|h[Rejuvenation]|h')
        self.sim.agent = lambda _p, _t: ('failed', 'Provider unavailable')
        self.sim.send(raw)
        self.assertFalse(self.enabled())
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() == 'Provider unavailable'))
        self.assertTrue(self.enabled())
        first_id, first = next(iter(self.sim.jobs.items()))
        draft = b'Different question I am still typing'
        g.AgentBridgeInput.SetText(g.AgentBridgeInput, draft)
        ns.SetChatAgent(g.AgentBridgeState.chat, b'hermes')
        ns.SetChatModel(g.AgentBridgeState.chat, b'deepseek')
        self.sim.agent = agent(lambda p: 'Recovered: ' + p)
        self.click()
        self.assertFalse(self.enabled())
        self.click()  # Handler also rejects duplicate clicks, not just the widget.
        self.assertEqual(ns.ReceiverInfo().pending, 1)
        self.assertEqual(g.AgentBridgeInput.text, draft)
        self.assertTrue(self.sim.run(90, until=lambda: (self.sim.last_reply() or '').startswith('Recovered:')))
        self.assertEqual(len(self.sim.jobs), 2)
        second_id, second = next((k, v) for k, v in self.sim.jobs.items() if k != first_id)
        self.assertNotEqual(first_id, second_id)
        self.assertEqual(second['prompt'], first['prompt'])
        self.assertIn('item 36942', second['prompt'])
        self.assertIn('spell 48441', second['prompt'])
        self.assertEqual(second['fields']['chat'], first['fields']['chat'])
        self.assertEqual(second['fields']['agent'], ['hermes'])
        self.assertEqual(second['fields']['model'], ['deepseek'])
        self.assertTrue(self.enabled())

    def test_retry_is_per_chat_and_restores_older_transcript_prompts(self):
        ns, state = self.sim.ns, self.sim.g.AgentBridgeState
        first = state.chat
        self.sim.send('First chat prompt')
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() is not None))
        # A chat saved before Retry was installed has no original-input field.
        ns.CurrentChat().lastPrompt = None
        self.assertEqual(ns.LastPrompt(first), b'First chat prompt')
        ns.NewChat(b'Second')
        second = state.chat
        self.assertFalse(self.enabled())
        self.sim.send('Second chat prompt')
        self.assertFalse(self.enabled())
        ns.SelectChat(first)
        self.assertTrue(self.enabled(), 'another chat working does not prevent retry')
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'retry')
        self.assertTrue(self.sim.run(90, until=lambda: len(self.sim.jobs) == 3 and not ns.IsChatBusy(first) and not ns.IsChatBusy(second)))
        self.assertEqual(sum(job['prompt'] == 'First chat prompt' for job in self.sim.jobs.values()), 2)
        ns.DeleteChat(first)
        self.assertIsNone(ns.LastPrompt(first))
        self.assertEqual(ns.LastPrompt(second), b'Second chat prompt')

    def test_reload_mid_request_keeps_original_input_for_retry(self):
        self.sim.companion_on = False
        raw = 'Explain |Hspell:48441|h[Rejuvenation]|h'
        self.sim.send(raw)
        self.assertFalse(self.enabled())
        self.sim.reload()
        self.sim.run(2)
        self.assertTrue(self.enabled())
        self.assertEqual(self.sim.ns.LastPrompt(self.sim.g.AgentBridgeState.chat), raw.encode())
        self.sim.companion_on = True
        self.click()
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() is not None))
        self.assertEqual(len(self.sim.jobs), 1)
        self.assertIn('spell 48441', next(iter(self.sim.jobs.values()))['prompt'])

    def test_transport_timeout_retires_old_prompt_before_retry(self):
        self.sim.companion_on = False
        self.sim.send('Companion was offline')
        # Advance the clock before the first slot read: expire without a 1-hour simulation.
        self.sim.t += 3601
        self.sim.run(1)
        self.assertTrue(self.enabled())
        self.assertEqual(self.sim.status().pending, 0)
        self.assertIsNone(self.sim.g.STUB.strip())
        self.assertEqual(self.sim.g.AgentBridgeState.history[1].s, 6)
        self.sim.companion_on = True
        self.click()
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() == 'Echo: Companion was offline'))
        self.assertEqual(len(self.sim.jobs), 1, 'the expired request must not restart alongside its retry')

    def test_character_sync_failure_can_be_retried(self):
        self.sim.lua.execute(FIXTURES.encode())
        self.sim.run(3)
        self.sim.send('Inventory question')
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context off')
        self.assertTrue(self.enabled())
        self.assertIn('Character sync failed', self.sim.last_reply())
        self.click()
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() == 'Echo: Inventory question'))

    def test_unavailable_channel_does_not_leave_a_phantom_busy_request(self):
        sim = Sim(self.tmp.name + '/exhausted', agent(lambda _: 'ok'), saved={'nextSlot': 65536, 'epoch': '1'})
        sim.run(2)
        sim.send('Keep this prompt')
        self.assertFalse(sim.ns.IsChatBusy(sim.g.AgentBridgeState.chat))
        self.assertEqual(sim.status().pending, 0)
        self.assertEqual(sim.g.AgentBridgeInput.text, b'Keep this prompt')
        self.assertEqual(sim.ns.LastPrompt(sim.g.AgentBridgeState.chat), b'Keep this prompt')
        self.assertTrue(sim.ns.RetryButton.IsEnabled(sim.ns.RetryButton))
        self.assertIsNone(sim.g.STUB.strip())
        sim.run(2)
        self.assertFalse(sim.jobs)


if __name__ == '__main__':
    unittest.main()
