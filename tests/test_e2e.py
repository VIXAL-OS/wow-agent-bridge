"""Addon Lua + companion publisher, end to end, under a pessimistic client model."""
import tempfile
import unittest

from tests.harness import Sim

LONG = '\n'.join(f'Line {i:03}: frost wyrms, Naxxramas and ☃ snowmen — 冰冠城塞.' for i in range(1, 220))


def agent(reply):
    """Scripted agent: queued, working, a streaming preview, then the final reply."""
    def run(prompt, elapsed):
        if elapsed < 2:
            return 'queued', ''
        if elapsed < 6:
            return 'working', ''
        if elapsed < 12:
            return 'streaming', reply(prompt)[:1500]
        return 'done', reply(prompt)
    return run


class EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tmp.cleanup()

    def sim(self, reply=lambda p: 'Echo: ' + p, **kw):
        return Sim(self.tmp.name, agent(reply), **kw)

    def test_self_test_picks_a_size_under_pixel_rounding(self):
        sim = self.sim()
        sim.run(3)
        self.assertTrue(sim.ns.selfTest.ok)
        self.assertEqual(sim.status().calib, 64)
        # The client caps the rasterised em, so nibble steps must stay pixels apart.
        best = list(sim.ns.selfTest.results.values())[0]
        self.assertGreater(best.gap * 1080 / 768, 2.0)

    def test_single_reply_round_trip(self):
        sim = self.sim()
        sim.run(2)
        sim.send('What drops Invincible\'s Reins?')
        self.assertTrue(sim.run(60, until=lambda: sim.last_reply() is not None))
        self.assertEqual(sim.last_reply(), "Echo: What drops Invincible's Reins?")
        self.assertIn("Echo: What drops Invincible's Reins?", sim.body())
        sim.run(2)
        self.assertIsNone(sim.g.STUB.strip(), 'strip should hide once the reply is complete')
        self.assertFalse(sim.status().active)
        self.assertEqual(list(sim.g.STUB.sounds.values()), [b'TellMessage'])

    def test_multipart_utf8_reply(self):
        sim = self.sim(reply=lambda p: LONG)
        sim.run(2)
        sim.send('long please')
        self.assertTrue(sim.run(120, until=lambda: sim.last_reply() is not None))
        self.assertEqual(sim.last_reply(), LONG)
        self.assertGreater(len(LONG.encode()), 2 * 4060)

    def test_consecutive_prompts_never_reload_a_slot(self):
        sim = self.sim()
        sim.run(2)
        for n in range(3):
            sim.send(f'prompt {n}')
            self.assertTrue(sim.run(60, until=lambda: sim.last_reply() == f'Echo: prompt {n}'), n)
        loads = [name for name in sim.client.loads if name.startswith('reply')]
        self.assertEqual(len(loads), len(set(loads)))

    def test_companion_starts_late(self):
        sim = self.sim()
        sim.run(2)
        sim.companion_on = False
        sim.send('are you there?')
        sim.run(25)
        self.assertIsNone(sim.last_reply())
        self.assertTrue(sim.status().active, 'receiver keeps retrying with backoff')
        sim.companion_on = True
        self.assertTrue(sim.run(90, until=lambda: sim.last_reply() == 'Echo: are you there?'))

    def test_reload_mid_request_skips_used_slots(self):
        sim = self.sim()
        sim.run(2)
        sim.send('first')
        sim.run(8)
        used = sim.status().slot
        sim.reload()
        sim.run(2)
        self.assertGreaterEqual(sim.status().slot, used)
        sim.send('second')
        self.assertTrue(sim.run(60, until=lambda: sim.last_reply() == 'Echo: second'))

    def test_window_resize_recalibrates(self):
        sim = self.sim()
        sim.run(2)
        sim.client.scale = 1440 / 768
        sim.send('after resize')
        self.assertTrue(sim.run(60, until=lambda: sim.last_reply() == 'Echo: after resize'))

    def test_new_epoch_recycles_bank(self):
        sim = self.sim(saved={'nextSlot': 50, 'epoch': '1'})
        self.assertEqual(sim.status().slot, 50)
        # The companion rewrote Epoch.lua while the game was closed: new process.
        (sim.addon / 'Epoch.lua').write_text('AgentBridgeEpoch = "2"\n')
        sim.client.cache.clear()
        sim.boot(sim.saved())
        self.assertEqual(sim.status().slot, 1)
        sim.run(2)
        sim.send('recycled')
        self.assertTrue(sim.run(60, until=lambda: sim.last_reply() == 'Echo: recycled'))
        # Same epoch on a later /reload: no reset.
        slot = sim.status().slot
        sim.reload()
        self.assertEqual(sim.status().slot, slot)

    def test_exact_renderer_also_works(self):
        sim = self.sim(scale=1.0, rounding=False)
        sim.run(2)
        sim.send('exact')
        self.assertTrue(sim.run(60, until=lambda: sim.last_reply() == 'Echo: exact'))


if __name__ == '__main__':
    unittest.main()
