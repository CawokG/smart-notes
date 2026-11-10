"""Immutable plans and fail-closed validation shared by review and publication."""
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import unquote, urlsplit

from .contracts import (PARSER_VERSION, PROMPT_VERSION, RULES_VERSION, VERSION,
                        canonical_json, claim_fingerprint, content_hash, parse_json, stable_id)
from .evidence import read_snapshot
from .planner import evidence_manifest, load_documents, policy_gate
from .storage import Ledger, contains_symlink


class PublicationError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise PublicationError(code)


def text_identity(value, field, maximum=500):
    require(isinstance(value, str) and 0 < len(value.strip()) <= maximum
            and not any(ord(c) < 32 for c in value), 'invalid_' + field)
    return value.strip()


def load_change_set(db, reference):
    reference = str(reference)
    supplied = None
    if reference.startswith('chg_') and '/' not in reference:
        change_id = reference
    else:
        path = Path(reference)
        require(not contains_symlink(path) and path.is_file(), 'invalid_change_set_file')
        supplied = parse_json(path.read_text(encoding='utf-8'))
        require(isinstance(supplied, dict), 'invalid_change_set')
        change_id = supplied.get('change_set_id')
    row = db.execute('SELECT * FROM change_sets WHERE id=?', (change_id,)).fetchone()
    require(row is not None, 'unknown_change_set')
    report = parse_json(row['payload'])
    require(isinstance(report, dict) and content_hash(report) == row['payload_hash'], 'change_set_hash_mismatch')
    require(supplied is None or supplied == report, 'change_set_file_mismatch')
    require(report.get('version') == VERSION, 'unsupported_change_set_version')
    job = db.execute('SELECT * FROM jobs WHERE id=?', (row['job_id'],)).fetchone()
    require(job is not None and report['job_id'] == job['id'] and report['change_set_id'] == row['id'], 'invalid_change_set_identity')
    require(report['base_revision'] == job['base_revision'] == row['base_revision'], 'invalid_base_revision')
    config = parse_json(job['config_json'])
    require(config == report['config'] and content_hash(config) == job['config_hash'], 'config_hash_mismatch')
    expected = {'version': VERSION, 'rules': RULES_VERSION, 'parser': PARSER_VERSION,
                'schema': VERSION, 'prompt': PROMPT_VERSION, 'temperature': 0, 'max_retries': 2}
    require(all(config.get(k) == v for k, v in expected.items()), 'replan_required')
    require(report['inputs']['collection_id'] == job['collection_id']
            and report['inputs']['vault_id'] == job['vault_id']
            and report['inputs']['documents'] == parse_json(job['manifest_json']), 'source_manifest_mismatch')
    # Stage1 stable_id encodes mapping insertion order. Reconstruct the original
    # manifest layout; canonical on-disk JSON intentionally sorts its keys.
    identity_manifest = [{k: d[k] for k in ('document_id', 'revision_id', 'sha256', 'locator', 'chunk_ids')}
                         for d in report['inputs']['documents']]
    require(stable_id('job', job['collection_id'], job['vault_id'], identity_manifest,
                     job['config_hash'], job['base_revision']) == job['id']
            and stable_id('chg', job['id'], job['base_revision']) == row['id'], 'invalid_change_set_identity')
    ledger = Ledger(db, job['id'], job['vault_id'])
    for name in ('entities', 'aliases', 'mentions', 'claims'):
        require(report[name] == ledger.staged(name), 'staged_payload_mismatch')
    require(report['evidence'] == evidence_manifest(db, ledger), 'evidence_manifest_mismatch')
    original_ops = report['operations']
    require(original_ops == policy_gate(original_ops, ledger, report['evidence']), 'invalid_operation_order')
    for op in original_ops:
        original = db.execute('SELECT payload FROM job_operations WHERE job_id=? AND id=?', (job['id'], op['operation_id'])).fetchone()
        require(original is not None and parse_json(original[0]) == op, 'operation_hash_mismatch')
    operations = {r['id']: dict(r) for r in db.execute('SELECT * FROM operations WHERE change_set_id=?', (row['id'],))}
    require(set(operations) == {o['operation_id'] for o in original_ops}, 'operation_rows_mismatch')
    for op in original_ops:
        stored = operations[op['operation_id']]
        require(parse_json(stored['payload']) == op and stored['operation_type'] == op['kind'], 'operation_payload_mismatch')
        require(stored['decision'] in {'approved', 'rejected', 'deferred'}, 'invalid_review_decision')
        if op['kind'] in {'defer', 'conflict_warning'} or op['decision'] != 'approved':
            require(stored['decision'] != 'approved', 'unsafe_review_approval')
    return report, job, operations


def source_path(uri):
    parsed = urlsplit(uri)
    require(parsed.scheme == 'file' and not parsed.netloc and not parsed.query and not parsed.fragment, 'invalid_source_uri')
    return Path(unquote(parsed.path))


def check_sources(db, report, data):
    """Recheck originals AND immutable snapshots; reject inherited scan omissions.

    Directory intake with skipped eligible files is deliberately fail-closed.
    The scanner's missing-source/hidden-dir gaps cannot publish stale material.
    """
    collection = db.execute('SELECT * FROM collections WHERE id=?', (report['inputs']['collection_id'],)).fetchone()
    require(collection is not None, 'stale_source')
    sources = json.loads(collection['sources_json'])
    documents = load_documents(db, collection['id'])
    actual = [{'document_id': d['document_id'], 'revision_id': d['revision_id'], 'sha256': d['sha256'],
               'locator': d['locator'], 'chunk_ids': [c['id'] for c in d['chunks']]} for d in documents]
    require(actual == report['inputs']['documents'], 'stale_source')
    data = Path(data).absolute()
    expected_paths = set()
    root = source_path(sources[0]) if collection['kind'] == 'directory' else None
    if root:
        require(not contains_symlink(root) and root.is_dir(), 'stale_source')
        seen = set()
        def onerror(error):
            raise PublicationError('stale_source')
        for base, dirs, names in os.walk(root, followlinks=False, onerror=onerror):
            base = Path(base)
            dirs[:] = sorted(n for n in dirs if not n.startswith('.') and base / n != data and not (base / n).is_symlink())
            for name in names:
                path = base / name
                if path.suffix.lower() in {'.md', '.txt'} and not path.is_symlink():
                    seen.add(path)
    else:
        seen = {source_path(uri) for uri in sources}
    for document in documents:
        path = root / document['locator'] if root else source_path(document['locator'])
        if root:
            require(not Path(document['locator']).is_absolute() and '..' not in Path(document['locator']).parts
                    and not any(p.startswith('.') for p in Path(document['locator']).parts[:-1]), 'stale_source')
        require(path not in expected_paths and not contains_symlink(path) and path.is_file(), 'stale_source')
        expected_paths.add(path)
        try:
            raw = path.read_bytes()
        except OSError:
            raise PublicationError('stale_source') from None
        require(hashlib.sha256(raw).hexdigest() == document['sha256'], 'stale_source')
        snapshot = Path(document['snapshot']).absolute()
        require(snapshot.parent == data / 'snapshots' and snapshot.name == document['sha256'], 'invalid_snapshot_path')
        require(read_snapshot(document) == raw, 'stale_source')
    require(seen == expected_paths, 'stale_source')


def dependencies(report, operations):
    for op in report['operations']:
        if operations[op['operation_id']]['decision'] == 'approved':
            require(all(operations[d]['decision'] == 'approved' for d in op['depends_on']), 'rejected_operation_dependency')


def check_preconditions(db, report, operations):
    entities = {e['id']: e for e in report['entities']}
    aliases = {a['id']: a for a in report['aliases']}
    claims = {c['claim_id']: c for c in report['claims']}
    creates = {op['target_id']: op['operation_id'] for op in report['operations'] if op['kind'] == 'create_claim'}
    entity_creates = {op['target_id']: op['operation_id'] for op in report['operations'] if op['kind'] == 'create_entity'}
    for op in report['operations']:
        stored = operations[op['operation_id']]
        if stored['decision'] != 'approved' or stored['state'] == 'applied':
            continue
        kind, target, pre = op['kind'], op['target_id'], op['preconditions']
        if kind == 'create_entity':
            require(target in entities and pre == {'identity_resolution': 'new_candidate'}, 'invalid_entity_precondition')
            require(not db.execute('SELECT 1 FROM entities WHERE id=?', (target,)).fetchone(), 'replan_required')
        elif kind == 'add_alias':
            alias = aliases.get(target)
            require(alias is not None and alias['status'] == 'approved' and alias['entity_id'], 'unsafe_alias')
            collisions = db.execute("SELECT a.entity_id FROM aliases a JOIN entities e ON e.id=a.entity_id WHERE a.normalized_value=? AND a.scope=? AND a.status='approved' AND e.vault_id=?", (alias['normalized_value'], alias['scope'], report['inputs']['vault_id']))
            require(all(r[0] == alias['entity_id'] for r in collisions), 'replan_required')
            require(db.execute('SELECT 1 FROM entities WHERE id=?', (alias['entity_id'],)).fetchone()
                    or entity_creates.get(alias['entity_id']) in op['depends_on'], 'invalid_alias_dependency')
        elif kind in {'create_claim', 'add_evidence', 'skip_duplicate', 'link_claim_entity'}:
            claim = claims.get(target)
            require(claim is not None and claim['state'] == 'proposed' and claim['vault_id'] == report['inputs']['vault_id'], 'unsafe_claim')
            fingerprint = claim_fingerprint(claim['vault_id'], claim['subject']['entity_id'], claim['predicate'],
                claim['object'], claim['qualifiers'], claim['polarity'], claim['modality'], claim['valid_time'])
            require(fingerprint == claim['canonical_hash'] and target == stable_id('clm', claim['vault_id'], fingerprint), 'claim_fingerprint_mismatch')
            refs = {claim['subject']['entity_id']}
            if claim['object']['kind'] == 'entity':
                refs.add(claim['object']['entity_id'])
            if claim['reported_by']:
                refs.add(claim['reported_by'])
            known = db.execute('SELECT * FROM claims WHERE id=?', (target,)).fetchone()
            if kind == 'create_claim':
                require(known is None and pre == {'canonical_hash': fingerprint, 'claim_not_committed': True,
                        'resolved_entity_ids': sorted(refs)}, 'replan_required')
                for ref in refs:
                    require(db.execute('SELECT 1 FROM entities WHERE id=? AND vault_id=?', (ref, claim['vault_id'])).fetchone()
                            or entity_creates.get(ref) in op['depends_on'], 'invalid_claim_dependency')
            elif kind in {'add_evidence', 'skip_duplicate'}:
                require(pre == {'canonical_hash': fingerprint, 'resolved_entity_ids': sorted(refs)}, 'invalid_claim_precondition')
                require((known is not None and known['canonical_hash'] == fingerprint)
                        or creates.get(target) in op['depends_on'], 'replan_required')
            else:
                require(pre == {} and creates.get(target) in op['depends_on'] and op['details'].get('entity_id') in refs,
                        'invalid_claim_link')
