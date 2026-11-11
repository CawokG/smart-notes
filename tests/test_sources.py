"""Current intake membership and publication use exactly the same policy."""
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from knowledge_ingestion.cli import scan
from knowledge_ingestion.commit import commit
from knowledge_ingestion.fake_backend import FakeBackend
from knowledge_ingestion.llm import FakeLLM
from knowledge_ingestion.planner import load_documents, plan
from knowledge_ingestion.sources import NoUsableSources
from knowledge_ingestion.storage import connect, migrate
from test_plan import claim, mention, proposal


class SourceMembershipTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.folder = self.root / 'vault'
        self.folder.mkdir()
        self.data = self.root / 'ledger'
        self.source = self.folder / 'a.md'
        self.source.write_text('Atlas is a project.\nAtlas uses SQLite.', encoding='utf-8')
        self.value = proposal([mention('atlas', 'Atlas', 'project', 'Atlas is a project.')],
                              claims=[claim('use', 'atlas', 'SQLite', 'Atlas uses SQLite.')])
        self.fixture = {'version': '1.0', 'responses': [
            {'document_path': 'a.md', 'chunk_ordinal': 0, 'proposal': self.value}]}
        self.backend = FakeBackend(self.root / 'FAKE.sqlite3', vault='personal')

    def make_plan(self, limit=100):
        scanned = scan(self.folder, self.data, limit)
        return scanned, plan(scanned['collection_id'], self.data, FakeLLM(self.fixture))

    def publish(self, report):
        return commit(self.data, report['change_set_id'], self.backend, confirmed=True, actor='synthetic-test')

    def test_hidden_service_trees_and_skipped_subset_never_publish(self):
        for dirname in ('.git', '.obsidian', '.cache', 'nested/.service'):
            path = self.folder / dirname
            path.mkdir(parents=True)
            (path / 'private.md').write_text('Never send this text to extraction.')
        (self.folder / 'bad.md').write_bytes(b'\xff')
        (self.folder / 'large.md').write_bytes(b'x' * 101)
        # data inside the source folder must remain excluded on every rescan.
        self.data = self.folder / 'app-data'
        scanned, report = self.make_plan()
        self.assertEqual([d['path'] for d in scanned['documents']], ['a.md'])
        self.assertEqual({w['reason'] for w in scanned['warnings']}, {'invalid_utf8_or_binary', 'too_large'})
        self.assertEqual(report['config']['source_policy']['max_bytes'], 100)
        self.assertEqual(self.publish(report)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)
        self.assertEqual([d['path'] for d in scan(self.folder, self.data, 100)['documents']], ['a.md'])

    def test_all_invalid_error_retains_warnings_and_empties_current_membership(self):
        scanned, _ = self.make_plan()
        snapshot = Path(scanned['documents'][0]['snapshot'])
        original = snapshot.read_bytes()
        self.source.write_bytes(b'\xff')
        with self.assertRaises(NoUsableSources) as failure:
            scan(self.folder, self.data, 100)
        self.assertEqual(failure.exception.result['documents'], [])
        self.assertEqual(failure.exception.result['warnings'][0]['reason'], 'invalid_utf8_or_binary')
        with connect(self.data) as db:
            with self.assertRaisesRegex(ValueError, 'collection_has_no_documents'):
                load_documents(db, scanned['collection_id'])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_revisions').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM scan_membership').fetchone()[0], 0)
        self.assertEqual(snapshot.read_bytes(), original)
        result = subprocess.run([sys.executable, '-m', 'knowledge_ingestion.cli', 'scan',
            str(self.folder), '--data', str(self.data)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, '')
        error = json.loads(result.stderr)
        self.assertEqual(error['error'], 'no_usable_sources')
        self.assertEqual(error['warnings'][0]['reason'], 'invalid_utf8_or_binary')

    def test_removed_and_ineligible_documents_leave_history_and_stale_memory_warning(self):
        (self.folder / 'b.md').write_text('Old but not committed.')
        scanned, report = self.make_plan()
        self.publish(report)
        with connect(self.data) as db:
            before = {name: [tuple(row) for row in db.execute('SELECT * FROM ' + name)]
                      for name in ('documents', 'source_revisions', 'evidence', 'claims', 'claim_evidence')}
        snapshots = {Path(d['snapshot']): Path(d['snapshot']).read_bytes() for d in scanned['documents']}
        self.source.unlink()
        (self.folder / 'b.md').write_bytes(b'\0')
        (self.folder / 'c.md').write_text('New eligible source.')
        rescanned, replanned = self.make_plan()
        self.assertEqual([d['path'] for d in rescanned['documents']], ['c.md'])
        self.assertEqual([d['locator'] for d in replanned['inputs']['documents']], ['c.md'])
        self.assertTrue(any(w['reason'] == 'possibly_stale_source' for w in rescanned['warnings']))
        self.assertTrue(any(w['reason'] == 'possibly_stale_source' for w in replanned['warnings']))
        with connect(self.data) as db:
            for name, rows in before.items():
                self.assertTrue(set(rows) <= {tuple(row) for row in db.execute('SELECT * FROM ' + name)}, name)
        for path, raw in snapshots.items():
            self.assertEqual(path.read_bytes(), raw)
        with self.assertRaisesRegex(ValueError, 'stale_source'):
            self.publish(report)
        self.assertEqual(self.backend.post_count, 1)

    def test_precommit_rechecks_valid_membership_changes_with_original_custom_limit(self):
        mutations = {
            'new_valid_file': lambda: (self.folder / 'new.md').write_text('new'),
            'skipped_becomes_valid': lambda: (self.folder / 'bad.md').write_text('valid'),
            'oversized_becomes_valid': lambda: (self.folder / 'large.md').write_text('valid'),
            'valid_edited': lambda: self.source.write_text('Atlas uses Redis.'),
            'valid_removed': self.source.unlink,
            'valid_now_invalid': lambda: self.source.write_bytes(b'\xff'),
            'valid_now_too_large': lambda: self.source.write_bytes(b'x' * 101),
        }
        for ordinal, (name, mutate) in enumerate(mutations.items()):
            with self.subTest(name=name):
                self.source.write_text('Atlas is a project.\nAtlas uses SQLite.')
                for path in ('new.md', 'bad.md', 'large.md'):
                    (self.folder / path).unlink(missing_ok=True)
                (self.folder / 'bad.md').write_bytes(b'\xff')
                (self.folder / 'large.md').write_bytes(b'x' * 101)
                self.data = self.root / ('ledger-' + str(ordinal))
                _, report = self.make_plan()
                mutate()
                with self.assertRaisesRegex(ValueError, 'stale_source'):
                    self.publish(report)
        self.assertEqual(self.backend.post_count, 0)

    def test_still_invalid_changes_and_hidden_additions_do_not_invalidate_subset(self):
        (self.folder / 'bad.md').write_bytes(b'\xff')
        _, report = self.make_plan()
        (self.folder / 'bad.md').write_bytes(b'\xfe')
        (self.folder / '.git').mkdir()
        (self.folder / '.git' / 'secret.md').write_text('excluded')
        self.assertEqual(self.publish(report)['status'], 'complete')

    def test_explicit_files_preserve_selection_identity_when_one_disappears(self):
        second = self.folder / 'b.md'
        second.write_text('second')
        first = scan([self.source, second], self.data)
        second.unlink()
        latest = scan([second, self.source], self.data)
        self.assertEqual(first['collection_id'], latest['collection_id'])
        with connect(self.data) as db:
            self.assertEqual(len(load_documents(db, first['collection_id'])), 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM documents').fetchone()[0], 2)

    def test_schema3_migration_is_lossless_and_requires_rescan_for_old_hidden_rows(self):
        scanned, report = self.make_plan()
        self.publish(report)
        with connect(self.data) as db:
            db.execute('DROP TABLE scan_membership')
            db.execute('DROP TABLE collection_scans')
            db.execute('PRAGMA user_version=3')
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {t: [tuple(r) for r in db.execute('SELECT * FROM ' + t)] for t in tables}
        with sqlite3.connect(self.data / 'ledger.sqlite3') as db:
            db.execute('PRAGMA foreign_keys=ON')
            migrate(db); migrate(db)
            for table, rows in before.items():
                self.assertEqual(db.execute('SELECT * FROM ' + table).fetchall(), rows, table)
            with self.assertRaisesRegex(ValueError, 'rescan_required'):
                load_documents(db, scanned['collection_id'])
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
        # Rescan re-establishes trustworthy current membership, never auto-adopts
        # old last_revision_id entries from an inherited hidden traversal.
        (self.folder / '.git').mkdir()
        (self.folder / '.git' / 'private.md').write_text('hidden')
        self.assertEqual([d['path'] for d in scan(self.folder, self.data)['documents']], ['a.md'])


if __name__ == '__main__':
    unittest.main()
