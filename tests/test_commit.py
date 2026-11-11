import copy
import http.client
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

from knowledge_ingestion.cli import scan
from knowledge_ingestion.commit import commit, resume_job, review, status, vault_lock
from knowledge_ingestion.contracts import canonical_json, content_hash
from knowledge_ingestion.fake_backend import FakeBackend
from knowledge_ingestion.llm import FakeLLM
from knowledge_ingestion.muninn import BackendError, MuninnAdapter, UnsupportedOperation
from knowledge_ingestion.planner import plan
from knowledge_ingestion.storage import connect, migrate
from test_plan import alias, claim, mention, proposal

ROOT = Path(__file__).resolve().parents[1]


class Crash(BaseException):
    pass


class CommitTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = self.root / 'data'
        self.source = self.root / 'source.md'
        self.source.write_text('Atlas is a project.\nAtlas uses SQLite.\n', encoding='utf-8')
        self.scanned = scan(self.source, self.data)
        self.value = proposal([mention('atlas', 'Atlas', kind='project', quote='Atlas is a project.')],
            claims=[claim('use', 'atlas', 'SQLite', 'Atlas uses SQLite.')])
        self.fixture = {'version': '1.0', 'responses': [{'document_path': self.source.as_uri(),
                         'chunk_ordinal': 0, 'proposal': self.value}]}
        self.report = plan(self.scanned['collection_id'], self.data, FakeLLM(self.fixture))
        self.backend = FakeBackend(self.root / 'FAKE.sqlite3')
        self.change_id, self.job = self.report['change_set_id'], self.report['job_id']

    def run_commit(self, **kwargs):
        return commit(self.data, self.change_id, self.backend, confirmed=True, actor='tester', **kwargs)

    def operation(self, kind):
        return next(op for op in self.report['operations'] if op['kind'] == kind)

    def crash_at(self, stage):
        def hook(actual, op):
            if actual == stage:
                raise Crash()
        return hook

    def test_confirmation_precedes_session_and_backend(self):
        with self.assertRaisesRegex(ValueError, 'confirmation'):
            commit(self.data, self.change_id, self.backend)
        self.assertEqual(self.backend.post_count, 0)
        self.assertIsNone(status(self.data, self.job)['commit_session'])

    def test_repeat_commit_and_reimport_no_second_engram(self):
        first = self.run_commit()
        self.assertEqual(first['status'], 'complete')
        self.assertEqual(self.run_commit()['status'], 'complete')
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)
        again = plan(self.scanned['collection_id'], self.data, FakeLLM(self.fixture))
        self.assertEqual(again['summary']['create_claim'], 0)
        self.assertEqual(again['summary']['skip_duplicate'], 1)
        result = commit(self.data, again['change_set_id'], self.backend, confirmed=True, actor='tester')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)
        with connect(self.data) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claims').fetchone()[0], 1)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_evidence').fetchone()[0], 1)

    def test_review_history_and_dependency_rejection(self):
        parent = self.operation('create_entity')['operation_id']
        def decisions(items):
            return {'version': '1.0', 'decisions': items}
        with self.assertRaisesRegex(ValueError, 'dependency'):
            review(self.data, self.change_id, decisions([{'operation_id': parent, 'decision': 'rejected', 'rationale': 'reject parent'}]), 'reviewer')
        self.assertEqual(status(self.data, self.job)['review_history'], [])
        items = [{'operation_id': op['operation_id'], 'decision': 'rejected', 'rationale': 'reject safely'} for op in self.report['operations']]
        reviewed = review(self.data, self.change_id, decisions(items), 'reviewer')
        self.assertEqual(len(reviewed['review_history']), len(items))
        self.assertTrue(all(h['actor'] == 'reviewer' and h['ts'] and h['rationale'] for h in reviewed['review_history']))
        self.assertTrue(reviewed['operations'][0]['evidence'][0]['filename'])
        self.assertIn('heading_path', reviewed['operations'][0]['evidence'][0])
        items = [{'operation_id': op['operation_id'], 'decision': 'approved', 'rationale': 'accept after inspection'} for op in self.report['operations']]
        review(self.data, self.change_id, decisions(items), 'reviewer-2')
        self.assertEqual(len(status(self.data, self.job)['review_history']), 2 * len(items))
        self.assertEqual(self.run_commit()['status'], 'complete')

    def test_unsafe_defer_cannot_be_published_by_review(self):
        source = self.root / 'uncertain.md'
        source.write_text('Maybe Atlas uses Redis.')
        scanned = scan(source, self.data)
        value = proposal([mention('a', 'Atlas', kind='project', quote='Atlas')], claims=[claim('u', 'a', 'Redis', 'Maybe Atlas uses Redis.', modality='hypothetical')])
        fixture = {'version': '1.0', 'responses': [{'document_path': source.as_uri(), 'chunk_ordinal': 0, 'proposal': value}]}
        report = plan(scanned['collection_id'], self.data, FakeLLM(fixture))
        op = next(o for o in report['operations'] if o['kind'] == 'defer')
        with self.assertRaisesRegex(ValueError, 'unsafe_review_approval'):
            review(self.data, report['change_set_id'], {'version': '1.0', 'decisions': [
                {'operation_id': op['operation_id'], 'decision': 'approved', 'rationale': 'force'}]}, 'reviewer')

    def test_prepared_checkpoint_before_post_crash_can_resume(self):
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('after_prepare'))
        state = status(self.data, self.job)
        self.assertEqual(state['outbox'][0]['attempts'], 0)
        self.assertEqual(self.backend.post_count, 0)
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)

    def test_crash_after_post_recovers_by_supported_fake_exact_marker(self):
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('after_post_before_id'))
        self.assertEqual(self.backend.post_count, 1)
        state = status(self.data, self.job)
        self.assertEqual(state['outbox'][0]['attempts'], 1)
        self.assertIsNone(state['outbox'][0]['observed_engram_id'])
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)

    def test_crash_after_post_unsupported_stops_without_retry_then_manual_known_id(self):
        self.backend = FakeBackend(self.root / 'FAKE.sqlite3', exhaustive=False)
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('after_post_before_id'))
        state = resume_job(self.data, self.job, self.backend)
        self.assertEqual(state['status'], 'needs_manual_repair')
        self.assertEqual(self.backend.post_count, 1)
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'needs_manual_repair')
        repair = {'operation_id': self.operation('create_claim')['operation_id'], 'engram_id': 'fake_1',
                  'actor': 'operator', 'rationale': 'Inspected durable backend record'}
        self.assertEqual(resume_job(self.data, self.job, self.backend, repair=repair)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)

    def test_known_id_persisted_before_crash_and_recovery_needs_no_marker(self):
        self.backend = FakeBackend(self.root / 'FAKE.sqlite3', exhaustive=False)
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('after_id_before_readback'))
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)

    def test_response_lost_resumes_by_marker_and_4xx_never_retries(self):
        self.backend.fail = 'response_lost'
        self.assertEqual(self.run_commit()['status'], 'needs_manual_repair')
        self.backend.fail = None
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)

    def test_definitive_4xx_is_terminal_and_preserves_all_unapplied(self):
        self.backend.fail = '4xx'
        result = self.run_commit()
        self.assertEqual(result['status'], 'needs_manual_repair')
        self.assertTrue(result['unapplied_operation_ids'])
        self.assertEqual(result['outbox'][0]['outcome'], 'write_rejected')
        self.backend.fail = None
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'needs_manual_repair')
        self.assertEqual(self.backend.post_count, 1)

    def test_readback_failure_preserves_known_id_and_resume_does_not_post(self):
        self.backend.fail = 'readback'
        self.assertEqual(self.run_commit()['status'], 'needs_manual_repair')
        self.assertEqual(status(self.data, self.job)['outbox'][0]['observed_engram_id'], 'fake_1')
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'needs_manual_repair')
        self.assertEqual(self.backend.post_count, 1)
        self.backend.fail = None
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)

    def test_mismatched_known_id_readback_never_applies(self):
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('after_id_before_readback'))
        with sqlite3.connect(self.backend.path) as db:
            value = json.loads(db.execute('SELECT payload FROM memories').fetchone()[0])
            value['content'] = 'different content'
            db.execute('UPDATE memories SET payload=?', (canonical_json(value),))
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'needs_manual_repair')
        self.assertEqual(self.backend.post_count, 1)
        with connect(self.data) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claims').fetchone()[0], 0)

    def test_source_and_snapshot_tamper_before_any_projection_or_post(self):
        self.source.write_text('changed')
        with self.assertRaisesRegex(ValueError, 'stale_source'):
            self.run_commit()
        self.assertEqual(self.backend.post_count, 0)
        with connect(self.data) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM entities').fetchone()[0], 0)
        self.source.write_text('Atlas is a project.\nAtlas uses SQLite.\n')
        Path(self.scanned['documents'][0]['snapshot']).write_text('tampered')
        with self.assertRaisesRegex(ValueError, 'snapshot'):
            self.run_commit()
        self.assertEqual(self.backend.post_count, 0)

    def test_artifact_and_persisted_config_tamper(self):
        path = self.root / 'change.json'
        altered = copy.deepcopy(self.report)
        altered['operations'][0]['decision'] = 'rejected'
        path.write_text(canonical_json(altered))
        with self.assertRaisesRegex(ValueError, 'file_mismatch'):
            commit(self.data, path, self.backend, confirmed=True, actor='tester')
        with connect(self.data) as db:
            db.execute("UPDATE jobs SET config_json='{}' WHERE id=?", (self.job,))
        with self.assertRaisesRegex(ValueError, 'config_hash_mismatch'):
            self.run_commit()
        self.assertEqual(self.backend.post_count, 0)

    def test_stale_ledger_and_config_rejected(self):
        with self.assertRaisesRegex(ValueError, 'replan_required'):
            self.run_commit(expected_config_hash='wrong')
        with connect(self.data) as db:
            entity = self.report['entities'][0]
            db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?)', (entity['id'], 'personal', entity['type'],
                       entity['canonical_name'], 'committed', 'now', canonical_json(entity)))
        with self.assertRaisesRegex(ValueError, 'replan_required'):
            self.run_commit()
        self.assertEqual(self.backend.post_count, 0)

    def test_concurrent_process_writer_is_rejected(self):
        command = [sys.executable, '-c', 'from knowledge_ingestion.commit import vault_lock;\nwith vault_lock("personal"):\n print("acquired")']
        with vault_lock('personal'):
            process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
        self.assertNotEqual(process.returncode, 0)
        self.assertIn('writer_busy', process.stderr)
        self.assertEqual(self.run_commit()['status'], 'complete')

    def test_changed_scan_membership_is_stale_and_hidden_directories_are_excluded(self):
        folder = self.root / 'vault'
        folder.mkdir()
        (folder / 'a.md').write_text('Atlas is a project.\nAtlas uses SQLite.')
        (folder / 'b.md').write_text('other')
        scanned = scan(folder, self.data)
        fixture = {'version': '1.0', 'responses': [{'document_path': 'a.md', 'chunk_ordinal': 0, 'proposal': self.value}]}
        report = plan(scanned['collection_id'], self.data, FakeLLM(fixture))
        (folder / 'b.md').unlink()
        scan(folder, self.data)
        with self.assertRaisesRegex(ValueError, 'stale_source'):
            commit(self.data, report['change_set_id'], self.backend, confirmed=True, actor='tester')
        (folder / 'b.md').write_text('other')
        (folder / '.git').mkdir()
        (folder / '.git' / 'hidden.md').write_text('hidden')
        scanned = scan(folder, self.data)
        report = plan(scanned['collection_id'], self.data, FakeLLM(fixture))
        self.assertNotIn('.git/hidden.md', [d['locator'] for d in report['inputs']['documents']])
        result = commit(self.data, report['change_set_id'], self.backend, confirmed=True, actor='tester')
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)

    def test_add_evidence_and_alias_are_local_only_with_readable_existing_entity(self):
        self.run_commit()
        self.source.write_text('Atlas is a project.\nAtlas uses SQLite.\nAtlas uses Redis.\nAtlas uses SQLite again.')
        scanned = scan(self.source, self.data)
        value = proposal([mention('atlas', 'Atlas', kind='project', quote='Atlas is a project.')], claims=[
            claim('use', 'atlas', 'SQLite', 'Atlas uses SQLite again.'),
            claim('new', 'atlas', 'Redis', 'Atlas uses Redis.')])
        fixture = {'version': '1.0', 'responses': [{'document_path': self.source.as_uri(), 'chunk_ordinal': 0, 'proposal': value}]}
        report = plan(scanned['collection_id'], self.data, FakeLLM(fixture))
        self.assertEqual(report['summary']['add_evidence'], 1)
        self.assertEqual(report['entities'], [])
        self.assertEqual(commit(self.data, report['change_set_id'], self.backend, confirmed=True, actor='tester')['status'], 'complete')
        self.assertEqual(self.backend.post_count, 2)
        with sqlite3.connect(self.backend.path) as db:
            contents = [json.loads(r[0]) for r in db.execute('SELECT payload FROM memories')]
        self.assertTrue(all(v['concept'].startswith('Atlas:') for v in contents))
        with connect(self.data) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claim_evidence').fetchone()[0], 3)

    def test_negative_published_statement_explicitly_preserves_negation(self):
        self.source.write_text('Atlas is a project.\nAtlas does not use SQLite.')
        scanned = scan(self.source, self.data)
        value = proposal([mention('a', 'Atlas', kind='project', quote='Atlas is a project.')], claims=[
            claim('n', 'a', 'SQLite', 'Atlas does not use SQLite.', polarity='negative')])
        fixture = {'version': '1.0', 'responses': [{'document_path': self.source.as_uri(), 'chunk_ordinal': 0, 'proposal': value}]}
        report = plan(scanned['collection_id'], self.data, FakeLLM(fixture))
        self.assertEqual(commit(self.data, report['change_set_id'], self.backend, confirmed=True, actor='tester')['status'], 'complete')
        with sqlite3.connect(self.backend.path) as db:
            content = json.loads(db.execute('SELECT payload FROM memories').fetchone()[0])['content']
        self.assertIn('НЕ uses SQLite', content.splitlines()[0])
        self.assertIn('valid time:', content.splitlines()[0])
        self.assertIn('Typed object:', content)

    def test_full_cli_scan_plan_review_commit_status_resume(self):
        fixture_path = self.root / 'fixture.json'
        fixture_path.write_text(canonical_json(self.fixture))
        artifact = self.root / 'artifact.json'
        def cli(*args, good=True):
            result = subprocess.run([sys.executable, '-m', 'knowledge_ingestion.cli', *args, '--data', str(self.data)], cwd=ROOT, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0 if good else 1, result.stderr)
            return json.loads(result.stdout if result.stdout else result.stderr)
        cli('scan', str(self.source))
        report = cli('plan', '--collection', self.scanned['collection_id'], '--fixture', str(fixture_path), '--output', str(artifact))
        inspected = cli('review', '--change-set', str(artifact))
        decision_file = self.root / 'decisions.json'
        decision_file.write_text(canonical_json({'version': '1.0', 'decisions': [
            {'operation_id': op['operation_id'], 'decision': 'approved', 'rationale': 'CLI inspection'} for op in report['operations']]}))
        cli('review', '--change-set', str(artifact), '--decisions', str(decision_file), '--actor', 'cli-tester')
        backend_args = ['--backend', 'fake', '--fake-store', str(self.backend.path)]
        cli('commit', '--change-set', str(artifact), '--actor', 'cli-tester', *backend_args, good=False)
        self.assertEqual(self.backend.post_count, 0)

        self.assertEqual(cli('commit', '--change-set', str(artifact), '--yes', '--actor', 'cli-tester', *backend_args)['status'], 'complete')
        self.assertEqual(cli('status', '--job', self.job)['status'], 'complete')
        self.assertEqual(cli('resume', '--job', self.job, *backend_args)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)

    def test_unsupported_publication_lock_platform_fails_with_fixed_code(self):
        with patch('knowledge_ingestion.commit.fcntl', None):
            with self.assertRaisesRegex(ValueError, 'publication_lock_platform_unsupported'):
                self.run_commit()
        self.assertEqual(self.backend.post_count, 0)

    def test_corpus_commit_remains_offline_and_source_injection_is_inert(self):
        corpus_data = self.root / 'corpus-ledger'
        scanned = scan(ROOT / 'fixtures/obsidian', corpus_data)
        with patch('socket.socket', side_effect=AssertionError('network forbidden')), patch('subprocess.run', side_effect=AssertionError('source executed')):
            report = plan(scanned['collection_id'], corpus_data, FakeLLM(ROOT / 'fixtures/offline-extraction.json'))
            result = commit(corpus_data, report['change_set_id'], self.backend, confirmed=True, actor='tester')
        self.assertEqual(result['status'], 'complete_with_deferred')
        self.assertEqual(self.backend.post_count, 2)
        self.assertTrue(result['unapplied_operation_ids'])
        self.assertTrue(all(o['kind'] in {'defer', 'conflict_warning'} for o in result['operations'] if o['state'] != 'applied'))
        with connect(corpus_data) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM aliases').fetchone()[0], 4)
            self.assertEqual(db.execute('SELECT COUNT(*) FROM claims').fetchone()[0], 2)

    def test_unknown_outbox_blocks_fresh_plan_marker_for_same_vault(self):
        self.backend.fail = 'response_lost'
        self.run_commit()
        self.backend.fail = None
        replanned = plan(self.scanned['collection_id'], self.data, FakeLLM(self.fixture))
        self.assertNotEqual(replanned['job_id'], self.job)
        with self.assertRaisesRegex(ValueError, 'pending_publication_requires_repair'):
            commit(self.data, replanned['change_set_id'], self.backend, confirmed=True, actor='tester')
        self.assertEqual(self.backend.post_count, 1)
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'complete')

    def test_crash_checkpoint_before_post_reconciles_supported_absence(self):
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('before_post'))
        self.assertEqual(self.backend.post_count, 0)
        self.assertEqual(resume_job(self.data, self.job, self.backend)['status'], 'complete')
        self.assertEqual(self.backend.post_count, 1)
        self.assertEqual(status(self.data, self.job)['outbox'][0]['attempts'], 2)

    def test_partial_commit_with_changed_source_or_ledger_is_not_retried(self):
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('after_post_before_id'))
        self.source.write_text('changed after successful POST')
        with self.assertRaisesRegex(ValueError, 'stale_source'):
            resume_job(self.data, self.job, self.backend)
        self.assertEqual(self.backend.post_count, 1)
        self.assertEqual(status(self.data, self.job)['status'], 'stale_source')

    def test_outbox_and_operation_payload_tamper_cannot_publish(self):
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('after_prepare'))
        with connect(self.data) as db:
            db.execute("UPDATE outbox SET expected_payload='{}'")
        with self.assertRaisesRegex(ValueError, 'outbox_payload_mismatch'):
            resume_job(self.data, self.job, self.backend)
        self.assertEqual(self.backend.post_count, 0)

    def test_backend_change_during_resume_is_replan_required(self):
        with self.assertRaises(Crash):
            self.run_commit(hook=self.crash_at('after_prepare'))
        other = FakeBackend(self.root / 'different-FAKE.sqlite3')
        with self.assertRaisesRegex(ValueError, 'replan_required'):
            resume_job(self.data, self.job, other)
        self.assertEqual(self.backend.post_count + other.post_count, 0)

    def test_interrupted_planning_cli_resume_requires_original_configuration(self):
        other_data = self.root / 'incomplete-ledger'
        scanned = scan(self.source, other_data)
        fixture = copy.deepcopy(self.fixture)
        fixture['responses'][0] = {k: v for k, v in fixture['responses'][0].items() if k != 'proposal'}
        fixture['responses'][0]['attempts'] = [{'provider_error': 'offline'}, {'provider_error': 'offline'},
                                               {'provider_error': 'offline'}, self.value]
        provider = FakeLLM(fixture)
        interrupted = plan(scanned['collection_id'], other_data, provider)
        self.assertEqual(interrupted['status'], 'needs_attention')
        with self.assertRaisesRegex(ValueError, 'plan_needs_attention'):
            review(other_data, interrupted['change_set_id'], {'version': '1.0', 'decisions': []}, 'tester')
        with self.assertRaisesRegex(ValueError, 'replan_required'):
            resume_job(other_data, interrupted['job_id'], provider=FakeLLM(self.fixture))
        recovered = resume_job(other_data, interrupted['job_id'], provider=provider)
        self.assertEqual(recovered['status'], 'planned')
        self.assertEqual(recovered['job_id'], interrupted['job_id'])

    def test_schema2_additive_migration_preserves_all_existing_rows(self):
        self.run_commit()
        with connect(self.data) as db:
            for table in ('review_decisions', 'commit_sessions', 'outbox', 'scan_membership', 'collection_scans'):
                db.execute('DROP TABLE ' + table)
            db.execute('PRAGMA user_version=2')
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")]
            before = {t: [tuple(r) for r in db.execute('SELECT * FROM ' + t)] for t in tables}
        with sqlite3.connect(self.data / 'ledger.sqlite3') as db:
            db.execute('PRAGMA foreign_keys=ON')
            migrate(db)
            migrate(db)
            for table, rows in before.items():
                self.assertEqual(db.execute('SELECT * FROM ' + table).fetchall(), rows, table)
            self.assertEqual(db.execute('PRAGMA user_version').fetchone()[0], 4)
            self.assertEqual(db.execute('PRAGMA foreign_key_check').fetchall(), [])


class AdapterTest(unittest.TestCase):
    def adapter(self):
        return MuninnAdapter('http://127.0.0.1:8475', 'synthetic', 'observe-secret', 'full-secret')

    def test_separate_credentials_vault_guard_and_explicit_marker_unsupported(self):
        with self.assertRaisesRegex(ValueError, 'separate'):
            MuninnAdapter('http://127.0.0.1:8475', 'v', 'same', 'same')
        adapter = self.adapter()
        with self.assertRaisesRegex(ValueError, 'vault'):
            adapter.get_engram('wrong', '0' * 26)
        with self.assertRaises(UnsupportedOperation):
            adapter.list_by_marker('synthetic', 'ki_op:example')
        self.assertFalse(adapter.capabilities['exact_exhaustive_marker_lookup'])
        self.assertNotIn('secret', adapter.identity)

    def test_truncated_post_and_redirect_timeout_are_unknown_no_retry(self):
        adapter = self.adapter()
        with patch.object(adapter._client, 'open', side_effect=http.client.IncompleteRead(b'partial')) as client:
            with self.assertRaises(BackendError) as raised:
                adapter.create_engram('synthetic', 'concept', 'content', ['ki_v1'], .8)
            self.assertTrue(raised.exception.unknown)
            self.assertEqual(client.call_count, 1)
        for code in (302, 408, 500, 403):
            with self.subTest(code=code), patch.object(adapter._client, 'open', side_effect=urllib.error.HTTPError('url', code, 'secret error', {}, None)):
                with self.assertRaises(BackendError) as raised:
                    adapter.create_engram('synthetic', 'c', 'x', ['ki_v1'], .8)
                self.assertEqual(raised.exception.unknown, code != 403)
                self.assertNotIn('secret', str(raised.exception))

    def test_observe_reads_and_full_post_use_distinct_bearers_without_cookies(self):
        adapter = self.adapter()
        requests = []
        class Response:
            status = 200
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{}'
        def open_request(request, **kwargs):
            requests.append(request)
            return Response()
        with patch.object(adapter._client, 'open', side_effect=open_request):
            adapter._request('GET', '/api/health')
            adapter._request('POST', '/api/engrams', {}, write=True)
        self.assertEqual(requests[0].get_header('Authorization'), 'Bearer observe-secret')
        self.assertEqual(requests[1].get_header('Authorization'), 'Bearer full-secret')
        self.assertTrue(all(r.get_header('Cookie') is None for r in requests))


if __name__ == '__main__':
    unittest.main()
