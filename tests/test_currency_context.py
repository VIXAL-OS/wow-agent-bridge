"""Currency snapshots (Currency tab tokens plus money) through production Lua and transport."""
import json
import tempfile
import unittest

from companion.character import canonical, currency_summary, validate_document
from companion.protocol import parse_envelope
from tests.harness import Sim
from tests.test_e2e import agent


# The Currency tab as GetCurrencyListInfo returns it on 3.3.5: name, header, expanded, unused, watched, count,
# extra currency type (1 arena points, 2 honor points), icon, item ID. A collapsed header hides the tokens under it,
# and ExpandCurrencyList fires CURRENCY_DISPLAY_UPDATE, as the client does.
CURRENCIES = r'''
STUB.currencies = {
    {'Dungeon and Raid', true, true, false, false, 0, 0, '', 0},
    {'Emblem of Frost', false, false, false, true, 23, 0, '', 49426},
    {'Emblem of Triumph', false, false, false, false, 45, 0, '', 47241},
    {'Player vs. Player', true, true, false, false, 0, 0, '', 0},
    {'Honor Points', false, false, false, false, 12345, 2, '', 0},
    {'Arena Points', false, false, false, false, 800, 1, '', 0},
    {'Wintergrasp Mark of Honor', false, false, false, false, 12, 0, '', 43589},
    {'Miscellaneous', true, true, false, false, 0, 0, '', 0},
    {"Champion's Seal", false, false, false, false, 30, 0, '', 44990},
    {'Dalaran Cooking Award', false, false, false, false, 5, 0, '', 43016},
    {'Unused', true, true, false, false, 0, 0, '', 0},
    {'Badge of Justice', false, false, true, false, 3, 0, '', 29434},
}
STUB.currencyReads, STUB.currencyExpands = 0, {}
local function visible()
    local out, hidden = {}, false
    for _, row in ipairs(STUB.currencies) do
        if row[2] then hidden = not row[3]; out[#out+1] = row
        elseif not hidden then out[#out+1] = row end
    end
    return out
end
function GetCurrencyListSize() return #visible() end
function GetCurrencyListInfo(index)
    STUB.currencyReads = STUB.currencyReads + 1
    local row = visible()[index]
    if row then return unpack(row, 1, 9) end
end
function ExpandCurrencyList(index, expand)
    local row = visible()[index]
    STUB.currencyExpands[#STUB.currencyExpands+1] = row[1]..'='..expand
    row[3] = expand == 1
    STUB.fire('CURRENCY_DISPLAY_UPDATE')
end
function STUB.currency(name)
    for _, row in ipairs(STUB.currencies) do if row[1] == name then return row end end
end
STUB.fire('PLAYER_ENTERING_WORLD')
'''

ROWS = [
    ['arena', 'Arena Points', 'Player vs. Player', '800', '', ''],
    ['honor', 'Honor Points', 'Player vs. Player', '12345', '', ''],
    ['item:29434', 'Badge of Justice', 'Unused', '3', '29434', 'unused'],
    ['item:43016', 'Dalaran Cooking Award', 'Miscellaneous', '5', '43016', ''],
    ['item:43589', 'Wintergrasp Mark of Honor', 'Player vs. Player', '12', '43589', ''],
    ['item:44990', "Champion's Seal", 'Miscellaneous', '30', '44990', ''],
    ['item:47241', 'Emblem of Triumph', 'Dungeon and Raid', '45', '47241', ''],
    ['item:49426', 'Emblem of Frost', 'Dungeon and Raid', '23', '49426', 'watched'],
    ['money', 'Money', '', '1234567', '', ''],
]
OWNER = 'ChromieCraft:Testbrew'


class CurrencySnapshots(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = Sim(tmp.name, agent(lambda p: 'Echo: ' + p), slots=200)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute(CURRENCIES.encode())
        self.sim.run(3)
        self.accepted = {}
        original = self.sim.character.accept
        def record(key, blob):
            self.accepted[key] = parse_envelope(blob)
            return original(key, blob)
        self.sim.character.accept = record

    def document(self):
        bundle = self.sim.ns.CharacterFields(self.sim.lua.table())
        doc = bundle[b'docs'][b'currencies']
        return (json.loads(doc[b'body']), doc) if doc else (None, None)

    def finish(self, text):
        self.sim.send(text)
        self.assertTrue(self.sim.run(200, until=lambda: self.sim.last_reply() == 'Echo: ' + text), self.sim.body())
        self.sim.run(1)
        return next(job for job in self.sim.jobs.values() if job['prompt'] == text)

    def test_prompt_carries_every_currency_and_money_then_reuses_unchanged(self):
        job = self.finish('my currencies')
        self.assertIn('currencies: 9 records; complete', job['character'])
        # The agent sees the counts without opening the file; unused tokens stay in the file only.
        self.assertIn('Currencies: Arena Points 800; Honor Points 12345; Dalaran Cooking Award 5; '
                      'Wintergrasp Mark of Honor 12; Champion\'s Seal 30; Emblem of Triumph 45; Emblem of Frost 23; '
                      'Money 123g 45s 67c', job['character'])
        value, doc = self.document()
        self.assertEqual(value[4], ROWS)
        self.assertEqual(self.sim.character.body(OWNER, 'currencies', doc[b'revision'].decode()), doc[b'body'].decode())
        rendered = self.sim.character.render(OWNER, 'currencies', value[2], value[3], value[4])
        self.assertIn('Currency\tCategory\tCount\tItem ID (blank: not an item)\tFlags', rendered)
        self.assertIn('Emblem of Frost\tDungeon and Raid\t23\t49426\twatched', rendered)
        self.assertIn('Money\t\t123g 45s 67c\t\t', rendered)
        before = self.sim.character_frames
        self.finish('unchanged')
        self.assertEqual(before, self.sim.character_frames, 'unchanged currencies need no upload')

    def test_collapsed_header_is_expanded_read_and_left_collapsed(self):
        self.sim.lua.execute(b"STUB.currency('Player vs. Player')[3] = false")
        self.sim.g.STUB.fire(b'CURRENCY_DISPLAY_UPDATE')
        self.sim.run(3)
        value, _ = self.document()
        self.assertEqual((value[2], value[4]), ('complete', ROWS))
        self.assertFalse(self.sim.lua.eval("STUB.currency('Player vs. Player')[3]"), 'the tab looks as the player left it')
        expands = list(self.sim.g.STUB.currencyExpands.values())
        self.assertEqual([bytes(e) for e in expands], [b'Player vs. Player=1', b'Player vs. Player=0'])
        # Its own expansion fires currency events; they must not start the scan over.
        self.sim.run(10)
        self.assertEqual(len(self.sim.g.STUB.currencyExpands), 2)

    def test_token_gain_rescans_the_list(self):
        self.sim.lua.execute(b"STUB.currency('Emblem of Frost')[6] = 25")
        self.sim.g.STUB.fire(b'CURRENCY_DISPLAY_UPDATE')
        self.sim.run(3)
        rows = {r[0]: r for r in self.document()[0][4]}
        self.assertEqual(rows['item:49426'][3], '25')

    def test_money_change_reads_no_list_and_goes_inline_as_one_row(self):
        self.finish('baseline')
        baseline = self.document()[1][b'revision'].decode()
        self.sim.reload()
        self.sim.lua.execute(CURRENCIES.encode())
        self.sim.run(3)
        self.assertEqual(self.document()[1][b'revision'].decode(), baseline)
        reads = self.sim.g.STUB.currencyReads
        self.sim.lua.execute(b'function GetMoney() return 1300000 end')
        self.sim.g.STUB.fire(b'PLAYER_MONEY')
        self.sim.run(3)
        self.assertEqual(self.sim.g.STUB.currencyReads, reads, 'money alone does not reread the Currency tab')
        self.accepted.clear()
        job = self.finish('richer')
        self.assertEqual(self.accepted, {}, 'a small change arrives with the prompt, without an upload handshake')
        payloads = [json.loads(p) for p in job['fields']['snapshotdata']]
        self.assertEqual(len(payloads), 1)
        self.assertEqual((payloads[0][0], payloads[0][1], payloads[0][4]), (2, 'currencies', baseline))
        self.assertEqual(payloads[0][5], [['money', 'Money', '', '1300000', '', '']])
        self.assertIn('Money 130g 0s 0c', job['character'])

    def test_unreadable_row_is_partial_and_kept_as_last_seen(self):
        self.sim.lua.execute(b"STUB.currency('Emblem of Triumph')[6] = nil")
        self.sim.g.STUB.fire(b'CURRENCY_DISPLAY_UPDATE')
        self.sim.run(3)
        value, _ = self.document()
        self.assertEqual(value[2], 'partial')
        self.assertIn('absence does not mean none held', value[3])
        self.assertEqual(value[4], ROWS)

    def test_missing_api_is_not_no_currencies(self):
        self.sim.lua.execute(b'GetCurrencyListSize = nil; GetCurrencyListInfo = nil')
        fields = self.sim.lua.table(); self.sim.ns.CharacterFields(fields)
        context = b'\n'.join(v[2] for v in fields.values() if v[1] == b'ctx')
        self.assertIn(b'Currency list API unavailable; do not infer no currencies.', context)

    def test_manual_sync_includes_currencies(self):
        ok, _ = self.sim.ns.SyncCharacter()
        self.assertTrue(ok)
        self.assertTrue(self.sim.run(900, until=lambda: not self.sim.ns.CharacterSyncBusy()))
        messages = '\n'.join(v.decode() for v in self.sim.g.STUB.prints.values())
        self.assertIn('Character sync complete:', messages)
        self.assertIn('currencies', messages)
        self.assertTrue(any(fields.get('section') == ['currencies'] for fields, _ in self.accepted.values()))
        self.assertEqual(self.sim.jobs, {}, 'manual sync must not launch an agent')


class CurrencyValidation(unittest.TestCase):
    def doc(self, *rows):
        return [1, 'currencies', 'complete', 'Currency tab tokens.', [list(r) for r in rows]]

    def test_accepts_the_shapes_the_addon_sends(self):
        value = self.doc(*ROWS, ['name:Stone Keeper\'s Shard', "Stone Keeper's Shard", 'Miscellaneous', '120', '', ''])
        self.assertEqual(validate_document(value, 'currencies'), value)
        self.assertTrue(canonical(value))

    def test_rejects_malformed_rows(self):
        good = ROWS[7]
        for bad in (
            ['item:0'] + good[1:4] + ['0', ''],               # no item 0
            good[:4] + ['49427', 'watched'],                  # item ID differs from the key
            ['honor'] + good[1:4] + ['49426', ''],            # honor is not an item
            good[:3] + ['-1'] + good[4:],                     # negative count
            good[:3] + ['01'] + good[4:],                     # not a canonical number
            good[:5] + ['bogus'],                             # unknown flag
            good[:5] + ['watched, watched'],                  # a flag twice
            ['gold'] + good[1:4] + ['', ''],                  # unknown key
            good[:5],                                         # a column short
        ):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                validate_document(self.doc(bad), 'currencies')
        with self.assertRaises(ValueError):
            validate_document(self.doc(good, good), 'currencies')

    def test_summary_leaves_out_unused_and_stays_bounded(self):
        line = currency_summary(ROWS)
        self.assertNotIn('Badge of Justice', line)
        self.assertTrue(line.endswith('Money 123g 45s 67c'))
        many = [[f'item:{i}', f'Token number {i}', 'Misc', '1', str(i), ''] for i in range(1, 400)]
        self.assertLessEqual(len(currency_summary(many)), 1500)


if __name__ == '__main__':
    unittest.main()
