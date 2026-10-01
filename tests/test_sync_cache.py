"""Acknowledged character baselines survive scans, reloads and interrupted uploads."""
import json
import tempfile
import unittest

from companion.character import canonical, revision
from companion.protocol import parse_envelope
from tests.harness import Sim
from tests.test_achievement_context import ACHIEVEMENTS
from tests.test_e2e import agent


CATALOG = r'''
local originalInfo = GetAchievementInfo
for i = 1, 8 do
    STUB.achievements[1001 + i * 8] = {'Additional objective '..i, false,
        {{'Collect an objective', 42, false, 0, 1, '', 0, 0, '0/1'}}}
end
function GetCategoryNumAchievements() return 11, 1 end
function GetAchievementInfo(id, index)
    if index then id = index <= 3 and ({1001, 1002, 1004})[index] or 1001 + (index - 3) * 8 end
    return originalInfo(id)
end
STUB.fire('RECEIVED_ACHIEVEMENT_LIST')
'''


class ConfirmedSyncCache(unittest.TestCase):
    OWNER = 'ChromieCraft:Testbrew'
    SECTION = 'achievements:2'

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = Sim(tmp.name, agent(lambda p: 'Echo: ' + p), slots=320)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute((ACHIEVEMENTS + CATALOG).encode())
        self.sim.run(3)
        self.accepted = {}
        original = self.sim.character.accept
        def record(key, blob):
            self.accepted[key] = parse_envelope(blob)
            return original(key, blob)
        self.sim.character.accept = record

    def docs(self):
        return self.sim.ns.CharacterFields(self.sim.lua.table())[b'docs']

    def doc(self):
        return self.docs()[self.SECTION.encode()]

    def baseline(self, owner=None):
        cache = self.sim.ns.S[b'characterSynced']
        return cache and cache[(owner or self.OWNER).encode()]

    def baseline_revision(self):
        return self.baseline()[self.SECTION.encode()][b'revision'].decode()

    def prime(self):
        # Like a prior upload or SavedVariables import: all exact revisions are
        # already present. An accepted prompt must establish the addon baseline.
        with self.sim.character.db:
            for section, doc in self.docs().items():
                self.sim.character.db.execute('INSERT OR IGNORE INTO character_snapshots VALUES (?,?,?,?,?)',
                    (self.OWNER, section.decode(), doc[b'revision'].decode(), doc[b'body'].decode(), 1))
        self.sim.send('baseline')
        self.assertTrue(self.sim.run(90, until=lambda: self.sim.last_reply() == 'Echo: baseline'))
        self.sim.run(1)
        self.assertEqual(self.baseline_revision(), self.doc()[b'revision'].decode())
        self.assertEqual(self.accepted, {}, 'cached prompt should need no character upload')
        return self.baseline_revision()

    def change(self, quantity):
        self.sim.lua.execute(('local c = STUB.achievements[1001][3][1]; '
                              'c[3] = false; c[4] = %d; c[5] = 20; c[9] = "%d/20"; '
                              'STUB.fire("CRITERIA_UPDATE")' % (quantity, quantity)).encode())
        self.sim.run(3)

    def sync(self):
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.assertTrue(self.sim.run(900, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        messages = '\n'.join(v.decode() for v in self.sim.g.STUB.prints.values())
        self.assertIn('Character sync complete:', messages)
        self.sim.run(.3)

    def payloads(self):
        result, pages = [], []
        for fields, chunk in self.accepted.values():
            if fields.get('section') != [self.SECTION] or 'probe' in fields:
                continue
            if fields['page'] == ['1']:
                pages = []
            pages.append(chunk)
            if fields['page'] == fields['total']:
                result.append(json.loads(''.join(pages)))
        return result

    def test_many_unsynced_changes_use_last_confirmed_revision_then_reuse_cache(self):
        baseline = self.prime()
        for quantity in (2, 3, 4):
            self.change(quantity)
            self.assertEqual(self.baseline_revision(), baseline)
        self.sync()
        payloads = self.payloads()
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0][0], 2)
        self.assertEqual(payloads[0][4], baseline)
        self.assertEqual([r[0] for r in payloads[0][5]], ['1001'])
        self.assertEqual(self.baseline_revision(), self.doc()[b'revision'].decode())
        self.assertEqual(self.sim.character.body(self.OWNER, self.SECTION, self.baseline_revision()), self.doc()[b'body'].decode())
        self.accepted.clear()
        self.sync()
        self.assertEqual(len(self.accepted), 1)
        self.assertTrue(all(fields.get('probe') == ['manifest'] for fields, _ in self.accepted.values()))
        self.assertEqual(len(self.sim.jobs), 1, 'manual sync must not launch an agent')

    def test_reload_preserves_confirmed_baseline_not_latest_unsynced_scan(self):
        baseline = self.prime()
        self.change(2); self.change(3)
        self.sim.reload()
        self.sim.lua.execute((ACHIEVEMENTS + CATALOG).encode())
        self.change(4)
        self.assertEqual(self.baseline_revision(), baseline)
        self.sync()
        self.assertEqual(self.payloads()[0][0], 2)
        self.assertEqual(self.payloads()[0][4], baseline)
        self.sim.lua.execute(b'function UnitName() return "Other" end; STUB.fire("PLAYER_ENTERING_WORLD")')
        self.sim.run(3)
        self.assertIsNone(self.baseline('ChromieCraft:Other'))
        self.accepted.clear()
        self.sync()
        self.assertTrue(all(fields['character'] == ['ChromieCraft:Other'] for fields, _ in self.accepted.values()))
        self.assertEqual(self.payloads()[0][0], 1)

    def test_changes_during_upload_do_not_confirm_the_newer_unsent_scan(self):
        baseline = self.prime()
        self.change(2)
        uploading = self.doc()[b'revision'].decode()
        original_get = self.sim.character.get
        hold = [True]
        def get(key):
            result = original_get(key)
            fields = self.accepted.get(key, ({}, ''))[0]
            if result and hold[0] and fields.get('section') == [self.SECTION] and 'probe' not in fields:
                return {'id': key, 'state': 'waiting', 'reply': ''}
            return result
        self.sim.character.get = get
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.assertTrue(self.sim.run(300, until=lambda: bool(self.payloads())))
        self.change(3)
        self.assertEqual(self.baseline_revision(), baseline)
        hold[0] = False
        self.assertTrue(self.sim.run(300, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        self.sim.run(.3)
        self.assertEqual(self.baseline_revision(), uploading)
        self.assertNotEqual(self.doc()[b'revision'].decode(), uploading)
        self.accepted.clear()
        self.change(4); self.sync()
        self.assertEqual(self.payloads()[0][4], uploading)

    def test_partial_page_ack_and_cancellation_do_not_advance_baseline(self):
        baseline = self.prime()
        self.sim.lua.execute(b'''
        local originalInfo = GetAchievementInfo
        for i = 1, 40 do
            STUB.achievements[1001 + i * 8] = {'Long achievement '..i..string.rep('x', 90), false, {}}
        end
        function GetCategoryNumAchievements() return 43, 1 end
        function GetAchievementInfo(id, index)
            if index then id = index <= 3 and ({1001, 1002, 1004})[index] or 1001 + (index - 3) * 8 end
            return originalInfo(id)
        end
        STUB.fire('RECEIVED_ACHIEVEMENT_LIST')
        ''')
        self.sim.run(5)
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        def second_page():
            return any(fields.get('section') == [self.SECTION] and fields['page'] == ['2']
                       for fields, _ in self.accepted.values())
        self.assertTrue(self.sim.run(300, until=second_page))
        self.assertEqual(self.baseline_revision(), baseline, 'first page ACK is not a snapshot ACK')
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context off')
        self.sim.run(2)
        self.assertFalse(self.sim.ns.CharacterSyncBusy())
        self.assertEqual(self.baseline_revision(), baseline)

    def test_missing_companion_baseline_falls_back_once_then_recovers(self):
        baseline = self.prime()
        with self.sim.character.db:
            self.sim.character.db.execute('DELETE FROM character_snapshots WHERE owner=? AND section=?', (self.OWNER, self.SECTION))
        self.change(2); self.change(3); self.sync()
        self.assertEqual([p[0] for p in self.payloads()], [2, 1])
        self.assertEqual(self.payloads()[0][4], baseline)
        self.assertEqual(self.baseline_revision(), self.doc()[b'revision'].decode())

    def test_corrupt_fingerprints_recover_with_a_full_validated_snapshot(self):
        self.prime(); self.change(2)
        row = next(r for r in json.loads(self.doc()[b'body'])[4] if r[0] == '1001')
        # A valid-looking but incorrect cached fingerprint cannot silently omit a change.
        self.baseline()[self.SECTION.encode()][b'hashes'][b'1001'] = revision(canonical(row)).encode()
        self.sync()
        self.assertEqual([p[0] for p in self.payloads()], [2, 1])
        self.assertEqual(self.sim.character.body(self.OWNER, self.SECTION, self.baseline_revision()), self.doc()[b'body'].decode())

    def test_cancel_before_first_packet_is_safe(self):
        self.prime(); self.change(2)
        status = [b'']
        original = self.sim.ns.SetStatus
        def remember(text):
            status[0] = text
            original(text)
        self.sim.ns.SetStatus = remember
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.assertTrue(self.sim.run(10, until=lambda: b'Checking' in status[0]))
        self.sim.run(self.sim.FRAME)
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context off')
        self.sim.run(2)
        self.assertFalse(self.sim.ns.CharacterSyncBusy())



    def test_only_changed_achievement_transfers_on_prompt_with_other_sections_cached(self):
        from tests.test_character import FIXTURES
        from tests.test_pet_context import PETS
        self.sim.lua.execute((FIXTURES + PETS).encode())
        self.sim.run(3)
        self.prime()
        before = {s: d[b'revision'] for s, d in self.docs().items()}
        self.change(2)
        self.sim.send('one achievement changed')
        self.assertTrue(self.sim.run(180, until=lambda: self.sim.last_reply() == 'Echo: one achievement changed'))
        self.assertEqual(self.accepted, {}, 'small changes arrive with the prompt, without an upload handshake')
        job = next(job for job in self.sim.jobs.values() if job['prompt'] == 'one achievement changed')
        payloads = [json.loads(p) for p in job['fields']['snapshotdata']]
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0][0], 2)
        self.assertEqual([r[0] for r in payloads[0][5]], ['1001'])
        self.assertLess(len(canonical(payloads[0])), len(self.doc()[b'body']) / 2)
        self.assertEqual([s for s, d in self.docs().items() if d[b'revision'] != before[s]], [self.SECTION.encode()])
        self.accepted.clear()
        self.sync()
        self.assertEqual(len(self.accepted), 1, 'unchanged manual sync needs one cache check')
        self.assertEqual(next(iter(self.accepted.values()))[0]['probe'], ['manifest'])

    def test_manual_change_uses_one_manifest_and_one_row_patch(self):
        self.prime(); self.change(2); self.sync()
        self.assertEqual(len(self.accepted), 2)
        self.assertEqual(sum(f.get('probe') == ['manifest'] for f, _ in self.accepted.values()), 1)
        self.assertEqual([r[0] for r in self.payloads()[0][5]], ['1001'])
        messages = '\n'.join(v.decode() for v in self.sim.g.STUB.prints.values())
        self.assertIn('1 snapshots uploaded, 7 unchanged snapshots reused', messages)

    def test_old_companion_falls_back_to_individual_probes(self):
        self.prime(); self.change(2)
        original = self.sim.character._page
        def old_page(blob):
            if parse_envelope(blob)[0].get('probe') == ['manifest']:
                raise ValueError('Invalid character page header')
            return original(blob)
        self.sim.character._page = old_page
        self.sync()
        self.assertEqual(sum(f.get('probe') == ['1'] for f, _ in self.accepted.values()), 8)
        self.assertEqual([r[0] for r in self.payloads()[0][5]], ['1001'])
        self.assertEqual(self.baseline_revision(), self.doc()[b'revision'].decode())

    def test_manifest_missing_unchanged_section_recovers_cache_loss(self):
        self.prime()
        with self.sim.character.db:
            self.sim.character.db.execute('DELETE FROM character_snapshots WHERE owner=? AND section=?', (self.OWNER, self.SECTION))
        self.sync()
        self.assertEqual(len(self.accepted), 2)
        self.assertEqual([p[0] for p in self.payloads()], [1])
        self.assertEqual(self.sim.character.body(self.OWNER, self.SECTION, self.baseline_revision()), self.doc()[b'body'].decode())

    def test_invalid_manifest_response_does_not_confirm_or_upload(self):
        baseline = self.prime(); self.change(2)
        original = self.sim.character._page
        def wrong_manifest(blob):
            if parse_envelope(blob)[0].get('probe') == ['manifest']:
                return '\x01ABCTX1\nachievements:2|00000001-100'
            return original(blob)
        self.sim.character._page = wrong_manifest
        self.assertTrue(self.sim.ns.SyncCharacter()[0])
        self.assertTrue(self.sim.run(120, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        self.assertEqual(self.baseline_revision(), baseline)
        self.assertEqual(self.payloads(), [])
        messages = '\n'.join(v.decode() for v in self.sim.g.STUB.prints.values())
        self.assertIn('Invalid cache response', messages)


if __name__ == '__main__':
    unittest.main()
