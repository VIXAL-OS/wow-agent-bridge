"""Gems by socket in the gear and bag snapshots: names, empty sockets, added sockets, socket bonuses."""
import json
from pathlib import Path
import re
import tempfile
import unittest

from companion.character import CharacterStore, gem_summary, validate_document
from tests.harness import Sim
from tests.test_e2e import agent

FIXTURES = r'''
EMPTY_SOCKET_META, EMPTY_SOCKET_RED, EMPTY_SOCKET_YELLOW = 'Meta Socket', 'Red Socket', 'Yellow Socket'
EMPTY_SOCKET_BLUE, EMPTY_SOCKET_NO_COLOR = 'Blue Socket', 'Prismatic Socket'
ITEM_SOCKET_BONUS, ITEM_MOD_STAMINA_SHORT = 'Socket Bonus: %s', 'Stamina'
STUB.gear = {
    [1] = '|Hitem:45439:3819:3623:3473:0:0:0:0:80|h[Unwavering Stare]|h',
    [6] = '|Hitem:39721:0:3448:0:0:0:0:0:80|h[Sash of the Parlor]|h',
    [7] = "|Hitem:46202:3720:3488:0:0:0:0:0:80|h[Conqueror's Worldbreaker Legguards]|h",
    [9] = '|Hitem:45316:2332:0:0:0:0:0:0:80|h[Armbraces of the Vibrant Flame]|h',
    [10] = "|Hitem:46199:3246:3488:0:0:0:0:0:80|h[Conqueror's Worldbreaker Handguards]|h",
    [11] = "|Hitem:49124:0:0:0:0:0:0:0:80|h[Wicked Witch's Band]|h",
}
STUB.bag = {['0:1'] = '|Hitem:6948:0:0:0:0:0:0:0:80|h[Hearthstone]|h',
    ['2:16'] = '|Hitem:40518:3838:3488:0:0:0:0:0:80|h[Valorous Earthshatter Shoulderpads]|h'}
STUB.equip = {[40518] = 'INVTYPE_SHOULDER', [46209] = 'INVTYPE_HEAD'}
STUB.sockets = {[45439] = {EMPTY_SOCKET_META = 1, EMPTY_SOCKET_RED = 1}, [46202] = {EMPTY_SOCKET_RED = 1, EMPTY_SOCKET_BLUE = 1},
    [46199] = {EMPTY_SOCKET_YELLOW = 1}, [40518] = {EMPTY_SOCKET_YELLOW = 1}, [46209] = {EMPTY_SOCKET_META = 1}}
-- Gem enchantment code in a link -> gem item, as the client's tables map them.
STUB.gemOf = {[3623] = 41285, [3473] = 40113, [3448] = 40026, [3488] = 40128, [3621] = 41285}
STUB.gemInfo = {[41285] = {'Chaotic Skyflare Diamond', 'Meta'}, [40113] = {'Runed Cardinal Ruby', 'Red'},
    [40026] = {'Purified Dreadstone', 'Purple'}, [40128] = {"Quick King's Amber", 'Yellow'}}
STUB.uncached, STUB.asked, STUB.cacheOnAsk = {}, {}, false

local function jewels(link)
    local a, b, c, d = link:match('item:%-?%d+:%-?%d+:(%-?%d+):(%-?%d+):(%-?%d+):(%-?%d+)')
    return {tonumber(a), tonumber(b), tonumber(c), tonumber(d)}
end
function GetItemGem(link, index)
    local gem = STUB.gemOf[jewels(link)[index] or 0]
    local info = gem and not STUB.uncached[gem] and STUB.gemInfo[gem]
    if info then return info[1], '|Hitem:'..gem..':0:0:0:0:0:0:0:80|h['..info[1]..']|h' end
end
local function gemIDs(link)
    if not link then return end
    local j = jewels(link)
    return STUB.gemOf[j[1]] or 0, STUB.gemOf[j[2]] or 0, STUB.gemOf[j[3]] or 0
end
function GetInventoryItemGems(slot) return gemIDs(STUB.gear[slot]) end
function GetContainerItemGems(bag, slot) return gemIDs(STUB.bag[bag..':'..slot]) end
function GetItemInfo(link)
    local id = tonumber(tostring(link):match('item:(%d+)'))
    local gem = STUB.gemInfo[id]
    if gem then
        if STUB.uncached[id] then return nil end
        return gem[1], link, 3, 80, 0, 'Gem', gem[2], 20, ''
    end
    local name = tostring(link):match('|h%[(.-)%]|h')
    if name then return name, link, 4, 226, 80, 'Armor', 'Mail', 1, STUB.equip[id] or '' end
end
function GetItemStats(link)
    local stats = {ITEM_MOD_STAMINA_SHORT = 10}
    for k, v in pairs(STUB.sockets[tonumber(link:match('item:(%d+)'))] or {}) do stats[k] = v end
    return stats
end
function GetInventoryItemLink(_, slot) return STUB.gear[slot] end
function GetInventoryItemTexture(_, slot) if STUB.gear[slot] then return 'texture' end end
function GetContainerNumSlots(bag) return 20 end
function GetContainerItemInfo(bag, slot) if STUB.bag[bag..':'..slot] then return 'texture', 1 end end
function GetContainerItemLink(bag, slot) return STUB.bag[bag..':'..slot] end

local grey, green = .5, {.1, 1, .1}
STUB.tooltips['item:45439:3819:0:0:0:0:0:0:80'] = {'Unwavering Stare', 'Meta Socket', 'Red Socket', 'Socket Bonus: +5 Spell Power'}
STUB.tooltips['inventory:1'] = {'Unwavering Stare', '+21 Critical Strike Rating and 3% Increased Critical Damage',
    '+23 Spell Power', {'Socket Bonus: +5 Spell Power', grey, grey, grey}}
STUB.tooltips['inventory:6'] = {'Sash of the Parlor', '+10 Spell Power and +10 Spirit'}
STUB.tooltips["item:46202:3720:0:0:0:0:0:0:80"] = {"Conqueror's Worldbreaker Legguards", 'Red Socket', 'Blue Socket',
    'Socket Bonus: +6 Intellect'}
STUB.tooltips['inventory:7'] = {"Conqueror's Worldbreaker Legguards", '+10 Haste Rating and +10 Critical Strike Rating',
    'Blue Socket', {'Socket Bonus: +6 Intellect', grey, grey, grey}}
STUB.tooltips['inventory:9'] = {'Armbraces of the Vibrant Flame', 'Prismatic Socket'}
STUB.tooltips['item:46199:3246:0:0:0:0:0:0:80'] = {"Conqueror's Worldbreaker Handguards", 'Yellow Socket',
    'Socket Bonus: +4 Haste Rating'}
-- An inline colour code, as some tooltip lines carry, wins over the font colour.
STUB.tooltips['inventory:10'] = {"Conqueror's Worldbreaker Handguards", '+20 Haste Rating',
    {'|cff20ff20Socket Bonus: +4 Haste Rating|r', 1, 1, 1}}
STUB.tooltips['inventory:11'] = {"Wicked Witch's Band"}
STUB.tooltips['item:40518:3838:0:0:0:0:0:0:80'] = {'Valorous Earthshatter Shoulderpads', 'Yellow Socket',
    'Socket Bonus: +4 Intellect'}
STUB.tooltips['bag:2:16'] = {'Valorous Earthshatter Shoulderpads', '+20 Haste Rating', {'Socket Bonus: +4 Intellect', unpack(green)}}
STUB.tooltips['item:46209:3820:0:0:0:0:0:0:80'] = {"Conqueror's Worldbreaker Helm", 'Meta Socket', 'Socket Bonus: +6 Spell Power'}
STUB.tooltips['bag:3:7'] = {"Conqueror's Worldbreaker Helm", '+21 Critical Strike Rating',
    {'Socket Bonus: +6 Spell Power', grey, grey, grey}}
for id, info in pairs(STUB.gemInfo) do STUB.tooltips['item:'..id] = {info[1]} end
-- Showing an uncached gem's tooltip is what makes the client fetch it.
local tip = AgentBridgeTip
local show = tip.SetHyperlink
tip.SetHyperlink = function(self, data)
    STUB.asked[data] = (STUB.asked[data] or 0) + 1
    local id = tonumber(data:match('^item:(%d+)$'))
    if id and STUB.cacheOnAsk then STUB.uncached[id] = nil end
    return show(self, data)
end
STUB.fire('PLAYER_ENTERING_WORLD')
'''

GEAR = {
    '1': 'Meta socket: Chaotic Skyflare Diamond (meta gem, item 41285); '
         'Red socket: Runed Cardinal Ruby (red gem, item 40113); socket bonus +5 Spell Power: inactive',
    '6': 'Added prismatic socket: Purified Dreadstone (purple gem, item 40026)',
    '7': "Red socket: Quick King's Amber (yellow gem, item 40128); Blue socket: empty; "
         'socket bonus +6 Intellect: inactive',
    '9': 'Added prismatic socket: empty',
    '10': "Yellow socket: Quick King's Amber (yellow gem, item 40128); socket bonus +4 Haste Rating: active",
    '11': '',
}


class GemsInGame(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.sim = Sim(self.tmp.name, agent(lambda p: 'Echo: ' + p), slots=160)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute(FIXTURES.encode())
        self.sim.run(3)

    def document(self, section):
        bundle = self.sim.ns.CharacterFields(self.sim.lua.table())
        return json.loads(bundle[b'docs'][section.encode()][b'body'])

    def rows(self, section):
        return {row[0]: row for row in self.document(section)[4]}

    def test_equipped_gems_by_socket(self):
        doc = self.document('gear')
        self.assertEqual(doc[2], 'complete')
        rows = {row[0]: row for row in doc[4]}
        self.assertEqual({slot: rows[slot][5] for slot in GEAR}, GEAR)
        self.assertTrue(all(len(row) == 6 for row in rows.values()))
        self.assertEqual(rows['0'][5], '', 'an empty slot has no gems')

    def test_bagged_gear_gems_and_nothing_for_other_items(self):
        rows = self.rows('bags')
        self.assertEqual(rows['2:16'][4],
                         "Yellow socket: Quick King's Amber (yellow gem, item 40128); socket bonus +4 Intellect: active")
        self.assertEqual(rows['0:1'][4], '', 'a hearthstone is not gear, so no socket tooltips are read')

    def test_uncached_gem_is_named_by_id_fetched_and_rescanned(self):
        self.sim.lua.execute(b"STUB.uncached[41285] = true; STUB.cacheOnAsk = true; "
                             b"STUB.bag['3:7'] = '|Hitem:46209:3820:3621:0:0:0:0:0:80|h[Conqueror\\'s Worldbreaker Helm]|h'; "
                             b"STUB.fire('BAG_UPDATE', 3)")
        self.sim.run(1)
        doc = self.document('bags')
        self.assertEqual(doc[2], 'partial')
        self.assertIn('Meta socket: gem item 41285, name not cached yet', {r[0]: r for r in doc[4]}['3:7'][4])
        self.assertEqual(self.sim.g.STUB.asked[b'item:41285'], 1, 'its tooltip asks the server for the gem')
        self.sim.run(5)
        doc = self.document('bags')
        self.assertEqual(doc[2], 'complete')
        self.assertEqual({r[0]: r for r in doc[4]}['3:7'][4],
                         'Meta socket: Chaotic Skyflare Diamond (meta gem, item 41285); socket bonus +6 Spell Power: inactive')

    def test_an_uncached_first_gem_keeps_later_socket_colours(self):
        self.sim.lua.execute(b"STUB.uncached[41285] = true; STUB.fire('PLAYER_EQUIPMENT_CHANGED', 1)")
        self.sim.run(1)
        self.assertEqual(self.rows('gear')['1'][5],
                         'Meta socket: gem item 41285, name not cached yet; '
                         'Red socket: Runed Cardinal Ruby (red gem, item 40113); socket bonus +5 Spell Power: inactive')

    def test_unreadable_socket_colours_are_numbered_not_guessed(self):
        self.sim.lua.execute(b"STUB.tooltips['item:45439:3819:0:0:0:0:0:0:80'] = {'Unwavering Stare', 'Socket Bonus: +5'}; "
                             b"STUB.fire('PLAYER_EQUIPMENT_CHANGED', 1)")
        self.sim.run(1)
        doc = self.document('gear')
        self.assertEqual(doc[2], 'partial')
        self.assertTrue({r[0]: r for r in doc[4]}['1'][5].startswith(
            'Socket 1: Chaotic Skyflare Diamond (meta gem, item 41285); Socket 2: Runed Cardinal Ruby'))

    def test_a_gem_that_never_arrives_stops_rescanning(self):
        self.sim.lua.execute(b"STUB.uncached[40113] = true; STUB.fire('PLAYER_EQUIPMENT_CHANGED', 1)")
        self.sim.run(60)
        self.assertEqual(self.sim.g.STUB.asked[b'item:40113'], 6, 'one scan and five retries')
        doc = self.document('gear')
        self.assertEqual(doc[2], 'partial')
        self.assertIn('Red socket: gem item 40113, name not cached yet', {r[0]: r for r in doc[4]}['1'][5])

    def test_prompt_carries_a_gem_summary_and_the_file_has_every_socket(self):
        self.sim.send('what gems do I have')
        ok = self.sim.run(300, until=lambda: self.sim.last_reply() == 'Echo: what gems do I have'
                          and self.sim.status()[b'pending'] == 0)
        self.assertTrue(ok, self.sim.last_reply())
        context = next(iter(self.sim.jobs.values()))['character']
        self.assertIn("Equipped gems: 2x Quick King's Amber, 1x Chaotic Skyflare Diamond, 1x Purified Dreadstone, "
                      '1x Runed Cardinal Ruby; 2 empty sockets; socket bonus active on 1 of 3 items.', context)
        files = re.findall(r'Full snapshot file: (.+)', context)
        gear = next(Path(f).read_text(encoding='utf-8') for f in files if 'Section: gear' in Path(f).read_text(encoding='utf-8'))
        self.assertIn('Slot\tName\tItem link\tItem level\tStats\tGems', gear)
        self.assertIn('Legs\t' + "Conqueror's Worldbreaker Legguards", gear)
        self.assertIn(GEAR['7'], gear)


class CompanionRows(unittest.TestCase):
    def test_old_and_new_rows_both_validate_and_render(self):
        old = [1, 'gear', 'complete', 'Equipped.', [['1', 'Hat', 'item:1:0:0:0:0:0:0:0:80', '200', 'Stamina=1']]]
        new = [1, 'gear', 'complete', 'Equipped.', [['1', 'Hat', 'item:1:0:3473:0:0:0:0:0:80', '200', 'Stamina=1',
                                                     'Red socket: Runed Cardinal Ruby (red gem, item 40113)']]]
        mixed = [1, 'gear', 'complete', 'Equipped.', old[4] + [['2', 'Necklace', 'item:2:0:0:0:0:0:0:0:80', '200', '', '']]]
        for value in (old, new, mixed):
            validate_document(value, 'gear')
        validate_document([1, 'bags', 'complete', 'Bags.', [['0:1', 'Hat', 'item:1', '1', 'Red socket: empty']]], 'bags')
        with self.assertRaises(ValueError):
            validate_document([1, 'gear', 'complete', 'Equipped.', [new[4][0] + ['extra']]], 'gear')
        text = CharacterStore.render('Owner', 'gear', 'complete', 'Equipped.', old[4])
        self.assertIn('Head\tHat\titem:1:0:0:0:0:0:0:0:80\t200\tStamina=1\t', text)
        self.assertEqual(gem_summary(old[4]), '')
        self.assertEqual(gem_summary(new[4]), '1x Runed Cardinal Ruby. Details per socket are in the gear file.')


if __name__ == '__main__':
    unittest.main()
