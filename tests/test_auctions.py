import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from companion.agents import AgentConfig, Job, codex_prompt, guidance_for
from companion.auctions import AuctionCache, auction_guidance, decode_record, parse_aux
from tests.test_savedvariables import literal


def saved(markets, names=None):
    return ('aux = ' + literal({'faction': markets}) + '\naux_items = '
            + literal(names or {45912: 'Book of Glyph Mastery#2#0#Recipe#Book##10#icon'})
            + '\naux_scale = 1\naux_ignore_owner = false\naux_item_ids = {}\n').encode()


class AuctionImports(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.game, self.output = self.root / 'game', self.root / 'state/auctions'
        self.source = self.game / 'WTF/Account/A/SavedVariables/aux-addon.lua'
        self.source.parent.mkdir(parents=True)
        self.history = {'45912:0': '1790913600#149500#75000@1790827200;145500@1790740800',
                        '100:-20': '1790913600##20000@1790827200'}
        self.source.write_bytes(saved({'Realm|Alliance': {'history': self.history}}))
        self.cache = AuctionCache(self.game, self.output)

    def records(self, market):
        return [json.loads(line) for line in Path(market['path']).read_text(encoding='utf-8').splitlines()]

    def test_imports_prices_names_dates_and_variants_without_touching_source(self):
        before = self.source.read_bytes()
        index = self.cache.refresh()
        self.assertEqual(self.source.read_bytes(), before)
        market = index['markets'][0]
        self.assertEqual((market['realm'], market['faction'], market['records']), ('Realm', 'Alliance', 2))
        records = {row['item_key']: row for row in self.records(market)}
        book = records['45912:0']
        self.assertEqual(book['name'], 'Book of Glyph Mastery')
        self.assertEqual(book['daily_low_copper'], 149500)
        self.assertEqual(book['aux_value_copper'], 75000)
        self.assertEqual(book['history'][0]['bucket_ends_at'], '2026-10-01T04:00:00+00:00')
        self.assertEqual(records['100:-20']['suffix_id'], -20)
        self.assertIsNone(records['100:-20']['daily_low_copper'])
        self.assertIn('not live listings', index['freshness'])
        self.assertTrue(self.cache.index.exists())

    def test_unchanged_files_are_not_reparsed_or_rewritten(self):
        first = self.cache.refresh()
        paths = [self.cache.index] + [Path(m['path']) for m in first['markets']]
        times = {path: path.stat().st_mtime_ns for path in paths}
        with patch('companion.auctions.parse_aux', side_effect=AssertionError('redundant parse')):
            self.assertEqual(self.cache.refresh(), first)
        self.assertEqual({path: path.stat().st_mtime_ns for path in paths}, times)

    def test_changed_file_publishes_new_version_old_reader_stays_valid(self):
        first = self.cache.refresh()['markets'][0]
        old_rows = self.records(first)
        self.history['45912:0'] = '1790913600#120000#75000@1790827200'
        self.source.write_bytes(saved({'Realm|Alliance': {'history': self.history}}))
        second = self.cache.refresh()['markets'][0]
        self.assertNotEqual(first['path'], second['path'])
        self.assertEqual(self.records(first), old_rows)
        self.assertEqual(next(r for r in self.records(second) if r['item_id'] == 45912)['daily_low_copper'], 120000)

    def test_partial_save_keeps_old_data_stale_then_recovers(self):
        first = self.cache.refresh()['markets'][0]
        self.source.write_bytes(b'aux = {')
        bad = self.cache.refresh()
        self.assertTrue(bad['markets'][0]['stale'])
        self.assertEqual(bad['markets'][0]['path'], first['path'])
        self.assertEqual(bad['sources'][0]['state'], 'unavailable')
        self.source.write_bytes(saved({'Realm|Alliance': {'history': self.history}}))
        self.assertFalse(self.cache.refresh()['markets'][0]['stale'])

    def test_restart_during_partial_save_retains_last_good_import(self):
        first = self.cache.refresh()['markets'][0]
        self.source.write_bytes(b'aux = {')
        restarted = AuctionCache(self.game, self.output)
        index = restarted.refresh()
        self.assertTrue(index['markets'][0]['stale'])
        self.assertEqual(index['markets'][0]['path'], first['path'])
        self.assertEqual(index['sources'][0]['state'], 'unavailable')

    def test_accounts_realms_and_factions_stay_separate(self):
        other = self.game / 'WTF/Account/B/SavedVariables/aux-addon.lua'
        other.parent.mkdir(parents=True)
        other.write_bytes(saved({'Realm|Alliance': {'history': {'45912:0': '1790913600#100#'}},
                                'Realm|Horde': {'history': self.history},
                                'Other Realm|Alliance': {'history': self.history}}))
        index = self.cache.refresh()
        self.assertEqual(len(index['markets']), 4)
        alliance = [m for m in index['markets'] if (m['realm'], m['faction']) == ('Realm', 'Alliance')]
        self.assertEqual(len({m['source'] for m in alliance}), 2)
        self.assertEqual({next(r for r in self.records(m) if r['item_id'] == 45912)['daily_low_copper'] for m in alliance}, {100, 149500})

    def test_missing_source_does_not_advertise_old_market(self):
        self.cache.refresh()
        self.source.unlink()
        self.assertEqual(self.cache.refresh()['markets'], [])

    def test_bad_records_are_counted_unknown_not_zero(self):
        self.history['bad-key'] = 'not-a-price'
        self.history['100:0'] = '1790913600#-1#'
        self.source.write_bytes(saved({'Realm|Alliance': {'history': self.history}}))
        market = self.cache.refresh()['markets'][0]
        self.assertEqual((market['records'], market['rejected_records']), (2, 2))

    def test_background_import_is_independent_of_prompts_and_stops(self):
        stop, messages = threading.Event(), []
        def received(message):
            messages.append(message); stop.set()
        worker = threading.Thread(target=self.cache.run, args=(stop, received))
        worker.start(); worker.join(5)
        self.assertFalse(worker.is_alive())
        self.assertTrue(self.cache.index.exists())
        self.assertEqual(len(messages), 1)

    def test_record_limit_and_escaping_scope_never_become_cache_paths(self):
        self.source.write_bytes(saved({'../../outside|Alliance': {'history': self.history}}))
        result = self.cache.refresh()
        self.assertTrue(Path(result['markets'][0]['path']).is_relative_to(self.output))
        with patch('companion.auctions.MAX_RECORDS', 1):
            self.source.write_bytes(saved({'Realm|Alliance': {'history': self.history}}))
            result = self.cache.refresh()
            self.assertTrue(result['markets'][0]['stale'])
            self.assertIn('limit', result['sources'][0]['error'])


class AuctionLiteralSafety(unittest.TestCase):
    def test_parser_accepts_only_known_complete_literal_assignments(self):
        good = saved({'Realm|Alliance': {'history': {}}})
        self.assertIn('aux', parse_aux(good))
        bad = [good + b'os.execute("bad")', good + b'aux = {}', good + b'evil = {}',
               good.replace(b'aux_scale = 1', b'aux_scale = 1 + 2'),
               b'aux = loadstring("bad")()', b'aux = {', b'aux = { ["faction"] = function() end }']
        for data in bad:
            with self.subTest(data=data[-60:]), self.assertRaises(ValueError):
                parse_aux(data)

    def test_prices_are_validated_and_history_is_bounded(self):
        bad = ['1790913600#nan#', '1790913600#0#', '1790913600#1.5#',
               '1790913600#1#1@2;bad', '9999999999999999#1#',
               '1790913600#1#' + ';'.join('1@1790827200' for _ in range(12))]
        for encoded in bad:
            with self.subTest(encoded=encoded), self.assertRaises(ValueError):
                decode_record('45912:0', encoded, {})


class AuctionDiscovery(unittest.TestCase):
    def test_agents_receive_lookup_pointer_not_prices_even_when_resuming(self):
        with tempfile.TemporaryDirectory() as tmp:
            index = Path(tmp) / 'auctions/index.json'
            resource = auction_guidance(index)
            cfg = AgentConfig('claude', Path(tmp), guidance_file=Path(tmp) / 'guidance.txt')
            cfg.guidance_file.write_text('Standing instructions', encoding='utf-8')
            for resume in (None, 'existing'):
                job = Job('k:1', 'How much is this?', resume=resume, resources=resource)
                # Hermes also uses codex_prompt(job) in its adapter.
                prompt = codex_prompt(job)
                self.assertIn(str(index.resolve()), prompt)
                self.assertNotIn('149500', prompt)
                self.assertNotIn('history":', prompt)
                path, temporary = guidance_for(cfg, job)
                self.assertTrue(temporary)
                self.assertIn(resource, path.read_text())
                self.assertEqual(job.context, '', 'local cache does not become game snapshot data')


if __name__ == '__main__':
    unittest.main()
