#!/usr/bin/env python3
"""Explicit fake/offline CLI flow on newly created synthetic sources only.

No real provider, backend, credentials, existing sources or stores are accepted.
The output directory must be new/empty. Artifacts remain available for review.
"""
import argparse
import json
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_demo(output):
    output = Path(output).absolute()
    if output.is_symlink() or any(p.is_symlink() for p in output.parents):
        raise ValueError('demo_output_must_not_contain_symlinks')
    if output.exists() and any(output.iterdir()):
        raise ValueError('demo_requires_new_or_empty_output_directory')
    output.mkdir(parents=True, exist_ok=True)
    # The checked-in corpus and extraction fixture are explicitly synthetic.
    sources = output / 'sources'
    shutil.copytree(ROOT / 'fixtures/obsidian', sources)
    fixture = ROOT / 'fixtures/offline-extraction.json'
    data, fake_store = output / 'data', output / 'FAKE-backend.sqlite3'
    commands, timings = [], {}

    def cli(name, *args):
        arguments = [str(a) for a in args]
        commands.append({'step': name, 'argv': ['python', '-m', 'knowledge_ingestion.cli', *arguments]})
        started = time.monotonic()
        result = subprocess.run([sys.executable, '-m', 'knowledge_ingestion.cli', *arguments],
                                cwd=ROOT, capture_output=True, text=True, timeout=60)
        timings[name] = round(time.monotonic() - started, 4)
        if result.returncode:
            raise RuntimeError('demo_step_failed:' + name + ':' + result.stderr[:300])
        value = json.loads(result.stdout)
        (output / (name + '.json')).write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        return value

    scanned = cli('scan', 'scan', sources, '--data', data)
    planned = cli('change-set', 'plan', '--collection', scanned['collection_id'], '--data', data,
                  '--llm-provider', 'fake', '--fixture', fixture, '--output', output / 'original-change-set.json')
    reviewed = cli('review', 'review', '--change-set', planned['change_set_id'], '--data', data)
    # Record a review decision separately, preserving the immutable original.
    safe = next(op for op in planned['operations'] if op['kind'] == 'create_claim')
    decisions = {'version': '1.0', 'decisions': [{'operation_id': safe['operation_id'],
        'decision': 'approved', 'rationale': 'Synthetic demo: inspected exact source evidence.'}]}
    decision_file = output / 'review-decisions.json'
    decision_file.write_text(json.dumps(decisions), encoding='utf-8')
    cli('review-applied', 'review', '--change-set', planned['change_set_id'], '--data', data,
        '--decisions', decision_file, '--actor', 'synthetic-demo')
    published = cli('commit', 'commit', '--change-set', planned['change_set_id'], '--data', data,
                    '--backend', 'fake', '--fake-store', fake_store, '--actor', 'synthetic-demo', '--yes')
    cli('status', 'status', '--job', planned['job_id'], '--data', data)
    cli('resume', 'resume', '--job', planned['job_id'], '--data', data,
        '--backend', 'fake', '--fake-store', fake_store)
    with sqlite3.connect(fake_store) as db:
        posts_before = db.execute("SELECT COUNT(*) FROM calls WHERE kind='POST'").fetchone()[0]
    cli('repeat-scan', 'scan', sources, '--data', data)
    repeated = cli('repeat-plan', 'plan', '--collection', scanned['collection_id'], '--data', data,
                   '--llm-provider', 'fake', '--fixture', fixture)
    cli('repeat-commit', 'commit', '--change-set', repeated['change_set_id'], '--data', data,
        '--backend', 'fake', '--fake-store', fake_store, '--actor', 'synthetic-demo', '--yes')
    with sqlite3.connect(fake_store) as db:
        posts_after = db.execute("SELECT COUNT(*) FROM calls WHERE kind='POST'").fetchone()[0]
        memories = db.execute('SELECT COUNT(*) FROM memories').fetchone()[0]
    checks = {'explicit_fake_provider': planned['config']['llm']['provider'] == 'fake',
              'explicit_fake_backend': published['commit_session']['backend_identity'].startswith('FAKE:'),
              'zero_muninn_calls_during_plan': planned['metrics']['muninn_write_calls'] == planned['metrics']['muninn_read_calls'] == 0,
              'repeat_import_zero_extra_posts': posts_after == posts_before,
              'review_history_recorded': len(published['review_history']) == 1,
              'all_chunks_completed': planned['metrics']['chunks'] == planned['metrics']['chunks_completed']}
    if not all(checks.values()):
        raise RuntimeError('demo_assertion_failed')
    report = {'version': '1.0', 'kind': 'synthetic_fake_offline_demo',
              'checked_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
              'result': 'PASS_with_documented_limitations', 'checks': checks,
              'scan': {'files': len(scanned['documents']), 'warnings': scanned['warnings']},
              'plan': {'job_id': planned['job_id'], 'config': planned['config'], 'summary': planned['summary'],
                       'metrics': planned['metrics'], 'warnings': planned['warnings']},
              'publication': {'status': published['status'], 'fake_memories': memories,
                              'fake_posts_before_repeat': posts_before, 'fake_posts_after_repeat': posts_after,
                              'unapplied_operation_ids': published['unapplied_operation_ids']},
              'semantic_quality': {'precision': None, 'recall': None, 'entity_resolution_accuracy': None,
                                   'status': 'NOT_MEASURED_scripted_fixture'},
              'external_llm_live': 'BLOCKED_unconfigured_no_credentials',
              'limits': ['FakeLLM scripts proposals; it does not understand arbitrary notes.',
                         'Fake marker recovery is stronger than pinned MuninnDB; see compatibility matrix.',
                         'This demo is not a semantic quality measurement or production readiness claim.'],
              'duration_seconds_by_step': timings, 'commands': commands}
    (output / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    summary = f'''# Synthetic offline demo

Result: **{report['result']}**. Explicit FakeLLM and durable fake backend.

- {len(scanned['documents'])} files; {planned['metrics']['chunks_completed']}/{planned['metrics']['chunks']} chunks.
- {planned['summary']['create_claim']} proposed new claims; {planned['summary']['defer']} defer operations; {planned['summary']['conflict_warning']} conflict warnings.
- Publication: `{published['status']}`; {memories} fake memories; {posts_before}→{posts_after} fake POSTs after identical reimport.
- Review history retained; exact evidence and deferred operations are in `original-change-set.json` and `status.json`.
- Precision, recall and entity resolution accuracy: **NOT MEASURED**. External LLM: **BLOCKED/unconfigured**.

Only the checked-in synthetic corpus was copied. This run made no MuninnDB or external LLM calls.
The fake backend's exhaustive marker lookup is a test guarantee, not a pinned MuninnDB capability.
See `docs/VALIDATION.md` and `KNOWN_LIMITATIONS.md` before using real sources.
'''
    (output / 'report.md').write_text(summary, encoding='utf-8')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'work/demo')
    args = parser.parse_args()
    try:
        report = run_demo(args.output)
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({'error': str(error)}), file=sys.stderr)
        return 1
    print(json.dumps({'result': report['result'], 'report': str(args.output / 'report.json'),
                      'summary': str(args.output / 'report.md')}))
    return 0


if __name__ == '__main__':
    sys.exit(main())
