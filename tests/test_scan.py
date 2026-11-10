import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from knowledge_ingestion.cli import scan, stable_id
from knowledge_ingestion.storage import migrate
from knowledge_ingestion.contracts import SCHEMA_VERSION


class ScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'data'

    def test_directory_bytes_ranges_and_exclusions(self):
        vault = self.root / 'vault'
        (vault / 'nested').mkdir(parents=True)
        (vault / '.obsidian').mkdir()
        (vault / '.obsidian' / 'hidden.md').write_text('hidden')
        raw = b'\xef\xbb\xbf' + '# Привет 🐈\n\n[[Missing]] [x](../../outside.md)\n\n'.encode() + ('длинный текст 🐈 ' * 5000).encode()
        (vault / 'nested' / 'a.md').write_bytes(raw)
        (vault / 'bad.txt').write_bytes(b'\xff')
        (vault / 'large.md').write_bytes(b'x' * (5 * 1024 * 1024 + 1))
        (vault / 'link.md').symlink_to(vault / 'nested' / 'a.md')
        (vault / 'dirlink').symlink_to(vault / 'nested', target_is_directory=True)
        result = scan(vault, vault / 'data')
        self.assertEqual(result, scan(vault, vault / 'data'))
        self.assertEqual([d['path'] for d in result['documents']], ['nested/a.md'])
        doc = result['documents'][0]
        self.assertEqual(Path(doc['snapshot']).read_bytes(), raw)
        self.assertEqual({w['reason'] for w in result['warnings']}, {'symlink', 'invalid_utf8_or_binary', 'too_large'})
        end = 0
        for chunk in doc['chunks']:
            self.assertLessEqual(chunk['start_byte'], end)
            self.assertGreater(chunk['end_byte'], end)
            raw[chunk['start_byte']:chunk['end_byte']].decode('utf-8')
            end = chunk['end_byte']
        self.assertEqual(end, len(raw))
        self.assertEqual(doc['chunks'][0]['start_byte'], 0)
        self.assertEqual(doc['chunks'][0]['heading_path'], ['Привет 🐈'])
        self.assertNotIn('wikilinks', doc)

    def test_multiple_files_dedupe_identity_and_change_history(self):
        a, b = self.root / 'a', self.root / 'b'
        a.mkdir(); b.mkdir()
        p, q = a / 'same.md', b / 'same.md'
        p.write_text('same'); q.write_text('same')
        first = scan([q, p, p], self.data)
        self.assertEqual(first, scan([p, q], self.data))
        self.assertEqual(len(first['documents']), 2)
        self.assertNotEqual(first['documents'][0]['document_id'], first['documents'][1]['document_id'])
        p.write_text('changed')
        changed = scan([p, q], self.data)
        self.assertEqual(first['collection_id'], changed['collection_id'])
        self.assertNotEqual(first['documents'][0]['revision_id'], changed['documents'][0]['revision_id'])
        with sqlite3.connect(self.data / 'ledger.sqlite3') as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_revisions').fetchone()[0], 3)

    def test_existing_store_migrated_without_loss(self):
        self.data.mkdir()
        with sqlite3.connect(self.data / 'ledger.sqlite3') as db:
            db.execute('CREATE TABLE revisions (root TEXT, path TEXT, sha256 TEXT, snapshot TEXT, PRIMARY KEY(root,path,sha256))')
            db.execute('INSERT INTO revisions VALUES (?,?,?,?)', ('old', 'old.md', 'hash', 'old-snapshot'))
        p = self.root / 'new.md'; p.write_text('new')
        scan(p, self.data); scan(p, self.data)
        with sqlite3.connect(self.data / 'ledger.sqlite3') as db:
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(db.execute('SELECT * FROM revisions').fetchall(), [('old', 'old.md', 'hash', 'old-snapshot')])
            self.assertEqual(db.execute('SELECT COUNT(*) FROM source_revisions').fetchone()[0], 1)

    def test_symlink_components_not_followed_and_errors(self):
        outside = self.root / 'outside'; outside.mkdir()
        (outside / 'a.md').write_text('secret')
        link = self.root / 'link'; link.symlink_to(outside, target_is_directory=True)
        result = scan(link / 'a.md', self.data)
        self.assertEqual(result['documents'], [])
        self.assertEqual(result['warnings'][0]['reason'], 'symlink')
        with self.assertRaises(ValueError):
            scan([outside, outside / 'a.md'], self.data)
        self.assertEqual(scan(outside / 'missing.md', self.data)['warnings'][0]['reason'], 'not_found')

    def test_stage1_schema_migration_preserves_every_row_and_snapshot(self):
        # Construct the actual four-table schema at stage1, not a schema2 store.
        self.data.mkdir()
        snapshot = self.data / 'snapshot-stage1'
        raw = '\ufeff# Старое 🐈\nИсходные байты.'.encode()
        snapshot.write_bytes(raw)
        with sqlite3.connect(self.data / 'ledger.sqlite3') as db:
            for statement in (
                'CREATE TABLE collections (id TEXT PRIMARY KEY,kind TEXT NOT NULL,sources_json TEXT NOT NULL)',
                'CREATE TABLE documents (id TEXT PRIMARY KEY,collection_id TEXT NOT NULL REFERENCES collections(id),locator TEXT NOT NULL,last_revision_id TEXT,UNIQUE(collection_id,locator))',
                'CREATE TABLE source_revisions (id TEXT PRIMARY KEY,document_id TEXT NOT NULL REFERENCES documents(id),sha256 TEXT NOT NULL,snapshot TEXT NOT NULL,parser_version TEXT NOT NULL,UNIQUE(document_id,sha256))',
                'CREATE TABLE chunks (id TEXT PRIMARY KEY,revision_id TEXT NOT NULL REFERENCES source_revisions(id),ordinal INTEGER NOT NULL,start_byte INTEGER NOT NULL,end_byte INTEGER NOT NULL,heading_path TEXT NOT NULL,overlap_start INTEGER,overlap_end INTEGER,UNIQUE(revision_id,ordinal))',
            ):
                db.execute(statement)
            db.execute('INSERT INTO collections VALUES (?,?,?)', ('old-col', 'files', '["file:///old.md"]'))
            db.execute('INSERT INTO documents VALUES (?,?,?,?)', ('old-doc', 'old-col', 'file:///old.md', 'old-rev'))
            db.execute('INSERT INTO source_revisions VALUES (?,?,?,?,?)', ('old-rev', 'old-doc', 'preserved-hash', str(snapshot), '1'))
            db.execute('INSERT INTO chunks VALUES (?,?,?,?,?,?,?,?)', ('old-chunk', 'old-rev', 0, 0, len(raw), '["Старое 🐈"]', None, None))
            db.execute('PRAGMA user_version=1')
            before = {table: db.execute(f'SELECT * FROM {table}').fetchall() for table in ('collections','documents','source_revisions','chunks')}
        with sqlite3.connect(self.data / 'ledger.sqlite3') as db:
            db.execute('PRAGMA foreign_keys=ON')
            migrate(db)
            migrate(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            for table, rows in before.items():
                self.assertEqual(db.execute(f'SELECT * FROM {table}').fetchall(), rows)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertEqual(db.execute('SELECT * FROM revision_metadata').fetchall(), [('old-rev', None)])
        self.assertEqual(snapshot.read_bytes(), raw)
        # The old identity encoding remains byte-for-byte compatible.
        import hashlib
        payload = json.dumps(('directory', ['file:///old']), ensure_ascii=False, separators=(',', ':')).encode()
        self.assertEqual(stable_id('col', 'directory', ['file:///old']), 'col_' + hashlib.sha256(payload).hexdigest())

    def test_newer_schema_is_rejected_without_downgrade(self):
        with sqlite3.connect(':memory:') as db:
            db.execute('PRAGMA user_version=99')
            with self.assertRaisesRegex(ValueError, 'newer'):
                migrate(db)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 99)

    def test_corpus(self):
        corpus = Path(__file__).resolve().parents[1] / 'fixtures' / 'obsidian'
        expected = json.loads((corpus.parent / 'expected.json').read_text())
        result = scan(corpus, self.data)
        self.assertEqual([d['path'] for d in result['documents']], expected['paths'])
        self.assertGreater(len(result['documents'][-1]['chunks']), 1)
        self.assertEqual(result['warnings'], [])


if __name__ == '__main__':
    unittest.main()
