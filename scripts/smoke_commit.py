#!/usr/bin/env python3
"""Exercise product scan/plan/review/commit/recovery in a disposable MuninnDB.

Reuses only the repository's isolation/bootstrap harness. Never accepts an
existing URL/data directory. All sources/credentials/storage are synthetic and
transient. Crash/response-loss injection is explicitly labelled in the report.
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.smoke_muninn import Probe, SmokeFailure, require, run
from knowledge_ingestion.cli import scan
from knowledge_ingestion.commit import commit, resume_job, review, status
from knowledge_ingestion.context import ObserveContextProvider
from knowledge_ingestion.llm import FakeLLM
from knowledge_ingestion.muninn import BackendError, MuninnAdapter, UnsupportedOperation
from knowledge_ingestion.planner import plan
from knowledge_ingestion.storage import connect

VAULT = 'ki-product'


class Crash(BaseException):
    pass


class InstrumentedAdapter(MuninnAdapter):
    """Real HTTP adapter with explicit service-boundary fault injection."""
    fail_readback = False
    lose_response = False
    last_id = None

    def create_engram(self, **kwargs):
        self.last_id = super().create_engram(**kwargs)
        if self.lose_response:
            raise BackendError('unknown_write_outcome', unknown=True)
        return self.last_id

    def get_engram(self, vault, engram_id):
        if self.fail_readback:
            raise BackendError('injected_readback_failure')
        return super().get_engram(vault, engram_id)


def extraction(name, negative=False):
    quote = f'{name} does not use SQLite.' if negative else f'{name} uses SQLite.'
    def anchor(text):
        return {'quote': text, 'chunk_ordinal': 0, 'occurrence_index': None,
                'origin': 'body', 'source_role': 'direct'}
    return {'version': '1.0', 'document_context_digest': '', 'aliases': [],
        'entity_mentions': [{'ref': 'project', 'surface': name, 'canonical_name': name,
            'type': 'project', 'scope': 'local', 'anchor': anchor(f'{name} is a project.'),
            'identity_hint': None, 'distinct': False}],
        'claim_proposals': [{'ref': 'use', 'subject_ref': 'project', 'predicate': 'uses',
            'object': {'kind': 'literal', 'value': 'SQLite'}, 'qualifiers': {},
            'polarity': 'negative' if negative else 'positive', 'modality': 'asserted',
            'valid_time': {'from': None, 'to': None, 'precision': 'unknown', 'temporal_expression': None},
            'reported_by_ref': None, 'anchors': [anchor(quote)], 'confidence_raw': .9}]}


def characterize_product(probe: Probe, report):
    probe.expect('POST', '/api/auth/login', {'username': 'root', 'password': probe.password}, service='ui')
    probe.expect('PUT', '/api/admin/vaults/config', {'name': VAULT, 'public': False}, admin=True)
    for role in ('observe', 'full'):
        key = probe.expect('POST', '/api/admin/keys', {'vault': VAULT, 'label': 'synthetic-product-' + role,
                            'mode': role}, admin=True, status=201)
        probe.tokens[role] = key['token']
    adapter = InstrumentedAdapter(f'http://127.0.0.1:{probe.ports["rest"]}', VAULT,
                                 probe.tokens['observe'], probe.tokens['full'])
    report['product_health'] = adapter.health()
    report['checks'] = {}
    checks = report['checks']
    with tempfile.TemporaryDirectory(prefix='product-', dir=ROOT / 'work/commit-smoke') as directory:
        temp = Path(directory)
        data = temp / 'ledger'
        def prepare(name, negative=False, observe=False):
            path = temp / (name + '.md')
            proposed = extraction(name, negative)
            path.write_text(f'{name} is a project.\n' + proposed['claim_proposals'][0]['anchors'][0]['quote'] + '\n', encoding='utf-8')
            scanned = scan(path, data)
            fixture = {'version': '1.0', 'responses': [{'document_path': path.as_uri(), 'chunk_ordinal': 0, 'proposal': proposed}]}
            provider = FakeLLM(fixture)
            context = ObserveContextProvider(MuninnAdapter(adapter.base_url, VAULT, probe.tokens['observe'])) if observe else None
            planned = plan(scanned['collection_id'], data, provider, vault=VAULT, context_provider=context)
            return planned, scanned, provider
        planned, scanned, provider = prepare('ProductQuasar')
        change, job = planned['change_set_id'], planned['job_id']
        require(planned['metrics']['muninn_read_calls'] == planned['metrics']['muninn_write_calls'] == 0, 'offline plan called backend')
        decisions = {'version': '1.0', 'decisions': [{'operation_id': op['operation_id'], 'decision': 'approved',
                       'rationale': 'Synthetic product smoke review'} for op in planned['operations']]}
        require(review(data, change, decisions, 'synthetic-smoke')['review_history'], 'review history absent')
        before = adapter.write_calls
        require(commit(data, change, adapter, confirmed=True, actor='synthetic-smoke')['status'] == 'complete', 'product commit failed')
        require(commit(data, change, adapter, confirmed=True, actor='synthetic-smoke')['status'] == 'complete', 'repeat commit failed')
        require(resume_job(data, job, adapter)['status'] == 'complete', 'repeat resume failed')
        reimported = plan(scanned['collection_id'], data, provider, vault=VAULT)
        require(reimported['summary']['create_claim'] == 0 and reimported['summary']['skip_duplicate'] == 1, 'reimport did not deduplicate')
        require(commit(data, reimported['change_set_id'], adapter, confirmed=True, actor='synthetic-smoke')['status'] == 'complete', 'reimport commit failed')
        require(adapter.write_calls == before + 1, 'repeat commit/reimport issued extra POST')
        checks['scan_fake_plan_review_commit_status_resume_reimport'] = {'result': 'PASS', 'create_posts': 1}
        observed, _, _ = prepare('ObserveQuasar', observe=True)
        require(observed['metrics']['muninn_write_calls'] == 0 and observed['metrics']['muninn_read_calls'] > 0, 'observe context metrics invalid')
        checks['explicit_observe_context'] = {'result': 'PASS', 'read_calls': observed['metrics']['muninn_read_calls'],
            'writes': 0, 'fallback': 'local ledger; optional native names never canonical IDs', 'cognitive_feedback': 'unmeasured'}
        negative, _, _ = prepare('NegativeQuasar', negative=True)
        require(commit(data, negative['change_set_id'], adapter, confirmed=True, actor='synthetic-smoke')['status'] == 'complete', 'negative commit failed')
        actual = adapter.get_engram(VAULT, adapter.last_id)
        require('НЕ uses SQLite' in actual['content'].splitlines()[0], 'negative content rendered positive')
        checks['negative_exact_product_readback'] = 'PASS'
        known, _, _ = prepare('KnownIdQuasar')
        adapter.fail_readback = True
        require(commit(data, known['change_set_id'], adapter, confirmed=True, actor='synthetic-smoke')['status'] == 'needs_manual_repair', 'injected readback failure not retained')
        saved = status(data, known['job_id'])['outbox'][0]
        require(saved['observed_engram_id'] == adapter.last_id and saved['attempts'] == 1, 'known ID not durable')
        before = adapter.write_calls
        adapter.fail_readback = False
        require(resume_job(data, known['job_id'], adapter)['status'] == 'complete', 'known-ID live recovery failed')
        require(adapter.write_calls == before, 'known-ID resume repeated POST')
        checks['injected_readback_failure_real_known_id_recovery'] = {'result': 'PASS', 'resume_posts': 0}
        crashed, _, _ = prepare('CrashQuasar')
        def crash(stage, operation):
            if stage == 'after_post_before_id':
                raise Crash()
        try:
            commit(data, crashed['change_set_id'], adapter, confirmed=True, actor='synthetic-smoke', hook=crash)
            raise SmokeFailure('crash was not injected')
        except Crash:
            pass
        before = adapter.write_calls
        for _ in range(2):
            require(resume_job(data, crashed['job_id'], adapter)['status'] == 'needs_manual_repair', 'unsupported recovery did not stop')
        require(adapter.write_calls == before, 'crash resume repeated real POST')
        op_id = next(o['operation_id'] for o in crashed['operations'] if o['kind'] == 'create_claim')
        require(resume_job(data, crashed['job_id'], adapter, repair={'operation_id': op_id, 'engram_id': adapter.last_id,
            'actor': 'synthetic-operator', 'rationale': 'Harness retained acknowledged ID outside simulated crashed service'})['status'] == 'complete', 'verified manual ID recovery failed')
        require(adapter.write_calls == before, 'manual recovery repeated POST')
        checks['injected_crash_after_real_POST_before_SQLite'] = {'result': 'PASS', 'automatic': 'needs_manual_repair',
            'resume_posts': 0, 'manual_known_id_exact_readback': 'PASS'}
        lost, _, _ = prepare('LostResponseQuasar')
        adapter.lose_response = True
        require(commit(data, lost['change_set_id'], adapter, confirmed=True, actor='synthetic-smoke')['status'] == 'needs_manual_repair', 'lost response not retained')
        adapter.lose_response = False
        before = adapter.write_calls
        require(resume_job(data, lost['job_id'], adapter)['status'] == 'needs_manual_repair', 'lost-response resume unsafe')
        op_id = next(o['operation_id'] for o in lost['operations'] if o['kind'] == 'create_claim')
        require(resume_job(data, lost['job_id'], adapter, repair={'operation_id': op_id, 'engram_id': adapter.last_id,
            'actor': 'synthetic-operator', 'rationale': 'Real acknowledged ID retained by response-loss injector'})['status'] == 'complete', 'lost-response manual repair failed')
        require(adapter.write_calls == before, 'lost-response repair repeated POST')
        checks['injected_response_loss_after_real_success'] = {'result': 'PASS', 'resume_posts': 0}
        try:
            adapter.list_by_marker(VAULT, 'ki_op:synthetic')
            raise SmokeFailure('real exhaustive marker capability incorrectly supported')
        except UnsupportedOperation:
            checks['real_exhaustive_marker'] = 'UNSUPPORTED; no list request/no POST retry'
        refused, _, _ = prepare('RefusedQuasar')
        invalid = InstrumentedAdapter(adapter.base_url, VAULT, probe.tokens['observe'], 'mk_synthetic_invalid_full')
        require(commit(data, refused['change_set_id'], invalid, confirmed=True, actor='synthetic-smoke')['status'] == 'needs_manual_repair', 'real 401 refusal not retained')
        require(resume_job(data, refused['job_id'], invalid)['status'] == 'needs_manual_repair' and invalid.write_calls == 1, 'refused POST retried')
        checks['real_invalid_full_401'] = {'result': 'PASS', 'write_attempts': 1, 'resume_posts': 0}
        report['product_calls'] = {'observe_reads': adapter.read_calls, 'full_posts': adapter.write_calls,
                                   'refused_full_posts': invalid.write_calls}
        with connect(data) as db:
            report['product_ledger'] = {'committed_claims': db.execute('SELECT COUNT(*) FROM claims').fetchone()[0],
                'applied_outbox': db.execute("SELECT COUNT(*) FROM outbox WHERE state='applied'").fetchone()[0],
                'manual_repair_outbox': db.execute("SELECT COUNT(*) FROM outbox WHERE state='needs_manual_repair'").fetchone()[0]}
        # No credentials, payload text, source paths, or SQLite files in report.
        encoded = json.dumps(report)
        require(all(secret not in encoded for secret in probe.secrets()), 'credential leaked into report')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary', type=Path, default=ROOT / 'work/muninn-spike/muninn-linux-amd64')
    parser.add_argument('--output', type=Path, default=ROOT / 'work/commit-smoke/report.json')
    args = parser.parse_args()
    work = ROOT / 'work/commit-smoke'
    work.mkdir(parents=True, exist_ok=True)
    report = {'report_version': 1, 'checked_at': datetime.now(timezone.utc).isoformat(), 'result': 'FAIL'}
    exit_code = 1
    try:
        run(args.binary.resolve(), work, report, characterize_instance=characterize_product)
        report['result'] = 'PASS_with_documented_limitations'
        exit_code = 0
    except (SmokeFailure, BackendError, OSError, ValueError) as error:
        report['failure'] = type(error).__name__ + ': ' + str(error)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + '\n', encoding='utf-8')
    print(json.dumps({'result': report['result'], 'checks': report.get('checks'), 'failure': report.get('failure')}, ensure_ascii=False))
    return exit_code


if __name__ == '__main__':
    sys.exit(main())
