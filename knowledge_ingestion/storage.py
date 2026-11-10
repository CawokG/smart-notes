"""SQLite migrations and isolated, resumable offline workspaces.

Planning never writes the committed entity/alias/claim/mention projections.
Evidence is immutable; job_evidence and claim_evidence give it staged/committed
membership. There is deliberately no publication method in this module.
"""
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .contracts import SCHEMA_VERSION, canonical_json, content_hash


def now():
    return datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z')


def contains_symlink(path):
    path = Path(os.path.abspath(path))
    return any(part.is_symlink() for part in (path, *path.parents))


def migrate(db):
    version = db.execute('PRAGMA user_version').fetchone()[0]
    if version > SCHEMA_VERSION:
        raise ValueError('ledger schema is newer than this application')
    if version == SCHEMA_VERSION:
        return
    with db:
        db.execute('BEGIN IMMEDIATE')
        if version == 0:
            # Preserve the original unversioned revisions table and snapshots.
            for statement in (
                'CREATE TABLE IF NOT EXISTS collections (id TEXT PRIMARY KEY, kind TEXT NOT NULL, sources_json TEXT NOT NULL)',
                'CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id), locator TEXT NOT NULL, last_revision_id TEXT, UNIQUE(collection_id, locator))',
                'CREATE TABLE IF NOT EXISTS source_revisions (id TEXT PRIMARY KEY, document_id TEXT NOT NULL REFERENCES documents(id), sha256 TEXT NOT NULL, snapshot TEXT NOT NULL, parser_version TEXT NOT NULL, UNIQUE(document_id, sha256))',
                'CREATE TABLE IF NOT EXISTS chunks (id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES source_revisions(id), ordinal INTEGER NOT NULL, start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, heading_path TEXT NOT NULL, overlap_start INTEGER, overlap_end INTEGER, UNIQUE(revision_id, ordinal))',
            ):
                db.execute(statement)
        for statement in (
            '''CREATE TABLE collection_metadata (
                collection_id TEXT PRIMARY KEY REFERENCES collections(id),
                vault_id TEXT NOT NULL, label TEXT, root_uri TEXT, created_at TEXT NOT NULL)''',
            '''CREATE TABLE revision_metadata (
                revision_id TEXT PRIMARY KEY REFERENCES source_revisions(id), captured_at TEXT)''',
            '''CREATE TABLE jobs (
                id TEXT PRIMARY KEY, collection_id TEXT NOT NULL REFERENCES collections(id),
                vault_id TEXT NOT NULL, config_hash TEXT NOT NULL, config_json TEXT NOT NULL,
                base_revision TEXT NOT NULL, manifest_json TEXT NOT NULL,
                status TEXT NOT NULL, stage TEXT NOT NULL, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, error TEXT)''',
            '''CREATE TABLE job_documents (
                job_id TEXT NOT NULL REFERENCES jobs(id), revision_id TEXT NOT NULL REFERENCES source_revisions(id),
                stage TEXT NOT NULL, last_completed_chunk INTEGER NOT NULL DEFAULT -1,
                checkpoint TEXT, PRIMARY KEY(job_id, revision_id))''',
            '''CREATE TABLE entities (
                id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, type TEXT NOT NULL,
                canonical_name TEXT NOT NULL, state TEXT NOT NULL CHECK(state='committed'),
                created_at TEXT NOT NULL, payload TEXT NOT NULL)''',
            '''CREATE TABLE evidence (
                id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES source_revisions(id),
                start_byte INTEGER NOT NULL CHECK(start_byte>=0),
                end_byte INTEGER NOT NULL CHECK(end_byte>start_byte), quote TEXT NOT NULL,
                origin TEXT NOT NULL, source_role TEXT NOT NULL, captured_at TEXT,
                parser_version TEXT NOT NULL, version TEXT NOT NULL,
                UNIQUE(revision_id,start_byte,end_byte,quote))''',
            '''CREATE TABLE job_evidence (
                job_id TEXT NOT NULL REFERENCES jobs(id), evidence_id TEXT NOT NULL REFERENCES evidence(id),
                PRIMARY KEY(job_id,evidence_id))''',
            '''CREATE TABLE aliases (
                id TEXT PRIMARY KEY, entity_id TEXT NOT NULL REFERENCES entities(id),
                value TEXT NOT NULL, normalized_value TEXT NOT NULL, scope TEXT NOT NULL,
                status TEXT NOT NULL, evidence_id TEXT REFERENCES evidence(id), payload TEXT NOT NULL)''',
            'CREATE INDEX alias_lookup ON aliases(normalized_value,scope,status)',
            '''CREATE TABLE mentions (
                id TEXT PRIMARY KEY, revision_id TEXT NOT NULL REFERENCES source_revisions(id),
                start_byte INTEGER NOT NULL, end_byte INTEGER NOT NULL, value TEXT NOT NULL,
                resolved_entity_id TEXT REFERENCES entities(id), state TEXT NOT NULL, payload TEXT NOT NULL)''',
            '''CREATE TABLE claims (
                id TEXT PRIMARY KEY, vault_id TEXT NOT NULL, canonical_hash TEXT NOT NULL,
                payload TEXT NOT NULL, state TEXT NOT NULL CHECK(state='committed'),
                engram_id TEXT NOT NULL, created_at TEXT NOT NULL,
                UNIQUE(vault_id,canonical_hash))''',
            '''CREATE TABLE claim_evidence (
                claim_id TEXT NOT NULL REFERENCES claims(id), evidence_id TEXT NOT NULL REFERENCES evidence(id),
                PRIMARY KEY(claim_id,evidence_id))''',
            '''CREATE TABLE staged_entities (
                job_id TEXT NOT NULL REFERENCES jobs(id), id TEXT NOT NULL,
                vault_id TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job_id,id))''',
            '''CREATE TABLE staged_aliases (
                job_id TEXT NOT NULL REFERENCES jobs(id), id TEXT NOT NULL,
                entity_id TEXT, normalized_value TEXT NOT NULL, scope TEXT NOT NULL,
                status TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(job_id,id))''',
            'CREATE INDEX staged_alias_lookup ON staged_aliases(job_id,normalized_value,scope,status)',
            '''CREATE TABLE staged_mentions (
                job_id TEXT NOT NULL REFERENCES jobs(id), id TEXT NOT NULL,
                revision_id TEXT NOT NULL REFERENCES source_revisions(id), payload TEXT NOT NULL,
                PRIMARY KEY(job_id,id))''',
            '''CREATE TABLE staged_claims (
                job_id TEXT NOT NULL REFERENCES jobs(id), id TEXT NOT NULL,
                vault_id TEXT NOT NULL, canonical_hash TEXT, payload TEXT NOT NULL,
                PRIMARY KEY(job_id,id), UNIQUE(job_id,vault_id,canonical_hash))''',
            '''CREATE TABLE staged_claim_evidence (
                job_id TEXT NOT NULL, claim_id TEXT NOT NULL,
                evidence_id TEXT NOT NULL REFERENCES evidence(id),
                PRIMARY KEY(job_id,claim_id,evidence_id),
                FOREIGN KEY(job_id,claim_id) REFERENCES staged_claims(job_id,id))''',
            '''CREATE TABLE chunk_checkpoints (
                job_id TEXT NOT NULL REFERENCES jobs(id), chunk_id TEXT NOT NULL REFERENCES chunks(id),
                extraction_hash TEXT NOT NULL, payload TEXT NOT NULL, digest_after TEXT NOT NULL,
                PRIMARY KEY(job_id,chunk_id))''',
            '''CREATE TABLE job_operations (
                job_id TEXT NOT NULL REFERENCES jobs(id), id TEXT NOT NULL,
                payload TEXT NOT NULL, PRIMARY KEY(job_id,id))''',
            '''CREATE TABLE change_sets (
                id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(id),
                base_revision TEXT NOT NULL, status TEXT NOT NULL, payload_hash TEXT NOT NULL,
                payload TEXT NOT NULL, created_at TEXT NOT NULL)''',
            '''CREATE TABLE operations (
                id TEXT NOT NULL, change_set_id TEXT NOT NULL REFERENCES change_sets(id),
                operation_type TEXT NOT NULL, payload TEXT NOT NULL, decision TEXT NOT NULL,
                state TEXT NOT NULL DEFAULT 'planned', applied_at TEXT, error TEXT,
                PRIMARY KEY(change_set_id,id))''',
            '''CREATE TABLE run_events (
                seq INTEGER PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(id),
                ts TEXT NOT NULL, stage TEXT NOT NULL, event_type TEXT NOT NULL,
                payload_redacted TEXT NOT NULL)''',
        ):
            db.execute(statement)
        # Metadata may be unknown for historical v1 rows; do not invent it.
        db.execute('INSERT INTO revision_metadata SELECT id,NULL FROM source_revisions')
        db.execute('PRAGMA user_version=2')


def connect(data):
    data = Path(os.path.abspath(data))
    if contains_symlink(data) or contains_symlink(data / 'ledger.sqlite3'):
        raise ValueError('ledger path must not contain symlinks')
    if not (data / 'ledger.sqlite3').is_file():
        raise ValueError('scan sources before planning')
    db = sqlite3.connect(data / 'ledger.sqlite3')
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    migrate(db)
    return db


class Ledger:
    def __init__(self, db, job_id, vault_id):
        self.db, self.job_id, self.vault_id = db, job_id, vault_id

    def event(self, kind, payload):
        self.db.execute('INSERT INTO run_events(job_id,ts,stage,event_type,payload_redacted) VALUES (?,?,?,?,?)',
                        (self.job_id, now(), 'extract', kind, canonical_json(payload)))

    def entities(self):
        result = {r['id']: json.loads(r['payload']) for r in self.db.execute(
            "SELECT id,payload FROM entities WHERE vault_id=? AND state='committed' ORDER BY id", (self.vault_id,))}
        for row in self.db.execute('SELECT id,payload FROM staged_entities WHERE job_id=? ORDER BY id', (self.job_id,)):
            result.setdefault(row['id'], json.loads(row['payload']))
        return list(result.values())

    def aliases(self):
        rows = self.db.execute('SELECT a.payload FROM aliases a JOIN entities e ON e.id=a.entity_id WHERE e.vault_id=? ORDER BY a.id', (self.vault_id,))
        result = [json.loads(r[0]) for r in rows]
        result += self.staged('aliases')
        return result

    def claims(self):
        result = {r['id']: json.loads(r['payload']) for r in self.db.execute(
            "SELECT id,payload FROM claims WHERE vault_id=? AND state='committed' ORDER BY id", (self.vault_id,))}
        for value in self.staged('claims'):
            if value['state'] == 'proposed':
                result.setdefault(value['claim_id'], value)
        return list(result.values())

    def staged(self, table):
        if table not in {'entities', 'aliases', 'mentions', 'claims'}:
            raise ValueError('invalid staged projection')
        return [json.loads(r[0]) for r in self.db.execute(
            f'SELECT payload FROM staged_{table} WHERE job_id=? ORDER BY id', (self.job_id,))]

    def put_evidence(self, ev):
        self.db.execute('''INSERT OR IGNORE INTO evidence
            (id,revision_id,start_byte,end_byte,quote,origin,source_role,captured_at,parser_version,version)
            VALUES (?,?,?,?,?,?,?,?,?,?)''',
            tuple(ev[k] for k in ('id', 'revision_id', 'start_byte', 'end_byte', 'quote', 'origin',
                                  'source_role', 'captured_at', 'parser_version', 'version')))
        self.db.execute('INSERT OR IGNORE INTO job_evidence VALUES (?,?)', (self.job_id, ev['id']))

    def put_entity(self, value):
        self.db.execute('INSERT OR IGNORE INTO staged_entities VALUES (?,?,?,?)',
                        (self.job_id, value['id'], self.vault_id, canonical_json(value)))

    def put_alias(self, value):
        old = self.db.execute('SELECT payload FROM staged_aliases WHERE job_id=? AND id=?', (self.job_id, value['id'])).fetchone()
        if old:
            value['evidence_ids'] = sorted(set(json.loads(old[0])['evidence_ids']) | set(value['evidence_ids']))
        self.db.execute('INSERT OR REPLACE INTO staged_aliases VALUES (?,?,?,?,?,?,?)',
                        (self.job_id, value['id'], value['entity_id'], value['normalized_value'],
                         value['scope'], value['status'], canonical_json(value)))

    def put_mention(self, value):
        self.db.execute('INSERT OR REPLACE INTO staged_mentions VALUES (?,?,?,?)',
                        (self.job_id, value['id'], value['revision_id'], canonical_json(value)))

    def put_claim(self, value):
        self.db.execute('''INSERT INTO staged_claims VALUES (?,?,?,?,?)
                        ON CONFLICT(job_id,id) DO UPDATE SET canonical_hash=excluded.canonical_hash,payload=excluded.payload''',
                        (self.job_id, value['claim_id'], self.vault_id,
                         value['canonical_hash'] if value['state'] == 'proposed' else None, canonical_json(value)))
        for evidence_id in value['evidence_ids']:
            self.db.execute('INSERT OR IGNORE INTO staged_claim_evidence VALUES (?,?,?)',
                            (self.job_id, value['claim_id'], evidence_id))

    def evidence_ids(self, claim_id):
        committed = {r[0] for r in self.db.execute('SELECT evidence_id FROM claim_evidence WHERE claim_id=?', (claim_id,))}
        staged = {r[0] for r in self.db.execute('SELECT evidence_id FROM staged_claim_evidence WHERE job_id=? AND claim_id=?', (self.job_id, claim_id))}
        return committed | staged

    def operation(self, operation):
        self.db.execute('INSERT OR IGNORE INTO job_operations VALUES (?,?,?)',
                        (self.job_id, operation['operation_id'], canonical_json(operation)))

    def operations(self):
        return [json.loads(r[0]) for r in self.db.execute('SELECT payload FROM job_operations WHERE job_id=? ORDER BY id', (self.job_id,))]

    def digest(self, budget):
        # Reconstruct from full staged state, never trust a model-written digest.
        items = []
        for entity in self.staged('entities'):
            items.append({'kind': 'entity', 'id': entity['id'], 'name': entity['canonical_name'],
                          'evidence_ids': entity['evidence_ids'][:1]})
        for alias in self.staged('aliases'):
            items.append({'kind': 'alias', 'entity_id': alias['entity_id'],
                          'value': alias['value'], 'scope': alias['scope'], 'status': alias['status']})
        for claim in self.staged('claims'):
            items.append({'kind': 'claim', 'id': claim['claim_id'], 'state': claim['state'],
                          'predicate': claim['predicate'], 'evidence_ids': claim['evidence_ids'][:1]})
        for mention in self.staged('mentions'):
            if mention['resolution']['status'] in {'ambiguous', 'unresolved'}:
                items.append({'kind': 'unresolved', 'id': mention['id'], 'surface': mention['surface'],
                              'evidence_ids': mention['evidence_ids'][:1]})
        result = {'version': '1.0', 'trust': 'staged_uncommitted', 'items': [], 'omitted': len(items)}
        for item in items:
            proposed = {**result, 'items': result['items'] + [item], 'omitted': result['omitted'] - 1}
            if len(canonical_json(proposed).encode('utf-8')) > budget:
                break
            result = proposed
        return result


def committed_revision(db, vault_id):
    # Timestamps and unrelated jobs do not invalidate a deterministic plan.
    projection = {}
    for table in ('entities', 'claims'):
        projection[table] = [json.loads(r[0]) for r in db.execute(
            f'SELECT payload FROM {table} WHERE vault_id=? ORDER BY id', (vault_id,))]
    projection['aliases'] = [json.loads(r[0]) for r in db.execute(
        'SELECT a.payload FROM aliases a JOIN entities e ON e.id=a.entity_id WHERE e.vault_id=? ORDER BY a.id', (vault_id,))]
    projection['evidence_links'] = [list(r) for r in db.execute(
        'SELECT ce.claim_id,ce.evidence_id FROM claim_evidence ce JOIN claims c ON c.id=ce.claim_id WHERE c.vault_id=? ORDER BY ce.claim_id,ce.evidence_id', (vault_id,))]
    return 'ledger_' + content_hash(projection)
