import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from companion.app import App, Inbox, Scheduler
from companion.character import CharacterStore, canonical, revision
from companion.savedvariables import SavedSnapshots, parse_savedvariables
from tests.test_character import envelope


def literal(value):
    if isinstance(value, str):
        return json.dumps(value, ensure_ascii=False)
    if isinstance(value, dict):
        return '{' + ','.join('[' + literal(k) + ']=' + literal(v) for k, v in value.items()) + '}'
    if isinstance(value, list):
        return '{' + ','.join(literal(v) for v in value) + '}'
    return str(value)


def saved(owners):
    return ('AgentBridgeState = ' + literal({'characterRecipes': owners}) + '\n').encode('utf-8')


class SavedLiteralReader(unittest.TestCase):
    def test_wow_tables_comments_numbers_and_byte_escapes(self):
        data = (b'\xef\xbb\xbf\r\nAgentBridgeState = {\n'
                b'["utf8"] = "\\195\\169", ["quoted"] = "a\\\"b\\\\c",\n'
                b'["history"] = {"line\\\r\nnext", -- [1]\n false, true, nil},\n'
                b'["x"] = -0.3950387519711767, ["seen"] = 1790689089, ["n"] = 1e2,\n'
                b'}; -- end\n')
        value = parse_savedvariables(data)
        self.assertEqual(value['utf8'], 'é')
        self.assertEqual(value['quoted'], 'a"b\\c')
        self.assertEqual(value['history'], {1: 'line\nnext', 2: False, 3: True, 4: None})
        self.assertEqual(value['seen'], 1790689089)
        self.assertEqual(value['n'], 100)

    def test_rejects_executable_ambiguous_and_truncated_source(self):
        invalid = [
            b'AgentBridgeState = os.execute("anything")',
            b'AgentBridgeState = {}; os.execute("anything")',
            b'AgentBridgeState = { ["x"] = (function() return {} end)() }',
            b'AgentBridgeState = { ["x"] = "a" .. "b" }',
            b'AgentBridgeState = { ["x"] = 1 + 2 }',
            b'AgentBridgeState = { ["x"] = 1, ["x"] = 2 }',
            b'AgentBridgeState = { [{}] = 1 }',
            b'AgentBridgeState = { [true] = 1 }',
            b'AgentBridgeState = { ["x"] = 1e999 }',
            b'AgentBridgeState = { ["x"] = "\\999" }',
            b'AgentBridgeState = { ["x"] = "\\q" }',
            b'AgentBridgeState = { ["x"] = "\xff" }',
            b'AgentBridgeState = { ["x"] = "unfinished',
            b'AgentBridgeState = { ["x"] = {}',
            b'OtherState = {}', b'AgentBridgeState = nil',
            b'AgentBridgeState = {}; --[[ not a line comment ]] code',
        ]
        for data in invalid:
            with self.subTest(data=data), self.assertRaises(ValueError):
                parse_savedvariables(data)

    def test_parser_has_size_depth_and_value_limits(self):
        with patch('companion.savedvariables.MAX_FILE', 8), self.assertRaises(ValueError):
            parse_savedvariables(b'AgentBridgeState = {}')
        with patch('companion.savedvariables.MAX_DEPTH', 2), self.assertRaises(ValueError):
            parse_savedvariables(b'AgentBridgeState = {{{{}}}}')
        with patch('companion.savedvariables.MAX_VALUES', 3), self.assertRaises(ValueError):
            parse_savedvariables(b'AgentBridgeState = {1,2,3,4}')


class SavedRecipeImport(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.game = Path(self.tmp.name)
        self.file = self.game / 'WTF/Account/TEST/SavedVariables/AgentBridge.lua'
        self.file.parent.mkdir(parents=True)
        self.now = 0
        self.source = SavedSnapshots(self.game, clock=lambda: self.now)
        self.db = sqlite3.connect(':memory:')
        self.addCleanup(self.db.close)
        self.store = CharacterStore(self.db, self.game / 'characters', self.source)
        self.owner, self.section = 'ChromieCraft:Player-A', 'recipes:Inscription'
        self.rows = [[str(i), f'Glyph {i} — é', str(1000 + i)] for i in range(420)]
        self.body = canonical([1, self.section, 'partial', 'Some filters active.', self.rows])
        self.rev = revision(self.body)
        self.fields = {'character': [self.owner], 'snapshot': [f'{self.section}|{self.rev}|1758000000|cached']}
        self.write()

    def write(self, owners=None):
        owners = owners if owners is not None else {self.owner: {self.section: {'revision': self.rev, 'body': self.body}}}
        self.file.write_bytes(saved(owners))
        self.now += 3

    def test_imports_only_requested_identity_and_preserves_coverage_and_freshness(self):
        self.write({self.owner: {self.section: {'revision': self.rev, 'body': self.body},
                                'recipes:Cooking': {'revision': self.rev, 'body': self.body}},
                    'Other:Player': {self.section: {'revision': self.rev, 'body': self.body}}})
        self.assertEqual(self.store.import_saved(self.fields), 1)
        self.assertEqual(self.store.missing(self.fields), [])
        self.assertEqual(self.db.execute('SELECT owner,section,revision FROM character_snapshots').fetchall(),
                         [(self.owner, self.section, self.rev)])
        self.assertIn('420 records; partial; cached', self.store.context(self.fields))
        self.assertEqual(self.store.import_saved(self.fields), 0)

    def test_empty_manifest_does_not_read_files(self):
        with patch.object(self.source, 'candidates', side_effect=AssertionError('Must not read saved files')):
            self.assertEqual(self.store.import_saved({}), 0)
            bags = {'character': [self.owner], 'snapshot': ['bags|12345678-10|1|current']}
            self.assertEqual(self.store.import_saved(bags), 0)

    def test_mismatch_or_invalid_document_keeps_optical_fallback(self):
        bad_schema = canonical([1, self.section, 'complete', '', [['1', 'Recipe', 'not-an-id']]])
        cases = [
            ({'Other:Player': {self.section: {'revision': self.rev, 'body': self.body}}}, self.fields),
            ({self.owner: {self.section: {'revision': '12345678-10', 'body': self.body}}}, self.fields),
            ({self.owner: {self.section: {'revision': self.rev, 'body': self.body + ' '}}}, self.fields),
            ({self.owner: {self.section: {'revision': revision(bad_schema), 'body': bad_schema}}},
             {'character': [self.owner], 'snapshot': [f'{self.section}|{revision(bad_schema)}|1|current']}),
        ]
        for owners, fields in cases:
            with self.subTest(owners=list(owners)):
                self.write(owners)
                self.assertEqual(self.store.import_saved(fields), 0)
                self.assertTrue(self.store.missing(fields))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM character_snapshots').fetchone()[0], 0)

    def test_bad_or_partial_file_is_never_executed_and_recovers_after_rewrite(self):
        self.file.write_bytes(saved({self.owner: {self.section: {'revision': self.rev, 'body': self.body}}})
                              + b'os.execute("must not execute")')
        self.assertEqual(self.store.import_saved(self.fields), 0)
        self.file.write_bytes(b'AgentBridgeState = {')
        self.now += 3
        self.assertEqual(self.store.import_saved(self.fields), 0)
        self.write()
        self.assertEqual(self.store.import_saved(self.fields), 1)

    def test_missing_oversized_and_unreadable_files_fall_back(self):
        self.file.unlink()
        self.assertEqual(self.store.import_saved(self.fields), 0)
        self.write()
        with patch('companion.savedvariables.MAX_FILE', 30):
            self.assertEqual(self.store.import_saved(self.fields), 0)
        self.now += 3
        with patch.object(Path, 'open', side_effect=PermissionError):
            self.assertEqual(self.store.import_saved(self.fields), 0)
        self.now += 3
        self.assertEqual(self.store.import_saved(self.fields), 1)

    def test_unmodified_files_are_cached_and_new_revisions_are_detected(self):
        with patch('companion.savedvariables.parse_savedvariables', wraps=parse_savedvariables) as parser:
            self.assertTrue(list(self.source.candidates(self.owner, [(self.section, self.rev)])))
            self.now += 3
            self.assertTrue(list(self.source.candidates(self.owner, [(self.section, self.rev)])))
            self.assertEqual(parser.call_count, 1)
            self.body = canonical([1, self.section, 'complete', 'Fresh scan.', self.rows])
            self.rev = revision(self.body)
            self.write()
            self.assertTrue(list(self.source.candidates(self.owner, [(self.section, self.rev)])))
            self.assertEqual(parser.call_count, 2)
            self.file.unlink(); self.now += 3
            self.assertEqual(list(self.source.candidates(self.owner, [(self.section, self.rev)])), [])

    def test_does_not_follow_a_candidate_outside_the_account_directory(self):
        outside = self.game / 'outside.lua'
        outside.write_bytes(self.file.read_bytes())
        with patch.object(Path, 'glob', return_value=iter([outside])):
            self.assertEqual(self.store.import_saved(self.fields), 0)

    def test_file_changed_during_read_is_retried(self):
        original = Path.open
        target = self.file.resolve()

        def changing(path, *args, **kwargs):
            if path == target and args == ('rb',):
                with original(path, 'ab') as out:
                    out.write(b'\n')
            return original(path, *args, **kwargs)

        with patch.object(Path, 'open', changing):
            self.assertEqual(self.store.import_saved(self.fields), 0)
        self.now += 3
        self.assertEqual(self.store.import_saved(self.fields), 1)

    def test_imported_baseline_accepts_later_optical_delta(self):
        self.assertEqual(self.store.import_saved(self.fields), 1)
        changed = ['0', 'Changed glyph', '1234']
        target = canonical([1, self.section, 'partial', 'Updated.', [changed] + self.rows[1:]])
        # CharacterStore sorts row IDs lexically when applying patches.
        target = canonical([1, self.section, 'partial', 'Updated.', sorted([changed] + self.rows[1:], key=lambda r: r[0])])
        rev = revision(target)
        delta = canonical([2, self.section, 'partial', 'Updated.', self.rev, [changed], []])
        result = self.store.accept('optical-change', envelope(self.owner, self.section, rev, 1, 1, delta))
        self.assertEqual(result['reply'], 'ABCTX_OK')
        self.assertEqual(self.store.body(self.owner, self.section, rev), target)

    def test_app_starts_job_without_uploading_a_matching_large_recipe_snapshot(self):
        inbox = Inbox(self.game / 'inbox.sqlite3')
        self.addCleanup(inbox.db.close)
        app = SimpleNamespace(inbox=inbox, character=self.store, character_pending={}, scheduler=Scheduler(),
                              backend=Mock(), settings={}, names={}, write=Mock(), job_status=Mock())
        app.backend.get.return_value = 'codex'
        app.name_of = lambda key: key
        blob = f'\x01AB1\ncharacter={self.owner}\nsnapshot={self.fields["snapshot"][0]}\x02What recipes do I know?'
        App.accept_prompt(app, '6162636465666768:1', blob)
        self.assertEqual(app.scheduler.open(), 1)
        request = app.scheduler.take()
        self.assertEqual(request.prompt, 'What recipes do I know?')
        self.assertIn('420 records; partial; cached', request.context)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM character_requests').fetchone()[0], 0)
        app.write.assert_any_call('Loaded 1 matching recipe/collection snapshot(s) from saved addon data.')
