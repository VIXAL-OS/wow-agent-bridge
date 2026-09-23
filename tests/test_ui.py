"""Exercise in-game entry points that the transport tests never reach."""
import tempfile
import unittest

from tests.harness import Sim
from tests.test_e2e import agent


class InGame(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Sim(self.tmp.name, agent(lambda p: 'Echo: ' + p))
        self.sim.run(2)
        self.ns, self.g = self.sim.ns, self.sim.g

    def tearDown(self):
        self.tmp.cleanup()

    def slash(self, text=''):
        self.g.SlashCmdList.AGENTBRIDGE(text.encode())
        return self.prints()

    def prints(self):
        return [bytes(line).decode() for line in self.g.STUB.prints.values()]

    def test_slash_commands(self):
        self.slash('test')
        self.sim.run(3)  # the self-test runs across frames
        self.assertIn('size 64', ' '.join(self.prints()))
        self.assertIn('font size 64', ' '.join(self.slash('status')))
        self.assertIn('/ab new', ' '.join(self.slash('help')))
        self.slash('strip bottomleft')
        self.assertEqual(bytes(self.g.AgentBridgeState.strip), b'BOTTOMLEFT')
        self.slash('sound off')
        self.assertFalse(self.g.AgentBridgeState.sound)
        self.slash('show')
        self.assertTrue(self.g.AgentBridgePanel.shown)
        self.slash('hide')
        self.assertFalse(self.g.AgentBridgePanel.shown)
        self.slash()  # toggle
        self.assertTrue(self.g.AgentBridgePanel.shown)

    def test_new_chat_starts_a_conversation(self):
        self.sim.send('first')
        self.sim.run(30, until=lambda: self.sim.last_reply() is not None)
        before = bytes(self.ns.session)[:4]
        self.slash('new')
        self.assertIsNone(self.g.AgentBridgeState.last)
        self.assertNotEqual(bytes(self.ns.session)[:4], before)
        self.assertFalse(self.ns.IsReceiving())

    def test_item_links_in_prompts_and_replies(self):
        link = '|cff0070dd|Hitem:36942:0:0:0:0:0:0:0:80|h[Frostmourne]|h|r'
        text = bytes(self.ns.MakePromptText(link.encode() + b' worth it?')).decode()
        self.assertEqual(text, '[Frostmourne] (item 36942; link item:36942:0:0:0:0:0:0:0:80) worth it?')
        # Unknown items stay plain text, and markup in replies is escaped.
        markup, missing, mono, rows = self.ns.RenderReply(b'See [Thunderfury](item:19019) and |cffff0000fake|r')
        markup = bytes(markup).decode()
        self.assertEqual(markup, 'See Thunderfury (item 19019) and ||cffff0000fake||r')

    def test_prompt_too_long_is_refused(self):
        box = self.g.AgentBridgeInput
        box.text = b'x' * 1400
        box.scripts[b'OnEnterPressed'](box)
        self.assertFalse(self.ns.IsReceiving())
        self.assertEqual(bytes(box.text), b'x' * 1400, 'the draft is kept so it can be shortened')

    def test_panel_and_minimap_handlers(self):
        panel, mm = self.g.AgentBridgePanel, self.g.AgentBridgeMinimapButton
        panel.scripts[b'OnDragStart'](panel)
        panel.scripts[b'OnDragStop'](panel)
        self.assertEqual(bytes(self.g.AgentBridgeState.panel.point), b'CENTER')
        mm.scripts[b'OnClick'](mm, b'RightButton')
        self.assertTrue(panel.shown)
        self.assertTrue(self.g.AgentBridgeInput.focus)
        mm.scripts[b'OnEnter'](mm)
        mm.scripts[b'OnDragStart'](mm)
        mm.scripts[b'OnUpdate'](mm)
        mm.scripts[b'OnDragStop'](mm)
        self.ns.Toggle()
        self.assertFalse(panel.shown)

    def test_scrolling_keeps_your_place_within_an_exchange(self):
        scroll = self.g.AgentBridgeScroll
        long = '\n'.join(f'line {i}' for i in range(120)).encode()
        rows = lambda text: self.ns.RenderReply(text)[3]
        self.ns.RenderBody(b'question', rows(long))
        self.assertGreater(scroll.GetVerticalScrollRange(scroll), 0)
        self.assertEqual(scroll.GetVerticalScroll(scroll), 0, 'a new exchange starts at the top')
        # No filler: the page is exactly as tall as its lines.
        self.assertEqual(self.g.AgentBridgeBody.height, (120 + 2) * 16)
        scroll.SetVerticalScroll(scroll, 500)  # you scroll down to read
        self.ns.RenderBody(b'question', rows(long + b'\nmore'), b'(writing...)')
        self.assertEqual(scroll.GetVerticalScroll(scroll), 500, 'an update does not yank you back up')
        self.ns.RenderBody(b'another question', rows(b'short'))
        self.assertEqual(scroll.GetVerticalScroll(scroll), 0)

    def test_saved_reply_restored_after_reload(self):
        self.sim.send('remember me')
        self.sim.run(30, until=lambda: self.sim.last_reply() is not None)
        self.sim.reload()
        self.assertIn('Echo: remember me', self.sim.body())



class ReplyFormatting(unittest.TestCase):
    """Markdown from agents, made readable in a narrow panel."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Sim(self.tmp.name, agent(lambda p: 'x'))
        self.ns = self.sim.ns

    def tearDown(self):
        self.tmp.cleanup()

    def format(self, text, columns=70):
        entries, mono = self.ns.FormatLines(text.encode(), columns)
        return [(bytes(e.style).decode() if e.style else '', bytes(e.text).decode()) for e in entries.values()], mono

    def test_narrow_table_is_aligned(self):
        lines, mono = self.format('| Slot | State |\n|---|---|\n| 1 | free |\n| 22 | used |\n')
        self.assertTrue(mono)
        self.assertEqual(lines[0][1].rstrip(), 'Slot   State')
        self.assertEqual([text.rstrip() for _, text in lines[2:]], ['1      free', '22     used'])

    def test_wide_table_becomes_one_block_per_row(self):
        wide = ('| Area | Next milestone | Where? | Effort |\n|---|---|---|---|\n'
                '| **CONTROL** | schema next, calibration stays external and needs hardware | Local | S-M |\n')
        lines, _ = self.format(wide, columns=40)
        self.assertEqual(lines[0], ('label', 'CONTROL'))
        self.assertIn(('', '   Where?: Local'), lines)
        self.assertIn(('', '   Effort: S-M'), lines)

    def test_no_mono_font_never_aligns(self):
        _, mono = self.format('| a | b |\n|---|---|\n| 1 | 2 |\n', columns=0)
        self.assertFalse(mono)

    def test_code_block_kept_verbatim_and_headings_marked(self):
        lines, mono = self.format('## Plan\n- do `x` now\n\n```lua\n  local a = 1\n```\n')
        self.assertTrue(mono)
        self.assertEqual(lines[0], ('heading', 'Plan'))
        self.assertEqual(lines[1], ('', '- do x now'))
        self.assertEqual(lines[-1], ('code', '  local a = 1'))

    def test_typographic_characters_become_ascii(self):
        lines, _ = self.format('Ready \u2014 see \u201cnotes\u201d\u2026 caf\u00e9')
        self.assertEqual(lines[0][1], 'Ready - see "notes"... caf\u00e9')

    def test_table_in_a_real_reply_reaches_the_panel(self):
        sim = Sim(self.tmp.name + '2', agent(lambda p: '| Slot | State |\n|---|---|\n| 1 | free |\n'))
        sim.run(3)
        sim.send('status?')
        self.assertTrue(sim.run(60, until=lambda: sim.last_reply() is not None))
        self.assertIn('Slot', sim.body())
        self.assertIn('free', sim.body())


if __name__ == '__main__':
    unittest.main()
