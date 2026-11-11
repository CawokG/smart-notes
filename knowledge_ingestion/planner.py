"""Deterministic, local-only extraction/audit/reconciliation and ChangeSets.

No network client or publication API is imported. A provider receives data and
returns a proposal; only this service can validate and stage that proposal.
"""
import calendar
import json
import re
from collections import Counter
from datetime import datetime, timezone

from .context import CONTEXT_VERSION, OfflineContextProvider, bounded_messages
from .contracts import (EXTRACTION_SCHEMA, PARSER_VERSION, PROMPT_VERSION, RULES_VERSION,
                        VERSION, ValidationError, canonical_json, canonical_object,
                        canonical_predicate, canonical_time, claim_fingerprint,
                        content_hash, normalize_alias, normalize_json, stable_id,
                        validate_extraction)
from .evidence import has_direct_name, plain_source, read_snapshot, validate_anchors
from .llm import REQUEST_BYTES, SYSTEM_PROMPT, WIRE_VERSION, ProviderError
from .resolution import audit_alias, resolve_mention
from .storage import Ledger, committed_revision, connect, now

KINDS = {'create_entity', 'add_alias', 'create_claim', 'add_evidence',
         'link_claim_entity', 'skip_duplicate', 'defer', 'conflict_warning'}
SINGLE_VALUED = {'has_status', 'date_of_birth'}


def make_operation(kind, target_id, evidence_ids=(), depends_on=(), reasons=(),
                   details=None, source_refs=(), preconditions=None):
    operation = {'version': VERSION, 'kind': kind, 'target_id': target_id,
                 'depends_on': sorted(set(depends_on)),
                 'decision': 'deferred' if kind in {'defer', 'conflict_warning'} else 'approved',
                 'reason_codes': sorted(set(reasons)), 'evidence_ids': sorted(set(evidence_ids)),
                 'source_refs': sorted(set(source_refs)), 'details': details or {},
                 'preconditions': preconditions or {}}
    operation['operation_id'] = stable_id('op', operation)
    return operation


def topological_operations(operations):
    remaining = {op['operation_id']: op for op in operations}
    if len(remaining) != len(operations):
        raise ValueError('duplicate_operation_id')
    all_ids, emitted, result = set(remaining), set(), []
    if any(set(op['depends_on']) - all_ids for op in operations):
        raise ValueError('missing_operation_dependency')
    while remaining:
        ready = sorted(op_id for op_id, op in remaining.items() if set(op['depends_on']) <= emitted)
        if not ready:
            raise ValueError('cyclic_operation_dependencies')
        for op_id in ready:
            result.append(remaining.pop(op_id))
            emitted.add(op_id)
    return result


def time_window(value):
    if value['precision'] in {'unknown', 'unresolved'}:
        return None
    def bound(text, upper):
        if len(text) == 4:
            return datetime(int(text), 12 if upper else 1, 31 if upper else 1,
                            23 if upper else 0, 59 if upper else 0, 59 if upper else 0, tzinfo=timezone.utc)
        if len(text) == 7:
            year, month = map(int, text.split('-'))
            return datetime(year, month, calendar.monthrange(year, month)[1] if upper else 1,
                            23 if upper else 0, 59 if upper else 0, 59 if upper else 0, tzinfo=timezone.utc)
        if len(text) == 10:
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc,
                    hour=23 if upper else 0, minute=59 if upper else 0, second=59 if upper else 0)
        return datetime.fromisoformat(text.replace('Z', '+00:00'))
    return bound(value['from'], False), bound(value['to'] or value['from'], True)


def time_overlap(a, b):
    left, right = time_window(a), time_window(b)
    if left is None or right is None:
        return None
    return left[0] <= right[1] and right[0] <= left[1]


def conflict_reasons(claim, known):
    warnings, uncertainty = [], []
    for previous in known:
        if (previous['subject'] != claim['subject'] or previous['predicate'] != claim['predicate']
                or previous['qualifiers'] != claim['qualifiers'] or previous['modality'] != 'asserted'):
            continue
        same_object = canonical_object(previous['object']) == canonical_object(claim['object'])
        overlap = time_overlap(previous['valid_time'], claim['valid_time'])
        if same_object and previous['polarity'] != claim['polarity']:
            if overlap is True:
                warnings.append((previous['claim_id'], 'polarity_conflict'))
            elif overlap is None:
                uncertainty.append((previous['claim_id'], 'polarity_requires_temporal_review'))
        elif (not same_object and claim['predicate'] in SINGLE_VALUED and overlap is True
              and previous['polarity'] == claim['polarity'] == 'positive'):
            warnings.append((previous['claim_id'], 'single_value_conflict'))
    return warnings, uncertainty


def source_claim_reasons(proposal, spans, mentions):
    reasons = []
    text = '\n'.join(plain_source(ev['quote']) for ev in spans)
    folded = normalize_alias(text)
    if any(ev['source_role'] != 'direct' for ev in spans):
        reasons.append('non_direct_claim_evidence')
    if proposal['modality'] != 'asserted':
        reasons.append('modality_' + proposal['modality'])
    markers = [('hypothetical', r'возможно|может быть|\bmaybe\b|\bmight\b|\bpossibly\b'),
               ('planned', r'планиру\w*|намерен\w*|\bplans?\b|\bplanning\b'),
               ('reported', r'говорят|по словам|сообща\w*|\breportedly\b|\baccording to\b')]
    for modality, pattern in markers:
        if re.search(pattern, folded) and proposal['modality'] != modality:
            reasons.append('source_modality_mismatch')
    if '?' in text and proposal['modality'] != 'question':
        reasons.append('source_question_mismatch')
    negated = bool(re.search(r'\bне\b|никогда|перестал\w*|\bnot\b|\bnever\b|no longer', folded))
    if negated != (proposal['polarity'] == 'negative'):
        reasons.append('source_polarity_mismatch')
    if proposal['valid_time']['precision'] == 'unresolved':
        reasons.append('unresolved_temporal_reference')
    if (re.search(r'\bраньше\b|\bранее\b|\bвчера\b|\bсегодня\b|\bсейчас\b|\bзавтра\b|'
                  r'\bpreviously\b|\byesterday\b|\bcurrently\b|\bnow\b|\btomorrow\b|last year', folded)
            and proposal['valid_time']['precision'] != 'unresolved'):
        reasons.append('relative_time_requires_source_date')
    for key in ('from', 'to'):
        point = proposal['valid_time'][key]
        if point and point not in text:
            reasons.append('time_not_in_source')
    subject = mentions.get(proposal['subject_ref'])
    if subject and not has_direct_name(text, subject['surface']):
        reasons.append('subject_not_in_claim_evidence')
    obj = proposal['object']
    if obj['kind'] == 'entity':
        target = mentions[obj['ref']]
        if not has_direct_name(text, target['surface']):
            reasons.append('object_not_in_claim_evidence')
    elif obj['kind'] in {'literal', 'date', 'number'}:
        if not has_direct_name(text, str(obj['value'])):
            reasons.append('object_not_in_claim_evidence')
    elif obj['kind'] == 'boolean':
        expected = r'\btrue\b|\bда\b' if obj['value'] else r'\bfalse\b|\bнет\b'
        if not re.search(expected, folded):
            reasons.append('object_not_in_claim_evidence')
    # A common dangerous relation upgrade has a simple conservative guard.
    if canonical_predicate(proposal['predicate']) == 'owns' and not re.search(r'владе\w*|владел\w*|принадлеж\w*|\bowns?\b|\bowner\b', folded):
        reasons.append('ownership_not_in_source')
    def grounded_qualifier(value):
        if value is None:
            return True
        if isinstance(value, dict):
            return all(grounded_qualifier(v) for v in value.values())
        if isinstance(value, list):
            return all(grounded_qualifier(v) for v in value)
        if type(value) is bool:
            return bool(re.search(r'\btrue\b|\bда\b' if value else r'\bfalse\b|\bнет\b', folded))
        return has_direct_name(text, str(value))
    if not grounded_qualifier(proposal['qualifiers']):
        reasons.append('qualifier_not_in_source')
    return reasons


class OfflinePlanner:
    def __init__(self, db, ledger, provider, context, config):
        self.db, self.ledger, self.provider, self.context = db, ledger, provider, context
        self.config = config

    def dependency(self, kind, target_id):
        return [op['operation_id'] for op in self.ledger.operations()
                if op['kind'] == kind and op['target_id'] == target_id]

    def operation(self, *args, **kwargs):
        operation = make_operation(*args, **kwargs)
        self.ledger.operation(operation)
        return operation

    def block_staged_conflicts(self, conflicting_ids, new_id):
        """Both unpublished sides require review once a conflict is detected."""
        for prior in self.ledger.staged('claims'):
            if prior['claim_id'] not in conflicting_ids:
                continue
            if not self.dependency('create_claim', prior['claim_id']):
                continue  # Existing committed claims are never rewritten.
            prior['state'] = 'deferred'
            prior['reason_codes'] = sorted(set(prior['reason_codes']) | {'conflict_requires_review'})
            self.ledger.put_claim(prior)
            for op in self.ledger.operations():
                if op['target_id'] == prior['claim_id'] and op['kind'] in {'create_claim', 'add_evidence', 'link_claim_entity'}:
                    self.db.execute('DELETE FROM job_operations WHERE job_id=? AND id=?', (self.ledger.job_id, op['operation_id']))
            self.operation('defer', prior['claim_id'], prior['evidence_ids'], reasons=['conflict_requires_review'],
                           details={'conflicting_claim_ids': [new_id]})

    def process(self, proposal, spans, document, chunk):
        for ev in spans.values():
            self.ledger.put_evidence(ev)
        resolutions, mentions = {}, {}
        for i, mention in enumerate(proposal['entity_mentions']):
            ev = spans[f'entity_mentions[{i}]']
            hint_ev = spans.get(f'entity_mentions[{i}].identity_hint')
            resolved, entity = resolve_mention(mention, ev, hint_ev, document, self.ledger)
            resolutions[mention['ref']] = resolved
            mentions[mention['ref']] = mention
            if entity:
                self.ledger.put_entity(entity)
                self.operation('create_entity', entity['id'], entity['evidence_ids'],
                               reasons=['new_grounded_identity'],
                               preconditions={'identity_resolution': 'new_candidate'})
            start = ev['start_byte'] + ev['quote'].encode('utf-8').find(mention['surface'].encode('utf-8'))
            record = {'version': VERSION, 'id': stable_id('men', ev['revision_id'], start,
                         start + len(mention['surface'].encode('utf-8')), mention['surface']),
                      'revision_id': ev['revision_id'], 'start_byte': start,
                      'end_byte': start + len(mention['surface'].encode('utf-8')),
                      'surface': mention['surface'], 'state': 'staged', 'resolution': resolved,
                      'evidence_ids': sorted({ev['id']} | ({hint_ev['id']} if hint_ev else set()))}
            self.ledger.put_mention(record)
            if resolved['status'] in {'ambiguous', 'unresolved'}:
                self.operation('defer', record['id'], record['evidence_ids'], reasons=resolved['basis'],
                               details={'candidate_entity_ids': resolved['candidate_entity_ids'], 'surface': mention['surface']})
        for i, alias in enumerate(proposal['aliases']):
            value, reasons = audit_alias(alias, resolutions[alias['entity_ref']], mentions[alias['entity_ref']],
                                          spans[f'aliases[{i}]'], self.ledger)
            self.ledger.put_alias(value)
            self.operation('defer' if reasons else 'add_alias', value['id'], value['evidence_ids'],
                           depends_on=self.dependency('create_entity', value['entity_id']) if not reasons else (),
                           reasons=reasons or ['direct_alias_evidence', 'no_alias_collision'],
                           details={'entity_id': value['entity_id'], 'value': value['value'], 'scope': value['scope'],
                                    'candidate_entity_ids': value['candidate_entity_ids']})
        for i, proposal_claim in enumerate(proposal['claim_proposals']):
            evidence = [spans[f'claim_proposals[{i}].anchors[{j}]']
                        for j in range(len(proposal_claim['anchors']))]
            self.claim(proposal_claim, evidence, resolutions, mentions, document, chunk)

    def claim(self, proposed, evidence, resolutions, mentions, document, chunk):
        reasons = source_claim_reasons(proposed, evidence, mentions)
        subject_resolution = resolutions.get(proposed['subject_ref'])
        subject_id = subject_resolution['chosen_entity_id'] if subject_resolution else None
        if not subject_id:
            reasons.append('unresolved_subject')
        obj = proposed['object'].copy()
        resolved_ids = [subject_id] if subject_id else []
        if obj['kind'] == 'entity':
            object_id = resolutions[obj.pop('ref')]['chosen_entity_id']
            obj['entity_id'] = object_id
            if not object_id:
                reasons.append('unresolved_object')
            else:
                resolved_ids.append(object_id)
        reported_by = None
        if proposed['reported_by_ref']:
            reported_by = resolutions[proposed['reported_by_ref']]['chosen_entity_id']
            if not reported_by:
                reasons.append('unresolved_reporter')
            else:
                resolved_ids.append(reported_by)
        fingerprint = None
        if subject_id and (obj['kind'] != 'entity' or obj['entity_id']):
            fingerprint = claim_fingerprint(self.ledger.vault_id, subject_id, proposed['predicate'], obj,
                proposed['qualifiers'], proposed['polarity'], proposed['modality'], proposed['valid_time'])
        claim_id = (stable_id('clm', self.ledger.vault_id, fingerprint) if fingerprint
                    else stable_id('clm_unresolved', document['revision_id'], chunk['id'], proposed['ref']))
        evidence_ids = sorted({ev['id'] for ev in evidence})
        claim = {'version': VERSION, 'claim_id': claim_id, 'vault_id': self.ledger.vault_id,
                 'canonical_hash': fingerprint, 'subject': {'entity_id': subject_id},
                 'predicate': canonical_predicate(proposed['predicate']), 'object': obj,
                 'qualifiers': normalize_json(proposed['qualifiers']), 'polarity': proposed['polarity'],
                 'modality': proposed['modality'], 'valid_time': canonical_time(proposed['valid_time']),
                 'reported_by': reported_by, 'evidence_ids': evidence_ids,
                 'extraction': {**self.provider.identity, 'schema': VERSION, 'prompt': PROMPT_VERSION,
                                'confidence_raw': proposed['confidence_raw']},
                 'recorded_at': None, 'state': 'deferred' if reasons else 'proposed',
                 'reason_codes': sorted(set(reasons))}
        known = self.ledger.claims()
        known += [c for c in self.ledger.staged('claims')
                  if c['state'] == 'deferred' and 'conflict_requires_review' in c['reason_codes']]
        existing = next((c for c in known if c['canonical_hash'] == fingerprint), None) if fingerprint else None
        warnings, uncertainty = conflict_reasons(claim, known) if not reasons else ([], [])
        external = [c['id'] for c in self.context.external_candidates
                    if normalize_alias(c['subject_name']) == normalize_alias(mentions.get(proposed['subject_ref'], {}).get('canonical_name', ''))
                    and canonical_predicate(c['predicate']) == claim['predicate']]
        if external:
            reasons.append('external_memory_possible_match')
        if warnings or uncertainty or reasons:
            reasons += [reason for _, reason in warnings + uncertainty]
            if warnings or uncertainty:
                reasons.append('conflict_requires_review')
            # A rejected extraction must not replace an earlier safe proposal
            # with the same canonical key (e.g. a mismarked hypothetical quote).
            claim_id = stable_id('clm_deferred', self.ledger.vault_id, fingerprint,
                                 document['revision_id'], chunk['id'], proposed['ref'], sorted(set(reasons)))
            claim['claim_id'] = claim_id
            if warnings or uncertainty:
                self.block_staged_conflicts({c for c, _ in warnings + uncertainty}, claim_id)
            claim['state'], claim['reason_codes'] = 'deferred', sorted(set(reasons))
            # Preserve repeated deferred evidence, not just the latest quote.
            previous = next((c for c in self.ledger.staged('claims') if c['claim_id'] == claim_id), None)
            if previous:
                claim['evidence_ids'] = sorted(set(previous['evidence_ids']) | set(evidence_ids))
            self.ledger.put_claim(claim)
            self.operation('conflict_warning' if warnings else 'defer', claim_id, evidence_ids,
                           reasons=reasons, details={'conflicting_claim_ids': sorted({c for c, _ in warnings + uncertainty}),
                                                    'external_memory_candidate_ids': sorted(external)},
                           source_refs=[document['revision_id'], chunk['id']])
            return
        already = self.ledger.evidence_ids(claim_id)
        added = set(evidence_ids) - already
        claim['evidence_ids'] = sorted(already | set(evidence_ids))
        self.ledger.put_claim(claim)
        dependencies = sum((self.dependency('create_entity', entity_id) for entity_id in resolved_ids), [])
        if existing:
            kind = 'add_evidence' if added else 'skip_duplicate'
            self.operation(kind, claim_id, added if added else evidence_ids,
                           depends_on=self.dependency('create_claim', claim_id),
                           reasons=['same_canonical_claim', 'new_evidence' if added else 'same_evidence'],
                           details={'reconciliation': 'reinforce' if added else 'skip_duplicate', 'ledger_only': True},
                           preconditions={'canonical_hash': fingerprint, 'resolved_entity_ids': sorted(set(resolved_ids))})
        else:
            create = self.operation('create_claim', claim_id, evidence_ids, dependencies,
                reasons=['new_claim', 'grounded_evidence', 'resolved_identities'],
                details={'reconciliation': 'create'},
                preconditions={'canonical_hash': fingerprint, 'claim_not_committed': True,
                               'resolved_entity_ids': sorted(set(resolved_ids))})
            for entity_id in sorted(set(resolved_ids)):
                self.operation('link_claim_entity', claim_id, evidence_ids,
                               depends_on=[create['operation_id']] + self.dependency('create_entity', entity_id),
                               reasons=['resolved_claim_reference'], details={'entity_id': entity_id, 'ledger_only': True})


def load_documents(db, collection_id):
    if not db.execute('SELECT 1 FROM collections WHERE id=?', (collection_id,)).fetchone():
        raise ValueError('unknown_collection')
    if not db.execute('SELECT 1 FROM collection_scans WHERE collection_id=?', (collection_id,)).fetchone():
        raise ValueError('rescan_required')
    result = []
    for row in db.execute('''SELECT d.id AS document_id,d.locator,r.*,m.captured_at FROM documents d
                            JOIN scan_membership s ON s.document_id=d.id AND s.collection_id=d.collection_id
                            JOIN source_revisions r ON r.id=s.revision_id
                            LEFT JOIN revision_metadata m ON m.revision_id=r.id
                            WHERE d.collection_id=? ORDER BY d.locator,d.id''', (collection_id,)):
        value = dict(row)
        value['revision_id'] = value['id']
        value['chunks'] = [dict(c) for c in db.execute('SELECT * FROM chunks WHERE revision_id=? ORDER BY ordinal', (value['id'],))]
        if value['parser_version'] != PARSER_VERSION:
            raise ValueError('unsupported_parser_version')
        result.append(value)
    if not result:
        raise ValueError('collection_has_no_documents')
    return result


def evidence_manifest(db, ledger):
    ids = {r[0] for r in db.execute('SELECT evidence_id FROM job_evidence WHERE job_id=?', (ledger.job_id,))}
    for collection in ('entities', 'aliases', 'mentions', 'claims'):
        for value in ledger.staged(collection):
            ids.update(value['evidence_ids'])
    result = []
    for evidence_id in sorted(ids):
        row = db.execute('''SELECT e.*,d.id AS document_id,d.locator,r.sha256,r.snapshot
                            FROM evidence e JOIN source_revisions r ON e.revision_id=r.id
                            JOIN documents d ON d.id=r.document_id WHERE e.id=?''', (evidence_id,)).fetchone()
        if row is None:
            raise ValueError('missing_evidence_record')
        value = dict(row)
        raw = read_snapshot({'snapshot': value.pop('snapshot'), 'sha256': value['sha256']})
        if raw[value['start_byte']:value['end_byte']] != value['quote'].encode('utf-8'):
            raise ValueError('persisted_evidence_bytes_mismatch')
        value.pop('captured_at')  # Capture time is metadata, not semantic identity.
        result.append(value)
    return result


def policy_gate(operations, ledger, evidence):
    valid_evidence = {ev['id'] for ev in evidence}
    entities = {e['id'] for e in ledger.entities()}
    claims = {c['claim_id']: c for c in ledger.staged('claims')}
    for op in operations:
        if op['kind'] not in KINDS or not set(op['evidence_ids']) <= valid_evidence:
            raise ValueError('invalid_operation_or_provenance')
        if op['kind'] in {'defer', 'conflict_warning'}:
            if op['decision'] != 'deferred':
                raise ValueError('unsafe_operation_decision')
            continue
        if not op['evidence_ids']:
            raise ValueError('operation_without_evidence')
        if op['kind'] in {'create_claim', 'add_evidence', 'link_claim_entity'}:
            claim = claims[op['target_id']]
            refs = {claim['subject']['entity_id']}
            if claim['object']['kind'] == 'entity':
                refs.add(claim['object']['entity_id'])
            if claim['reported_by']:
                refs.add(claim['reported_by'])
            if (None in refs or not refs <= entities or claim['state'] != 'proposed'
                    or claim['modality'] != 'asserted' or claim['valid_time']['precision'] == 'unresolved'):
                raise ValueError('unsafe_claim_operation')
    return topological_operations(operations)


def stale_source_warnings(db, documents, vault, collection_id):
    warnings = []
    for document in documents:
        for row in db.execute('''SELECT ce.claim_id,e.quote,e.revision_id FROM claim_evidence ce
            JOIN claims c ON c.id=ce.claim_id JOIN evidence e ON e.id=ce.evidence_id
            JOIN source_revisions r ON r.id=e.revision_id
            WHERE c.vault_id=? AND r.document_id=? AND r.id<>? ORDER BY ce.claim_id,e.id''',
            (vault, document['document_id'], document['revision_id'])):
            if row['quote'].encode('utf-8') not in read_snapshot(document):
                warnings.append({'reason': 'possibly_stale_source', 'claim_id': row['claim_id'],
                                 'previous_revision_id': row['revision_id'], 'revision_id': document['revision_id']})
    for row in db.execute('''SELECT DISTINCT ce.claim_id,e.revision_id,d.locator FROM claim_evidence ce
        JOIN claims c ON c.id=ce.claim_id JOIN evidence e ON e.id=ce.evidence_id
        JOIN source_revisions r ON r.id=e.revision_id JOIN documents d ON d.id=r.document_id
        LEFT JOIN scan_membership s ON s.document_id=d.id AND s.collection_id=d.collection_id
        WHERE c.vault_id=? AND d.collection_id=? AND s.document_id IS NULL ORDER BY ce.claim_id,e.revision_id''',
        (vault, collection_id)):
        warnings.append({'reason': 'possibly_stale_source', 'claim_id': row['claim_id'],
                         'previous_revision_id': row['revision_id'], 'locator': row['locator'],
                         'detail': 'Source absent or ineligible in the current scan; memory retained.'})
    return warnings


def plan(collection_id, data, provider, vault='personal', digest_budget=4096,
         context_provider=None, request_budget=REQUEST_BYTES):
    if not isinstance(vault, str) or not vault.strip() or len(vault) > 100:
        raise ValueError('provide_a_vault_id')
    if type(digest_budget) is not int or not 512 <= digest_budget <= 16384:
        raise ValueError('digest_budget_must_be_512_to_16384_bytes')
    if type(request_budget) is not int or not 16384 <= request_budget <= 262144:
        raise ValueError('request_budget_must_be_16384_to_262144_bytes')
    context = context_provider or OfflineContextProvider()
    # External snippets stay possible matches, never canonical IDs or evidence.
    for candidate in context.external_candidates:
        if (not isinstance(candidate, dict) or set(candidate) != {'id', 'subject_name', 'predicate', 'snippet'}
                or any(not isinstance(v, str) or not v or len(v) > 1000 for v in candidate.values())):
            raise ValueError('invalid_external_memory_candidate')
        canonical_predicate(candidate['predicate'])
    db = connect(data)
    try:
        documents = load_documents(db, collection_id)
        for document in documents:
            read_snapshot(document)
        manifest = [{'document_id': d['document_id'], 'revision_id': d['revision_id'],
                     'sha256': d['sha256'], 'locator': d['locator'],
                     'chunk_ids': [c['id'] for c in d['chunks']]} for d in documents]
        config = {'version': VERSION, 'rules': RULES_VERSION, 'parser': PARSER_VERSION,
                  'schema': VERSION, 'prompt': PROMPT_VERSION, 'llm': provider.identity,
                  'temperature': 0, 'max_retries': 2, 'digest_budget_bytes': digest_budget,
                  'request_budget_bytes': request_budget, 'context_version': CONTEXT_VERSION,
                  'schema_hash': content_hash(EXTRACTION_SCHEMA), 'prompt_hash': content_hash(SYSTEM_PROMPT),
                  'wire_version': WIRE_VERSION,
                  'context_mode': context.mode, 'external_candidates_hash': content_hash(context.external_candidates)}
        scan_record = db.execute('SELECT * FROM collection_scans WHERE collection_id=?', (collection_id,)).fetchone()
        config['source_policy'] = json.loads(scan_record['policy_json'])
        if hasattr(context, 'identity'):
            config['context_backend_identity'] = context.identity
        base = committed_revision(db, vault)
        job_id = stable_id('job', collection_id, vault, manifest, content_hash(config), base)
        change_id = stable_id('chg', job_id, base)
        ledger = Ledger(db, job_id, vault)
        existing = db.execute('SELECT payload,status,payload_hash FROM change_sets WHERE id=?', (change_id,)).fetchone()
        if existing and existing['status'] != 'needs_attention':
            value = json.loads(existing['payload'])
            if content_hash(value) != existing['payload_hash']:
                raise ValueError('change_set_hash_mismatch')
            policy_gate(value['operations'], ledger, evidence_manifest(db, ledger))
            return value
        with db:
            db.execute('''INSERT OR IGNORE INTO jobs VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
                (job_id, collection_id, vault, content_hash(config), canonical_json(config), base,
                 canonical_json(manifest), 'running', 'extract', now(), now(), None))
            db.execute("UPDATE jobs SET status='running',stage='extract',error=NULL,updated_at=? WHERE id=?", (now(), job_id))
            for document in documents:
                db.execute("INSERT OR IGNORE INTO job_documents(job_id,revision_id,stage) VALUES (?,?,'pending')", (job_id, document['revision_id']))
        service = OfflinePlanner(db, ledger, provider, context, config)
        failure = None
        for document in documents:
            raw = read_snapshot(document)
            for chunk in document['chunks']:
                checkpoint = db.execute('SELECT * FROM chunk_checkpoints WHERE job_id=? AND chunk_id=?', (job_id, chunk['id'])).fetchone()
                if checkpoint:
                    if content_hash(json.loads(checkpoint['payload'])) != checkpoint['extraction_hash']:
                        raise ValueError('checkpoint_hash_mismatch')
                    continue
                source = {'document_path': document['locator'], 'document_id': document['document_id'],
                          'revision_id': document['revision_id'], 'chunk_id': chunk['id'],
                          'chunk_ordinal': chunk['ordinal'], 'heading_path': json.loads(chunk['heading_path']),
                          'text': raw[chunk['start_byte']:chunk['end_byte']].decode('utf-8'),
                          'source_date': None}
                previous_error = None
                for attempt in range(3):
                    try:
                        model_config = {'temperature': 0, **provider.identity, 'schema': VERSION,
                                        'prompt': PROMPT_VERSION, 'request_budget_bytes': request_budget}
                        bundle = context.bundle(ledger, digest_budget)
                        messages, budget, byte_count = bounded_messages(source, bundle, model_config,
                                                                        request_budget, previous_error)
                        with db:
                            ledger.event('context_prepared', {'chunk_id': chunk['id'], 'request_bytes': byte_count,
                                'budget': budget, 'request_hash': content_hash(messages),
                                'retrieval_ids': [h['id'] for h in bundle.get('relevant_engrams', []) if h.get('id')]})
                            ledger.event('provider_call', {'chunk_id': chunk['id'], 'attempt': attempt})
                        generated = provider.generate_structured('extract', EXTRACTION_SCHEMA, messages, model_config)
                        if getattr(provider, 'last_usage', None):
                            with db:
                                ledger.event('provider_usage', provider.last_usage)
                        proposed = validate_extraction(generated)
                        spans = validate_anchors(proposed, raw, document, document['chunks'])
                        break
                    except (ValidationError, ProviderError, TimeoutError, OSError) as exc:
                        previous_error = {'code': exc.code if isinstance(exc, (ValidationError, ProviderError)) else 'provider_unavailable',
                                          'path': exc.path if isinstance(exc, ValidationError) else '$',
                                          'chunk_id': chunk['id']}
                        with db:
                            ledger.event('validation_error' if isinstance(exc, ValidationError) else 'provider_error', previous_error)
                else:
                    failure = previous_error
                    with db:
                        service.operation('defer', chunk['id'], reasons=[failure['code'], 'needs_attention'],
                                          details={'error_path': failure['path']}, source_refs=[document['revision_id'], chunk['id']])
                        db.execute("UPDATE job_documents SET stage='needs_attention' WHERE job_id=? AND revision_id=?", (job_id, document['revision_id']))
                    break
                with db:
                    # A recovered chunk no longer needs its previous failure op.
                    for op in ledger.operations():
                        if op['target_id'] == chunk['id'] and 'needs_attention' in op['reason_codes']:
                            db.execute('DELETE FROM job_operations WHERE job_id=? AND id=?', (job_id, op['operation_id']))
                    service.process(proposed, spans, document, chunk)
                    digest = ledger.digest(digest_budget)
                    db.execute('INSERT INTO chunk_checkpoints VALUES (?,?,?,?,?)',
                               (job_id, chunk['id'], content_hash(proposed), canonical_json(proposed), canonical_json(digest)))
                    db.execute("UPDATE job_documents SET stage='extract',last_completed_chunk=?,checkpoint=? WHERE job_id=? AND revision_id=?",
                               (chunk['ordinal'], canonical_json({'chunk_id': chunk['id'], 'digest': digest}), job_id, document['revision_id']))
            if failure:
                break
            with db:
                db.execute("UPDATE job_documents SET stage='complete' WHERE job_id=? AND revision_id=?", (job_id, document['revision_id']))
        evidence = evidence_manifest(db, ledger)
        operations = policy_gate(ledger.operations(), ledger, evidence)
        status = 'needs_attention' if failure else 'planned'
        counts = Counter(op['kind'] for op in operations)
        event_counts = Counter(dict(r)['event_type'] for r in db.execute('SELECT event_type FROM run_events WHERE job_id=?', (job_id,)))
        completed = db.execute('SELECT COUNT(*) FROM chunk_checkpoints WHERE job_id=?', (job_id,)).fetchone()[0]
        context_events = [json.loads(r[0]) for r in db.execute(
            "SELECT payload_redacted FROM run_events WHERE job_id=? AND event_type='context_prepared'", (job_id,))]
        usages = [json.loads(r[0]) for r in db.execute(
            "SELECT payload_redacted FROM run_events WHERE job_id=? AND event_type='provider_usage'", (job_id,))]
        report = {'version': VERSION, 'change_set_id': change_id, 'job_id': job_id,
                  'base_revision': base, 'status': status, 'config': config,
                  'inputs': {'collection_id': collection_id, 'vault_id': vault, 'documents': manifest},
                  'summary': {kind: counts[kind] for kind in sorted(KINDS)},
                  'operations': operations, 'entities': ledger.staged('entities'),
                  'aliases': ledger.staged('aliases'), 'mentions': ledger.staged('mentions'),
                  'claims': ledger.staged('claims'), 'evidence': evidence,
                  'warnings': context.warnings + json.loads(scan_record['warnings_json'])
                              + stale_source_warnings(db, documents, vault, collection_id),
                  'metrics': {'files': len(documents), 'chunks': sum(len(d['chunks']) for d in documents),
                              'chunks_completed': completed, 'provider_calls': event_counts['provider_call'],
                              'validation_errors': event_counts['validation_error'], 'provider_errors': event_counts['provider_error'],
                              'max_request_bytes': max((e['request_bytes'] for e in context_events), default=0),
                              'context_entries_omitted': sum(sum(e['budget']['omitted_counts'].values()) for e in context_events),
                              'llm_usage': {key: sum(u.get(key, 0) for u in usages)
                                            for key in sorted({k for u in usages for k in u})} or None,
                              'llm_cost': None,
                              'muninn_read_calls': context.read_calls, 'muninn_write_calls': 0},
                  'error': failure}
        payload = canonical_json(report)
        with db:
            db.execute('''INSERT INTO change_sets VALUES (?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET
                          status=excluded.status,payload_hash=excluded.payload_hash,payload=excluded.payload''',
                       (change_id, job_id, base, status, content_hash(report), payload, now()))
            db.execute('DELETE FROM operations WHERE change_set_id=?', (change_id,))
            for operation in operations:
                db.execute('''INSERT INTO operations(id,change_set_id,operation_type,payload,decision) VALUES (?,?,?,?,?)''',
                           (operation['operation_id'], change_id, operation['kind'], canonical_json(operation), operation['decision']))
            db.execute('UPDATE jobs SET status=?,stage=?,updated_at=?,error=? WHERE id=?',
                       (status, 'plan', now(), canonical_json(failure) if failure else None, job_id))
        return report
    finally:
        db.close()
