"""Synthetic achievement IDs with real 3.3.5 API shapes and optical transport."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from companion.character import CharacterStore, canonical, manifest, revision, validate_document
from companion.savedvariables import SavedSnapshots
from companion.protocol import Assembler, encode_character, parse_character_frame, parse_envelope
from tests.harness import Sim
from tests.test_character import envelope
from tests.test_e2e import agent
from tests.test_savedvariables import literal


ACHIEVEMENTS = r'''
STUB.achievementReads = 0
STUB.achievements = {
    [1001] = {'Dalaran copper coins', false, {{'First copper coin', 42, true, 1, 1, '', 0, 43001, '1/1'},
                                           {'Second copper coin', 42, false, 0, 1, '', 0, 43002, '0/1'}}},
    [1002] = {'Fishing catches I', true, {{'Catch fish', 27, true, 10, 10, '', 0, 0, '10/10'}}},
    [1003] = {'Fishing catches II', false, {{'Catch fish', 27, false, 12, 100, '', 0, 0, '12/100'}}},
    [1004] = {'Coin collection meta', false, {{'Dalaran copper coins', 8, false, 0, 1, '', 0, 1001, '0/1'}}},
}
function GetCategoryList() if STUB.emptyAchievements then return {} end return {10} end
function GetCategoryInfo() return 'Fishing', -1 end
-- The client returns both total and completed counts.
function GetCategoryNumAchievements() return 3, 1 end
function GetAchievementInfo(id, index)
    assert(id ~= nil)
    if index then id = ({1001, 1002, 1004})[index] end
    local row = STUB.achievements[id]
    if row then return id, row[1], 10, row[2], row[2] and 9, row[2] and 30, row[2] and 26, 'Earn credit for the named objectives.', 0 end
end
function GetAchievementNumCriteria(id) return #STUB.achievements[id][3] end
function GetAchievementCriteriaInfo(id, index)
    STUB.achievementReads = STUB.achievementReads + 1
    if STUB.missingCriterion and id == 1001 and index == 2 then return end
    return unpack(STUB.achievements[id][3][index])
end
function GetPreviousAchievement(id) if id == 1003 then return 1002 end end
function GetNextAchievement(id) if id == 1002 then return 1003 end end
-- An unrelated comparison view and a completed-only display filter must not matter.
function GetComparisonAchievementInfo() error('another player must not be read') end
ACHIEVEMENTUI_SELECTEDFILTER = function() error('display filter must not be read') end
STUB.fire('PLAYER_ENTERING_WORLD')
'''


class AchievementSnapshots(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = Sim(tmp.name, agent(lambda p: 'Echo: ' + p), slots=240)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute(ACHIEVEMENTS.encode())
        self.sim.run(3)

    def docs(self):
        bundle = self.sim.ns.CharacterFields(self.sim.lua.table())
        return {key.decode(): doc for key, doc in bundle[b'docs'].items() if key.startswith(b'achievements:')}

    def row(self, ident):
        section = f'achievements:{ident % 8 + 1}'
        doc = self.docs()[section]
        return next(row for row in json.loads(doc[b'body'])[4] if row[0] == str(ident))

    def sync(self):
        ok, message = self.sim.ns.SyncCharacter()
        self.assertTrue(ok, message)
        self.assertTrue(self.sim.run(600, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        prints = '\n'.join(v.decode() for v in self.sim.g.STUB.prints.values())
        self.assertIn('Character sync complete:', prints)

    def test_all_criteria_chains_meta_and_completion_without_inventory(self):
        docs = self.docs()
        self.assertEqual(len(docs), 8)
        for section, doc in docs.items():
            data = validate_document(json.loads(doc[b'body']), section)
            self.assertEqual(data[2], 'complete')
        coins = self.row(1001)
        criteria = json.loads(coins[9])
        self.assertEqual((coins[2], coins[7], coins[8]), ('0', '2', 'complete'))
        self.assertEqual([row[2] for row in criteria], ['1', '0'])
        self.assertEqual(self.row(1002)[6], '2026-09-30')
        self.assertEqual(json.loads(self.row(1003)[9])[0][3:5], ['12', '100'])
        self.assertEqual(json.loads(self.row(1004)[9])[0][6], '1001')
        text = CharacterStore.render('owner', 'achievements:2', 'complete', '', [coins])
        self.assertIn('First copper coin\tcomplete\t1\t1', text)
        self.assertIn('Second copper coin\tincomplete\t0\t1', text)
        self.assertIn('sold or used', text)

    def test_category_completed_count_is_not_used_as_numeric_base(self):
        self.sim.lua.execute(b'''
        STUB.achievements = {}
        for id = 2001, 2012 do STUB.achievements[id] = {'Achievement '..id, false, {}} end
        local oldInfo = GetAchievementInfo
        function GetAchievementInfo(id, index) return oldInfo(index and 2000 + index or id) end
        function GetCategoryNumAchievements() return 12, STUB.categoryCompleted end
        ''')
        for completed in (0, 1, 2, 8, 10, 12):
            with self.subTest(completed=completed):
                self.sim.g.STUB.categoryCompleted = completed
                self.sim.lua.execute(b'''
                for id, row in pairs(STUB.achievements) do row[2] = id - 2000 <= STUB.categoryCompleted end
                STUB.fire('RECEIVED_ACHIEVEMENT_LIST')
                ''')
                self.sim.run(3)
                docs = [validate_document(json.loads(doc[b'body']), section) for section, doc in self.docs().items()]
                self.assertEqual(len(docs), 8)
                self.assertTrue(all(doc[2] == 'complete' for doc in docs))
                rows = [row for doc in docs for row in doc[4]]
                self.assertEqual({row[0] for row in rows}, {str(i) for i in range(2001, 2013)})
                self.assertEqual(sum(row[2] == '1' for row in rows), completed)

    def test_criteria_event_rescans_and_small_delta_keeps_unchanged_revisions(self):
        before = {key: doc[b'revision'] for key, doc in self.docs().items()}
        reads = self.sim.g.STUB.achievementReads
        self.sim.run(4)
        self.assertEqual(self.sim.g.STUB.achievementReads, reads)
        self.sim.lua.execute(b'STUB.achievements[1001][3][2][3] = true; STUB.achievements[1001][3][2][4] = 1')
        self.sim.g.STUB.fire(b'CRITERIA_UPDATE')
        fields = self.sim.lua.table(); self.sim.ns.CharacterFields(fields)
        refs = [v[2] for v in fields.values() if v[1] == b'snapshot' and v[2].startswith(b'achievements:')]
        self.assertTrue(all(ref.endswith(b'|stale') for ref in refs))
        self.sim.run(3)
        self.assertEqual(json.loads(self.row(1001)[9])[1][2:4], ['1', '1'])
        changed = [key for key, doc in self.docs().items() if doc[b'revision'] != before[key]]
        self.assertEqual(changed, ['achievements:2'])

    def test_manual_upload_and_repeat_probe_do_not_launch_agent(self):
        self.sync()
        self.assertEqual(self.sim.jobs, {})
        for section, doc in self.docs().items():
            self.assertEqual(self.sim.character.body('ChromieCraft:Testbrew', section, doc[b'revision'].decode()), doc[b'body'].decode())
        self.sim.run(.3)
        accepted = {}
        original = self.sim.character.accept
        def record(key, blob):
            accepted[key] = parse_envelope(blob)
            return original(key, blob)
        self.sim.character.accept = record
        self.sync()
        self.assertEqual(len(accepted), 1)
        self.assertTrue(all(fields.get('probe') == ['manifest'] and body == 'ABCTX_PROBE' for fields, body in accepted.values()))
        self.sim.run(.3)
        frames = self.sim.character_frames
        self.sim.send('which coins remain?')
        self.assertTrue(self.sim.run(90, until=lambda: self.sim.last_reply() == 'Echo: which coins remain?'))
        self.assertEqual(self.sim.character_frames, frames)
        self.assertIn('achievement', next(iter(self.sim.jobs.values()))['character'].lower())

    def test_missing_criteria_are_unknown_and_empty_lists_preserve_observations(self):
        self.sim.g.STUB.missingCriterion = True
        self.sim.g.STUB.fire(b'CRITERIA_UPDATE'); self.sim.run(3)
        row = self.row(1001)
        self.assertEqual(row[8], 'partial')
        self.assertEqual(json.loads(row[9])[1][2:5], ['', '', ''])
        self.sim.g.STUB.emptyAchievements = True
        self.sim.g.STUB.fire(b'RECEIVED_ACHIEVEMENT_LIST'); self.sim.run(3)
        self.assertEqual(self.row(1001), row)
        self.assertTrue(all(json.loads(doc[b'body'])[2] == 'unavailable' for doc in self.docs().values()))

    def test_reload_import_and_character_isolation(self):
        docs = self.docs()
        self.sim.reload(); self.sim.run(1)
        self.assertEqual({k: v[b'revision'] for k, v in self.docs().items()}, {k: v[b'revision'] for k, v in docs.items()})
        self.assertTrue(all(doc[b'session'] is None for doc in self.docs().values()))
        self.sim.lua.execute(b"function UnitName() return 'Other' end; STUB.fire('PLAYER_ENTERING_WORLD')")
        self.assertEqual(self.docs(), {})

    def test_large_catalog_fits_partitions_and_manifest_keeps_prompt_room(self):
        self.sim.lua.execute(b'''
        STUB.achievements = {}
        for id = 1, 1800 do
            STUB.achievements[id] = {'Achievement '..id, false, {}}
            for criterion = 1, 5 do
                STUB.achievements[id][3][criterion] = {'Objective '..criterion, 42, false, 0, 1, '', 0, id, '0/1'}
            end
        end
        function GetCategoryNumAchievements() return 1800, 0 end
        local oldInfo = GetAchievementInfo
        function GetAchievementInfo(id, index) return oldInfo(index or id) end
        function GetNextAchievement() end
        function GetPreviousAchievement() end
        STUB.fire('RECEIVED_ACHIEVEMENT_LIST')
        ''')
        self.sim.run(20)
        docs = self.docs()
        self.assertEqual(sum(len(json.loads(doc[b'body'])[4]) for doc in docs.values()), 1800)
        self.assertTrue(all(json.loads(doc[b'body'])[2] == 'complete' for doc in docs.values()))
        refs = [f'{key}|{doc[b"revision"].decode()}|1758000000|current' for key, doc in docs.items()]
        refs += [f'recipes:Profession{i}|00000001-10|1758000000|cached' for i in range(8)]
        refs += [f'{key}|00000001-10|1758000000|current' for key in ('gear', 'bags', 'pets', 'mounts', 'combatpet', 'stablepets')]
        self.assertEqual(len(manifest({'character': ['owner'], 'snapshot': refs})[1]), 22)
        self.assertLess(len('\n'.join(refs)) + 2000 + 2000, 8000)

    def test_combat_defers_reads_and_context_off_cancels_work(self):
        reads = self.sim.g.STUB.achievementReads
        self.sim.g.STUB.combat = True
        self.sim.g.STUB.fire(b'CRITERIA_UPDATE'); self.sim.run(4)
        self.assertEqual(reads, self.sim.g.STUB.achievementReads)
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context off')
        self.sim.g.STUB.combat = False; self.sim.run(4)
        self.assertEqual(reads, self.sim.g.STUB.achievementReads)
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context on'); self.sim.run(4)
        self.assertGreater(self.sim.g.STUB.achievementReads, reads)



    def test_earned_event_reads_only_affected_achievement_and_meta_parent(self):
        before = {k: d[b'revision'] for k, d in self.docs().items()}
        reads = self.sim.g.STUB.achievementReads
        self.sim.lua.execute(b'''
        function GetCategoryList() error('must reuse the known catalog') end
        local original = GetAchievementInfo
        function GetAchievementInfo(id, index)
            assert(not index and (id == 1001 or id == 1004), 'unrelated achievement read')
            return original(id)
        end
        STUB.achievements[1001][2] = true
        STUB.achievements[1001][3][2][3] = true
        STUB.achievements[1001][3][2][4] = 1
        STUB.achievements[1004][3][1][3] = true
        STUB.achievements[1004][3][1][4] = 1
        STUB.fire('ACHIEVEMENT_EARNED', 1001)
        ''')
        self.sim.run(3)
        self.assertEqual(self.sim.g.STUB.achievementReads - reads, 3)
        self.assertEqual(self.row(1001)[2], '1')
        self.assertEqual(json.loads(self.row(1004)[9])[0][2:4], ['1', '1'])
        self.assertEqual({k for k, d in self.docs().items() if d[b'revision'] != before[k]}, {'achievements:2', 'achievements:5'})
        self.assertNotIn(b'failed', self.sim.ns.CharacterStatus())

    def test_criteria_burst_reuses_catalog_and_preserves_untouched_documents(self):
        docs = self.docs()
        reads = self.sim.g.STUB.achievementReads
        self.sim.lua.execute(b'''
        function GetCategoryList() error('must reuse the known catalog') end
        STUB.achievements[1001][3][2][4] = 1
        for i = 1, 100 do STUB.fire('CRITERIA_UPDATE') end
        ''')
        self.sim.run(3)
        self.assertEqual(self.sim.g.STUB.achievementReads - reads, 5)
        self.assertEqual(json.loads(self.row(1001)[9])[1][3], '1')
        for section, doc in docs.items():
            if section != 'achievements:2':
                self.assertTrue(self.sim.lua.eval(b'function(a, b) return rawequal(a, b) end')(doc, self.docs()[section]))

    def test_update_during_running_sweep_is_not_lost(self):
        self.sim.lua.execute(b'''
        local original = GetAchievementCriteriaInfo
        local fired = false
        function GetAchievementCriteriaInfo(id, index)
            if not fired and id == 1001 and index == 2 then
                fired = true
                -- Change a criterion that this pass has already read.
                STUB.achievements[1001][3][1][4] = 9
                STUB.fire('CRITERIA_UPDATE')
            end
            return original(id, index)
        end
        STUB.fire('CRITERIA_UPDATE')
        ''')
        self.sim.run(4)
        self.assertEqual(json.loads(self.row(1001)[9])[0][3], '9')
        self.assertNotIn(b'stale', self.sim.ns.CharacterStatus())

    def test_completed_achievement_optional_criteria_can_still_change(self):
        self.sim.lua.execute(b'''
        STUB.achievements[1002][3][1][4] = 11
        STUB.fire('CRITERIA_UPDATE')
        ''')
        self.sim.run(3)
        self.assertEqual(json.loads(self.row(1002)[9])[0][3], '11')


class AchievementCache(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = sqlite3.connect(':memory:'); self.addCleanup(self.db.close)
        self.store = CharacterStore(self.db, self.root / 'characters', SavedSnapshots(self.root))
        self.row = ['1001', 'Coins', '0', '10', 'Collect coins.', 'Fishing', '', '1', 'complete',
                    canonical([['1', 'First coin', '1', '1', '1', '42', '43001', '1/1']])]
        self.body = canonical([1, 'achievements:2', 'complete', 'Observed.', [self.row]])
        self.rev = revision(self.body)

    def probe(self, request='probe', owner='owner'):
        return self.store.accept(request, envelope(owner, 'achievements:2', self.rev, 1, 1, 'ABCTX_PROBE').replace('\npage=', '\nprobe=1\npage='))

    def test_probe_imports_exact_saved_revision_and_never_queues_a_job(self):
        self.assertEqual(self.probe()['reply'], 'ABCTX_BASE_MISSING')
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM character_pages').fetchone()[0], 0)
        path = self.root / 'WTF/Account/TEST/SavedVariables/AgentBridge.lua'
        path.parent.mkdir(parents=True)
        path.write_text('AgentBridgeState = ' + literal({'characterAchievements': {'owner': {
            'achievements:2': {'revision': self.rev, 'body': self.body}}}}), encoding='utf-8')
        self.store.saved_snapshots.next_check = -1e9
        self.assertEqual(self.probe('saved')['reply'], 'ABCTX_OK')
        self.assertEqual(self.store.body('owner', 'achievements:2', self.rev), self.body)
        self.assertEqual(self.probe('other', 'other')['reply'], 'ABCTX_BASE_MISSING')

    def test_rejects_malformed_criteria_coverage_ids_and_partition(self):
        cases = []
        bad = copy.deepcopy(self.row); bad[9] = '{}'; cases.append(bad)
        bad = copy.deepcopy(self.row); bad[7] = '2'; cases.append(bad)
        bad = copy.deepcopy(self.row); bad[0] = '1002'; cases.append(bad)
        bad = copy.deepcopy(self.row); criteria = json.loads(bad[9]); criteria[0][2] = 'yes'; bad[9] = canonical(criteria); cases.append(bad)
        bad = copy.deepcopy(self.row); criteria = json.loads(bad[9]); criteria[0][3] = '-1'; bad[9] = canonical(criteria); cases.append(bad)
        for row in cases:
            with self.subTest(row=row), self.assertRaises(ValueError):
                validate_document([1, 'achievements:2', 'complete', '', [row]], 'achievements:2')
        for section in ('achievements:0', 'achievements:9', 'achievements:../x'):
            with self.assertRaises(ValueError):
                manifest({'character': ['owner'], 'snapshot': [f'{section}|{self.rev}|1|current']})

    def test_slow_pages_expire_on_stall_but_not_while_new_fragments_arrive(self):
        clock = [0]
        assembler = Assembler(clock=lambda: clock[0], parser=parse_character_frame)
        frames = encode_character('x' * 100)
        self.assertIsNone(assembler.accept(frames[0]))
        clock[0] = 170
        self.assertIsNone(assembler.accept(frames[1]))
        clock[0] = 185
        self.assertEqual(assembler.accept(frames[2])[1], 'x' * 100)
        clock[0] = 200
        self.assertIsNone(assembler.accept(frames[0]))
        clock[0] = 370
        self.assertIsNone(assembler.accept(frames[0]))  # Duplicate is not progress.
        clock[0] = 385
        self.assertIsNone(assembler.accept(frames[1]))
        self.assertIsNone(assembler.accept(frames[2]), 'stalled first fragment was discarded')



    def test_manifest_checks_all_sections_once_and_imports_only_requested_owner(self):
        path = self.root / 'WTF/Account/TEST/SavedVariables/AgentBridge.lua'
        path.parent.mkdir(parents=True)
        path.write_text('AgentBridgeState = ' + literal({'characterAchievements': {'owner': {
            'achievements:2': {'revision': self.rev, 'body': self.body}}}}), encoding='utf-8')
        empty = canonical([1, 'achievements:3', 'complete', '', []])
        missing_rev = revision(empty)
        blob = (f'\x01AB1\ncharacter=owner\nprobe=manifest\nsnapshot=achievements:2|{self.rev}|1|cached'
                f'\nsnapshot=achievements:3|{missing_rev}|1|cached\n\x02ABCTX_PROBE')
        result = self.store.accept('manifest', blob)
        self.assertEqual(result['state'], 'done')
        self.assertEqual(result['reply'], f'\x01ABCTX1\nachievements:3|{missing_rev}')
        self.assertEqual(self.store.body('owner', 'achievements:2', self.rev), self.body)
        other = self.store.accept('other-manifest', blob.replace('character=owner', 'character=other'))
        self.assertIn(f'achievements:2|{self.rev}', other['reply'])
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM character_pages').fetchone()[0], 0)

    def test_manifest_rejects_invalid_identity_duplicates_and_extra_headers(self):
        blob = f'\x01AB1\ncharacter=owner\nprobe=manifest\nsnapshot=achievements:2|{self.rev}|1|cached\n\x02ABCTX_PROBE'
        cases = [blob.replace('character=owner\n', ''),
                 blob.replace('snapshot=', 'character=other\nsnapshot='),
                 blob.replace('snapshot=', 'section=bags\nsnapshot='),
                 blob.replace('snapshot=', f'snapshot=achievements:2|{self.rev}|1|cached\nsnapshot='),
                 blob.replace('ABCTX_PROBE', 'anything'),
                 blob.replace('achievements:2', 'achievements:99')]
        for index, value in enumerate(cases):
            with self.subTest(index=index):
                self.assertEqual(self.store.accept('invalid-' + str(index), value)['state'], 'failed')


if __name__ == '__main__':
    unittest.main()
