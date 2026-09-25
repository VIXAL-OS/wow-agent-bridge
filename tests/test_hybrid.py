"""Exercise hostile payloads, slot lifecycle and fallback with production Lua."""
from io import BytesIO
import tempfile
import unittest
from unittest.mock import patch

from fontTools.ttLib import TTFont
from companion.hybrid import install_slots, lua_file, payload, slot_path, validate_source
from companion.native import make_font, NativeBridge, atomic_write
from companion.protocol import REPLY_SIZE, encode_control, parse_control, parse_reply_packet
from tests.harness import Sim, install_small, packet_from_font
from tests.test_e2e import agent

HOSTILE = ('"; error("executed"); -- ]] [=[ \\n \\000 \x00 |cffff0000 café 雪☃\n' * 750)


class Hybrid(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def sim(self, reply=HOSTILE):
        sim = Sim(self.tmp.name, agent(lambda p: reply), hybrid=True)
        sim.run(3)
        return sim

    def finish(self, sim, prompt='long please', expected=HOSTILE):
        sim.send(prompt)
        self.assertTrue(sim.run(150, until=lambda: sim.last_reply() == expected))

    def test_hostile_text_round_trip_and_metadata(self):
        sim = self.sim()
        self.finish(sim)
        self.assertEqual(len(sim.hybrid_loads), 1)
        self.assertEqual(bytes(sim.ns.ChatAgent(sim.g.AgentBridgeState.chat)[0]), b'mock')
        self.assertIsNone(sim.g.AgentBridgeHybridData)

    def test_short_reply_uses_only_fonts(self):
        sim = self.sim('short')
        self.finish(sim, expected='short')
        self.assertEqual(sim.hybrid_loads, [])

    def test_bad_payload_falls_back_without_displaying_it(self):
        sim = self.sim()
        sim.hybrid_transform = lambda source: lua_file(b'bad payload')
        self.finish(sim)
        self.assertEqual(len(sim.hybrid_loads), 1)
        self.assertIn(b'disabled after error', sim.ns.HybridInfo())

    def test_checksum_and_request_mismatch_fall_back(self):
        for change in ('checksum', 'request'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as game:
                sim = Sim(game, agent(lambda p: HOSTILE), hybrid=True)
                def corrupt(source):
                    data = bytearray(validate_source(source))
                    data[-1 if change == 'checksum' else 15] ^= 1
                    return lua_file(data)
                sim.hybrid_transform = corrupt
                self.finish(sim)
                self.assertIn(b'disabled after error', sim.ns.HybridInfo())

    def test_reload_reuses_lua_slot_but_not_font_path(self):
        sim = self.sim()
        self.finish(sim)
        slot = sim.status().slot
        sim.reload()
        sim.run(3)
        self.finish(sim, prompt='again')
        # last_reply already held the old response: wait for this request too.
        sim.run(60, until=lambda: not sim.status().active)
        self.assertEqual(sim.hybrid_loads, [b'AgentBridgeReply01', b'AgentBridgeReply01'])
        self.assertGreater(sim.status().slot, slot)
        fonts = [f for f in sim.client.loads if f.startswith('reply')]
        self.assertEqual(len(fonts), len(set(fonts)))

    def test_combat_postpones_the_load_and_keeps_the_fast_path(self):
        sim = self.sim()
        sim.agent = agent(lambda prompt: prompt + HOSTILE)
        blank = lua_file(b'')
        fonts = lambda: len([f for f in sim.client.loads if f.startswith('reply')])
        sim.send('first')
        self.assertTrue(sim.run(150, until=lambda: slot_path(sim.addon, 1).read_bytes() != blank))
        sim.g.STUB.combat = True  # the fight starts before the addon reads the announcement
        sim.run(5)
        held = fonts()
        sim.run(20)
        self.assertEqual(sim.hybrid_loads, [], 'nothing is loaded in combat')
        self.assertIsNone(sim.last_reply())
        self.assertEqual(fonts(), held, 'no font slots are spent while it waits')
        self.assertIn(b'loads wait for combat to end', sim.ns.HybridInfo())
        sim.g.STUB.combat = False
        self.assertTrue(sim.run(10, until=lambda: sim.last_reply() == 'first' + HOSTILE))
        self.assertEqual(sim.hybrid_loads, [b'AgentBridgeReply01'])
        # Combat was not an error: the next long reply still takes the fast path.
        sim.send('second')
        self.assertTrue(sim.run(150, until=lambda: sim.last_reply() == 'second' + HOSTILE))
        self.assertEqual(sim.hybrid_loads, [b'AgentBridgeReply01', b'AgentBridgeReply02'])

    def test_exhausted_pool_falls_back(self):
        sim = self.sim()
        sim.hybrid_loaded.update(sim.hybrid_installed)
        self.finish(sim)
        self.assertEqual(sim.hybrid_loads, [])

    def test_parallel_chats_receive_their_own_payload(self):
        sim = self.sim()
        sim.agent = agent(lambda prompt: prompt + HOSTILE)
        first = sim.g.AgentBridgeState.chat
        sim.send('first')
        second = sim.ns.NewChat(b'Second').id
        sim.send('second')
        self.assertTrue(sim.run(150, until=lambda: len(sim.replies(first)) == len(sim.replies(second)) == 1))
        self.assertEqual(sim.replies(first), ['first' + HOSTILE])
        self.assertEqual(sim.replies(second), ['second' + HOSTILE])
        self.assertEqual(len(set(sim.hybrid_loads)), 2)

    def test_fixed_source_template_rejects_code(self):
        _, source = payload('6162636465666768', 1, 1, 4, HOSTILE.encode())
        self.assertTrue(validate_source(source).endswith(HOSTILE.encode()))
        for bad in (source + b'error("oops")', b'AgentBridgeHybridData = "0"\n',
                    b'AgentBridgeHybridData = "zz"\n', b'AgentBridgeHybridData = "\\"; f()"\n'):
            with self.assertRaises(ValueError):
                validate_source(bad)

    def test_installer_preserves_existing_payload(self):
        addon = install_small(self.tmp.name)
        self.assertEqual(install_slots(addon), 32)
        path = slot_path(addon, 1)
        source = lua_file(b'kept')
        path.write_bytes(source)
        self.assertEqual(install_slots(addon), 0)
        self.assertEqual(path.read_bytes(), source)

    def test_protocol_backwards_compatible(self):
        self.assertEqual(parse_control(encode_control()).hybrid_slot, 0)
        self.assertEqual(parse_control(encode_control(hybrid_slot=16)).hybrid_slot, 16)
        with self.assertRaises(ValueError):
            encode_control(hybrid_slot=17)

    def test_locked_payload_file_uses_fonts(self):
        addon = install_small(self.tmp.name)
        install_slots(addon)
        native = NativeBridge(addon, clock=lambda: 0)
        control = parse_control(encode_control(hybrid_slot=1))
        def write(path, source):
            if path.suffix == '.lua':
                raise PermissionError('locked for test')
            return atomic_write(path, source)
        with patch('companion.native.atomic_write', write):
            self.assertTrue(native.accept(control, {'id': control.key, 'state': 'done', 'reply': HOSTILE}))
        packet = parse_reply_packet(packet_from_font(addon / 'reply00001.ttf'))
        self.assertEqual(packet['state'], 4)
        self.assertGreater(packet['total'], 1)

    def test_lua_v4_control_matches_python_and_bad_descriptor_is_rejected(self):
        sim = self.sim()
        self.assertEqual(bytes(sim.ns.EncodeControl(b'abcdefgh', 5, 9000, 1, True, 3, 2)),
                         encode_control(b'abcdefgh', 5, 9000, 1, True, 3, hybrid_slot=2))
        self.assertIsNone(sim.ns.LoadHybrid(b'bad', b'abcdefgh', 1)[0])
        self.assertIsNone(sim.ns.ClaimHybrid(b'bad')[0])
        self.assertEqual(sim.hybrid_loads, [])

    def test_two_replies_held_in_combat_keep_their_own_slots(self):
        sim = self.sim()
        sim.agent = agent(lambda prompt: prompt + HOSTILE)
        sim.g.STUB.combat = True
        first = sim.g.AgentBridgeState.chat
        sim.send('first')
        second = sim.ns.NewChat(b'Second').id
        sim.send('second')
        sim.run(90)
        self.assertEqual((sim.hybrid_loads, sim.replies(first), sim.replies(second)), ([], [], []))
        sim.g.STUB.combat = False
        self.assertTrue(sim.run(10, until=lambda: sim.replies(first) and sim.replies(second)))
        self.assertEqual(sim.replies(first), ['first' + HOSTILE])
        self.assertEqual(sim.replies(second), ['second' + HOSTILE])
        self.assertEqual(sorted(sim.hybrid_loads), [b'AgentBridgeReply01', b'AgentBridgeReply02'])

    def test_perf_records_timings_without_resetting_shared_clock(self):
        sim = self.sim('short')
        sim.lua.execute(b"""
            STUB.clock = 0
            debugprofilestop = function() STUB.clock = STUB.clock + .1; return STUB.clock end
            debugprofilestart = function() error('must never reset the shared timer') end
        """)
        # Profile availability is captured when each module loads.
        sim.lua.execute((sim.addon / 'Perf.lua').read_bytes())
        sim.g.SlashCmdList.AGENTBRIDGE(b'perf on')
        self.finish(sim, expected='short')
        sim.g.SlashCmdList.AGENTBRIDGE(b'perf')
        report = b'\n'.join(sim.g.STUB.prints.values())
        self.assertIn(b'font-load:', report)
        self.assertIn(b'strip-paint:', report)
        self.assertIn(b'worst interval', report)
        sim.g.SlashCmdList.AGENTBRIDGE(b'perf off')

    def test_compact_font_all_bytes_and_live_metrics(self):
        data = bytes(range(256)) * (REPLY_SIZE // 256)
        compact = make_font(data)
        self.assertLess(len(compact), 50000)
        font = TTFont(BytesIO(compact))
        self.assertLess(font['maxp'].numGlyphs, 128)
        self.assertEqual(packet_from_font(compact), data)
        sim = Sim(self.tmp.name, agent(lambda p: 'font test'))
        sim.run(3)
        self.assertTrue(sim.ns.selfTest.ok)

    def test_hidden_panel_skips_layout_and_strip_updates_few_cells(self):
        sim = self.sim('short')
        sim.lua.execute(b"""
            STUB.layouts, STUB.paints = 0, 0
            local render = AgentBridge.RenderTranscript
            AgentBridge.RenderTranscript = function(...)
                STUB.layouts = STUB.layouts + 1
                return render(...)
            end
            for _, cell in ipairs(AgentBridgeStrip.textures) do
                local paint = cell.SetTexture
                cell.SetTexture = function(...)
                    STUB.paints = STUB.paints + 1
                    return paint(...)
                end
            end
        """)
        self.finish(sim, expected='short')
        self.assertEqual(sim.g.STUB.layouts, 0)
        self.assertGreater(sim.g.STUB.paints, 0)
        # Once prompts are acknowledged, the control countdown changes only
        # a few bytes. It must not repaint all 1,024 textures on each tick.
        sim.agent = lambda prompt, elapsed: ('working', 'unchanged')
        sim.send('working')
        sim.run(15)
        sim.g.STUB.paints = 0
        sim.run(.2)
        self.assertLess(sim.g.STUB.paints, 256)
        sim.g.AgentBridgePanel.Show(sim.g.AgentBridgePanel)
        sim.g.AgentBridgePanel.scripts[b'OnShow'](sim.g.AgentBridgePanel)
        self.assertIn('short', sim.body())
