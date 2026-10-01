"""End-to-end automatic sends: tiny changes need no extra character exchange."""
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import Mock

from companion.app import App, Inbox, Scheduler
from companion.character import CharacterStore, canonical, revision
from companion.protocol import parse_envelope
from tests.harness import Sim
from tests.test_character import FIXTURES
from tests.test_achievement_context import ACHIEVEMENTS
from tests.test_pet_context import PETS
from tests.test_e2e import agent


class AutomaticChanges(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.sim = Sim(tmp.name, agent(lambda p: 'Echo: ' + p), slots=320)
        self.addCleanup(self.sim.browser.db.close)
        self.sim.lua.execute((FIXTURES + PETS + ACHIEVEMENTS).encode())
        self.sim.lua.execute(b"STUB.recipeCount = 160; STUB.fire('TRADE_SKILL_SHOW'); STUB.stableOpen = true; STUB.fire('PET_STABLE_SHOW')")
        self.sim.run(5)
        bundle = self.sim.ns.CharacterFields(self.sim.lua.table())
        self.owner = bundle[b'owner'].decode()
        with self.sim.character.db:
            for section, doc in bundle[b'docs'].items():
                self.sim.character.db.execute('INSERT INTO character_snapshots VALUES (?,?,?,?,?)',
                    (self.owner, section.decode(), doc[b'revision'].decode(), doc[b'body'].decode(), 1))
        self.finish('baseline')
        self.accepted = {}
        original = self.sim.character.accept
        def record(key, blob):
            self.accepted[key] = parse_envelope(blob)
            return original(key, blob)
        self.sim.character.accept = record

    def finish(self, prompt):
        self.sim.send(prompt)
        self.assertTrue(self.sim.run(400, until=lambda: self.sim.last_reply() == 'Echo: ' + prompt), self.sim.body())
        self.sim.run(1)
        return next(j for j in self.sim.jobs.values() if j['prompt'] == prompt)

    def docs(self):
        return self.sim.ns.CharacterFields(self.sim.lua.table())[b'docs']

    def test_each_section_changes_independently_without_upload_handshake(self):
        cases = [
            ('recipes:Alchemy', "STUB.recipeCount = 161; STUB.fire('TRADE_SKILL_UPDATE')", ['1162']),
            ('bags', "STUB.bagCount = 17; STUB.fire('BAG_UPDATE', 0)", ['0:1']),
            ('gear', "STUB.gearLink = STUB.gearLink:gsub('item:100:7:', 'item:100:8:'); STUB.fire('PLAYER_EQUIPMENT_CHANGED', 1)", ['1']),
            ('pets', "table.insert(STUB.collections.CRITTER, {3003, 'New pet', 4003}); STUB.fire('COMPANION_LEARNED', 'CRITTER')", ['4003']),
            ('mounts', "table.insert(STUB.collections.MOUNT, {1003, 'New mount', 2003}); STUB.fire('COMPANION_LEARNED', 'MOUNT')", ['2003']),
            ('combatpet', "STUB.pet.name = 'New name'; STUB.fire('UNIT_NAME_UPDATE', 'pet')", ['active']),
            ('stablepets', "STUB.stable[1][2] = 'New stable name'; STUB.fire('PET_STABLE_UPDATE')", ['1']),
        ]
        for section, change, expected in cases:
            with self.subTest(section=section):
                before = {s: d[b'revision'] for s, d in self.docs().items()}
                frames = self.sim.character_frames
                self.sim.lua.execute(change.encode()); self.sim.run(5)
                job = self.finish(section)
                self.assertEqual(self.sim.character_frames, frames)
                self.assertEqual(self.accepted, {})
                values = [json.loads(p) for p in job['fields']['snapshotdata']]
                self.assertEqual([p[1] for p in values], [section])
                payload = values[0]
                if payload[0] == 2:
                    self.assertEqual([r[0] for r in payload[5]], expected)
                changed = [s.decode() for s, d in self.docs().items() if d[b'revision'] != before[s]]
                self.assertEqual(changed, [section])
                current = self.docs()[section.encode()]
                self.assertEqual(self.sim.character.body(self.owner, section, current[b'revision'].decode()), current[b'body'].decode())
        job = self.finish('nothing changed')
        self.assertNotIn('snapshotdata', job['fields'])
        self.assertEqual(self.accepted, {})

    def test_multiple_local_scans_and_multiple_changed_sections_share_initial_prompt(self):
        for count in (161, 162, 163):
            self.sim.lua.execute(f"STUB.recipeCount = {count}; STUB.fire('TRADE_SKILL_UPDATE')".encode())
            self.sim.run(4)
        self.sim.lua.execute(b"STUB.bagCount = 37; STUB.fire('BAG_UPDATE', 0)"); self.sim.run(4)
        job = self.finish('several changes')
        payloads = {p[1]: p for p in map(json.loads, job['fields']['snapshotdata'])}
        self.assertEqual(set(payloads), {'bags', 'recipes:Alchemy'})
        self.assertEqual([r[0] for r in payloads['recipes:Alchemy'][5]], ['1162', '1163', '1164'])
        self.assertEqual(self.accepted, {})

    def test_deleted_rows_are_inline_and_do_not_remove_other_sections(self):
        self.sim.lua.execute(b"table.remove(STUB.collections.CRITTER, 1); STUB.fire('COMPANION_UNLEARNED', 'CRITTER')")
        self.sim.run(4)
        job = self.finish('one pet removed')
        self.assertEqual(self.accepted, {})
        values = [json.loads(p) for p in job['fields']['snapshotdata']]
        self.assertEqual([p[1] for p in values], ['pets'])
        rows = json.loads(self.docs()[b'pets'][b'body'])[4]
        self.assertEqual([r[0] for r in rows], ['4002'])

    def test_old_companion_ignores_inline_data_and_falls_back_to_recipe_only_upload(self):
        self.sim.character.import_inline = lambda fields: 0
        self.sim.lua.execute(b"STUB.recipeCount = 161; STUB.fire('TRADE_SKILL_UPDATE')"); self.sim.run(4)
        self.finish('legacy companion')
        self.assertTrue(self.accepted)
        self.assertTrue(all(f.get('section') == ['recipes:Alchemy'] for f, _ in self.accepted.values()))

    def test_cache_loss_falls_back_to_only_missing_section(self):
        with self.sim.character.db:
            self.sim.character.db.execute("DELETE FROM character_snapshots WHERE section='recipes:Alchemy'")
        self.sim.lua.execute(b"STUB.recipeCount = 161; STUB.fire('TRADE_SKILL_UPDATE')"); self.sim.run(4)
        self.finish('recover recipe baseline')
        self.assertTrue(self.accepted)
        self.assertTrue(all(f.get('section') == ['recipes:Alchemy'] for f, _ in self.accepted.values()))
        doc = self.docs()[b'recipes:Alchemy']
        self.assertEqual(self.sim.character.body(self.owner, 'recipes:Alchemy', doc[b'revision'].decode()), doc[b'body'].decode())

    def test_large_delta_uses_normal_pages_without_resending_unchanged_sections(self):
        self.sim.lua.execute(b"STUB.recipeCount = 260; STUB.fire('TRADE_SKILL_UPDATE')"); self.sim.run(6)
        job = self.finish('many new recipes')
        self.assertNotIn('snapshotdata', job['fields'])
        self.assertTrue(self.accepted)
        self.assertTrue(all(f.get('section') == ['recipes:Alchemy'] for f, _ in self.accepted.values()))

    def test_shortcut_never_truncates_prompt_or_exceeds_wire_budget(self):
        self.sim.lua.execute(b"STUB.recipeCount = 161; STUB.fire('TRADE_SKILL_UPDATE')"); self.sim.run(4)
        # Leave practically no space in the envelope. The normal upload path
        # must handle the change while preserving the user's complete text.
        self.sim.lua.execute(b'''
        local chat = AgentBridge.CurrentChat()
        local fields = {{'chat', chat.id}, {'name', chat.name or chat.auto}}
        AgentBridge.CharacterFields(fields)
        local occupied = #AgentBridge.Envelope(fields, string.rep('Q', 1900))
        local padding = AgentBridge.MAX_PROMPT - occupied - #'ctx=' - 1 - 8
        function AgentBridge.GameContext() return string.rep('x', padding) end
        ''')
        prompt = 'Q' * 1900
        job = self.finish(prompt)
        self.assertEqual(job['prompt'], prompt)
        self.assertNotIn('snapshotdata', job['fields'])
        self.assertTrue(self.accepted)


class InlineValidation(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.db = sqlite3.connect(':memory:'); self.addCleanup(self.db.close)
        self.store = CharacterStore(self.db, self.root)
        self.body = canonical([1, 'pets', 'complete', 'Known companions.', [['4001', 'Pet', '3001']]])
        self.rev = revision(self.body)
        self.fields = {'character': ['owner'], 'snapshot': [f'pets|{self.rev}|1|current'], 'snapshotdata': [self.body]}

    def test_real_app_accepts_inline_data_and_queues_once_without_character_wait(self):
        inbox = Inbox(self.root / 'inbox.sqlite3'); self.addCleanup(inbox.db.close)
        app = SimpleNamespace(inbox=inbox, character=self.store, character_pending={}, scheduler=Scheduler(),
                              backend=Mock(), settings={}, names={}, write=Mock(), job_status=Mock())
        app.backend.get.return_value = 'codex'; app.name_of = lambda key: key
        fields = '\n'.join(f'{k}={v}' for k, vs in self.fields.items() for v in vs)
        blob = f'\x01AB1\n{fields}\n\x02My pets?'
        key = '6162636465666768:10'
        App.accept_prompt(app, key, blob)
        self.assertEqual(app.character_pending, {})
        self.assertEqual(app.scheduler.open(), 1)
        App.accept_prompt(app, key, blob)
        self.assertEqual(app.scheduler.open(), 1)
        request = app.scheduler.take()
        self.assertEqual(request.prompt, 'My pets?')
        self.assertIn('Full snapshot file:', request.context)
        self.assertEqual(self.store.body('owner', 'pets', self.rev), self.body)

    def test_invalid_unreferenced_and_oversized_data_cannot_poison_cache(self):
        cases = [self.body.replace('Pet', 'Wrong'), '{bad json}', '[]',
                 canonical([1, 'mounts', 'complete', '', [['4001', 'Pet', '3001']]]),
                 ' ' * 1800 + self.body]
        for payload in cases:
            with self.subTest(payload=payload[:60]):
                self.assertEqual(self.store.import_inline(dict(self.fields, snapshotdata=[payload])), 0)
                self.assertIsNone(self.store.body('owner', 'pets', self.rev))
        self.assertEqual(self.store.import_inline(self.fields), 1)
        self.assertEqual(self.store.import_inline(self.fields), 0)

    def test_patch_baseline_is_scoped_to_owner_and_target_checksum(self):
        self.store.import_inline(self.fields)
        body = canonical([1, 'pets', 'complete', 'Known companions.', [['4001', 'New pet', '3001']]])
        rev = revision(body)
        patch = canonical([2, 'pets', 'complete', 'Known companions.', self.rev, [['4001', 'New pet', '3001']], []])
        fields = dict(self.fields, snapshot=[f'pets|{rev}|1|current'], snapshotdata=[patch])
        self.assertEqual(self.store.import_inline(dict(fields, character=['other'])), 0)
        self.assertIsNone(self.store.body('other', 'pets', rev))
        self.assertEqual(self.store.import_inline(fields), 1)
        self.assertEqual(self.store.body('owner', 'pets', rev), body)


if __name__ == '__main__':
    unittest.main()
