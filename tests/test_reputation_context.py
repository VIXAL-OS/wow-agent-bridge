"""Reputation snapshots through production Lua and transport."""
import json
import tempfile
import unittest

from companion.character import canonical, validate_document
from companion.protocol import parse_envelope
from tests.harness import Sim
from tests.test_e2e import agent


# The Reputation pane's rows as GetFactionInfo returns them on 3.3.5: name, description, standing ID, bar min, bar max,
# bar value, at war, can toggle war, header, collapsed, header has its own standing, watched, child.
FACTIONS = r'''
STUB.factions = {
    {'Wrath of the Lich King', '', 0, 0, 0, 0, false, false, true, false, false, false, false},
    {'Alliance Vanguard', '', 6, 9000, 21000, 15000, false, false, true, false, true, false, true},
    {'Valiance Expedition', '', 5, 3000, 9000, 4500, false, false, false, false, false, true, true},
    {'Argent Crusade', '', 8, 42000, 43000, 42999, false, false, false, false, false, false, false},
    {'Classic', '', 0, 0, 0, 0, false, false, true, false, false, false, false},
    {'Stormwind', '', 7, 21000, 42000, 30000, false, false, false, false, false, false, false},
    {'Inactive', '', 0, 0, 0, 0, false, false, true, false, false, false, false},
    {'Bloodsail Buccaneers', '', 2, -6000, -3000, -4500, true, true, false, false, false, false, false},
}
for i, label in ipairs({'Hated', 'Hostile', 'Unfriendly', 'Neutral', 'Friendly', 'Honored', 'Revered', 'Exalted'}) do
    _G['FACTION_STANDING_LABEL'..i] = label
end
STUB.factionReads = 0
-- What the pane lists: a collapsed header hides the rows under it (a sub-group's children, or a whole category).
local function visible()
    local out, hideCategory, hideGroup = {}, false, false
    for _, row in ipairs(STUB.factions) do
        local header, collapsed, child = row[9], row[10], row[13]
        if header and not child then
            hideCategory, hideGroup = collapsed, false
            out[#out+1] = row
        elseif not hideCategory then
            if header then hideGroup = collapsed; out[#out+1] = row
            elseif not child then hideGroup = false; out[#out+1] = row
            elseif not hideGroup then out[#out+1] = row end
        end
    end
    return out
end
function GetNumFactions() return #visible() end
function GetFactionInfo(index)
    STUB.factionReads = STUB.factionReads + 1
    local row = visible()[index]
    if row then return unpack(row) end
end
function STUB.faction(name)
    for _, row in ipairs(STUB.factions) do if row[1] == name then return row end end
end
STUB.fire('PLAYER_ENTERING_WORLD')
'''

ROWS = [
    ['Alliance Vanguard', 'Wrath of the Lich King', '6', 'Honored', '6000', '12000', '15000', 'group'],
    ['Argent Crusade', 'Wrath of the Lich King', '8', 'Exalted', '999', '1000', '42999', ''],
    ['Bloodsail Buccaneers', 'Inactive', '2', 'Hostile', '1500', '3000', '-4500', 'at war'],
    ['Stormwind', 'Classic', '7', 'Revered', '9000', '21000', '30000', ''],
    ['Valiance Expedition', 'Wrath of the Lich King > Alliance Vanguard', '5', 'Friendly', '1500', '6000', '4500', 'watched'],
]
OWNER = 'ChromieCraft:Testbrew'


class ReputationSnapshots(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = Sim(tmp.name, agent(lambda p: 'Echo: ' + p), slots=200)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute(FACTIONS.encode())
        self.sim.run(3)
        self.accepted = {}
        original = self.sim.character.accept
        def record(key, blob):
            self.accepted[key] = parse_envelope(blob)
            return original(key, blob)
        self.sim.character.accept = record

    def document(self):
        bundle = self.sim.ns.CharacterFields(self.sim.lua.table())
        doc = bundle[b'docs'][b'reputations']
        return (json.loads(doc[b'body']), doc) if doc else (None, None)

    def finish(self, text):
        self.sim.send(text)
        self.assertTrue(self.sim.run(200, until=lambda: self.sim.last_reply() == 'Echo: ' + text), self.sim.body())
        self.sim.run(1)
        return next(job for job in self.sim.jobs.values() if job['prompt'] == text)

    def change(self, lua):
        self.sim.lua.execute(lua.encode())
        self.sim.g.STUB.fire(b'UPDATE_FACTION')
        self.sim.run(3)

    def test_prompt_carries_every_listed_faction_then_reuses_unchanged(self):
        job = self.finish('my reputations')
        self.assertIn('reputations: 5 records; complete', job['character'])
        value, doc = self.document()
        self.assertEqual(value[4], ROWS)
        self.assertEqual(self.sim.character.body(OWNER, 'reputations', doc[b'revision'].decode()), doc[b'body'].decode())
        rendered = self.sim.character.render(OWNER, 'reputations', value[2], value[3], value[4])
        self.assertIn('Faction\tCategory\tStanding ID\tStanding\tEarned\tNeeded\tTotal\tFlags', rendered)
        self.assertIn('Valiance Expedition\tWrath of the Lich King > Alliance Vanguard\t5\tFriendly\t1500\t6000\t4500\twatched', rendered)
        self.assertIn('Bloodsail Buccaneers\tInactive\t2\tHostile\t1500\t3000\t-4500\tat war', rendered)
        before = self.sim.character_frames
        self.finish('unchanged')
        self.assertEqual(before, self.sim.character_frames, 'unchanged reputations need no upload')

    def test_a_gain_after_reload_goes_inline_as_one_row(self):
        self.finish('baseline')
        _, doc = self.document()
        baseline = doc[b'revision'].decode()
        # The confirmed baseline survives a /reload, so the next change can travel as a small patch.
        self.sim.reload()
        self.sim.lua.execute(FACTIONS.encode())
        self.sim.run(3)
        self.assertEqual(self.document()[1][b'revision'].decode(), baseline)
        self.accepted.clear()
        self.change("STUB.faction('Valiance Expedition')[6] = 4750")
        job = self.finish('a little more valiance')
        self.assertEqual(self.accepted, {}, 'a small change arrives with the prompt, without an upload handshake')
        payloads = [json.loads(p) for p in job['fields']['snapshotdata']]
        self.assertEqual(len(payloads), 1)
        self.assertEqual((payloads[0][0], payloads[0][1], payloads[0][4]), (2, 'reputations', baseline))
        self.assertEqual(payloads[0][5], [['Valiance Expedition', 'Wrath of the Lich King > Alliance Vanguard', '5',
                                           'Friendly', '1750', '6000', '4750', 'watched']])
        value, doc = self.document()
        self.assertEqual(json.loads(self.sim.character.body(OWNER, 'reputations', doc[b'revision'].decode())), value)

    def test_reaching_a_new_standing_and_watching_another_faction(self):
        self.change("local r = STUB.faction('Stormwind'); r[3], r[4], r[5], r[6], r[12] = 8, 42000, 43000, 42000, true; "
                    "STUB.faction('Valiance Expedition')[12] = false")
        value, _ = self.document()
        rows = {r[0]: r for r in value[4]}
        self.assertEqual(rows['Stormwind'], ['Stormwind', 'Classic', '8', 'Exalted', '0', '1000', '42000', 'watched'])
        self.assertEqual(rows['Valiance Expedition'][7], '')

    def test_collapsed_header_is_partial_and_keeps_factions_last_seen(self):
        self.change("STUB.faction('Classic')[10] = true")
        value, _ = self.document()
        self.assertEqual(value[2], 'partial')
        self.assertIn('Collapsed in the Reputation pane', value[3])
        self.assertIn('Classic', value[3])
        self.assertEqual(value[4], ROWS, 'Stormwind stays as last seen')
        self.change("STUB.faction('Alliance Vanguard')[10] = true")
        value, _ = self.document()
        self.assertIn('Alliance Vanguard', value[3])
        self.assertIn('Valiance Expedition', [r[0] for r in value[4]])
        self.change("STUB.faction('Classic')[10] = false; STUB.faction('Alliance Vanguard')[10] = false")
        value, _ = self.document()
        self.assertEqual((value[2], value[4]), ('complete', ROWS))

    def test_unreadable_row_is_partial_not_dropped(self):
        self.change("STUB.faction('Argent Crusade')[3] = nil")
        value, _ = self.document()
        self.assertEqual(value[2], 'partial')
        self.assertEqual(value[4], ROWS)

    def test_missing_api_is_not_no_standing(self):
        self.sim.lua.execute(b'GetNumFactions = nil; GetFactionInfo = nil')
        fields = self.sim.lua.table(); self.sim.ns.CharacterFields(fields)
        context = b'\n'.join(v[2] for v in fields.values() if v[1] == b'ctx')
        self.assertIn(b'Reputation APIs unavailable; do not infer no standing.', context)

    def test_manual_sync_includes_reputations(self):
        ok, _ = self.sim.ns.SyncCharacter()
        self.assertTrue(ok)
        self.assertTrue(self.sim.run(900, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        messages = '\n'.join(v.decode() for v in self.sim.g.STUB.prints.values())
        self.assertIn('Character sync complete:', messages)
        self.assertIn('reputations', messages)
        self.assertTrue(any(fields.get('section') == ['reputations'] for fields, _ in self.accepted.values()))
        self.assertEqual(self.sim.jobs, {}, 'manual sync must not launch an agent')


class ReputationValidation(unittest.TestCase):
    def doc(self, *rows):
        return [1, 'reputations', 'complete', 'Reputation pane factions.', [list(r) for r in rows]]

    def test_accepts_the_shapes_the_addon_sends(self):
        value = self.doc(*ROWS)
        self.assertEqual(validate_document(value, 'reputations'), value)
        self.assertTrue(canonical(value))

    def test_rejects_malformed_rows(self):
        good = ROWS[1]
        for bad in (
            good[:2] + ['9'] + good[3:],                    # no standing 9
            good[:2] + ['0'] + good[3:],                    # nor 0
            good[:4] + ['1001', '1000'] + good[6:],         # earned beyond needed
            good[:4] + ['0', '0'] + good[6:],               # an empty standing
            good[:6] + ['-0'] + good[7:],                   # not a canonical number
            good[:6] + ['4.5'] + good[7:],
            good[:7] + ['bogus'],                           # unknown flag
            good[:7] + ['watched, watched'],                # a flag twice
            good[:7],                                       # a column short
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_document(self.doc(bad), 'reputations')
        with self.assertRaises(ValueError):
            validate_document(self.doc(good, good), 'reputations')


if __name__ == '__main__':
    unittest.main()
