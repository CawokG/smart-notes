import tempfile
import unittest
from pathlib import Path
from knowledge_ingestion.cli import scan

class ScanTest(unittest.TestCase):
    def test_snapshots_are_exact_and_repeatable(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'vault'; root.mkdir()
            raw = b'\xef\xbb\xbf' + 'Привет 🐈 [[Missing]]'.encode()
            (root / 'a.md').write_bytes(raw)
            data = Path(tmp) / 'data'
            first = scan(root / 'a.md', data)
            self.assertEqual(first, scan(root / 'a.md', data))
            self.assertEqual(Path(first['documents'][0]['snapshot']).read_bytes(), raw)
            self.assertNotIn('wikilinks', first['documents'][0])

    def test_invalid_utf8_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / 'vault'; root.mkdir()
            (root / 'bad.txt').write_bytes(b'\xff')
            result = scan(root / 'bad.txt', Path(tmp) / 'data')
            self.assertEqual(result['documents'], [])
            self.assertEqual(result['warnings'][0]['reason'], 'invalid_utf8_or_binary')

    def test_directory_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, 'single file'):
                scan(Path(tmp), Path(tmp) / 'data')
