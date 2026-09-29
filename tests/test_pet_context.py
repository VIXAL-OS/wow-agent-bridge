"""Collection/active-pet/stable observations through production Lua and transport."""
import json
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest

from companion.character import CharacterStore, canonical, manifest, revision, validate_document
from companion.savedvariables import SavedSnapshots
from companion.protocol import frame_kind
from tests.harness import Sim
from tests.test_character import envelope
from tests.test_e2e import agent
from tests.test_savedvariables import literal


PETS = r'''
STUB.collections = {
    MOUNT = {{1001, 'Snow Gryphon', 2001}, {1002, 'Swift Steed', 2002}},
    CRITTER = {{3001, 'Tiny Snowman', 4001}, {3002, 'Tiny Snowman', 4002}},
}
STUB.collectionReads, STUB.petReads, STUB.stableReads = 0, 0, 0
function GetNumCompanions(kind) return #STUB.collections[kind] end
function GetCompanionInfo(kind, index)
    STUB.collectionReads = STUB.collectionReads + 1
    local row = STUB.collections[kind][index]
    if row then return unpack(row) end
end
STUB.pet = {exists = true, name = 'Frostfang', family = 'Wolf', level = 42, tree = 'Ferocity', hunter = true}
local playerName, playerLevel = UnitName, UnitLevel
function UnitExists(unit) return unit == 'pet' and STUB.pet.exists end
function UnitName(unit) if unit == 'pet' then STUB.petReads = STUB.petReads + 1; return STUB.pet.name end return playerName(unit) end
function UnitLevel(unit) if unit == 'pet' then return STUB.pet.level end return playerLevel(unit) end
function UnitCreatureFamily(unit) assert(unit == 'pet'); return STUB.pet.family end
function HasPetUI() return STUB.pet.exists, STUB.pet.hunter end
function GetPetTalentTree() return STUB.pet.tree end
STUB.stable = {[0] = {'icon', 'Frostfang', 42, 'Wolf', 'Ferocity'}, [1] = {'icon', 'Snow', 40, 'Cat', 'Ferocity'}}
STUB.stableOpen = false
function GetNumStableSlots() assert(STUB.stableOpen); return 4 end
function GetStablePetInfo(slot)
    assert(STUB.stableOpen, 'Stable read while closed')
    STUB.stableReads = STUB.stableReads + 1
    if STUB.closeDuringScan then
        STUB.closeDuringScan = nil; STUB.stableOpen = false; STUB.fire('PET_STABLE_CLOSED')
    end
    local row = STUB.stable[slot]
    if row then return unpack(row) end
end
STUB.fire('PLAYER_ENTERING_WORLD')
'''


class PetSnapshots(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = Sim(tmp.name, agent(lambda p: 'Echo: ' + p), saved={'alpha': .2}, slots=200)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute(PETS.encode())
        self.sim.run(3)

    def document(self, section):
        fields = self.sim.lua.table()
        bundle = self.sim.ns.CharacterFields(fields)
        doc = bundle[b'docs'][section.encode()]
        return (json.loads(doc[b'body']), doc) if doc else (None, None)

    def finish(self, text):
        self.sim.send(text)
        self.assertTrue(self.sim.run(200, until=lambda: self.sim.last_reply() == 'Echo: ' + text), self.sim.body())

    def test_sync_collections_and_active_pet_then_reuse_unchanged(self):
        traffic = {'upload_started': False, 'redundant_prompt_frames': 0, 'resumed_prompt_frames': 0}
        def observe(data, alpha):
            kind = frame_kind(data)
            if kind == 'character':
                traffic['upload_started'] = True
            elif kind == 'prompt' and traffic['upload_started']:
                missing = any(self.sim.character.missing(fields) for fields, _ in self.sim.character_pending.values())
                traffic['redundant_prompt_frames' if missing else 'resumed_prompt_frames'] += 1
            return data
        self.sim.optical_filter = observe
        self.finish('pets and mounts')
        self.assertEqual(traffic['redundant_prompt_frames'], 0, 'Known prompts must not compete with their uploads')
        self.assertGreater(traffic['resumed_prompt_frames'], 0, 'A fresh complete prompt must still start the agent')
        job = next(iter(self.sim.jobs.values()))
        for section, count in (('mounts', 2), ('pets', 2), ('combatpet', 1)):
            self.assertIn(f'{section}: {count} records; complete', job['character'])
        mounts, _ = self.document('mounts')
        self.assertEqual(mounts[4][0], ['2001', 'Snow Gryphon', '1001'])
        pets, _ = self.document('pets')
        self.assertEqual(len(pets[4]), 2, 'Duplicate names keep distinct summon spell IDs')
        combat, _ = self.document('combatpet')
        self.assertEqual(combat[4], [['active', 'Frostfang', 'Wolf', '42', 'Ferocity', 'hunter']])
        self.assertIn('Summon spell ID', self.sim.character.render('owner', 'mounts', 'complete', '', mounts[4]))
        self.assertIn('Active slot', self.sim.character.render('owner', 'combatpet', 'complete', '', combat[4]))
        before = self.sim.character_frames
        self.finish('unchanged')
        self.assertEqual(before, self.sim.character_frames)

    def test_learning_unlearning_and_reordering_apply_by_spell_id(self):
        self.finish('baseline')
        _, before = self.document('mounts')
        baseline = before[b'revision']
        self.sim.lua.execute(b"STUB.collections.MOUNT[1], STUB.collections.MOUNT[2] = STUB.collections.MOUNT[2], STUB.collections.MOUNT[1]")
        self.sim.g.STUB.fire(b'COMPANION_UPDATE', b'MOUNT'); self.sim.run(3)
        _, reordered = self.document('mounts')
        self.assertEqual(baseline, reordered[b'revision'])
        self.sim.lua.execute(b"STUB.collections.MOUNT = {{1001, 'Snow Gryphon', 2001}, {1003, 'New Drake', 2003}}")
        self.sim.g.STUB.fire(b'COMPANION_LEARNED'); self.sim.run(3)
        self.finish('changed mounts')
        value, doc = self.document('mounts')
        self.assertEqual([r[0] for r in value[4]], ['2001', '2003'])
        self.assertEqual(json.loads(self.sim.character.body('ChromieCraft:Testbrew', 'mounts', doc[b'revision'].decode())), value)

    def test_partial_collection_keeps_observations_and_empty_is_explicit(self):
        self.sim.lua.execute(b"STUB.collections.MOUNT[2] = {1002}")
        self.sim.g.STUB.fire(b'COMPANION_UPDATE', b'MOUNT'); self.sim.run(3)
        value, _ = self.document('mounts')
        self.assertEqual(value[2], 'partial')
        self.assertEqual(len(value[4]), 2)
        self.sim.lua.execute(b'STUB.collections.MOUNT = {}')
        self.sim.g.STUB.fire(b'COMPANION_UNLEARNED', b'MOUNT'); self.sim.run(3)
        value, _ = self.document('mounts')
        self.assertEqual((value[2], value[4]), ('complete', []))

    def test_combat_pet_rename_level_summon_and_dismiss(self):
        self.sim.lua.execute(b"STUB.pet.name = 'Imp'; STUB.pet.family = 'Imp'; STUB.pet.level = 43; STUB.pet.hunter = false; STUB.pet.tree = nil")
        self.sim.g.STUB.fire(b'UNIT_PET', b'player'); self.sim.run(3)
        value, _ = self.document('combatpet')
        self.assertEqual(value[4], [['active', 'Imp', 'Imp', '43', '', 'summoned']])
        reads = self.sim.g.STUB.petReads
        self.sim.g.STUB.fire(b'UNIT_NAME_UPDATE', b'party1'); self.sim.run(3)
        self.assertEqual(self.sim.g.STUB.petReads, reads)
        self.sim.g.STUB.pet.exists = False
        self.sim.g.STUB.fire(b'UNIT_PET', b'player'); self.sim.run(3)
        value, _ = self.document('combatpet')
        self.assertEqual(value[4], [])
        self.assertIn('dismissed or stabled', value[3])

    def test_stable_is_passive_cached_after_close_and_survives_reload(self):
        self.assertEqual(self.sim.g.STUB.stableReads, 0)
        self.assertIsNone(self.document('stablepets')[0])
        self.sim.g.STUB.stableOpen = True
        self.sim.g.STUB.fire(b'PET_STABLE_SHOW'); self.sim.run(3)
        value, doc = self.document('stablepets')
        self.assertEqual(value[4], [['0', 'Frostfang', 'Wolf', '42', 'Ferocity'], ['1', 'Snow', 'Cat', '40', 'Ferocity']])
        rev = doc[b'revision']
        self.sim.g.STUB.stableOpen = False
        self.sim.g.STUB.fire(b'PET_STABLE_CLOSED'); self.sim.run(3)
        reads = self.sim.g.STUB.stableReads
        fields = self.sim.lua.table(); self.sim.ns.CharacterFields(fields)
        self.assertTrue(any(v[1] == b'snapshot' and v[2].startswith(b'stablepets|') and v[2].endswith(b'|cached') for v in fields.values()))
        self.sim.run(3)
        self.assertEqual(self.sim.g.STUB.stableReads, reads)
        self.sim.reload()
        _, loaded = self.document('stablepets')
        self.assertEqual(loaded[b'revision'], rev)
        self.assertIsNone(loaded[b'session'])

    def test_stable_closing_during_scan_cannot_publish_partial_replacement(self):
        self.sim.g.STUB.stableOpen = True
        self.sim.g.STUB.fire(b'PET_STABLE_SHOW'); self.sim.run(3)
        _, doc = self.document('stablepets')
        rev = doc[b'revision']
        self.sim.lua.execute(b"STUB.stable[1][2] = 'Changed'; STUB.closeDuringScan = true")
        self.sim.g.STUB.fire(b'PET_STABLE_UPDATE'); self.sim.run(3)
        _, doc = self.document('stablepets')
        self.assertEqual(doc[b'revision'], rev)

    def test_opt_out_stops_scans_and_missing_api_is_not_empty_collection(self):
        before = self.sim.g.STUB.collectionReads
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context off')
        self.sim.g.STUB.fire(b'COMPANION_LEARNED'); self.sim.run(3)
        self.assertEqual(self.sim.g.STUB.collectionReads, before)
        self.sim.g.SlashCmdList.AGENTBRIDGE(b'context on'); self.sim.run(3)
        self.assertGreater(self.sim.g.STUB.collectionReads, before)
        self.sim.lua.execute(b'GetNumCompanions = nil; GetCompanionInfo = nil; UnitExists = nil')
        fields = self.sim.lua.table(); self.sim.ns.CharacterFields(fields)
        context = b'\n'.join(v[2] for v in fields.values() if v[1] == b'ctx')
        self.assertIn(b'APIs unavailable', context)
        self.assertIn(b'do not infer no pet', context)

    def test_character_switch_does_not_inherit_collections(self):
        self.sim.lua.execute(b"function UnitName(unit) return unit == 'player' and 'Another' or nil end; STUB.collections = {MOUNT={}, CRITTER={}}; STUB.pet.exists = false")
        self.sim.g.STUB.fire(b'PLAYER_ENTERING_WORLD'); self.sim.run(3)
        for section in ('mounts', 'pets', 'combatpet'):
            self.assertEqual(self.document(section)[0][4], [])
        saved = self.sim.saved()['characterCollections']
        self.assertIn('ChromieCraft:Testbrew', saved)
        self.assertIn('ChromieCraft:Another', saved)


class PetSnapshotValidation(unittest.TestCase):
    def test_collection_cache_import_is_scoped_and_active_pet_is_never_imported(self):
        with tempfile.TemporaryDirectory() as tmp, closing(sqlite3.connect(':memory:')) as db:
            owner = 'Realm:Player-A'
            docs = {
                'mounts': [1, 'mounts', 'complete', 'Learned mounts.', [['2001', 'Gryphon', '1001']]],
                'pets': [1, 'pets', 'complete', 'Companion pets.', [['4001', 'Tiny Snowman', '3001']]],
                'stablepets': [1, 'stablepets', 'partial', 'Stable observed.', [['1', 'Snow', 'Cat', '40', 'Ferocity']]],
                'combatpet': [1, 'combatpet', 'complete', 'Active pet.', [['active', 'Frost', 'Wolf', '42', 'Ferocity', 'hunter']]],
            }
            entries = {section: {'body': canonical(doc), 'revision': revision(canonical(doc))} for section, doc in docs.items()}
            path = Path(tmp) / 'WTF/Account/TEST/SavedVariables/AgentBridge.lua'
            path.parent.mkdir(parents=True)
            path.write_text('AgentBridgeState = ' + literal({'characterCollections': {owner: entries}}), encoding='utf-8')
            source = SavedSnapshots(tmp)
            store = CharacterStore(db, Path(tmp) / 'snapshots', source)
            refs = [f'{section}|{entry["revision"]}|1758000000|cached' for section, entry in entries.items()]
            self.assertEqual(store.import_saved({'character': ['Other'], 'snapshot': refs}), 0)
            self.assertEqual(store.import_saved({'character': [owner], 'snapshot': refs}), 3)
            self.assertEqual(store.missing({'character': [owner], 'snapshot': refs}), [('combatpet', entries['combatpet']['revision'])])
            fields = {'character': [owner], 'snapshot': refs[:3]}
            context = store.context(fields)
            self.assertIn('stablepets: 1 records; partial; cached', context)
            changed = [1, 'mounts', 'complete', 'Learned mounts.', [['2001', 'Gryphon', '1001'], ['2002', 'Steed', '1002']]]
            body = canonical(changed)
            patch = canonical([2, 'mounts', 'complete', 'Learned mounts.', entries['mounts']['revision'], [changed[4][1]], []])
            self.assertEqual(store.accept('delta', envelope(owner, 'mounts', revision(body), 1, 1, patch))['reply'], 'ABCTX_OK')

    def test_invalid_ids_slots_levels_and_unknown_sections_are_rejected(self):
        bad = [('mounts', ['0', 'Name', '1']), ('pets', ['1', 'Name', '-1']),
               ('combatpet', ['active', 'Name', 'Wolf', '999', '', 'hunter']),
               ('combatpet', ['active', 'Name', 'Wolf', '40', '', 'other']),
               ('stablepets', ['5', 'Name', 'Cat', '40', 'Ferocity']),
               ('unknown', ['1', 'Name', '2'])]
        for section, row in bad:
            with self.subTest(section=section, row=row), self.assertRaises(ValueError):
                validate_document([1, section, 'complete', '', [row]], section)
        refs = [f'{section}|12345678-10|1758000000|current' for section in
                ('gear', 'bags', 'mounts', 'pets', 'combatpet', 'stablepets', *[f'recipes:{i}' for i in range(6)])]
        self.assertEqual(len(manifest({'character': ['Realm:Player'], 'snapshot': refs})[1]), 12)
