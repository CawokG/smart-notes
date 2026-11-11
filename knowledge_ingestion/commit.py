"""Confirmed single-writer publication with a durable transactional outbox.

A POST is attempted only after its durable attempting checkpoint. On restart,
that checkpoint ALWAYS reconciles first. Unknown pinned-Muninn outcomes never
cause a second POST. Hooks exist solely for deterministic offline crash tests.
"""
from contextlib import contextmanager
try:
    import fcntl
except ImportError:  # Keep scan/plan/status importable on unsupported platforms.
    fcntl = None
import json
import os
from pathlib import Path
import tempfile

from .contracts import canonical_json, content_hash, parse_json
from .muninn import BackendError, UnsupportedOperation
from .publication import (PublicationError, check_preconditions, check_sources, dependencies,
                          load_change_set, require, text_identity)
from .storage import committed_revision, connect, contains_symlink, now


@contextmanager
def vault_lock(vault):
    # Shared between CLI processes/data dirs on this OS account, even when they
    # select different backend URLs. flock is released by the OS after a crash.
    require(fcntl is not None and hasattr(os, 'getuid') and hasattr(os, 'O_NOFOLLOW'),
            'publication_lock_platform_unsupported')
    directory = Path(tempfile.gettempdir()) / ('knowledge-ingestion-locks-' + str(os.getuid()))
    require(not contains_symlink(directory), 'invalid_vault_lock_path')
    directory.mkdir(mode=0o700, exist_ok=True)
    require(directory.stat().st_uid == os.getuid() and directory.stat().st_mode & 0o077 == 0, 'insecure_vault_lock_directory')
    path = directory / (content_hash(vault) + '.lock')
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise PublicationError('writer_busy') from None
        yield
    finally:
        os.close(fd)


def event(db, job_id, kind, payload):
    db.execute('INSERT INTO run_events(job_id,ts,stage,event_type,payload_redacted) VALUES (?,?,?,?,?)',
               (job_id, now(), 'commit', kind, canonical_json(payload)))


def set_status(db, report, state, error=None):
    db.execute('UPDATE jobs SET status=?,stage=?,error=?,updated_at=? WHERE id=?',
               (state, 'commit', error, now(), report['job_id']))
    db.execute('UPDATE change_sets SET status=? WHERE id=?', (state, report['change_set_id']))


def status(data, job_id):
    db = connect(data)
    try:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        require(job is not None, 'unknown_job')
        cs = db.execute('SELECT id,payload_hash FROM change_sets WHERE job_id=?', (job_id,)).fetchone()
        result = {'version': '1.0', 'job_id': job_id, 'status': job['status'], 'stage': job['stage'],
                  'error': job['error'], 'collection_id': job['collection_id'], 'vault_id': job['vault_id'],
                  'config_hash': job['config_hash'], 'operations': [], 'review_history': [], 'outbox': []}
        if cs:
            change_id = cs['id']
            result.update(change_set_id=change_id, payload_hash=cs['payload_hash'])
            result['operations'] = [dict(r) for r in db.execute('''SELECT id AS operation_id,operation_type AS kind,
                decision,state,applied_at,error FROM operations WHERE change_set_id=? ORDER BY id''', (change_id,))]
            result['outbox'] = [dict(r) for r in db.execute('''SELECT operation_id,marker,state,attempts,attempted_at,
                observed_engram_id,outcome,error FROM outbox WHERE change_set_id=? ORDER BY operation_id''', (change_id,))]
            result['review_history'] = [dict(r) for r in db.execute('SELECT * FROM review_decisions WHERE change_set_id=? ORDER BY seq', (change_id,))]
            session = db.execute('SELECT backend_identity,expected_revision,actor,confirmed_at FROM commit_sessions WHERE change_set_id=?', (change_id,)).fetchone()
            result['commit_session'] = dict(session) if session else None
        result['unapplied_operation_ids'] = [r['operation_id'] for r in result['operations'] if r['state'] != 'applied']
        result['manual_repair_operation_ids'] = [r['operation_id'] for r in result['outbox'] if r['state'] == 'needs_manual_repair']
        result['events'] = [{**dict(r), 'payload': json.loads(r['payload_redacted'])} for r in db.execute(
            'SELECT seq,ts,stage,event_type,payload_redacted FROM run_events WHERE job_id=? ORDER BY seq DESC LIMIT 200', (job_id,))][::-1]
        for item in result['events']:
            item.pop('payload_redacted')
        return result
    finally:
        db.close()


def review(data, reference, decisions=None, actor=None):
    db = connect(data)
    try:
        # Review and commit use the same lock. Append history and decisions in a
        # single transaction; an incomplete batch never leaves broken dependencies.
        report, job, _ = load_change_set(db, reference)
        with vault_lock(job['vault_id']):
            with db:
                db.execute('BEGIN IMMEDIATE')
                report, job, operations = load_change_set(db, reference)
                if decisions is not None:
                    require(report['status'] == 'planned', 'plan_needs_attention')
                    actor = text_identity(actor, 'actor', 100)
                    require(isinstance(decisions, dict) and set(decisions) == {'version', 'decisions'}
                            and decisions['version'] == '1.0' and isinstance(decisions['decisions'], list), 'invalid_review_file')
                    seen = set()
                    changes = []
                    for decision in decisions['decisions']:
                        require(isinstance(decision, dict) and set(decision) == {'operation_id', 'decision', 'rationale'}, 'invalid_review_decision')
                        op_id, value = decision['operation_id'], decision['decision']
                        require(isinstance(op_id, str) and op_id in operations and op_id not in seen, 'unknown_or_duplicate_operation_id')
                        seen.add(op_id)
                        require(value in {'approved', 'rejected', 'deferred'}, 'invalid_review_decision')
                        rationale = text_identity(decision['rationale'], 'rationale', 2000)
                        stored = operations[op_id]
                        require(stored['state'] == 'planned' and not db.execute('SELECT 1 FROM commit_sessions WHERE change_set_id=?', (report['change_set_id'],)).fetchone(), 'review_after_commit_started')
                        original = parse_json(stored['payload'])
                        require(value != 'approved' or original['decision'] == 'approved'
                                and original['kind'] not in {'defer', 'conflict_warning'}, 'unsafe_review_approval')
                        changes.append((op_id, stored['decision'], value, rationale))
                        stored['decision'] = value
                    dependencies(report, operations)
                    for op_id, previous, value, rationale in changes:
                        db.execute('INSERT INTO review_decisions(change_set_id,operation_id,actor,rationale,ts,previous_decision,decision) VALUES (?,?,?,?,?,?,?)',
                            (report['change_set_id'], op_id, actor, rationale, now(), previous, value))
                        db.execute('UPDATE operations SET decision=? WHERE change_set_id=? AND id=?', (value, report['change_set_id'], op_id))
                view = []
                for op in report['operations']:
                    evidence = []
                    for ev in report['evidence']:
                        if ev['id'] in op['evidence_ids']:
                            chunk = db.execute('SELECT heading_path FROM chunks WHERE revision_id=? AND start_byte<=? AND end_byte>=? ORDER BY ordinal LIMIT 1',
                                               (ev['revision_id'], ev['start_byte'], ev['end_byte'])).fetchone()
                            evidence.append({'evidence_id': ev['id'], 'filename': ev['locator'], 'quote': ev['quote'],
                                             'heading_path': json.loads(chunk[0]) if chunk else []})
                    view.append({**op, 'decision': operations[op['operation_id']]['decision'], 'evidence': evidence})
        return {'version': '1.0', 'change_set_id': report['change_set_id'], 'job_id': job['id'],
                'operations': view, 'review_history': status(data, job['id'])['review_history']}
    finally:
        db.close()


def expected_engram(db, report, operation, marker):
    claim = next(c for c in report['claims'] if c['claim_id'] == operation['target_id'])
    entities = {r['id']: r['canonical_name'] for r in db.execute('SELECT id,canonical_name FROM entities WHERE vault_id=?', (report['inputs']['vault_id'],))}
    entities.update({e['id']: e['canonical_name'] for e in report['entities']})
    subject = entities.get(claim['subject']['entity_id'], claim['subject']['entity_id'])
    obj = claim['object']
    target = entities.get(obj.get('entity_id'), obj.get('entity_id')) if obj['kind'] == 'entity' else str(obj['value'])
    concept = (subject + ': ' + claim['predicate'])[:200]
    content = (subject + (' — НЕ ' if claim['polarity'] == 'negative' else ' — ')
               + claim['predicate'] + ' ' + target + '; valid time: ' + canonical_json(claim['valid_time']) + '.\n'
               + 'Claim: ' + claim['claim_id'] + '\n'
               + 'Polarity: ' + claim['polarity'] + '; modality: ' + claim['modality'] + '\n'
               + 'Qualifiers: ' + canonical_json(claim['qualifiers']) + '\n'
               + 'Typed object: ' + canonical_json(obj) + '\n'
               + 'Valid time: ' + canonical_json(claim['valid_time']) + '\n'
               + 'Evidence: ' + ', '.join(operation['evidence_ids']))
    return {'vault': report['inputs']['vault_id'], 'concept': concept, 'content': content,
            'tags': ['ki_v1', marker, 'claim_ref:' + claim['claim_id']],
            'confidence': claim['extraction']['confidence_raw']}


def exact_readback(adapter, expected, engram_id):
    # GET is bounded; never place a create call in this retry loop.
    for _ in range(3):
        try:
            value = adapter.get_engram(expected['vault'], engram_id)
        except BackendError:
            continue
        if (isinstance(value, dict) and value.get('id') == engram_id
                and all(value.get(k) == expected[k] for k in ('vault', 'concept', 'content', 'confidence'))
                and isinstance(value.get('tags'), list) and set(value['tags']) == set(expected['tags'])):
            return True
    return False


def apply_local(db, report, operation, engram_id=None):
    target, kind = operation['target_id'], operation['kind']
    if kind == 'create_entity':
        value = next(e for e in report['entities'] if e['id'] == target)
        value = {**value, 'state': 'committed'}
        db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?)', (target, value['vault_id'], value['type'],
            value['canonical_name'], 'committed', now(), canonical_json(value)))
    elif kind == 'add_alias':
        value = next(a for a in report['aliases'] if a['id'] == target)
        value = {**value, 'state': 'committed'}
        old = db.execute('SELECT payload FROM aliases WHERE id=?', (target,)).fetchone()
        if old:
            previous = json.loads(old[0])
            require(all(previous[k] == value[k] for k in ('entity_id', 'normalized_value', 'scope')), 'replan_required')
            value['evidence_ids'] = sorted(set(previous['evidence_ids']) | set(value['evidence_ids']))
            db.execute('UPDATE aliases SET payload=? WHERE id=?', (canonical_json(value), target))
        else:
            db.execute('INSERT INTO aliases VALUES (?,?,?,?,?,?,?,?)', (target, value['entity_id'], value['value'],
                value['normalized_value'], value['scope'], value['status'], value['evidence_ids'][0], canonical_json(value)))
    elif kind in {'create_claim', 'add_evidence'}:
        value = next(c for c in report['claims'] if c['claim_id'] == target)
        if kind == 'create_claim':
            require(engram_id is not None, 'readback_required')
            value = {**value, 'state': 'committed', 'engram_id': engram_id, 'recorded_at': now()}
            db.execute('INSERT INTO claims VALUES (?,?,?,?,?,?,?)', (target, value['vault_id'], value['canonical_hash'],
                canonical_json(value), 'committed', engram_id, now()))
        for evidence_id in operation['evidence_ids']:
            db.execute('INSERT OR IGNORE INTO claim_evidence VALUES (?,?)', (target, evidence_id))
        # Preserve old claim semantics/engram and keep payload evidence in sync.
        current = json.loads(db.execute('SELECT payload FROM claims WHERE id=?', (target,)).fetchone()[0])
        current['evidence_ids'] = [r[0] for r in db.execute('SELECT evidence_id FROM claim_evidence WHERE claim_id=? ORDER BY evidence_id', (target,))]
        db.execute('UPDATE claims SET payload=? WHERE id=?', (canonical_json(current), target))
    for mention in report['mentions']:
        chosen = mention['resolution']['chosen_entity_id']
        if chosen and db.execute('SELECT 1 FROM entities WHERE id=?', (chosen,)).fetchone():
            value = {**mention, 'state': 'committed'}
            db.execute('INSERT OR IGNORE INTO mentions VALUES (?,?,?,?,?,?,?,?)', (value['id'], value['revision_id'],
                value['start_byte'], value['end_byte'], value['surface'], chosen, 'committed', canonical_json(value)))


def commit(data, reference, adapter, *, confirmed=False, actor=None, resume=False,
           expected_config_hash=None, repair=None, hook=None):
    db = connect(data)
    report = None
    try:
        report, job, _ = load_change_set(db, reference)
        with vault_lock(job['vault_id']):
            # Everything affecting eligibility is checked under both the vault
            # lock and SQLite write transaction before preparing any operations.
            with db:
                db.execute('BEGIN IMMEDIATE')
                report, job, operations = load_change_set(db, reference)
                require(report['status'] == 'planned', 'plan_needs_attention')
                require(adapter.vault == job['vault_id'], 'adapter_vault_mismatch')
                require(expected_config_hash is None or expected_config_hash == job['config_hash'], 'replan_required')
                session = db.execute('SELECT * FROM commit_sessions WHERE change_set_id=?', (report['change_set_id'],)).fetchone()
                if session:
                    require(session['payload_hash'] == content_hash(report) and session['config_hash'] == job['config_hash']
                            and session['backend_identity'] == adapter.identity, 'replan_required')
                    base = session['expected_revision']
                else:
                    require(not resume and confirmed, 'explicit_commit_confirmation_required')
                    actor = text_identity(actor, 'actor', 100)
                    base = report['base_revision']
                    # An unresolved earlier publication reserves this vault's
                    # writer workflow. A replan must not acquire a fresh marker
                    # and publish the same uncertain claim under another job.
                    pending = db.execute('''SELECT 1 FROM outbox o JOIN change_sets c ON c.id=o.change_set_id
                        JOIN jobs j ON j.id=c.job_id WHERE j.vault_id=? AND o.change_set_id<>?
                        AND o.attempts>0 AND o.state<>'applied' LIMIT 1''', (job['vault_id'], report['change_set_id'])).fetchone()
                    require(pending is None, 'pending_publication_requires_repair')
                require(committed_revision(db, job['vault_id']) == base, 'replan_required')
                check_sources(db, report, data)
                dependencies(report, operations)
                check_preconditions(db, report, operations)
                if not session:
                    db.execute('INSERT INTO commit_sessions VALUES (?,?,?,?,?,?,?)', (report['change_set_id'],
                        content_hash(report), job['config_hash'], adapter.identity, base, actor, now()))
                    event(db, job['id'], 'commit_confirmed', {'actor': actor, 'payload_hash': content_hash(report)})
                for op in report['operations']:
                    stored = operations[op['operation_id']]
                    if stored['decision'] != 'approved' or stored['state'] == 'applied':
                        continue
                    db.execute("UPDATE operations SET state=CASE WHEN state='planned' THEN 'prepared' ELSE state END WHERE change_set_id=? AND id=?", (report['change_set_id'], op['operation_id']))
                    if op['kind'] == 'create_claim':
                        marker = 'ki_op:' + op['operation_id']
                        expected = expected_engram(db, report, op, marker)
                        db.execute('''INSERT OR IGNORE INTO outbox(operation_id,change_set_id,marker,expected_payload,payload_hash,state)
                            VALUES (?,?,?,?,?,'prepared')''', (op['operation_id'], report['change_set_id'], marker,
                            canonical_json(expected), content_hash(expected)))
                        out = db.execute('SELECT * FROM outbox WHERE operation_id=?', (op['operation_id'],)).fetchone()
                        require(out['change_set_id'] == report['change_set_id'] and out['marker'] == marker
                                and parse_json(out['expected_payload']) == expected and out['payload_hash'] == content_hash(expected), 'outbox_payload_mismatch')
                set_status(db, report, 'committing')
            if hook:
                hook('after_prepare', None)
            try:
                health = adapter.health()
                require(health['capabilities']['exact_exhaustive_marker_lookup'] == adapter.capabilities['exact_exhaustive_marker_lookup'], 'backend_capability_mismatch')
            except BackendError as error:
                with db:
                    set_status(db, report, 'blocked_backend', error.code)
                return status(data, job['id'])
            if repair is not None:
                require(isinstance(repair, dict) and set(repair) == {'operation_id', 'engram_id', 'actor', 'rationale'}, 'invalid_manual_repair')
                repair_actor = text_identity(repair['actor'], 'actor', 100)
                text_identity(repair['rationale'], 'rationale', 2000)
                out = db.execute('SELECT * FROM outbox WHERE operation_id=? AND change_set_id=?', (repair['operation_id'], report['change_set_id'])).fetchone()
                require(out is not None and out['attempts'] > 0 and out['state'] != 'applied', 'invalid_manual_repair_operation')
                expected = json.loads(out['expected_payload'])
                require(exact_readback(adapter, expected, repair['engram_id']), 'manual_repair_readback_mismatch')
                with db:
                    db.execute("UPDATE outbox SET observed_engram_id=?,state='known_id',outcome='manual_verified_id',error=NULL WHERE operation_id=?", (repair['engram_id'], out['operation_id']))
                    event(db, job['id'], 'manual_repair_id_verified', {'operation_id': out['operation_id'], 'actor': repair_actor,
                        'rationale': repair['rationale'], 'engram_id': repair['engram_id']})
            for op in report['operations']:
                stored = db.execute('SELECT * FROM operations WHERE change_set_id=? AND id=?', (report['change_set_id'], op['operation_id'])).fetchone()
                if stored['decision'] != 'approved' or stored['state'] == 'applied':
                    continue
                require(all(db.execute('SELECT state FROM operations WHERE change_set_id=? AND id=?', (report['change_set_id'], d)).fetchone()[0] == 'applied' for d in op['depends_on']), 'unapplied_operation_dependency')
                engram_id = None
                if op['kind'] == 'create_claim':
                    out = dict(db.execute('SELECT * FROM outbox WHERE operation_id=?', (op['operation_id'],)).fetchone())
                    expected = json.loads(out['expected_payload'])
                    if out['attempts']:
                        engram_id = out['observed_engram_id']
                        if engram_id and exact_readback(adapter, expected, engram_id):
                            with db:
                                event(db, job['id'], 'known_id_reconciled', {'operation_id': op['operation_id'], 'engram_id': engram_id})
                        else:
                            engram_id = None
                            # A known ID whose content differs is never replaced
                            # using another marker hit or another create.
                            try:
                                if out['observed_engram_id'] or out['outcome'] == 'write_rejected':
                                    raise UnsupportedOperation('known_id_unverified_or_write_rejected')
                                require(adapter.capabilities.get('exact_exhaustive_marker_lookup') is True, 'marker_lookup_unsupported')
                                matches = adapter.list_by_marker(job['vault_id'], out['marker'])
                                if len(matches) == 1 and exact_readback(adapter, expected, matches[0]['id']):
                                    engram_id = matches[0]['id']
                                    with db:
                                        event(db, job['id'], 'marker_reconciled', {'operation_id': op['operation_id'], 'engram_id': engram_id})
                                elif not matches:
                                    # Only a complete, supported fake query can
                                    # establish absence. Pinned real never enters.
                                    pass
                                else:
                                    raise UnsupportedOperation('marker_ambiguous_or_mismatch')
                            except (UnsupportedOperation, BackendError, PublicationError):
                                with db:
                                    db.execute("UPDATE outbox SET state='needs_manual_repair',error='unknown_write_outcome' WHERE operation_id=?", (op['operation_id'],))
                                    db.execute("UPDATE operations SET state='needs_manual_repair',error='unknown_write_outcome' WHERE change_set_id=? AND id=?", (report['change_set_id'], op['operation_id']))
                                    set_status(db, report, 'needs_manual_repair', 'unknown_write_outcome')
                                    event(db, job['id'], 'reconciliation_unverified', {'operation_id': op['operation_id']})
                                return status(data, job['id'])
                    if engram_id is None:
                        with db:
                            db.execute("UPDATE outbox SET state='attempting',attempts=attempts+1,attempted_at=?,outcome='unknown_write_outcome',error=NULL WHERE operation_id=?", (now(), op['operation_id']))
                            event(db, job['id'], 'post_attempt', {'operation_id': op['operation_id']})
                        if hook:
                            hook('before_post', op)
                        try:
                            engram_id = adapter.create_engram(**expected)
                        except BackendError as error:
                            with db:
                                db.execute("UPDATE outbox SET state='needs_manual_repair',outcome=?,error=? WHERE operation_id=?", ('unknown_write_outcome' if error.unknown else 'write_rejected', error.code, op['operation_id']))
                                db.execute("UPDATE operations SET state='needs_manual_repair',error=? WHERE change_set_id=? AND id=?", (error.code, report['change_set_id'], op['operation_id']))
                                set_status(db, report, 'needs_manual_repair', error.code)
                                event(db, job['id'], 'post_outcome', {'operation_id': op['operation_id'],
                                    'outcome': 'unknown_write_outcome' if error.unknown else 'write_rejected', 'error': error.code})
                            return status(data, job['id'])
                        if hook:
                            hook('after_post_before_id', op)
                        with db:
                            db.execute("UPDATE outbox SET observed_engram_id=?,state='known_id',outcome='acknowledged',error=NULL WHERE operation_id=?", (engram_id, op['operation_id']))
                            event(db, job['id'], 'post_outcome', {'operation_id': op['operation_id'], 'outcome': 'acknowledged', 'engram_id': engram_id})
                        if hook:
                            hook('after_id_before_readback', op)
                        if not exact_readback(adapter, expected, engram_id):
                            with db:
                                db.execute("UPDATE outbox SET state='needs_manual_repair',error='readback_unverified' WHERE operation_id=?", (op['operation_id'],))
                                db.execute("UPDATE operations SET state='needs_manual_repair',error='readback_unverified' WHERE change_set_id=? AND id=?", (report['change_set_id'], op['operation_id']))
                                set_status(db, report, 'needs_manual_repair', 'readback_unverified')
                                event(db, job['id'], 'readback_unverified', {'operation_id': op['operation_id'], 'engram_id': engram_id})
                            return status(data, job['id'])
                    if hook:
                        hook('after_readback_before_apply', op)
                with db:
                    apply_local(db, report, op, engram_id)
                    db.execute("UPDATE operations SET state='applied',applied_at=?,error=NULL WHERE change_set_id=? AND id=?", (now(), report['change_set_id'], op['operation_id']))
                    if op['kind'] == 'create_claim':
                        db.execute("UPDATE outbox SET state='applied',observed_engram_id=?,outcome='readback_verified',error=NULL WHERE operation_id=?", (engram_id, op['operation_id']))
                    db.execute('UPDATE commit_sessions SET expected_revision=? WHERE change_set_id=?', (committed_revision(db, job['vault_id']), report['change_set_id']))
                    event(db, job['id'], 'operation_applied', {'operation_id': op['operation_id'], 'kind': op['kind']})
                if hook:
                    hook('after_local_apply', op)
            with db:
                remaining = db.execute("SELECT COUNT(*) FROM operations WHERE change_set_id=? AND state<>'applied'", (report['change_set_id'],)).fetchone()[0]
                set_status(db, report, 'complete_with_deferred' if remaining else 'complete')
            return status(data, job['id'])
    except PublicationError as error:
        if report is not None and error.code in {'stale_source', 'replan_required'}:
            with db:
                set_status(db, report, error.code, error.code)
        raise
    finally:
        db.close()


def resume_job(data, job_id, adapter=None, provider=None, context_provider=None, **kwargs):
    db = connect(data)
    try:
        job = db.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
        require(job is not None, 'unknown_job')
        change = db.execute('SELECT id FROM change_sets WHERE job_id=?', (job_id,)).fetchone()
        session = db.execute('SELECT 1 FROM commit_sessions WHERE change_set_id=?', (change[0],)).fetchone() if change else None
        if session:
            require(adapter is not None, 'resume_backend_required')
            return commit(data, change[0], adapter, resume=True, **kwargs)
        require(job['stage'] in {'extract', 'plan'} and provider is not None, 'resume_requires_original_provider_or_confirmed_commit')
        config = json.loads(job['config_json'])
        from .context import OfflineContextProvider
        context = context_provider or OfflineContextProvider()
        require(provider.identity == config['llm'] and context.mode == config['context_mode']
                and getattr(context, 'identity', None) == config.get('context_backend_identity')
                and content_hash(context.external_candidates) == config['external_candidates_hash'], 'replan_required')
        if kwargs.get('expected_config_hash'):
            require(kwargs['expected_config_hash'] == job['config_hash'], 'replan_required')
        require(committed_revision(db, job['vault_id']) == job['base_revision'], 'replan_required')
        if change:
            original, _, _ = load_change_set(db, change[0])
            check_sources(db, original, data)
        from .planner import plan
        result = plan(job['collection_id'], data, provider, vault=job['vault_id'],
                      digest_budget=config['digest_budget_bytes'], context_provider=context,
                      request_budget=config['request_budget_bytes'])
        require(result['job_id'] == job_id, 'replan_required')
        return result
    finally:
        db.close()
