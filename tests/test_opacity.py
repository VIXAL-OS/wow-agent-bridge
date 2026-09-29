"""Adaptive opacity exercised through real Lua, pixel decoding and font replies."""
import tempfile
import unittest

from PIL import ImageOps

from companion.protocol import decode_image, frame_kind, render_frame
from tests.harness import Sim
from tests.test_character import FIXTURES
from tests.test_e2e import agent
from tools.check_strip_opacity import measure


class Opacity(unittest.TestCase):
    def sim(self, reply=None, preferred=.2):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        sim = Sim(tmp.name, reply or agent(lambda p: 'Echo: ' + p), saved={'alpha': preferred}, slots=160)
        self.addCleanup(sim.browser.db.close)
        sim.run(3)
        return sim

    def test_decoder_matrix_handles_edges_at_sixty_percent(self):
        for row in measure():
            self.assertEqual(row['wrong'], 0, row)
            if row['alpha'] >= .6 or row['background'] == 'flat':
                self.assertEqual(row['rejected'], 0, row)
            elif row['background'] == 'opposing cells':
                self.assertGreater(row['rejected'], 0, row)

    def test_faint_prompt_recovers_over_opposing_background_and_restores(self):
        sim = self.sim()
        seen = set()
        def optical(data, alpha):
            seen.add(alpha)
            image = render_frame(data, alpha=alpha, background=ImageOps.invert(render_frame(data)))
            try:
                return decode_image(image.resize((720, 45)))
            except ValueError:
                return None
        sim.optical_filter = optical
        sim.send('faint')
        self.assertTrue(sim.run(120, until=lambda: sim.last_reply() == 'Echo: faint'))
        self.assertIn(.2, seen)
        self.assertIn(1, seen)
        sim.run(.3)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.2, .2))
        self.assertEqual(sim.saved()['alpha'], .2)
        self.assertIsNone(sim.g.STUB.strip())

    def test_character_uploads_boost_then_follow_changed_preference(self):
        sim = self.sim()
        sim.lua.execute(FIXTURES.encode())
        sim.g.STUB.recipeCount = 210
        sim.g.STUB.fire(b'TRADE_SKILL_SHOW')
        sim.run(6)
        alphas = []
        def optical(data, alpha):
            if frame_kind(data) == 'character':
                alphas.append(alpha)
                if len(alphas) == 1:
                    sim.ns.SetStripAlpha(.4)
                image = render_frame(data, alpha=alpha, background=ImageOps.invert(render_frame(data)))
                return decode_image(image.resize((720, 45)))
            return data
        sim.optical_filter = optical
        sim.send('recipes')
        self.assertTrue(sim.run(400, until=lambda: sim.last_reply() == 'Echo: recipes'))
        self.assertTrue(alphas)
        self.assertEqual(min(alphas), .6)
        sim.run(.3)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.4, .4))
        self.assertEqual(sim.saved()['alpha'], .4)

    def test_missing_prompt_fragments_boost_even_with_healthy_control(self):
        sim = self.sim()
        alphas = []
        def optical(data, alpha):
            if frame_kind(data) == 'prompt':
                alphas.append(alpha)
                if alpha < 1:
                    return None
            return data
        sim.optical_filter = optical
        sim.send('lost fragments')
        sim.run(10)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.2, .2))
        self.assertFalse(sim.jobs)
        self.assertTrue(sim.run(90, until=lambda: sim.last_reply() == 'Echo: lost fragments'))
        self.assertIn(1, alphas)
        sim.run(.3)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.2, .2))

    def test_finishing_one_request_does_not_clear_another_recovery(self):
        sim = self.sim()
        sim.companion_on = False
        sim.ns.BeginRequest(998, None, None, 30)
        sim.ns.BeginRequest(999, None, None, 30)
        sim.ns.StripStalled(998)
        sim.ns.StripStalled(999)
        sim.ns.AckPrompt(998)
        sim.ns.ForgetRequest(998)
        sim.run(.3)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.2, 1))
        sim.ns.AckPrompt(999)
        sim.ns.ForgetRequest(999)
        sim.run(.3)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.2, .2))

    def test_lost_control_after_ack_recovers_without_slow_agent_boost(self):
        sim = self.sim(lambda p, t: ('working', 'thinking'))
        sim.send('slow agent')
        sim.run(20)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.2, .2))
        sim.optical_filter = lambda data, alpha: data if alpha == 1 else None
        self.assertTrue(sim.run(40, until=lambda: sim.ns.StripAlphaInfo()[1] == 1))
        self.assertTrue(sim.run(50, until=lambda: sim.ns.StripAlphaInfo()[1] == .2))
        sim.optical_filter = None
        sim.agent = lambda p, t: ('done', 'finished')
        self.assertTrue(sim.run(60, until=lambda: sim.last_reply() == 'finished'))

    def test_cancel_and_timeout_release_upload_boost(self):
        for mode in ('cancel', 'timeout'):
            with self.subTest(mode=mode):
                sim = self.sim()
                sim.companion_on = False
                self.assertTrue(sim.ns.BeginRequest(999, None, None, 2))
                sim.ns.QueuePrompt(999, sim.ns.EncodeCharacter(b'test', sim.ns.session, 999), True)
                sim.run(.3)
                self.assertEqual(sim.ns.StripAlphaInfo(), (.2, .6))
                if mode == 'cancel':
                    sim.ns.AckPrompt(999)
                    sim.ns.ForgetRequest(999)
                sim.run(3)
                self.assertEqual(sim.ns.StripAlphaInfo(), (.2, .2))
                self.assertFalse(sim.status().active)

    def test_higher_preference_and_reload_are_preserved(self):
        sim = self.sim(preferred=.8)
        sim.ns.QueuePrompt(999, sim.ns.EncodeCharacter(b'test', sim.ns.session, 999), True)
        sim.run(.3)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.8, .8))
        sim.ns.StripStalled(999)
        sim.run(.3)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.8, 1))
        sim.reload()
        sim.run(.3)
        self.assertEqual(sim.ns.StripAlphaInfo(), (.8, .8))
        self.assertEqual(sim.saved()['alpha'], .8)


if __name__ == '__main__':
    unittest.main()
