"""Character sync survives a companion that loses sight of the strip, and says why when it gives up."""
import tempfile
import unittest

from lupa.lua51 import LuaRuntime
from PIL import Image

from companion.app import BLIND
from companion.capture import FrozenWatch
from companion.native import NativeBridge, font_path
from companion.protocol import (encode_control, make_reply_packet, make_status_packet, parse_control,
                                parse_reply_packet)
from tests.harness import ROOT, Sim, install_small, packet_from_font
from tests.test_character import FIXTURES
from tests.test_e2e import agent


class OutOfSight(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = Sim(tmp.name, agent(lambda p: 'Echo: ' + p), slots=400)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute(FIXTURES.encode())
        self.sim.run(3)
        self.stored, self.controls = [], []
        accept = self.sim.character.accept
        def record(key, blob):
            if not self.sim.character.get(key):
                self.stored.append(self.sim.t)
            return accept(key, blob)
        self.sim.character.accept = record
        publish = self.sim.native.accept
        def announce(control, snapshot):
            self.controls.append((self.sim.t, control.request))
            return publish(control, snapshot)
        self.sim.native.accept = announce

    def messages(self):
        return '\n'.join(bytes(v).decode() for v in self.sim.g.STUB.prints.values())

    def history(self):
        return [bytes(e.r).decode() for e in self.sim.g.AgentBridgeState.history.values()]

    def test_lost_sight_after_pages_arrive_waits_reports_why_and_resumes(self):
        # The incidents of 2026-10-18..21: every page reached the companion, then the
        # confirmation never came back for minutes and the whole prompt was dropped.
        # Out of sight from the first stored page until well past the point where the old addon gave up:
        # until the upload has been told to keep waiting, and a while after.
        blind = {'from': None, 'until': None, 'notes': 0, 'printed': 0}
        def sight(data, _opacity):
            if blind['from'] is None and self.stored:
                blind['from'], blind['until'] = self.sim.t, self.sim.t + 1500
            prints = self.sim.g.STUB.prints
            if len(prints) != blind['printed']:
                blind['printed'] = len(prints)
                if 'still waiting' in bytes(prints[len(prints)]).decode() and blind['until'] > self.sim.t + 30:
                    blind['until'] = self.sim.t + 30
            if blind['from'] is not None and self.sim.t < blind['until']:
                # The companion sees nothing, knows why, and says so in the slots read next.
                blind['notes'] += self.sim.native.blind('frozen', BLIND['frozen'])
                return None
            return data
        self.sim.optical_filter = sight
        self.sim.send('inventory question')
        self.assertTrue(self.sim.run(2500, until=lambda: self.sim.last_reply() == 'Echo: inventory question'),
                        self.messages())
        self.assertGreater(blind['until'] - blind['from'], 240, 'out of sight longer than the old timeout')
        self.assertGreater(blind['notes'], 0)
        self.assertIn('Character sync is still waiting', self.messages())
        self.assertIn('The companion cannot see the strip: WoW is covered by another window', self.messages())
        self.assertFalse(any('Character sync failed' in r for r in self.history()), self.history())
        self.assertEqual(len(self.sim.jobs), 1)

    def test_prompt_is_not_read_while_its_uploads_run(self):
        self.sim.send('inventory question')
        self.assertTrue(self.sim.run(600, until=lambda: self.sim.last_reply() == 'Echo: inventory question'))
        self.assertGreaterEqual(len(self.stored), 2)
        first, last = self.stored[0], self.stored[-1]
        during = [request for t, request in self.controls if first + 1 < t < last - 1]
        self.assertTrue(during, 'uploads were confirmed through announced slots')
        self.assertNotIn(1, during, 'the held prompt took no slot announcement from its uploads')

    def test_silent_companion_fails_with_the_reason(self):
        self.sim.companion_on = False
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.assertTrue(self.sim.run(600, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        self.assertIn('Character sync failed: The companion never answered.', self.messages())
        self.assertIn('snapshots already uploaded are kept', self.messages())


class LockstepCapture(unittest.TestCase):
    def test_capture_in_step_with_the_strip_still_sees_every_fragment(self):
        # A capture loop that samples every 2nd or 3rd strip step in lockstep (the companion polls
        # every 0.3 s while it sees no strip) used to miss fragments forever for 3k+1 of them.
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sim = Sim(tmp.name, agent(lambda p: 'x'))
        self.addCleanup(sim.browser.db.close)
        sim.run(2)
        request = 9000
        for count in (10, 13, 14, 15, 21, 22, 35):
            for every in (2, 3):
                request += 1
                frames = {i + 1: bytes([i + 1]) * 64 for i in range(count)}
                sim.ns.QueuePrompt(request, sim.lua.table_from(frames), False)
                seen = set()
                for step in range(count * 12):
                    sim.t += .15
                    sim.g.STUB.update(.15)  # one strip step
                    data = sim.g.STUB.strip() if step % every == 0 else None
                    if data and bytes(data) in frames.values():
                        seen.add(bytes(data))
                sim.ns.AckPrompt(request)
                self.assertEqual(len(seen), count, f'{count} fragments, every {every} steps')


class StatusNotes(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.addon = install_small(tmp.name, slots=40)
        self.clock = [100.0]
        self.bridge = NativeBridge(self.addon, clock=lambda: self.clock[0], wall=lambda: 5000 + self.clock[0])
        self.session = b'abcdefgh'

    def control(self, slot, ms=5000, active=True):
        return parse_control(encode_control(self.session, slot, ms, 1, active, 7))

    def note(self, slot):
        try:
            packet = parse_reply_packet(packet_from_font(font_path(self.addon, slot)))
        except ValueError:
            return None
        return packet if packet['state'] == 8 else None

    def test_written_after_the_answered_slot_and_only_while_receiving(self):
        self.assertEqual(self.bridge.blind('minimized', 'm'), 0, 'no addon seen yet')
        self.bridge.accept(self.control(5, active=False), {})
        self.assertEqual(self.bridge.blind('minimized', 'm'), 0, 'the addon was idle')
        self.assertTrue(self.bridge.accept(self.control(6), {'id': self.session.hex() + ':7', 'state': 'done', 'reply': 'ok'}))
        self.assertEqual(self.bridge.blind('minimized', 'WoW is minimized.'), 4)
        self.assertIsNone(self.note(6), 'a reply already written for the announced slot stays')
        notes = [self.note(slot) for slot in range(7, 11)]
        self.assertTrue(all(notes))
        self.assertEqual(notes[0]['request'], 0)
        self.assertEqual(notes[0]['text'], b'\x01status=minimized\nat=5100\x02WoW is minimized.')
        self.assertIsNone(self.note(11))
        # Nothing to do until it is time to refresh or to reach further ahead.
        self.clock[0] += 10
        self.assertEqual(self.bridge.blind('minimized', 'WoW is minimized.'), 0)
        self.clock[0] += 25
        self.assertEqual(self.bridge.blind('minimized', 'WoW is minimized.'), 1)
        self.assertTrue(self.note(11))
        self.clock[0] += 15
        self.assertEqual(self.bridge.blind('minimized', 'WoW is minimized.'), 5, 'a refresh rewrites every note')
        self.assertEqual(self.note(7)['text'], b'\x01status=minimized\nat=5150\x02WoW is minimized.')
        # A different cause is written at once; sight regained ends the outage.
        self.assertEqual(self.bridge.blind('paused', 'Capture is paused.'), 5)
        self.bridge.seen()
        self.assertTrue(self.bridge.accept(self.control(9), {'id': self.session.hex() + ':7', 'state': 'done', 'reply': 'ok'}))
        self.assertIsNone(self.note(9), 'a real reply replaces a note')
        self.clock[0] += self.bridge.STATUS_WINDOW + 1
        self.assertEqual(self.bridge.blind('paused', 'Capture is paused.'), 0, 'not for an addon idle this long')

    def test_unannounced_slot_gets_a_note_too(self):
        self.bridge.accept(self.control(6, ms=500), {})  # too late to write: slot 6 stays empty
        self.assertEqual(self.bridge.blind('frozen', 'f'), 4)
        self.assertTrue(self.note(6))

    def test_packet_shape(self):
        packet = parse_reply_packet(make_status_packet('6162636465666768', 3, 'paused', 'p', 42))
        self.assertEqual((packet['state'], packet['request'], packet['slot'], packet['part'], packet['total']),
                         (8, 0, 3, 1, 1))
        with self.assertRaises(ValueError):
            make_status_packet('6162636465666768', 3, 'bogus', 'p', 42)
        with self.assertRaises(ValueError):
            parse_reply_packet(make_reply_packet('6162636465666768', 5, 3, 1, 8, b'x'))


class AddonParsesNotes(unittest.TestCase):
    def setUp(self):
        self.lua = LuaRuntime(encoding=None, unpack_returned_tuples=True)
        self.lua.execute(b'AgentBridge = {}; CreateFrame = function() return setmetatable({}, {__index = function() return function() end end}) end')
        for name in ('Core.lua', 'Protocol.lua'):
            self.lua.execute((ROOT / 'addon' / 'AgentBridge' / name).read_bytes())
        self.ns = self.lua.globals().AgentBridge
        self.session = b'abcdefgh'

    def test_a_note_answers_any_request_but_only_its_slot_and_session(self):
        packet = make_status_packet(self.session.hex(), 9, 'minimized', 'WoW is minimized.', 1234)
        parsed = self.ns.ParseReply(packet, self.session, 77, 9)
        self.assertEqual(parsed[b'state'], 8)
        code, at, message = self.ns.ParseStatus(parsed[b'text'])
        self.assertEqual((code, at, message), (b'minimized', 1234, b'WoW is minimized.'))
        self.assertEqual(self.ns.PeekReply(packet[:32], self.session, 3, 9)[b'state'], 8)
        self.assertEqual(self.ns.ParseReply(packet, self.session, 77, 10)[1], b'Stale reply packet')
        self.assertEqual(self.ns.ParseReply(packet, b'zzzzzzzz', 77, 9)[1], b'Stale reply packet')
        # A request-0 packet that is not a status is never anyone's reply.
        other = make_reply_packet(self.session.hex(), 0, 9, 1, 4, b'ABCTX_OK')
        self.assertEqual(self.ns.ParseReply(other, self.session, 77, 9)[1], b'Stale reply packet')
        self.assertIsNone(self.ns.ParseStatus(b'no header'))


class Frozen(unittest.TestCase):
    def test_black_or_unchanging_capture_is_frozen_live_is_not(self):
        clock = [0.0]
        watch = FrozenWatch(clock=lambda: clock[0], hold=3)
        self.assertTrue(watch.frozen(Image.new('RGB', (1920, 1080))))
        scene = Image.new('RGB', (1920, 1080), (90, 120, 60))
        self.assertFalse(watch.frozen(scene))
        clock[0] = 2
        self.assertFalse(watch.frozen(scene))
        clock[0] = 3.5
        self.assertTrue(watch.frozen(scene))
        moved = scene.copy(); moved.paste((200, 30, 30), (0, 0, 400, 400))
        self.assertFalse(watch.frozen(moved))


if __name__ == '__main__':
    unittest.main()
