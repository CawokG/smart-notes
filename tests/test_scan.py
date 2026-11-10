import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from knowledge_ingestion.cli import scan


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
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 1)
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

    def test_corpus(self):
        corpus = Path(__file__).resolve().parents[1] / 'fixtures' / 'obsidian'
        expected = json.loads((corpus.parent / 'expected.json').read_text())
        result = scan(corpus, self.data)
        self.assertEqual([d['path'] for d in result['documents']], expected['paths'])
        self.assertGreater(len(result['documents'][-1]['chunks']), 1)
        self.assertEqual(result['warnings'], [])


if __name__ == '__main__':
    unittest.main()
