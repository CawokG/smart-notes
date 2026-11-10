"""A conservative first-stage source snapshotter."""
import argparse
import hashlib
import json
import sqlite3
import sys
from pathlib import Path


def scan(source: Path, data: Path, limit: int = 5 * 1024 * 1024) -> dict:
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError('source must be a single file')
    if source.suffix.lower() not in ('.md', '.txt'):
        raise ValueError('unsupported source type')
    root = source.parent
    data.mkdir(parents=True, exist_ok=True)
    snapshots = data / 'snapshots'
    snapshots.mkdir(exist_ok=True)
    db = sqlite3.connect(data / 'ledger.sqlite3')
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''CREATE TABLE IF NOT EXISTS revisions (
      root TEXT NOT NULL, path TEXT NOT NULL, sha256 TEXT NOT NULL,
      snapshot TEXT NOT NULL, PRIMARY KEY(root,path,sha256));''')
    files = [source]
    docs, warnings = [], []
    for p in files:
        rel = p.relative_to(root).as_posix()
        if p.is_symlink() or not p.resolve().is_relative_to(root) or not p.is_file():
            warnings.append({'path': rel, 'reason': 'symlink_or_nonfile'}); continue
        if p.stat().st_size > limit:
            warnings.append({'path': rel, 'reason': 'too_large'}); continue
        raw = p.read_bytes()
        try:
            text = raw.decode('utf-8-sig')
            if '\0' in text: raise ValueError('binary')
        except (UnicodeDecodeError, ValueError):
            warnings.append({'path': rel, 'reason': 'invalid_utf8_or_binary'}); continue
        digest = hashlib.sha256(raw).hexdigest()
        snap = snapshots / digest
        if not snap.exists(): snap.write_bytes(raw)
        db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?,?)', (root.as_uri(), rel, digest, str(snap)))
        docs.append({'path': rel, 'sha256': digest, 'snapshot': str(snap)})
    db.commit()
    db.close()
    return {'version': '1.0', 'root_uri': root.as_uri(), 'documents': docs, 'warnings': warnings}


def main():
    parser = argparse.ArgumentParser(prog='ki')
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('scan')
    s.add_argument('source', type=Path)
    s.add_argument('--data', type=Path, default=Path('data'))
    args = parser.parse_args()
    try:
        result = scan(args.source, args.data)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
