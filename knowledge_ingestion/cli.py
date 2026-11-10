"""Deterministic, local-only source snapshot and chunk scanner."""
import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from pathlib import Path

from .contracts import PARSER_VERSION, SCHEMA_VERSION, stable_id
from .storage import contains_symlink, migrate, now

MAX_BYTES = 5 * 1024 * 1024
CHUNK_BYTES = 9000  # Approximate token budget; UTF-8 bytes are not tokens.
OVERLAP_BYTES = 700


def chunks_for(raw, revision_id):
    # Segment at headings and paragraph boundaries, retaining every byte, BOM included.
    text = raw.decode('utf-8')
    lines = text.splitlines(keepends=True)
    segments, start, pos, headings = [], 0, 0, []
    current_heading = []
    def flush(end):
        nonlocal start
        if end > start:
            segments.append((start, end, list(current_heading)))
            start = end
    for line in lines:
        line_bytes = line.encode('utf-8')
        match = re.match(r'^\ufeff?(#{1,6})[ \t]+(.+?)[ \t]*#*[ \t]*(?:\r?\n)?$', line)
        if match:
            flush(pos)
            level = len(match.group(1))
            headings = headings[:level - 1]
            headings += [''] * (level - 1 - len(headings))
            headings.append(match.group(2).strip())
            current_heading = list(headings)
        pos += len(line_bytes)
        if not line.strip():
            flush(pos)
    flush(pos)
    # Split oversized segments only at UTF-8 boundaries.
    pieces = []
    for a, b, heading in segments:
        while b - a > CHUNK_BYTES:
            end = a + CHUNK_BYTES
            while end > a and (raw[end] & 0xC0) == 0x80:
                end -= 1
            pieces.append((a, end, heading))
            a = end
        if a < b:
            pieces.append((a, b, heading))
    result = []
    i = 0
    previous_end = 0
    while i < len(pieces):
        a, b, heading = pieces[i]
        j = i + 1
        while j < len(pieces) and pieces[j][1] - a <= CHUNK_BYTES and pieces[j][2] == heading:
            b = pieces[j][1]
            j += 1
        ordinal = len(result)
        overlap_end = min(previous_end, b)
        overlap = [a, overlap_end] if a < overlap_end else None
        result.append({'chunk_id': stable_id('chk', revision_id, PARSER_VERSION, ordinal, a, b),
                       'revision_id': revision_id, 'ordinal': ordinal, 'start_byte': a, 'end_byte': b,
                       'heading_path': heading, 'overlap_range': overlap, 'context_refs': []})
        previous_end = b
        # Reuse whole small segments for bounded overlap, always making progress.
        next_i = j
        while (j < len(pieces) and next_i > i + 1
               and pieces[next_i - 1][2] == pieces[j][2]
               and pieces[j][1] - pieces[next_i - 1][0] <= CHUNK_BYTES
               and b - pieces[next_i - 1][0] <= OVERLAP_BYTES):
            next_i -= 1
        i = next_i
    return result


def scan(source, data: Path, limit: int = MAX_BYTES) -> dict:
    sources = [Path(source)] if isinstance(source, (str, Path)) else [Path(p) for p in source]
    if not sources or limit <= 0:
        raise ValueError('provide sources and a positive size limit')
    data = Path(os.path.abspath(data))
    if contains_symlink(data):
        raise ValueError('data path must not contain symlinks')
    warnings = []
    valid = []
    for p in sorted(set(Path(os.path.abspath(p)) for p in sources), key=str):
        if contains_symlink(p):
            warnings.append({'path': str(p), 'reason': 'symlink'}); continue
        if not p.exists():
            warnings.append({'path': str(p), 'reason': 'not_found'}); continue
        valid.append(p)
    directories = [p for p in valid if p.is_dir()]
    if directories and (len(valid) != 1 or len(sources) != 1):
        raise ValueError('select either one directory or one or more files')
    kind = 'directory' if directories else 'files'
    root = directories[0] if directories else None
    if root == data:
        raise ValueError('data directory must differ from source directory')
    if (data / 'ledger.sqlite3').is_symlink():
        raise ValueError('ledger path is a symlink')
    files = []
    if root:
        for base, dirs, names in os.walk(root, followlinks=False, onerror=lambda e: warnings.append({'path': str(e.filename), 'reason': 'unreadable'})):
            base = Path(base)
            kept = []
            for name in sorted(dirs):
                p = base / name
                if p.is_symlink():
                    warnings.append({'path': str(p), 'reason': 'symlink'})
                elif name != '.obsidian' and p != data:
                    kept.append(name)
            dirs[:] = kept
            for name in sorted(names):
                p = base / name
                if p.is_symlink():
                    warnings.append({'path': str(p), 'reason': 'symlink'})
                elif p.suffix.lower() in ('.md', '.txt'):
                    files.append(p)
    else:
        files = valid
    file_uris = sorted(p.as_uri() for p in files) if not root else []
    collection_sources = [root.as_uri()] if root else file_uris
    collection_id = stable_id('col', kind, collection_sources)
    data.mkdir(parents=True, exist_ok=True)
    snapshots = data / 'snapshots'
    snapshots.mkdir(exist_ok=True)
    if contains_symlink(snapshots):
        raise ValueError('snapshot directory must not contain symlinks')
    docs = []
    with sqlite3.connect(data / 'ledger.sqlite3') as db:
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA journal_mode=WAL')
        migrate(db)
        with db:
            db.execute('INSERT OR IGNORE INTO collections VALUES (?,?,?)', (collection_id, kind, json.dumps(collection_sources, ensure_ascii=False)))
            for p in sorted(files, key=str):
                locator = p.relative_to(root).as_posix() if root else p.as_uri()
                if contains_symlink(p) or not p.is_file():
                    warnings.append({'path': str(p), 'reason': 'symlink_or_nonfile'}); continue
                if p.suffix.lower() not in ('.md', '.txt'):
                    warnings.append({'path': str(p), 'reason': 'unsupported_type'}); continue
                try:
                    with p.open('rb') as f:
                        raw = f.read(limit + 1)
                    if len(raw) > limit:
                        warnings.append({'path': str(p), 'reason': 'too_large'}); continue
                    if b'\0' in raw:
                        raise ValueError('binary')
                    raw.decode('utf-8')
                except (UnicodeDecodeError, ValueError):
                    warnings.append({'path': str(p), 'reason': 'invalid_utf8_or_binary'}); continue
                except OSError:
                    warnings.append({'path': str(p), 'reason': 'unreadable'}); continue
                digest = hashlib.sha256(raw).hexdigest()
                snap = snapshots / digest
                if snap.is_symlink():
                    raise ValueError('snapshot path is a symlink')
                if snap.exists():
                    if snap.read_bytes() != raw:
                        raise ValueError('snapshot content does not match hash')
                else:
                    snap.write_bytes(raw)
                doc_id = stable_id('doc', collection_id, locator)
                rev_id = stable_id('rev', doc_id, digest)
                chunks = chunks_for(raw, rev_id)
                db.execute('INSERT OR IGNORE INTO documents VALUES (?,?,?,NULL)', (doc_id, collection_id, locator))
                inserted = db.execute('INSERT OR IGNORE INTO source_revisions VALUES (?,?,?,?,?)', (rev_id, doc_id, digest, str(snap), PARSER_VERSION)).rowcount
                if inserted:
                    db.execute('INSERT INTO revision_metadata VALUES (?,?)', (rev_id, now()))
                db.execute('UPDATE documents SET last_revision_id=? WHERE id=?', (rev_id, doc_id))
                for c in chunks:
                    overlap = c['overlap_range'] or [None, None]
                    db.execute('INSERT OR IGNORE INTO chunks VALUES (?,?,?,?,?,?,?,?)', (c['chunk_id'], rev_id, c['ordinal'], c['start_byte'], c['end_byte'], json.dumps(c['heading_path'], ensure_ascii=False), *overlap))
                docs.append({'path': locator, 'document_id': doc_id, 'revision_id': rev_id, 'sha256': digest, 'snapshot': str(snap), 'chunks': chunks})
    return {'version': '2.0', 'schema_version': SCHEMA_VERSION, 'parser_version': PARSER_VERSION, 'collection_id': collection_id, 'source_kind': kind, 'root_uri': root.as_uri() if root else None, 'documents': docs, 'warnings': sorted(warnings, key=lambda w: (w['path'], w['reason']))}


def main():
    parser = argparse.ArgumentParser(prog='ki')
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('scan')
    s.add_argument('source', type=Path, nargs='+')
    s.add_argument('--data', type=Path, default=Path('data'))
    s.add_argument('--max-bytes', type=int, default=MAX_BYTES)
    p = sub.add_parser('plan', help='build an offline ChangeSet; never publish')
    p.add_argument('--collection', required=True)
    p.add_argument('--data', type=Path, default=Path('data'))
    p.add_argument('--vault', default='personal')
    p.add_argument('--llm-provider', choices=['fake'], default='fake')
    p.add_argument('--fixture', type=Path, required=True)
    p.add_argument('--digest-bytes', type=int, default=4096)
    p.add_argument('--output', type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'scan':
            result = scan(args.source, args.data, args.max_bytes)
        else:
            from .llm import FakeLLM
            from .planner import plan
            result = plan(args.collection, args.data, FakeLLM(args.fixture),
                          vault=args.vault, digest_budget=args.digest_bytes)
            if args.output:
                if contains_symlink(args.output):
                    raise ValueError('output path must not contain symlinks')
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if result.get('status') == 'needs_attention':
            return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0

if __name__ == '__main__':
    sys.exit(main())
