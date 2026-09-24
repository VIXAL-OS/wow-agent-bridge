"""Explicit source clicks, browser isolation, and selection inside the panel."""
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from companion.app import App
from companion.browser import BrowserRequests, validate_url
from companion.protocol import (Assembler, decode_image, encode_prompt, encode_url, frame_kind,
                                parse_url_frame, render_frame)
from tests.harness import Sim
from tests.test_e2e import agent


class Browser(unittest.TestCase):
    def setUp(self):
        self.db = sqlite3.connect(':memory:')
        self.opener = Mock(return_value=True)
        self.browser = BrowserRequests(self.db, self.opener)

    def tearDown(self):
        self.db.close()

    def test_only_explicit_http_urls_reach_browser(self):
        invalid = ['file:///C:/Windows/notepad.exe', 'javascript:alert(1)', 'mailto:x@y.com',
                   'https://user:secret@example.org/', 'https://example.org\\evil',
                   'https://example.org/\nfoo', 'https://example.org/"x"', '//example.org',
                   'https://', 'https://example.org:bad/', 'https://example.org/' + 'x' * 2048]
        for i, url in enumerate(invalid):
            with self.subTest(url=url[:80]):
                with self.assertRaises(ValueError):
                    validate_url(url)
                self.assertEqual(self.browser.accept(str(i), url)['state'], 'failed')
        self.opener.assert_not_called()
        url = 'https://en.wikipedia.org/wiki/Foo_(bar)?a=1&b=2#Section'
        self.assertEqual(self.browser.accept('valid', url)['state'], 'done')
        self.opener.assert_called_once_with(url)

    def test_retransmits_and_restart_open_one_tab_per_click(self):
        url = 'https://example.org/source'
        self.browser.accept('one', url)
        for _ in range(4):
            self.browser.accept('one', url)
        restarted = BrowserRequests(self.db, self.opener)
        restarted.accept('one', url)
        self.opener.assert_called_once_with(url)
        restarted.accept('two', url)
        self.assertEqual(self.opener.call_count, 2, 'a new deliberate click may reopen the same source')

    def test_failure_and_interrupted_clicks_are_not_replayed(self):
        self.opener.side_effect = OSError('no browser')
        self.assertEqual(self.browser.accept('failed', 'https://example.org')['state'], 'failed')
        self.browser.accept('failed', 'https://example.org')
        with self.db:
            self.db.execute('INSERT INTO browser_requests VALUES (?,?,?,?,?)',
                            ('crashed', 'https://example.org', 'interrupted', 'retry manually', 0))
        self.assertEqual(self.browser.accept('crashed', 'https://example.org')['state'], 'interrupted')
        self.assertEqual(self.opener.call_count, 1)

    def test_url_wire_frames_are_separate_from_prompts(self):
        url = 'https://example.org/' + 'café/' * 50 + '?x=1&y=2#fragment'
        assembler = Assembler(parser=parse_url_frame)
        frames = encode_url(url, b'abcdefgh', 7)
        for frame in frames:
            self.assertEqual(frame_kind(frame), 'browser')
            self.assertEqual(decode_image(render_frame(frame)), frame)
            result = assembler.accept(frame)
        self.assertEqual(result, ('6162636465666768:7', url))
        with self.assertRaises(ValueError):
            Assembler().accept(frames[0])
        with self.assertRaises(ValueError):
            assembler.accept(encode_prompt('https://example.org')[0])

    def test_capture_dispatch_never_queues_a_browser_click_as_a_prompt(self):
        app = SimpleNamespace(last_frame_at=0, last_attempt=0, frames=0, capture_mode='screen',
                              capture_status=Mock(), last_status='', browser=self.browser,
                              url_assembler=Assembler(parser=parse_url_frame), assembler=Mock(),
                              scheduler=Mock(), write=Mock())
        for _ in range(3):
            for frame in encode_url('https://example.org/source', b'abcdefgh', 3):
                app.grab_frame = lambda frame=frame: (frame, (None, 'manual', (0, 0, 512, 32)))
                App.capture_tick(app)
        self.opener.assert_called_once()
        app.assembler.accept.assert_not_called()
        app.scheduler.open.assert_not_called()
        app.scheduler.put.assert_not_called()


class InGameBrowser(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sim = Sim(self.tmp.name, agent(lambda p: 'Echo: ' + p))
        self.sim.run(2)
        self.ns, self.g = self.sim.ns, self.sim.g

    def tearDown(self):
        self.sim.browser.db.close()
        self.tmp.cleanup()

    def test_sources_keep_exact_destinations_and_ignore_code(self):
        code = chr(96)
        text = ('See [Wiki](https://example.org/Foo_(bar)?x=1&y=2#part) '
                'and https://example.org/other.\n'
                '[Again](https://example.org/Foo_(bar)?x=1&y=2#part)\n'
                '[Bad](file:///C:/test)\n' + code + 'https://example.org/code' + code +
                '\n' + code * 3 + '\nhttps://example.org/fenced\n' + code * 3)
        readable, sources = self.ns.WebReferences(text.encode())
        self.assertIn(b'Wiki [1]', readable)
        self.assertEqual(len(sources), 2)
        self.assertEqual(sources[1][b'url'], b'https://example.org/Foo_(bar)?x=1&y=2#part')
        self.assertEqual(sources[2][b'url'], b'https://example.org/other')
        self.assertEqual(self.sim.opened_urls, [], 'rendering a reply must never open its sources')

    def test_reload_without_discovering_new_browser_file_keeps_text_and_links(self):
        # Reproduce the live client's "Error loading ...Browser.lua" after an
        # in-session update. Existing files still load their updated contents.
        self.sim.known_files.discard('Browser.lua')
        toc = self.sim.addon / 'AgentBridge.toc'
        content = toc.read_text()
        if 'Browser.lua' not in content:
            toc.write_text(content.replace('Links.lua', 'Browser.lua\nLinks.lua'))
        self.sim.reload()
        self.assertIn('Browser.lua', self.sim.load_errors)
        self.sim.send('new reply')
        self.assertTrue(self.sim.run(40, until=lambda: self.sim.last_reply() == 'Echo: new reply'))
        self.assertIn('Echo: new reply', self.sim.body())
        self.assertIn('Echo: new reply', '\n'.join(line.decode() for line in self.sim.g.STUB.prints.values()))
        self.assertTrue(self.sim.ns.OpenURL(b'https://example.org/source'))
        self.assertTrue(self.sim.run(25, until=lambda: self.sim.status()[b'pending'] == 0))
        self.assertEqual(self.sim.opened_urls, ['https://example.org/source'])

    def test_lua_url_frames_match_python_at_the_size_limit(self):
        prefix = 'https://example.org/'
        url = prefix + 'x' * (2048 - len(prefix))
        lua_frames = self.ns.EncodeURL(url.encode(), b'abcdefgh', 8)
        self.assertEqual(list(lua_frames.values()), encode_url(url, b'abcdefgh', 8))
        self.assertFalse(self.ns.ValidURL((url + 'x').encode()))
        self.assertFalse(self.ns.OpenURL(b'javascript:alert(1)'))
        self.assertEqual(self.sim.status()[b'pending'], 0)

    def test_source_list_is_bounded_and_buttons_do_not_survive_chat_switches(self):
        text = '\n'.join(f'[Source {i}](https://example.org/{i})' for i in range(40))
        _, sources = self.ns.WebReferences(text.encode())
        self.assertEqual(len(sources), 32)
        self.ns.StartExchange(900, self.ns.S.chat, b'citations')
        self.ns.ShowReply(900, text.encode(), 4, True)
        self.g.AgentBridgePanel.Show(self.g.AgentBridgePanel)
        self.ns.RefreshTranscript()
        sources = [f for f in self.g.STUB.frames.values()
                   if f.kind == b'Button' and isinstance(f[b'url'], bytes) and f.shown]
        self.assertEqual(len(sources), 32)
        self.ns.NewChat()
        self.assertTrue(all(not f.shown for f in sources))

    def test_source_button_opens_once_acknowledges_and_preserves_chat(self):
        url = 'https://example.org/source?q=a&lang=en#part'
        self.ns.StartExchange(900, self.ns.S.chat, b'please cite sources')
        self.ns.ShowReply(900, ('See [Source](' + url + ').').encode(), 4, True)
        before = self.ns.CopyText(True)
        self.g.AgentBridgePanel.Show(self.g.AgentBridgePanel)
        self.ns.RefreshTranscript()
        buttons = [f for f in self.g.STUB.frames.values()
                   if f.kind == b'Button' and f[b'url'] == url.encode() and f.shown]
        self.assertEqual(len(buttons), 1)
        buttons[0].scripts[b'OnClick'](buttons[0])
        self.assertTrue(self.sim.run(25, until=lambda: self.sim.status()[b'pending'] == 0))
        self.assertEqual(self.sim.opened_urls, [url])
        self.assertEqual(self.sim.jobs, {}, 'a source click must not use an agent')
        self.assertEqual(self.ns.CopyText(True), before)
        self.assertTrue(self.ns.OpenURL(url.encode()), 'acknowledgement permits another deliberate click')
        self.assertTrue(self.sim.run(25, until=lambda: self.sim.status()[b'pending'] == 0))
        self.assertEqual(self.sim.opened_urls, [url, url])

    def test_browser_click_and_prompt_do_not_collide(self):
        self.sim.send('normal question')
        self.assertTrue(self.ns.OpenURL(b'https://example.org'))
        self.assertTrue(self.sim.run(45, until=lambda: self.sim.status()[b'pending'] == 0))
        self.assertEqual(self.sim.last_reply(), 'Echo: normal question')
        self.assertEqual(self.sim.opened_urls, ['https://example.org'])
        self.assertEqual(len(self.sim.jobs), 1)

    def test_offline_click_expires_and_can_be_retried(self):
        self.sim.companion_on = False
        self.assertTrue(self.ns.OpenURL(b'https://example.org'))
        self.sim.run(48)
        self.assertEqual(self.sim.status()[b'pending'], 0)
        self.assertEqual(self.sim.opened_urls, [])
        self.sim.companion_on = True
        self.assertTrue(self.ns.OpenURL(b'https://example.org'))
        self.assertTrue(self.sim.run(25, until=lambda: self.sim.status()[b'pending'] == 0))
        self.assertEqual(self.sim.opened_urls, ['https://example.org'])

    def test_selection_is_inside_panel_and_includes_streaming_text(self):
        self.ns.StartExchange(900, self.ns.S.chat, b'question')
        self.ns.ShowReply(900, b'partial answer', 3, False)
        self.ns.ShowCopy(True)
        frame, box = self.g.AgentBridgeCopy, self.g.AgentBridgeCopyBox
        self.assertEqual(frame.parent.name, b'AgentBridgePanel')
        self.assertTrue(box.HasFocus(box))
        selected = box.text
        self.assertIn(b'partial answer', selected)
        self.ns.ShowReply(900, b'partial answer and more', 3, False)
        self.assertEqual(box.text, selected, 'streaming must not disrupt a selection')
        box.scripts[b'OnEscapePressed'](box)
        self.assertFalse(frame.shown)
        self.assertTrue(self.g.AgentBridgePanel.shown)
        self.ns.ShowCopy(True)
        self.ns.NewChat()
        self.assertFalse(frame.shown, 'changing chats closes stale copy content')
