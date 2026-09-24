"""Exercise in-game entry points that the transport tests never reach."""
import re
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
        state = self.g.AgentBridgeState
        before = state.chat
        self.slash('new Raid prep')
        self.assertNotEqual(state.chat, before)
        self.assertEqual(bytes(self.ns.ChatTitle(state.chat)), b'Raid prep')
        self.assertIn('(new conversation)', self.sim.body())
        self.assertEqual(len(state.chats), 2, 'the first chat is kept')
        self.assertEqual(self.sim.replies(before), ['Echo: first'])
        self.assertFalse(self.ns.IsReceiving())

    def test_item_links_in_prompts_and_replies(self):
        link = '|cff0070dd|Hitem:36942:0:0:0:0:0:0:0:80|h[Frostmourne]|h|r'
        agent, display = self.ns.MakePromptText(link.encode() + b' worth it?')
        self.assertEqual(bytes(agent).decode(), '[Frostmourne] (item 36942; link item:36942:0:0:0:0:0:0:0:80) worth it?')
        self.assertEqual(bytes(display).decode(), '[Frostmourne] worth it?', 'the transcript shows what you typed')
        # Unknown items stay plain text, and markup in replies is escaped.
        markup, missing, mono, rows = self.ns.RenderReply(b'See [Thunderfury](item:19019) and |cffff0000fake|r')
        markup = bytes(markup).decode()
        self.assertEqual(markup, 'See Thunderfury (item 19019) and ||cffff0000fake||r')

    def test_linked_tooltips_reach_the_agent_within_the_budget(self):
        tips = self.g.STUB.tooltips
        tips[b'item:19019:0:0:0:0:0:0:0:80'] = self.sim.lua.table_from(
            [b'|cffff8000Thunderfury, Blessed Blade of the Windseeker|r', b'Binds when picked up', b'44 - 84 Damage'])
        tips[b'spell:48441'] = self.sim.lua.table_from([b'Rejuvenation', b'Heals the target over 15 sec.'])
        raw = (b'|cffff8000|Hitem:19019:0:0:0:0:0:0:0:80|h[Thunderfury]|h|r vs '
               b'|cff71d5ff|Hspell:48441|h[Rejuvenation]|h|r?')
        agent, display = self.ns.MakePromptText(raw, 8000)
        agent = bytes(agent).decode()
        self.assertEqual(bytes(display).decode(), '[Thunderfury] vs [Rejuvenation]?')
        self.assertIn('Linked from the game (tooltip text):', agent)
        self.assertIn('Thunderfury, Blessed Blade of the Windseeker\nBinds when picked up\n44 - 84 Damage', agent)
        self.assertIn('Rejuvenation\nHeals the target over 15 sec.', agent)
        self.assertNotIn('|c', agent, 'colour codes never reach the agent')
        # Without room, the tooltips are left out rather than the message refused.
        short, _ = self.ns.MakePromptText(raw, 120)
        self.assertNotIn('Linked from the game', bytes(short).decode())

    def test_prompt_too_long_is_refused(self):
        box = self.g.AgentBridgeInput
        box.text = b'x' * 8500
        box.scripts[b'OnEnterPressed'](box)
        self.assertFalse(self.ns.IsReceiving())
        self.assertEqual(bytes(box.text), b'x' * 8500, 'the draft is kept so it can be shortened')

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



class Transcript(unittest.TestCase):
    """Scrolling back through a conversation, across prompts and reloads."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Sim(self.tmp.name, agent(lambda p: 'Echo: ' + p))
        self.sim.run(2)

    def tearDown(self):
        self.tmp.cleanup()

    def ask(self, text):
        self.sim.send(text)
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() == 'Echo: ' + text), text)

    request = 1000

    def finish(self, prompt, reply):
        """Record a finished exchange directly, without a round trip."""
        Transcript.request += 1
        ns = self.sim.ns
        ns.StartExchange(Transcript.request, self.sim.g.AgentBridgeState.chat, prompt.encode())
        ns.ShowReply(Transcript.request, reply.encode(), 4, True)

    def test_earlier_exchanges_stay_on_the_page_in_order(self):
        self.ask('first question')
        self.ask('second question')
        body = self.sim.body()
        order = [body.index(s) for s in ('first question', 'Echo: first question',
                                         'second question', 'Echo: second question')]
        self.assertEqual(order, sorted(order))

    def test_transcript_survives_reload_but_not_new_chat(self):
        self.ask('remember this')
        self.sim.reload()
        self.assertIn('Echo: remember this', self.sim.body())
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'new')
        self.assertNotIn('remember this', self.sim.body())
        self.assertIn('(new conversation)', self.sim.body())

    def test_history_is_capped_and_never_duplicated(self):
        for n in range(45):
            self.finish(f'q{n}', f'a{n}')
        self.finish('q44', 'a44')  # the same reply shown again, as item data or a reload would
        history = self.sim.g.AgentBridgeState.history
        self.assertEqual(len(history), 30)
        self.assertEqual(bytes(history[1].p), b'q15')
        self.assertEqual(bytes(history[30].r), b'a44')

    def test_opening_the_panel_lands_on_the_newest_line(self):
        for n in range(30):
            self.finish(f'q{n}', '\n'.join(f'line {i}' for i in range(5)))
        panel, scroll = self.sim.g.AgentBridgePanel, self.sim.g.AgentBridgeScroll
        scroll.SetVerticalScroll(scroll, 0)  # left at the top last time
        panel.scripts[b'OnShow'](panel)
        end = scroll.GetVerticalScrollRange(scroll)
        self.assertGreater(end, 0)
        self.assertEqual(scroll.GetVerticalScroll(scroll), end)
        self.sim.run(0.1)  # and still there after the deferred second pass
        self.assertEqual(scroll.GetVerticalScroll(scroll), end)
        self.assertTrue(self.sim.body().rstrip().endswith('line 4'))

    def test_new_prompt_scrolls_to_itself_and_updates_keep_your_place(self):
        for n in range(30):
            self.finish(f'q{n}', '\n'.join(f'line {i}' for i in range(5)))
        scroll = self.sim.g.AgentBridgeScroll
        self.sim.ns.StartExchange(900, self.sim.g.AgentBridgeState.chat, b'brand new')
        bottom = scroll.GetVerticalScroll(scroll)
        self.assertGreater(bottom, 0, 'the new prompt is brought into view')
        scroll.SetVerticalScroll(scroll, 100)  # you scroll back up to reread
        self.sim.ns.ShowReply(900, b'partial answer', 3, False)
        self.assertEqual(scroll.GetVerticalScroll(scroll), 100, 'an update does not drag you back down')


class ParallelChats(unittest.TestCase):
    """Several chats at once, the envelope around each prompt, and the extras."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Sim(self.tmp.name, agent(lambda p: 'Echo: ' + p))
        self.sim.run(2)
        self.ns, self.g, self.state = self.sim.ns, self.sim.g, self.sim.g.AgentBridgeState

    def tearDown(self):
        self.tmp.cleanup()

    def prints(self):
        return [bytes(line).decode() for line in self.g.STUB.prints.values()]

    def job(self, text):
        return next(job for job in self.sim.jobs.values() if job['prompt'] == text)

    def test_two_chats_answer_at_once(self):
        first = self.state.chat
        self.sim.send('question one')
        self.ns.NewChat(b'Second')
        second = self.state.chat
        self.sim.send('question two')
        self.assertEqual(self.ns.ReceiverInfo().pending, 2)
        both = lambda: self.sim.replies(first) and self.sim.replies(second)
        self.assertTrue(self.sim.run(120, until=both))
        self.assertEqual(self.sim.replies(first), ['Echo: question one'])
        self.assertEqual(self.sim.replies(second), ['Echo: question two'])
        # Each prompt names its chat, so the companion keeps two conversations.
        self.assertEqual(self.job('question one')['fields']['chat'], [str(first)])
        self.assertEqual(self.job('question one')['fields']['name'], ['question one'])
        self.assertEqual(self.job('question two')['fields']['name'], ['Second'])
        # The panel shows only the chat you are reading.
        self.assertIn('Echo: question two', self.sim.body())
        self.assertNotIn('question one', self.sim.body())
        self.ns.SelectChat(first)
        self.assertIn('Echo: question one', self.sim.body())
        loads = [name for name in self.sim.client.loads if name.startswith('reply')]
        self.assertEqual(len(loads), len(set(loads)), 'no slot is ever loaded twice')

    def test_game_context_travels_with_the_prompt(self):
        self.sim.send('where am I?')
        self.assertTrue(self.sim.run(20, until=lambda: self.sim.jobs))
        context = self.job('where am I?')['fields']['ctx']
        self.assertIn('Character: Testbrew, level 42 Tauren Druid (Horde)', context)
        self.assertIn('Location: Stranglethorn Vale - Booty Bay (27.3, 77.1)', context)
        self.assertIn('Money: 123g 45s 67c', context)
        self.assertIn('Talents: Balance 0 / Feral Combat 31 / Restoration 8', context)
        self.assertIn('Professions: Herbalism 300/375, Alchemy 280/300, Fishing 150/225', context)
        self.g.SlashCmdList.AGENTBRIDGE(b'context off')
        self.sim.send('and now?')
        self.assertTrue(self.sim.run(20, until=lambda: len(self.sim.jobs) == 2))
        self.assertNotIn('ctx', self.job('and now?')['fields'])

    def test_live_progress_under_the_prompt(self):
        working = 'Mock is working · 3 actions · Read: Core.lua'
        sim = Sim(self.tmp.name + '2', lambda prompt, elapsed: ('working', working) if elapsed < 60 else ('done', 'ok'))
        sim.run(2)
        panel = sim.g.AgentBridgePanel
        panel.Show(panel)
        sim.send('go')
        self.assertTrue(sim.run(30, until=lambda: working in sim.body()))
        seconds = lambda: int(re.search(r'Read: Core\.lua  0:(\d\d)\)', sim.body()).group(1))
        before = seconds()
        sim.run(3)
        self.assertGreaterEqual(seconds(), before + 2, 'the elapsed time ticks between reply packets')
        self.assertTrue(sim.run(90, until=lambda: sim.last_reply() == 'ok'))
        self.assertNotIn(working, sim.body(), 'progress is not kept in the transcript')

    def test_finished_reply_you_are_not_reading_is_echoed_and_marked(self):
        first = self.state.chat
        self.sim.send('ping')
        self.ns.NewChat()
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.replies(first)))
        self.assertTrue(self.ns.FindChat(first)[0].unread)
        text = '\n'.join(self.prints())
        self.assertIn('[ping]', text)
        self.assertIn('  Echo: ping', text)
        self.ns.SelectChat(first)
        self.assertFalse(self.ns.FindChat(first)[0].unread)

    def test_echo_can_be_shortened_or_turned_off(self):
        self.g.SlashCmdList.AGENTBRIDGE(b'echo 5')
        self.sim.send('abcdefgh')
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply()))
        text = '\n'.join(self.prints())
        self.assertIn('  Echo:', text)
        self.assertIn('9 more characters: /ab to read the rest', text)
        self.g.SlashCmdList.AGENTBRIDGE(b'echo off')
        self.sim.send('second')
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() == 'Echo: second'))
        self.assertIn('reply ready in "abcdefgh"', '\n'.join(self.prints()))

    def test_ai_command_sends_without_the_panel(self):
        self.g.SlashCmdList.AGENTBRIDGEAI(b'from the chat box')
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.last_reply() == 'Echo: from the chat box'))
        self.assertFalse(self.g.AgentBridgePanel.shown)
        self.g.SlashCmdList.AGENTBRIDGEAI(b'   ')
        self.assertIn('Type a message first.', self.prints()[-1])

    def test_copy_box_holds_the_reply_as_sent(self):
        chat = self.state.chat
        self.ns.StartExchange(500, chat, b'table please')
        self.ns.ShowReply(500, b'| a | b |\n|---|---|\n|cffcolour', 4, True)
        box = self.g.AgentBridgeCopyBox
        self.ns.ShowCopy(False)
        self.assertEqual(bytes(box.text), b'| a | b |\n|---|---|\n||cffcolour', 'only markup pipes are doubled')
        self.ns.ShowCopy(True)
        self.assertTrue(bytes(box.text).startswith(b'You: table please\n\n| a | b |'))
        box.text = b'edited'
        box.scripts[b'OnTextChanged'](box, True)
        self.assertTrue(bytes(box.text).startswith(b'You: table please'), 'typing into it changes nothing')

    def test_rename_and_delete_through_dialogs(self):
        first = self.state.chat
        self.sim.send('keep going')
        self.sim.run(3)
        self.ns.NewChat()
        second = self.state.chat
        self.ns.AskRename(second)
        self.g.StaticPopup1EditBox.text = b'Raid prep'
        self.g.StaticPopupDialogs.AGENTBRIDGE_RENAME.OnAccept(self.g.STUB.popups[1].dialog)
        self.assertEqual(bytes(self.ns.ChatTitle(second)), b'Raid prep')
        self.assertEqual(self.ns.ReceiverInfo().pending, 1)
        self.ns.AskDelete(first)
        self.g.StaticPopupDialogs.AGENTBRIDGE_DELETE.OnAccept(self.g.STUB.popups[2].dialog)
        self.assertIsNone(self.ns.FindChat(first))
        self.assertEqual(self.ns.ReceiverInfo().pending, 0, 'its reply is no longer awaited')
        self.assertEqual(self.state.chat, second)
        self.sim.run(30)
        self.assertEqual(self.sim.replies(first), [])

    def test_sidebar_lists_chats_and_switches_on_click(self):
        first = self.state.chat
        self.sim.send('alpha question')
        self.ns.NewChat(b'Beta')
        rawget = self.sim.lua.eval('rawget')
        rows = [f for f in self.g.STUB.frames.values() if f.kind == b'Button' and rawget(f, b'chat') is not None]
        titles = {rawget(f, b'chat'): bytes(rawget(f, b'label').text).decode() for f in rows if f.shown}
        self.assertTrue(titles[self.state.chat].endswith('Beta'))
        self.assertIn('...', titles[first], 'a chat still working is marked')
        row = next(f for f in rows if rawget(f, b'chat') == first)
        row.scripts[b'OnClick'](row, b'LeftButton')
        self.assertEqual(self.state.chat, first)
        self.assertIn('alpha question', self.sim.body())

    def sidebar(self):
        """{chat id: (title, agent tag)} for the rows showing in the chat list."""
        rawget = self.sim.lua.eval('rawget')
        return {rawget(f, b'chat'): (bytes(rawget(f, b'label').text).decode(), bytes(rawget(f, b'tag').text).decode())
                for f in self.g.STUB.frames.values()
                if f.kind == b'Button' and rawget(f, b'chat') is not None and f.shown}

    def test_each_chat_has_its_own_agent_and_model(self):
        slash = lambda text: self.g.SlashCmdList.AGENTBRIDGE(text.encode())
        first = self.state.chat
        # No choice made: the companion decides, and says which agent answered.
        self.sim.send('mocked')
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.replies(first)))
        self.assertEqual(self.sim.replies(first), ['Echo: mocked'], 'the header is not part of the reply')
        self.assertNotIn('agent', self.job('mocked')['fields'])
        self.assertEqual(bytes(self.ns.ChatAgent(first)[0]), b'mock')
        # A second chat on another agent and model, chosen in game.
        self.ns.NewChat(b'Codex one')
        second = self.state.chat
        slash('agent codex')
        slash('model gpt-5.5')
        self.sim.send('coded')
        self.assertTrue(self.sim.run(60, until=lambda: self.sim.replies(second)))
        fields = self.job('coded')['fields']
        self.assertEqual((fields['agent'], fields['model']), (['codex'], ['gpt-5.5']))
        agent, model, own_agent, own_model = self.ns.ChatAgent(second)
        self.assertEqual((bytes(agent), bytes(model), own_agent, own_model), (b'codex', b'gpt-5.5', True, True))
        self.assertEqual(self.sidebar()[first][1], 'Mock')
        self.assertEqual(self.sidebar()[second][1], 'Codex')
        slash('model')
        self.assertIn('Codex (set for this chat), model gpt-5.5 (set for this chat)', self.prints()[-1])
        # Only model names get through, and "default" hands the choice back.
        slash('model --dangerously-skip-permissions')
        self.assertIn('is not a model name', self.prints()[-1])
        slash('model default')
        self.assertIsNone(self.ns.FindChat(second)[0].model)
        # The model dialog from the chat list does the same as /ab model.
        self.ns.AskModel(second)
        self.g.StaticPopup1EditBox.text = b'gpt-6-astra'
        self.g.StaticPopupDialogs.AGENTBRIDGE_MODEL.OnAccept(self.g.STUB.popups[len(self.g.STUB.popups)].dialog)
        self.assertEqual(bytes(self.ns.FindChat(second)[0].model), b'gpt-6-astra')
        # A new agent cannot resume the old one's session, and drops its model.
        slash('agent claude')
        self.assertIn('starts a new Claude session', self.prints()[-1])
        self.assertIsNone(self.ns.FindChat(second)[0].model)
        slash('agent bash')
        self.assertIn('Unknown agent', self.prints()[-1])

    def test_one_conversation_from_an_earlier_version_becomes_chats(self):
        sim = Sim(self.tmp.name + '3', agent(lambda p: 'x'), saved={
            'conversation': 1757000000, 'nextSlot': 1,
            'history': {1: {'c': 1756000000, 'p': 'older', 'r': 'older reply', 's': 4},
                        2: {'c': 1757000000, 'p': 'latest', 'r': 'latest reply', 's': 4}}})
        state = sim.g.AgentBridgeState
        self.assertEqual([chat.id for chat in state.chats.values()], [1757000000, 1756000000])
        self.assertEqual(state.chat, 1757000000)
        self.assertIsNone(state.conversation)
        self.assertIn('latest reply', sim.body())
        self.assertNotIn('older reply', sim.body())
        self.assertEqual(bytes(sim.ns.ChatTitle(1756000000)), b'older')


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
