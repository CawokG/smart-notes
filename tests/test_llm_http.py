"""Real urllib wire protocol, controlled synthetic loopback server; no external LLM."""
import copy
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from knowledge_ingestion.cli import scan
from knowledge_ingestion.commit import resume_job
from knowledge_ingestion.context import OfflineContextProvider, bounded_messages
from knowledge_ingestion.contracts import EXTRACTION_SCHEMA, canonical_json
from knowledge_ingestion.llm import (OpenAICompatibleLLM, ProviderError, empty_proposal,
                                     request_size, structured_payload)
from knowledge_ingestion.planner import plan
from knowledge_ingestion.storage import Ledger, connect
from test_plan import claim, mention, proposal


def envelope(value, **kwargs):
    return {'choices': [{'finish_reason': 'stop', 'message': {'role': 'assistant',
            'content': canonical_json(value)}}], **kwargs}


class SyntheticHTTP:
    def __init__(self, responses):
        self.responses, self.requests = list(responses), []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers['Content-Length']))
                owner.requests.append({'path': self.path, 'headers': dict(self.headers), 'body': body})
                action = owner.responses.pop(0) if len(owner.responses) > 1 else owner.responses[0]
                status, value, delay = action if isinstance(action, tuple) else (200, action, 0)
                if delay:
                    time.sleep(delay)
                raw = canonical_json(value).encode('utf-8')
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                if status == 302:
                    self.send_header('Location', owner.url + '/redirect-target')
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                try:
                    self.wfile.write(raw)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.server.daemon_threads = True
        self.url = 'http://127.0.0.1:' + str(self.server.server_port) + '/v1'
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class HTTPProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.data = self.root / 'ledger'
        self.source = self.root / 'source.md'
        self.source.write_text('Atlas is a project.\nAtlas uses SQLite.', encoding='utf-8')
        self.scanned = scan(self.source, self.data)
        self.value = proposal([mention('atlas', 'Atlas', 'project', 'Atlas is a project.')],
                              claims=[claim('use', 'atlas', 'SQLite', 'Atlas uses SQLite.')])

    def server(self, responses):
        server = SyntheticHTTP(responses)
        self.addCleanup(server.close)
        return server

    def test_wire_protocol_schema_anchors_usage_and_secret_redaction(self):
        server = self.server([envelope(self.value, usage={'prompt_tokens': 100, 'completion_tokens': 30,
            'total_tokens': 130, 'secret': 'RESPONSE_BODY_SENTINEL'})])
        secret = 'ephemeral-synthetic-key-only'
        with patch.dict(os.environ, {'SYNTHETIC_LLM_KEY': secret}):
            provider = OpenAICompatibleLLM(server.url, 'synthetic-model-v1', 'SYNTHETIC_LLM_KEY', local=True)
        report = plan(self.scanned['collection_id'], self.data, provider)
        self.assertEqual(report['status'], 'planned')
        self.assertEqual(report['summary']['create_claim'], 1)
        self.assertEqual(report['metrics']['llm_usage']['total_tokens'], 130)
        self.assertIsNone(report['metrics']['llm_cost'])
        self.assertEqual(len(server.requests), 1)
        request = server.requests[0]
        self.assertEqual(request['path'], '/v1/chat/completions')
        self.assertEqual(request['headers']['Authorization'], 'Bearer ' + secret)
        self.assertNotIn('Cookie', request['headers'])
        payload = json.loads(request['body'])
        self.assertEqual(set(payload), {'model', 'messages', 'temperature', 'response_format', 'stream'})
        self.assertEqual(payload['response_format'], {'type': 'json_object'})
        self.assertIn(canonical_json(EXTRACTION_SCHEMA), payload['messages'][1]['content'])
        source = json.loads(payload['messages'][-1]['content'])['source_untrusted']
        self.assertEqual(source['text'], self.source.read_text())
        self.assertLessEqual(len(request['body']), report['config']['request_budget_bytes'])
        self.assertEqual(report['metrics']['max_request_bytes'], len(request['body']))
        for ev in report['evidence']:
            raw = Path(self.scanned['documents'][0]['snapshot']).read_bytes()
            self.assertEqual(raw[ev['start_byte']:ev['end_byte']], ev['quote'].encode('utf-8'))
        with connect(self.data) as db:
            events = canonical_json([r[0] for r in db.execute('SELECT payload_redacted FROM run_events')])
        self.assertNotIn(secret, canonical_json(report) + events)
        self.assertNotIn('RESPONSE_BODY_SENTINEL', canonical_json(report) + events)

    def test_rate_limit_exhaustion_and_real_adapter_resume_same_hash(self):
        server = self.server([(429, {'error': 'NEVER_LOG_THIS_BODY'}, 0)] * 3 + [envelope(self.value)])
        provider = OpenAICompatibleLLM(server.url, 'fixture-model', local=True)
        report = plan(self.scanned['collection_id'], self.data, provider)
        self.assertEqual(report['status'], 'needs_attention')
        self.assertEqual(report['metrics']['chunks_completed'], 0)
        self.assertEqual(len(server.requests), 3)
        self.assertEqual(report['error']['code'], 'llm_rate_limited')
        with connect(self.data) as db:
            config_hash = db.execute('SELECT config_hash FROM jobs WHERE id=?', (report['job_id'],)).fetchone()[0]
        for wrong in (OpenAICompatibleLLM(server.url, 'different-model', local=True),
                      OpenAICompatibleLLM(server.url, 'fixture-model', local=True, timeout=2)):
            with self.assertRaisesRegex(ValueError, 'replan_required'):
                resume_job(self.data, report['job_id'], provider=wrong)
        with self.assertRaisesRegex(ValueError, 'replan_required'):
            resume_job(self.data, report['job_id'], provider=provider, expected_config_hash='wrong')
        self.assertEqual(len(server.requests), 3)
        resumed = resume_job(self.data, report['job_id'], provider=OpenAICompatibleLLM(server.url, 'fixture-model', local=True),
                             expected_config_hash=config_hash)
        self.assertEqual(resumed['job_id'], report['job_id'])
        self.assertEqual(resumed['status'], 'planned')
        self.assertEqual(len(server.requests), 4)
        self.assertNotIn('NEVER_LOG_THIS_BODY', canonical_json(report))

    def test_timeout_schema_anchor_and_malformed_envelopes_remain_incomplete(self):
        bad_anchor = copy.deepcopy(self.value)
        bad_anchor['entity_mentions'][0]['anchor']['quote'] = 'NOT IN THE SYNTHETIC SOURCE'
        cases = [
            ('timeout', (200, envelope(self.value), 0.06)),
            ('schema', envelope({'unexpected': 'RESPONSE_BODY_SENTINEL'})),
            ('anchor', envelope(bad_anchor)),
            ('null-choice', {'choices': [None]}),
            ('null-message', {'choices': [{'finish_reason': 'stop', 'message': None}]}),
            ('not-a-list', {'choices': 'secret-response-body'}),
            ('truncated', {'choices': [{'finish_reason': 'length', 'message': {'content': '{}'}}]}),
            ('tools', {'choices': [{'finish_reason': 'stop', 'message': {'tool_calls': [{'id': 'unsafe'}], 'content': '{}'}}]}),
        ]
        for index, (name, action) in enumerate(cases):
            with self.subTest(case=name):
                server = self.server([action])
                data = self.root / ('case-' + str(index))
                scanned = scan(self.source, data)
                provider = OpenAICompatibleLLM(server.url, 'fixture-model', local=True,
                                               timeout=0.02 if name == 'timeout' else 30)
                report = plan(scanned['collection_id'], data, provider)
                self.assertEqual(report['status'], 'needs_attention')
                self.assertEqual(report['metrics']['chunks_completed'], 0)
                self.assertEqual(len(server.requests), 3)
                with connect(data) as db:
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM chunk_checkpoints').fetchone()[0], 0)
                    self.assertEqual(db.execute('SELECT COUNT(*) FROM claims').fetchone()[0], 0)
                self.assertNotIn('RESPONSE_BODY_SENTINEL', canonical_json(report))

    def test_schema_and_anchor_errors_recover_within_initial_plus_two(self):
        wrong = copy.deepcopy(self.value)
        wrong['entity_mentions'][0]['anchor']['quote'] = 'missing'
        server = self.server([envelope({}), envelope(wrong), envelope(self.value)])
        report = plan(self.scanned['collection_id'], self.data,
                      OpenAICompatibleLLM(server.url, 'fixture-model', local=True))
        self.assertEqual(report['status'], 'planned')
        self.assertEqual(report['metrics']['validation_errors'], 2)
        self.assertEqual(len(server.requests), 3)

    def test_http_resume_keeps_successful_chunks_and_retries_only_incomplete_chunk(self):
        self.source.write_text('# One\n\nFirst synthetic chunk.\n\n# Two\n\nSecond synthetic chunk.')
        scanned = scan(self.source, self.data)
        self.assertEqual(len(scanned['documents'][0]['chunks']), 2)
        server = self.server([envelope(empty_proposal())] + [(429, {}, 0)] * 3 + [envelope(empty_proposal())])
        provider = OpenAICompatibleLLM(server.url, 'fixture-model', local=True)
        report = plan(scanned['collection_id'], self.data, provider)
        self.assertEqual(report['status'], 'needs_attention')
        self.assertEqual(report['metrics']['chunks_completed'], 1)
        resumed = resume_job(self.data, report['job_id'], provider=provider)
        self.assertEqual(resumed['status'], 'planned')
        self.assertEqual(resumed['metrics']['chunks_completed'], 2)
        ordinals = [json.loads(json.loads(r['body'])['messages'][-1]['content'])['source_untrusted']['chunk_ordinal']
                    for r in server.requests]
        self.assertEqual(ordinals, [0, 1, 1, 1, 1])

    def test_redirect_does_not_forward_credentials_or_make_another_request(self):
        server = self.server([(302, {'error': 'redacted'}, 0)])
        with patch.dict(os.environ, {'SYNTHETIC_LLM_KEY': 'synthetic-key'}):
            provider = OpenAICompatibleLLM(server.url, 'fixture-model', 'SYNTHETIC_LLM_KEY', local=True)
        with self.assertRaises(ProviderError) as failure:
            provider.generate_structured('extract', EXTRACTION_SCHEMA,
                [{'role': 'system', 'content': 'JSON'}, {'role': 'user', 'content': 'synthetic'}], provider.identity)
        self.assertEqual(str(failure.exception), 'llm_http_error')
        self.assertEqual(len(server.requests), 1)

    def test_real_selection_is_explicit_and_external_unconfigured_fails_without_network(self):
        for url, local in [('http://remote.invalid/v1', False), ('http://remote.invalid/v1', True),
                           ('https://user:password@remote.invalid/v1', False),
                           ('https://remote.invalid/v1?key=secret', False)]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                OpenAICompatibleLLM(url, 'model', local=local)
        with self.assertRaisesRegex(ValueError, 'unconfigured'):
            OpenAICompatibleLLM('https://api.openai.com/v1', 'model')
        server = self.server([envelope(empty_proposal())])
        cmd = [sys.executable, '-m', 'knowledge_ingestion.cli', 'plan', '--collection', self.scanned['collection_id'],
               '--data', str(self.data), '--llm-base-url', server.url, '--llm-model', 'fixture-model', '--llm-local']
        result = subprocess.run(cmd, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertIn('require_explicit', result.stderr)
        self.assertEqual(len(server.requests), 0)
        result = subprocess.run(cmd + ['--llm-provider', 'openai-compatible'], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout)['status'], 'planned')
        self.assertEqual(len(server.requests), 1)

    def test_resume_real_adapter_with_observe_context_requires_original_backend_configuration(self):
        from knowledge_ingestion.context import ObserveContextProvider
        from knowledge_ingestion.muninn import BackendError

        class ObserveFixture:
            identity = 'synthetic-observe:one'
            read_calls = 0

            def health(self):
                self.read_calls += 1
                raise BackendError('controlled_unavailable')

        server = self.server([(429, {}, 0)] * 3 + [envelope(self.value)])
        provider = OpenAICompatibleLLM(server.url, 'fixture-model', local=True)
        report = plan(self.scanned['collection_id'], self.data, provider,
                      context_provider=ObserveContextProvider(ObserveFixture()), request_budget=20000)
        with self.assertRaisesRegex(ValueError, 'replan_required'):
            resume_job(self.data, report['job_id'], provider=provider)
        wrong = ObserveFixture()
        wrong.identity = 'synthetic-observe:other'
        with self.assertRaisesRegex(ValueError, 'replan_required'):
            resume_job(self.data, report['job_id'], provider=provider, context_provider=ObserveContextProvider(wrong))
        resumed = resume_job(self.data, report['job_id'], provider=provider,
                             context_provider=ObserveContextProvider(ObserveFixture()))
        self.assertEqual(resumed['status'], 'planned')
        self.assertEqual(resumed['config']['request_budget_bytes'], 20000)
        self.assertIn('legacy_semantic_reconciliation_partial', [w['reason'] for w in resumed['warnings']])


class RequestBudgetTest(unittest.TestCase):
    def test_local_resolution_uses_entities_omitted_from_provider_context(self):
        from knowledge_ingestion.llm import FakeLLM
        from knowledge_ingestion.storage import now
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.md'
            source.write_text('Atlas is a project.\nAtlas uses SQLite.')
            data = root / 'data'
            scanned = scan(source, data)
            with connect(data) as db:
                for index in range(70):
                    name = 'Atlas' if index == 69 else 'Distinct Project ' + str(index)
                    entity = {'id': 'ent_' + str(index).zfill(3), 'vault_id': 'personal',
                              'canonical_name': name, 'type': 'project', 'state': 'committed', 'evidence_ids': []}
                    db.execute('INSERT INTO entities VALUES (?,?,?,?,?,?,?)',
                        (entity['id'], 'personal', 'project', name, 'committed', now(), canonical_json(entity)))
            value = proposal([mention('atlas', 'Atlas', 'project', 'Atlas is a project.')],
                              claims=[claim('use', 'atlas', 'SQLite', 'Atlas uses SQLite.')])
            provider = FakeLLM({'version': '1.0', 'responses': [{'document_path': source.as_uri(),
                                                               'chunk_ordinal': 0, 'proposal': value}]})
            report = plan(scanned['collection_id'], data, provider)
            context = json.loads(provider.requests[0]['messages'][-1]['content'])['context_bundle']
            self.assertNotIn('ent_069', [e['id'] for e in context['known_entities']])
            self.assertEqual(report['mentions'][0]['resolution']['chosen_entity_id'], 'ent_069')
            self.assertEqual(report['summary']['create_entity'], 0)
            self.assertEqual(report['summary']['create_claim'], 1)

    def test_full_wire_budget_omits_large_claims_and_counts_all_ledger_entries(self):
        # A real Ledger bundle, including >64 entities, long claim payload and
        # multibyte/control strings. Local resolution must retain every row.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.md'
            source.write_text('Synthetic source.')
            data = root / 'data'
            scanned = scan(source, data)
            with connect(data) as db:
                # Create the job with the public plan path, then add staged rows.
                from knowledge_ingestion.llm import FakeLLM
                report = plan(scanned['collection_id'], data, FakeLLM())
                ledger = Ledger(db, report['job_id'], 'personal')
                for index in range(70):
                    ledger.put_entity({'id': 'ent_' + str(index), 'vault_id': 'personal',
                                       'canonical_name': ('Ж🐈\t' * 40) + str(index),
                                       'evidence_ids': [], 'state': 'staged'})
                ledger.put_claim({'claim_id': 'large-claim', 'vault_id': 'personal',
                                  'canonical_hash': 'hash', 'state': 'proposed', 'evidence_ids': [],
                                  'qualifiers': {'huge': '🐈\t' * 20000}, 'predicate': 'uses'})
                bundle = OfflineContextProvider().bundle(ledger, 4096)
                model_config = {'model': 'synthetic-model'}
                source_info = {'text': 'Synthetic source.', 'heading_path': ['Ж🐈']}
                messages, metadata, measured = bounded_messages(source_info, bundle, model_config, 16384)
                actual = canonical_json(structured_payload(EXTRACTION_SCHEMA, messages, model_config)).encode('utf-8')
                context = json.loads(messages[-1]['content'])['context_bundle']
                self.assertEqual(metadata['input_counts']['known_entities'], 70)
                self.assertGreaterEqual(metadata['omitted_counts']['known_entities'], 6)
                self.assertEqual(metadata['omitted_counts']['known_claims'], 1)
                self.assertEqual(context['known_claims'], [])
                self.assertLessEqual(measured, 16384)
                self.assertEqual(measured, len(actual))
                self.assertEqual(len(ledger.entities()), 70)
                self.assertEqual(len(ledger.claims()), 1)
                self.assertEqual(json.loads(messages[-1]['content'])['source_untrusted'], source_info)

    def test_oversized_source_is_not_silently_truncated_or_sent(self):
        from knowledge_ingestion.llm import FakeLLM
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.md'
            # Each byte expands to six wire bytes; scanner chunk remains <=9000.
            source.write_bytes(b'\x01' * 8900)
            scanned = scan(source, root / 'data')
            provider = FakeLLM()
            report = plan(scanned['collection_id'], root / 'data', provider, request_budget=16384)
            self.assertEqual(report['status'], 'needs_attention')
            self.assertEqual(report['metrics']['chunks_completed'], 0)
            self.assertEqual(provider.requests, [])
            self.assertEqual(report['metrics']['provider_calls'], 0)


if __name__ == '__main__':
    unittest.main()
