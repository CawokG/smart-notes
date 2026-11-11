"""Offline capability fallback: committed ledger plus this job's staged overlay."""
import copy

from .contracts import EXTRACTION_SCHEMA, VERSION, canonical_json

CONTEXT_VERSION = 'bounded-context-1'
ENTRY_BYTES = 2048


def bounded_messages(source, bundle, model_config, maximum, feedback=None):
    """Bound the complete serialized wire body, retaining exact source text.

    Context is a shortlist only; full resolution always reads the local ledger.
    Oversized entries are omitted whole rather than silently changing a claim.
    """
    from .llm import ProviderError, extraction_messages, request_size
    value = copy.deepcopy(bundle)
    limits = {'known_entities': 64, 'approved_aliases': 64, 'known_claims': 40,
              'external_memory_candidates': 12, 'relevant_engrams': 7, 'retrieval_trace': 8}
    before, omitted = {}, {}
    for field, limit in limits.items():
        entries = value.get(field, [])
        before[field] = len(entries)
        value[field] = [e for e in entries[:limit]
                        if len(canonical_json(e).encode('utf-8')) <= ENTRY_BYTES]
        omitted[field] = before[field] - len(value[field])
    digest = value.get('staged_digest', {})
    before['staged_digest_items'] = len(digest.get('items', []))
    omitted['staged_digest_items'] = 0
    metadata = {'version': CONTEXT_VERSION, 'request_budget_bytes': maximum,
                'entry_budget_bytes': ENTRY_BYTES, 'input_counts': before, 'omitted_counts': omitted,
                'source_truncated': False, 'full_local_resolution': True}
    value['budget'] = metadata

    def messages():
        result = extraction_messages(source, value)
        if feedback:
            result[0]['content'] += '\nValidation feedback: ' + canonical_json(feedback)
        return result

    result = messages()
    while request_size(EXTRACTION_SCHEMA, result, model_config) > maximum:
        # Drop the largest remaining context tail, including staged digest.
        groups = [(key, value[key]) for key in limits if value[key]]
        if digest.get('items'):
            groups.append(('staged_digest_items', digest['items']))
        if not groups:
            raise ProviderError('request_budget_exceeded')
        field, entries = max(groups, key=lambda pair: len(canonical_json(pair[1]).encode('utf-8')))
        entries.pop()
        omitted[field] += 1
        if field == 'staged_digest_items':
            digest['omitted'] = digest.get('omitted', 0) + 1
        result = messages()
    return result, metadata, request_size(EXTRACTION_SCHEMA, result, model_config)


class OfflineContextProvider:
    mode = 'offline_ledger_only'

    def __init__(self, external_candidates=()):
        self.external_candidates = list(external_candidates)
        self.read_calls = 0
        self.warnings = [{'reason': 'offline_ledger_only', 'detail': 'Explicit offline context; no backend calls made.'}]

    def bundle(self, ledger, digest_budget):
        return {'version': VERSION, 'capability_fallback': 'offline_ledger_only',
                'known_entities': [dict(e, trust='committed' if e.get('state') == 'committed' else 'staged_uncommitted')
                                   for e in ledger.entities()],
                'approved_aliases': [a for a in ledger.aliases() if a['status'] == 'approved'],
                'known_claims': ledger.claims(),
                'external_memory_candidates': self.external_candidates,
                'retrieval_trace': [], 'muninn_read_calls': 0, 'muninn_write_calls': 0,
                'staged_digest': ledger.digest(digest_budget)}


class ObserveContextProvider(OfflineContextProvider):
    """Opt-in, bounded pinned-Muninn context with explicit local fallback.

    Native names/snippets never become canonical IDs or source evidence.
    Observe keys reduce side effects; hidden cognitive feedback is unmeasured.
    """
    mode = 'muninn_observe_with_local_fallback'

    def __init__(self, adapter):
        super().__init__()
        self.adapter = adapter
        self.identity = adapter.identity
        self.ready = None
        self.warnings = [{'reason': 'observe_cognitive_feedback_unmeasured',
                          'detail': 'Observe GET/ACTIVATE; full cognitive immutability is not established.'},
                         {'reason': 'native_entities_local_fallback', 'detail': 'Native name lookup is optional; local IDs remain authoritative.'},
                         {'reason': 'legacy_semantic_reconciliation_partial',
                          'detail': 'Retrieved snippets are unstructured possible matches; no complete legacy semantic conflict/dedup gate.'}]

    def bundle(self, ledger, digest_budget):
        from .muninn import BackendError
        bundle = super().bundle(ledger, digest_budget)
        trace, hits = [], []
        try:
            if self.ready is None:
                self.adapter.health()
                self.ready = True
            if self.ready:
                for claim in [c for c in ledger.claims() if c.get('engram_id')][:2]:
                    hit = self.adapter.get_engram(ledger.vault_id, claim['engram_id'])
                    if hit:
                        hits.append({'id': hit['id'], 'concept': str(hit.get('concept', ''))[:240],
                                     'content': str(hit.get('content', ''))[:1800], 'trust': 'external_possible_match'})
                contexts = [e['canonical_name'][:240] for e in ledger.entities()[:3]] or ['Knowledge context']
                hits.extend(self.adapter.activate_observe(ledger.vault_id, contexts, limit=5))
                trace.append({'capability': 'observe_get_activate', 'status': 'available', 'max_results': 5})
        except (BackendError, ValueError):
            self.ready = False
            warning = {'reason': 'muninn_context_unavailable', 'detail': 'Explicit local-ledger fallback; no full-key read or write.'}
            if warning not in self.warnings:
                self.warnings.append(warning)
            trace.append({'capability': 'observe_get_activate', 'status': 'local_ledger_fallback'})
        self.read_calls = self.adapter.read_calls
        bundle.update(capability_fallback=self.mode if self.ready else 'local_ledger_fallback',
                      relevant_engrams=hits[:7], retrieval_trace=trace, muninn_read_calls=self.read_calls)
        return bundle
