import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from io import BytesIO

from fontTools.ttLib import TTFont
from lupa.lua51 import LuaRuntime
from PIL import Image

from companion import agents, protocol
from companion.agents import AgentConfig, ClaudeStream, CodexStream, Job, claude_command, history_prompt
from companion.native import (BANK_FORMAT, NativeBridge, atomic_write, bank_format, ensure_bank,
                              font_path, make_font, prepare_bank)
from companion.protocol import (Assembler, REPLY_CHUNK, REPLY_SIZE, decode_image, encode_control, encode_prompt,
                                make_reply_packet, parse_control, parse_reply_packet, render_frame)
from companion.wow import EpochKeeper, GameWindow, StripLocator, strip_candidates
from tests.harness import ROOT, install_small, packet_from_font

REPLY_SIZE_BYTES = bytes(REPLY_SIZE)


class Protocol(unittest.TestCase):
    def test_prompt_round_trip_and_duplicates(self):
        text = 'Frostmourne hungers ' * 30
        frames = encode_prompt(text, b'abcdefgh', 7)
        assembler = Assembler()
        results = [assembler.accept(f) for f in frames[:-1] + frames[:-1]]
        self.assertTrue(all(r is None for r in results))
        self.assertEqual(assembler.accept(frames[-1]), ('6162636465666768:7', text))

    def test_prompt_limits(self):
        with self.assertRaises(ValueError):
            encode_prompt('x' * 1281)
        corrupted = bytearray(encode_prompt('hi')[0]); corrupted[25] ^= 1
        with self.assertRaises(ValueError):
            protocol.parse_prompt(bytes(corrupted))

    def test_control_validation(self):
        c = parse_control(encode_control(b'abcdefgh', slot=5, remaining_ms=12000, part=2, request=3))
        self.assertEqual((c.slot, c.remaining_ms, c.part, c.active, c.request, c.loaded), (5, 12000, 2, True, 3, 4))
        with self.assertRaises(ValueError):
            encode_control(remaining_ms=40000)

    def test_reply_packet_fields(self):
        text = ('é' * 2400).encode()
        p = parse_reply_packet(make_reply_packet('0102030405060708', 9, 44, 2, 4, text))
        self.assertEqual((p['part'], p['total'], p['slot'], p['request']), (2, 2, 44, 9))
        self.assertEqual(p['text'], text[REPLY_CHUNK:])

    def test_sampling_survives_display_scaling(self):
        frame = encode_prompt('scaled strip', b'abcdefgh', 1)[0]
        image = render_frame(frame)
        for size, resample in (((720, 22), Image.BILINEAR), ((720, 22), Image.NEAREST), ((960, 30), Image.BICUBIC)):
            self.assertEqual(decode_image(image.resize(size, resample)), frame)
        scaled = image.resize((722, 24), Image.BILINEAR)
        self.assertEqual(decode_image(scaled.crop((1, 1, 721, 23))), frame)

    def test_game_scene_is_rejected(self):
        noise = Image.effect_noise((720, 22), 60).convert('RGB')
        with self.assertRaises(ValueError):
            decode_image(noise)


class Fonts(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addon = install_small(self.tmp.name, slots=1100)

    def tearDown(self):
        self.tmp.cleanup()

    def test_font_encodes_every_byte(self):
        data = bytes(range(256)) * 16
        self.assertEqual(packet_from_font(make_font(data)), data)
        self.assertNotIn('fpgm', TTFont(BytesIO(make_font(data))))  # no hinting programs

    def test_bank_shares_files_and_publishing_isolates_a_slot(self):
        one, two = font_path(self.addon, 1), font_path(self.addon, 2)
        self.assertEqual(os.stat(one).st_ino, os.stat(two).st_ino)
        before = two.read_bytes()
        atomic_write(one, b'new')
        self.assertEqual(two.read_bytes(), before)
        self.assertEqual(prepare_bank(self.addon, count=1100)['created'], 0)

    def test_publisher_deadlines_and_frozen_fragments(self):
        clock = [0.0]
        bridge = NativeBridge(self.addon, clock=lambda: clock[0])
        session = b'abcdefgh'
        ctl = lambda slot, ms, part=1: parse_control(encode_control(session, slot, ms, part, True, 1))
        snap = {'id': session.hex() + ':1', 'state': 'streaming', 'reply': 'a' * (REPLY_CHUNK + 512)}
        self.assertTrue(bridge.accept(ctl(1, 5000), snap))
        self.assertFalse(bridge.accept(ctl(1, 800), snap), 'under 1 s left: never write')
        clock[0] = 10
        # Part 2 must come from the text whose part 1 was written, even if it changed.
        snap['reply'] = 'b' * (REPLY_CHUNK + 512)
        self.assertTrue(bridge.accept(ctl(2, 5000, part=2), snap))
        published = parse_reply_packet(packet_from_font(font_path(self.addon, 2)))
        self.assertEqual(published['text'], b'a' * 512)
        # Content updates before the deadline are allowed, throttled to 1 s.
        clock[0] = 20
        self.assertTrue(bridge.accept(ctl(3, 9000), snap))
        snap['reply'] = 'c'
        self.assertFalse(bridge.accept(ctl(3, 8000), snap))
        clock[0] = 21.5
        self.assertTrue(bridge.accept(ctl(3, 7000), snap))
        # A repeated old capture cannot extend a deadline.
        clock[0] = 27.5
        self.assertFalse(bridge.accept(ctl(3, 9000), {**snap, 'reply': 'd'}))


class Agents(unittest.TestCase):
    def test_claude_stream(self):
        updates, activity = [], []
        s = ClaudeStream(updates.append, activity.append)
        events = [
            {'type': 'system', 'subtype': 'init', 'session_id': 'S1'},
            {'type': 'stream_event', 'event': {'type': 'message_start'}},
            {'type': 'stream_event', 'event': {'type': 'content_block_delta', 'delta': {'type': 'text_delta', 'text': 'Let me look.'}}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Let me look.'},
                {'type': 'tool_use', 'name': 'Read', 'input': {'file_path': 'C:/x/Receiver.lua'}}]}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'Fixed it.'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'result': 'Fixed it.', 'session_id': 'S1'},
        ]
        for e in events:
            s.feed(e)
        self.assertEqual(activity, ['Read: Receiver.lua'])
        self.assertIn('[Read: Receiver.lua]', updates[1])
        r = s.outcome(0, '', False)
        self.assertEqual((r.state, r.reply, r.agent_session), ('done', 'Fixed it.', 'S1'))

    def test_claude_error_result(self):
        s = ClaudeStream()
        s.feed({'type': 'result', 'subtype': 'success', 'is_error': True, 'result': 'Failed to authenticate'})
        self.assertEqual(s.outcome(1, '', False).state, 'failed')

    def test_codex_stream(self):
        s, activity = CodexStream(None, None), []
        s.on_activity = activity.append
        for e in [{'type': 'thread.started', 'thread_id': 'T'},
                  {'type': 'item.started', 'item': {'type': 'command_execution', 'command': 'rg  foo'}},
                  {'type': 'item.completed', 'item': {'id': 'i1', 'type': 'agent_message', 'text': 'pong'}},
                  {'type': 'turn.completed'}]:
            s.feed(e)
        self.assertEqual(activity, ['Run: rg foo'])
        self.assertEqual(s.outcome(0, '', False).reply, 'pong')
        self.assertEqual(CodexStream().outcome(0, '', False).state, 'failed')

    def test_claude_flags_by_access(self):
        cfg = AgentConfig('claude', Path('.'), claude='claude.exe', guidance_file=Path('g.txt'))
        read_only = claude_command(cfg, Job('k', 'p'))
        self.assertIn('dontAsk', read_only)
        self.assertNotIn('p', read_only, 'prompt goes on stdin, never argv')
        cfg.sandbox = 'workspace-write'
        write = claude_command(cfg, Job('k', 'p', resume='S9'))
        self.assertIn('acceptEdits', write)
        self.assertEqual(write[-2:], ['--resume', 'S9'])

    def test_shell_access_level(self):
        from companion.agents import codex_sandbox
        cfg = AgentConfig('claude', Path('.'), claude='claude.exe', sandbox='workspace-write+shell')
        command = claude_command(cfg, Job('k', 'p'))
        self.assertIn('acceptEdits', command)
        self.assertIn('Read,Glob,Grep,Edit,Write,Bash', command)
        self.assertEqual(codex_sandbox('workspace-write+shell'), 'workspace-write')
        self.assertEqual(codex_sandbox('read-only'), 'read-only')

    def test_history_prompt(self):
        self.assertEqual(history_prompt(Job('k', 'hi')), 'hi')
        body = history_prompt(Job('k', 'next', history=[('q', 'a')]))
        self.assertEqual(json.loads(body[body.index('{'):])['history'][1], {'role': 'assistant', 'content': 'a'})

    def test_process_streaming_and_stdin(self):
        # Agents read stdin as UTF-8 bytes; make the stand-in child do the same.
        script = ('import sys,json; t=sys.stdin.buffer.read().decode("utf-8"); '
                  'print(json.dumps({"type":"echo","text":t})); print("noise")')
        events = []
        code, tail, timed_out = agents.run_process([sys.executable, '-c', script], '.', 'päyload', 30, events.append)
        self.assertEqual((code, timed_out, events), (0, False, [{'type': 'echo', 'text': 'päyload'}]))


class Wow(unittest.TestCase):
    def test_candidates(self):
        window = GameWindow(1, 0, 0, 1920, 1080)
        boxes = dict(strip_candidates(window, 16 / 9))
        self.assertEqual(boxes['TOP'], (600, 0, 720, 45))
        self.assertEqual(boxes['BOTTOMRIGHT'], (1200, 1035, 720, 45))
        # Boxes are client-relative so the same crop works for window capture.
        moved = GameWindow(1, 100, 50, 1920, 1080)
        self.assertEqual(dict(strip_candidates(moved, 16 / 9))['TOP'], (600, 0, 720, 45))
        self.assertEqual(moved.on_screen((600, 0, 720, 45)), (700, 50, 720, 45))
        stretched = strip_candidates(GameWindow(1, 0, 0, 1920, 1017), 16 / 9)
        self.assertEqual(len(stretched), 12, 'render/window aspect mismatch adds a second model')

    def test_epoch_only_written_while_game_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            addon = install_small(tmp, slots=2)
            running = [True]
            keeper = EpochKeeper(addon, processes=lambda _: [1] if running[0] else [], clock=lambda: 5.0)
            before = (addon / 'Epoch.lua').read_text()
            self.assertFalse(keeper.poll())
            self.assertEqual((addon / 'Epoch.lua').read_text(), before)
            running[0] = False
            self.assertTrue(keeper.poll())
            self.assertIn('"5000"', (addon / 'Epoch.lua').read_text())
            self.assertFalse(keeper.poll(), 'once per closed period')

    def test_locator_lock_and_release(self):
        clock = [0.0]
        loc = StripLocator(Path('.'), clock=lambda: clock[0])
        loc.windows = [GameWindow(1, 0, 0, 1920, 1080)]
        crop = loc.crops()[0]
        loc.success(crop)
        self.assertEqual(loc.crops(), [crop])
        clock[0] = 2; loc.failure()
        self.assertEqual(loc.crops(), [crop])
        clock[0] = 4; loc.failure()
        self.assertEqual(len(loc.crops()), 6)


class LuaUnits(unittest.TestCase):
    """Addon Lua against the Python reference implementation."""

    def setUp(self):
        self.lua = LuaRuntime(encoding=None, unpack_returned_tuples=True)
        self.lua.execute(b'AgentBridge = {}; CreateFrame = function() return setmetatable({}, {__index = function() return function() end end}) end')
        for name in ('Core.lua', 'Protocol.lua'):
            self.lua.execute((ROOT / 'addon' / 'AgentBridge' / name).read_bytes())
        self.ns = self.lua.globals().AgentBridge

    def test_frames_match_python(self):
        text = 'Ice Ice Baby — 冰'.encode()
        lua_frames = [bytes(f) for f in self.ns.EncodePrompt(text, b'abcdefgh', 3).values()]
        self.assertEqual(lua_frames, encode_prompt(text.decode(), b'abcdefgh', 3))
        self.assertEqual(bytes(self.ns.EncodeControl(b'abcdefgh', 9, 2500, 2, True, 3)),
                         encode_control(b'abcdefgh', 9, 2500, 2, True, 3))

    def test_reply_parse_and_assembly(self):
        text = ('ü' * 2200).encode()
        session = b'abcdefgh'
        packets = [make_reply_packet(session.hex(), 4, 10 + p, p, 4, text) for p in (1, 2)]
        ok, why = self.ns.ParseReply(packets[0], session, 4, 99)
        self.assertEqual(bytes(why), b'Stale reply packet')
        assembly = self.ns.NewAssembly()
        p1 = self.ns.ParseReply(make_reply_packet(session.hex(), 4, 11, 1, 4, text), session, 4, 11)
        p2 = self.ns.ParseReply(make_reply_packet(session.hex(), 4, 12, 2, 4, text), session, 4, 12)
        partial, state, complete = self.ns.AcceptFragment(assembly, p1)
        self.assertFalse(complete)
        self.assertNotEqual(bytes(self.ns.TrimUTF8(partial))[-1:], b'\xc3', 'no dangling lead byte')
        whole, state, complete = self.ns.AcceptFragment(assembly, p2)
        self.assertEqual((bytes(whole), state, complete), (text, 4, True))
        bad = bytearray(packets[1]); bad[100] ^= 4
        self.assertEqual(bytes(self.ns.ParseReply(bytes(bad), session, 4, 12)[1]), b'Invalid reply checksum')
        self.assertEqual(bytes(self.ns.ParseReply(REPLY_SIZE_BYTES, session, 4, 12)[1]), b'Empty reply slot')


class Installer(unittest.TestCase):
    def test_clean_install_then_rerun_preserves(self):
        from tools.install_addon import install
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / 'Interface' / 'AddOns' / 'AgentBridge'
            settings = Path(tmp) / 'settings.json'
            self.assertEqual(install(target, count=64, settings_path=settings)['created'], 64)
            names = {p.name for p in target.iterdir()}
            self.assertTrue({'AgentBridge.toc', 'Epoch.lua', 'selftest.ttf', 'reply00001.ttf', 'reply00064.ttf'} <= names)
            self.assertFalse([n for n in names if n.startswith(('.seed-', '.next-'))], 'no temp files left behind')
            atomic_write(target / 'reply00003.ttf', b'used')
            (target / 'Epoch.lua').write_text('AgentBridgeEpoch = "77"\n')
            self.assertEqual(install(target, count=64, settings_path=settings)['created'], 0)
            self.assertEqual((target / 'reply00003.ttf').read_bytes(), b'used')
            self.assertIn('"77"', (target / 'Epoch.lua').read_text())
            self.assertEqual(json.loads(settings.read_text())['addon'], str(target.resolve()))
            self.assertEqual(bank_format(target), BANK_FORMAT)
            # An older bank format is rebuilt, but never while the game runs.
            (target / '.bankformat').write_text('older')
            with self.assertRaises(RuntimeError):
                ensure_bank(target, count=64, running=True)
            self.assertEqual((target / 'reply00003.ttf').read_bytes(), b'used')
            report = ensure_bank(target, count=64, running=False)
            # One write per shared inode: the 64-name link group plus the detached slot 3.
            self.assertEqual((report['rebuilt'], report['created']), (2, 0))
            self.assertEqual(packet_from_font(target / 'reply00003.ttf'), REPLY_SIZE_BYTES)
            self.assertEqual(packet_from_font(target / 'reply00064.ttf'), REPLY_SIZE_BYTES)
            self.assertEqual(os.stat(target / 'reply00001.ttf').st_ino,
                             os.stat(target / 'reply00064.ttf').st_ino, 'links preserved')
        with self.assertRaises(ValueError):
            install(Path(tmp) / 'Elsewhere', count=1, settings_path=settings)


if __name__ == '__main__':
    unittest.main()


class TranslucentStrip(unittest.TestCase):
    """Cell pairs are read as differences, so opacity and background must not matter."""

    def setUp(self):
        self.frame = encode_prompt('see-through strip', b'abcdefgh', 1)[0]

    def backgrounds(self):
        from PIL import Image
        return {'black': None, 'game scene': Image.effect_noise((256, 64), 30).convert('RGB'),
                'bright': Image.new('RGB', (8, 8), (240, 235, 210)),
                'blue UI': Image.new('RGB', (8, 8), (30, 60, 160))}

    def test_decodes_over_backgrounds_at_reduced_opacity(self):
        for name, background in self.backgrounds().items():
            for alpha in (1.0, 0.7, 0.5):
                image = render_frame(self.frame, cell=4, alpha=alpha, background=background)
                self.assertEqual(decode_image(image), self.frame, f'{name} at alpha {alpha}')

    def test_harsh_background_rejects_rather_than_misreads(self):
        from PIL import Image
        harsh = Image.effect_noise((256, 64), 120).convert('RGB')
        for alpha in (0.5, 0.35, 0.2):
            image = render_frame(self.frame, cell=4, alpha=alpha, background=harsh)
            try:
                self.assertEqual(decode_image(image), self.frame)
            except ValueError:
                pass  # rejected: the addon simply repeats the frame

    def test_scaled_translucent_strip(self):
        from PIL import Image
        image = render_frame(self.frame, cell=4, alpha=0.6, background=self.backgrounds()['game scene'])
        self.assertEqual(decode_image(image.resize((720, 45), Image.BILINEAR)), self.frame)

    def test_very_faint_still_decodes_over_a_clean_background(self):
        # Measured in game: 20% opacity leaves ~50 of 255 between paired cells.
        for alpha in (0.2, 0.12, 0.08):
            self.assertEqual(decode_image(render_frame(self.frame, cell=4, alpha=alpha)), self.frame, alpha)

    def test_blank_screen_is_not_mistaken_for_a_strip(self):
        from PIL import Image
        for flat in (Image.new('RGB', (720, 45), (20, 25, 30)), Image.new('RGB', (720, 45), 'white')):
            with self.assertRaises(ValueError):
                decode_image(flat)


class Sessions(unittest.TestCase):
    def test_lists_newest_first_with_folder_and_opening_line(self):
        from companion.sessions import list_sessions, read_session
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'D--work-proj').mkdir()
            old = root / 'D--work-proj' / 'aaaaaaaa-1111-2222-3333-444444444444.jsonl'
            new = root / 'D--work-proj' / 'bbbbbbbb-1111-2222-3333-444444444444.jsonl'
            old.write_text(json.dumps({'type': 'queue-operation', 'cwd': r'D:\work\proj',
                                       'content': 'old thread about fonts'}) + '\n' + 'x' * 4096, encoding='utf-8')
            new.write_text(json.dumps({'type': 'user', 'cwd': r'D:\work\proj',
                                       'message': {'content': [{'type': 'text', 'text': 'newer thread'}]}}) + '\n' + 'y' * 4096, encoding='utf-8')
            os.utime(old, (1, 1))
            found = list_sessions(root=root)
            self.assertEqual([s.id for s in found], [new.stem, old.stem])
            self.assertEqual(found[0].summary, 'newer thread')
            self.assertEqual(found[1].cwd, r'D:\work\proj')
            self.assertIn('proj', found[0].label())
            # Stubs too small to hold an exchange are skipped.
            (root / 'D--work-proj' / 'cccccccc.jsonl').write_text('{}', encoding='utf-8')
            self.assertEqual(len(list_sessions(root=root)), 2)
            self.assertEqual(read_session(root / 'D--work-proj' / 'cccccccc.jsonl'), ('', ''))

    def test_lists_codex_rollouts(self):
        from companion.sessions import list_sessions
        with tempfile.TemporaryDirectory() as tmp:
            day = Path(tmp) / '2026' / '09' / '22'
            day.mkdir(parents=True)
            path = day / 'rollout-2026-09-22T13-34-11-01a0ca2e-ac86-7752-87fb-16052d62b95f.jsonl'
            lines = [
                {'type': 'session_meta', 'payload': {'session_id': '01a0ca2e-ac86-7752-87fb-16052d62b95f',
                                                     'cwd': r'D:\work\proj'}},
                {'type': 'response_item', 'payload': {'role': 'user', 'content': [
                    {'type': 'input_text', 'text': '<environment_context>ignored</environment_context>'}]}},
                {'type': 'response_item', 'payload': {'role': 'user', 'content': [
                    {'type': 'input_text', 'text': 'fix the strip decoder'}]}},
            ]
            body = '\n'.join(json.dumps(line) for line in lines) + '\n' + 'z' * 4096
            path.write_text(body, encoding='utf-8')
            found = list_sessions(root=Path(tmp), backend='codex')
            self.assertEqual(len(found), 1)
            self.assertEqual(found[0].id, '01a0ca2e-ac86-7752-87fb-16052d62b95f')
            self.assertEqual(found[0].cwd, r'D:\work\proj')
            self.assertEqual(found[0].summary, 'fix the strip decoder', 'injected context blocks are skipped')

    def test_codex_resume_command(self):
        from companion.agents import codex_command
        cfg = AgentConfig('codex', Path('.'), codex='codex.exe', sandbox='workspace-write', model='gpt-5')
        fresh = codex_command(cfg, Job('k', 'p'))
        self.assertIn('--sandbox', fresh)
        resumed = codex_command(cfg, Job('k', 'p', resume='T1'))
        # resume takes neither --sandbox nor -C: the sandbox is a config override.
        self.assertNotIn('--sandbox', resumed)
        self.assertNotIn('-C', resumed)
        self.assertIn('sandbox_mode="workspace-write"', resumed)
        self.assertEqual(resumed[-2:], ['T1', '-'])
        self.assertIn('--fork-session', claude_command(AgentConfig('claude', Path('.'), claude='c.exe'),
                                                       Job('k', 'p', resume='S', fork=True)))

    def test_fork_flag_branches_instead_of_appending(self):
        cfg = AgentConfig('claude', Path('.'), claude='claude.exe')
        plain = claude_command(cfg, Job('k', 'p', resume='S1'))
        self.assertNotIn('--fork-session', plain)
        forked = claude_command(cfg, Job('k', 'p', resume='S1', fork=True))
        self.assertEqual(forked[-3:], ['--resume', 'S1', '--fork-session'])


class CompanionModules(unittest.TestCase):
    """Import every module: a syntax error here would not fail any other test."""

    def test_all_modules_import(self):
        import importlib
        for name in ('companion.app', 'companion.agents', 'companion.capture', 'companion.native',
                     'companion.notifications', 'companion.protocol', 'companion.sessions',
                     'companion.wow', 'companion.launching', 'tools.install_addon', 'tools.reset_bank'):
            self.assertTrue(importlib.import_module(name))

    def test_saved_reply_file(self):
        from companion.app import App
        with tempfile.TemporaryDirectory() as tmp:
            fake = type('S', (), {'args': type('A', (), {'state': Path(tmp)})()})()
            path = App.save_reply(fake, 'abc:1', 'done', 'why is the sky blue?', 'Rayleigh scattering.')
            body = path.read_text(encoding='utf-8')
            self.assertIn('why is the sky blue?', body)
            self.assertIn('Rayleigh scattering.', body)
            self.assertTrue(path.name.endswith('abc-1.md'))
