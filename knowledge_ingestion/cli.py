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


def backend_options(parser):
    parser.add_argument('--backend', choices=['muninn', 'fake'], default='muninn')
    parser.add_argument('--muninn-url', default='http://127.0.0.1:8475')
    parser.add_argument('--observe-token-env', default='MUNINN_OBSERVE_TOKEN')
    parser.add_argument('--full-token-env', default='MUNINN_FULL_TOKEN')
    parser.add_argument('--fake-store', type=Path)


def configured_backend(args, vault):
    if args.backend == 'fake':
        if args.fake_store is None:
            raise ValueError('explicit_fake_store_required')
        from .fake_backend import FakeBackend
        return FakeBackend(args.fake_store, vault=vault)
    if args.fake_store is not None:
        raise ValueError('fake_store_requires_fake_backend')
    from .muninn import MuninnAdapter
    return MuninnAdapter(args.muninn_url, vault, os.environ.get(args.observe_token_env),
                         os.environ.get(args.full_token_env))


def main():
    parser = argparse.ArgumentParser(prog='ki')
    sub = parser.add_subparsers(dest='command', required=True)
    s = sub.add_parser('scan')
    s.add_argument('source', type=Path, nargs='+')
    s.add_argument('--data', type=Path, default=Path('data'))
    s.add_argument('--max-bytes', type=int, default=MAX_BYTES)
    p = sub.add_parser('plan', help='build a ChangeSet; never publish')
    p.add_argument('--collection', required=True)
    p.add_argument('--data', type=Path, default=Path('data'))
    p.add_argument('--vault', default='personal')
    p.add_argument('--llm-provider', choices=['fake'], default='fake')
    p.add_argument('--fixture', type=Path, required=True)
    p.add_argument('--digest-bytes', type=int, default=4096)
    p.add_argument('--output', type=Path)
    p.add_argument('--context', choices=['offline', 'muninn-observe'], default='offline')
    p.add_argument('--muninn-url', default='http://127.0.0.1:8475')
    p.add_argument('--observe-token-env', default='MUNINN_OBSERVE_TOKEN')
    r = sub.add_parser('review', help='inspect or record operation decisions')
    r.add_argument('--change-set', required=True, help='persisted ID or original JSON file')
    r.add_argument('--data', type=Path, default=Path('data'))
    r.add_argument('--decisions', type=Path)
    r.add_argument('--actor')
    c = sub.add_parser('commit', help='publish approved operations after confirmation')
    c.add_argument('--change-set', required=True)
    c.add_argument('--data', type=Path, default=Path('data'))
    c.add_argument('--yes', action='store_true')
    c.add_argument('--actor', required=True)
    c.add_argument('--expected-config-hash')
    backend_options(c)
    t = sub.add_parser('status')
    t.add_argument('--job', required=True)
    t.add_argument('--data', type=Path, default=Path('data'))
    u = sub.add_parser('resume', help='continue original fake planning or a confirmed commit')
    u.add_argument('--job', required=True)
    u.add_argument('--data', type=Path, default=Path('data'))
    u.add_argument('--fixture', type=Path, help='original fake fixture for interrupted planning')
    u.add_argument('--context', choices=['offline', 'muninn-observe'], default='offline')
    u.add_argument('--expected-config-hash')
    u.add_argument('--repair-operation')
    u.add_argument('--engram-id')
    u.add_argument('--actor')
    u.add_argument('--rationale')
    backend_options(u)
    args = parser.parse_args()
    try:
        if args.command == 'scan':
            result = scan(args.source, args.data, args.max_bytes)
        elif args.command == 'plan':
            from .llm import FakeLLM
            from .planner import plan
            context = None
            if args.context == 'muninn-observe':
                from .context import ObserveContextProvider
                from .muninn import MuninnAdapter
                context = ObserveContextProvider(MuninnAdapter(args.muninn_url, args.vault,
                    os.environ.get(args.observe_token_env)))
            result = plan(args.collection, args.data, FakeLLM(args.fixture), vault=args.vault,
                          digest_budget=args.digest_bytes, context_provider=context)
            if args.output:
                if contains_symlink(args.output):
                    raise ValueError('output path must not contain symlinks')
                args.output.parent.mkdir(parents=True, exist_ok=True)
                args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        else:
            from .commit import commit, resume_job, review, status
            from .contracts import parse_json
            from .storage import connect
            if args.command == 'review':
                decisions = None
                if args.decisions:
                    if contains_symlink(args.decisions):
                        raise ValueError('invalid_review_file')
                    decisions = parse_json(args.decisions.read_text(encoding='utf-8'))
                result = review(args.data, args.change_set, decisions, args.actor)
            elif args.command == 'status':
                result = status(args.data, args.job)
            elif args.command == 'commit':
                # Refuse before creating a session, local projection, or backend.
                confirmed = args.yes
                if not confirmed and sys.stdin.isatty():
                    print('Publish approved operations? Type yes to confirm: ', file=sys.stderr, end='', flush=True)
                    confirmed = input().strip() == 'yes'
                if not confirmed:
                    raise ValueError('explicit_commit_confirmation_required')
                from .publication import load_change_set
                with connect(args.data) as db:
                    _, job, _ = load_change_set(db, args.change_set)
                    vault = job['vault_id']
                result = commit(args.data, args.change_set, configured_backend(args, vault),
                    confirmed=True, actor=args.actor, expected_config_hash=args.expected_config_hash)
            else:
                with connect(args.data) as db:
                    job = db.execute('SELECT vault_id FROM jobs WHERE id=?', (args.job,)).fetchone()
                    if job is None:
                        raise ValueError('unknown_job')
                    is_commit = db.execute('SELECT 1 FROM commit_sessions s JOIN change_sets c ON c.id=s.change_set_id WHERE c.job_id=?', (args.job,)).fetchone()
                repair = None
                if any((args.repair_operation, args.engram_id, args.rationale)):
                    if not all((args.repair_operation, args.engram_id, args.actor, args.rationale)):
                        raise ValueError('manual_repair_requires_operation_id_engram_id_actor_rationale')
                    repair = {'operation_id': args.repair_operation, 'engram_id': args.engram_id,
                              'actor': args.actor, 'rationale': args.rationale}
                from .llm import FakeLLM
                context = None
                if not is_commit and args.context == 'muninn-observe':
                    from .context import ObserveContextProvider
                    from .muninn import MuninnAdapter
                    context = ObserveContextProvider(MuninnAdapter(args.muninn_url, job['vault_id'],
                        os.environ.get(args.observe_token_env)))
                result = resume_job(args.data, args.job,
                    adapter=configured_backend(args, job['vault_id']) if is_commit else None,
                    provider=FakeLLM(args.fixture) if args.fixture else None,
                    context_provider=context,
                    expected_config_hash=args.expected_config_hash, repair=repair)
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        if result.get('status') in {'needs_attention', 'needs_manual_repair', 'blocked_backend', 'replan_required', 'stale_source'}:
            return 1
    except (OSError, ValueError, sqlite3.Error) as exc:
        # Application exceptions use fixed codes; no raw backend body is exposed.
        print(json.dumps({'error': str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
