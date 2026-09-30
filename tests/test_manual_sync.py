"""Manual scans and uploads through the production Lua 5.1 optical transport."""
import json
import tempfile
import unittest

from tests.harness import Sim
from tests.test_character import FIXTURES
from tests.test_e2e import agent
from tests.test_pet_context import PETS


class ManualSync(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = Sim(tmp.name, agent(lambda p: 'Echo: ' + p), slots=240)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute(FIXTURES.encode())
        self.sim.lua.execute(PETS.encode())
        self.sim.run(3)

    def document(self, section):
        bundle = self.sim.ns.CharacterFields(self.sim.lua.table())
        doc = bundle[b'docs'][section.encode()]
        return json.loads(doc[b'body']), doc

    def messages(self):
        return '\n'.join(v.decode() for v in self.sim.g.STUB.prints.values())

    def sync(self):
        ok, message = self.sim.ns.SyncCharacter()
        self.assertTrue(ok, message)
        self.assertTrue(self.sim.run(600, until=lambda: not self.sim.ns.CharacterSyncBusy()), self.messages())
        self.assertIn('Character sync complete:', self.messages())

    def test_button_rescans_and_uploads_all_sections_without_agent(self):
        self.sim.g.STUB.recipeCount = 210
        self.sim.g.STUB.fire(b'TRADE_SKILL_SHOW')
        self.sim.g.STUB.stableOpen = True
        self.sim.g.STUB.fire(b'PET_STABLE_SHOW')
        # No BAG_UPDATE: the button must really rescan, not just send old data.
        self.sim.g.STUB.bagCount = 77
        self.sim.ns.SyncButton.scripts[b'OnClick']()
        self.assertFalse(self.sim.ns.SyncButton.IsEnabled(self.sim.ns.SyncButton))
        self.assertTrue(self.sim.run(600, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        self.assertIn('7 snapshots uploaded', self.messages())
        self.assertEqual(self.sim.jobs, {})
        for section in ('gear', 'bags', 'recipes:Alchemy', 'pets', 'mounts', 'combatpet', 'stablepets'):
            body, doc = self.document(section)
            stored = self.sim.character.body('ChromieCraft:Testbrew', section, doc[b'revision'].decode())
            self.assertEqual(json.loads(stored), body)
        self.assertEqual(self.document('bags')[0][4][0][3], '77')
        self.assertEqual(len(self.document('recipes:Alchemy')[0][4]), 210)
        self.assertTrue(self.sim.ns.SyncButton.IsEnabled(self.sim.ns.SyncButton))
        self.sim.run(.3)  # Allow the already painted final packet to clear.
        frames = self.sim.character_frames
        self.sim.send('fresh recipes')
        self.assertTrue(self.sim.run(90, until=lambda: self.sim.last_reply() == 'Echo: fresh recipes'))
        self.assertEqual(frames, self.sim.character_frames, 'next prompt reuses the manual upload')

    def test_duplicate_sync_and_send_keep_draft_and_do_not_launch_agent(self):
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'sync')
        self.assertTrue(self.sim.ns.CharacterSyncBusy())
        self.assertFalse(self.sim.ns.SyncCharacter()[0])
        self.sim.send('keep my draft')
        self.assertEqual(self.sim.g.AgentBridgeInput.text, b'keep my draft')
        self.assertTrue(self.sim.run(300, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        self.assertEqual(self.sim.jobs, {})

    def test_sync_off_and_cancellation_stop_upload_and_allow_retry(self):
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context off')
        self.assertFalse(self.sim.ns.SyncCharacter()[0])
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context on')
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.sim.run(3)
        self.assertTrue(self.sim.ns.CharacterSyncBusy())
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context off')
        # Allow the already painted strip to clear before counting new frames.
        self.sim.run(1)
        frames = self.sim.character_frames
        self.sim.run(5)
        self.assertFalse(self.sim.ns.CharacterSyncBusy())
        self.assertEqual(frames, self.sim.character_frames)
        self.assertEqual(self.sim.jobs, {})
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context on')
        self.sync()

    def test_combat_defers_scanning_and_closed_profession_aborts(self):
        self.sim.lua.execute(b'function InCombatLockdown() return STUB.combat end; STUB.combat = true')
        reads = self.sim.g.STUB.gearReads
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.sim.run(65)
        self.assertTrue(self.sim.ns.CharacterSyncBusy())
        self.assertEqual(self.sim.g.STUB.gearReads, reads)
        self.sim.g.STUB.combat = False
        self.assertTrue(self.sim.run(300, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        self.sim.g.STUB.fire(b'TRADE_SKILL_SHOW')
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.sim.g.STUB.fire(b'TRADE_SKILL_CLOSE')
        self.sim.run(1)
        self.assertFalse(self.sim.ns.CharacterSyncBusy())
        self.assertIn('closed or changed', self.messages())

    def test_filtered_then_complete_then_cached_reports_actual_coverage(self):
        self.sim.g.STUB.filtered = True
        self.sim.g.STUB.fire(b'TRADE_SKILL_SHOW'); self.sim.run(3)
        self.assertIn('category filter', self.sim.ns.CharacterStatus().decode())
        self.sim.g.STUB.filtered = False
        # Manual rescan repairs the old observation even without a filter event.
        self.sync()
        body, doc = self.document('recipes:Alchemy')
        self.assertEqual(body[2], 'complete')
        self.assertNotIn('category filter', body[3])
        self.sim.g.STUB.fire(b'TRADE_SKILL_CLOSE')
        self.assertIn('complete, cached', self.sim.ns.CharacterStatus().decode())
        fields = self.sim.lua.table()
        self.sim.ns.CharacterFields(fields)
        context = '\n'.join(v[2].decode() for v in fields.values() if v[1] == b'ctx')
        self.assertIn('coverage and freshness are separate', context)
        self.assertIn('NOT a filtered scan', context)
        # Closed professions are uploaded as cached, without reading their UI.
        self.sim.lua.execute(b'function GetNumTradeSkills() error("closed profession was scanned") end')
        self.sync()
        self.assertEqual(doc[b'revision'], self.document('recipes:Alchemy')[1][b'revision'])

    def test_missing_filter_api_is_unknown_not_an_active_filter(self):
        self.sim.lua.execute(b'GetTradeSkillSubClassFilter = nil; GetTradeSkillInvSlotFilter = nil')
        self.sim.g.STUB.fire(b'TRADE_SKILL_SHOW'); self.sim.run(3)
        body, _ = self.document('recipes:Alchemy')
        self.assertEqual(body[2], 'partial')
        self.assertIn('category state unavailable', body[3])
        self.assertNotIn('category filter', body[3])
        self.assertNotIn('slot filter', body[3])

    def test_scanner_error_and_empty_recipe_list_never_report_sync_success(self):
        self.sim.lua.execute(b'function GetContainerNumSlots() error("bags unavailable") end')
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.sim.run(3)
        self.assertFalse(self.sim.ns.CharacterSyncBusy())
        self.assertIn('bags scan failed', self.messages())
        self.sim.lua.execute(b'function GetContainerNumSlots() return 0 end; function GetNumTradeSkills() return 0 end')
        self.sim.g.STUB.fire(b'TRADE_SKILL_SHOW')
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.sim.run(65)
        self.assertFalse(self.sim.ns.CharacterSyncBusy())
        self.assertIn('did not settle', self.messages())
        self.assertNotIn('Character sync complete:', self.messages())

    def test_missing_companion_times_out_and_retry_is_available(self):
        self.sim.companion_on = False
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.assertTrue(self.sim.run(300, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        self.assertIn('Character sync failed:', self.messages())
        self.assertNotIn('Character sync complete:', self.messages())
        self.sim.companion_on = True
        self.sim.run(1)
        self.sync()


if __name__ == '__main__':
    unittest.main()
