"""Offline capability fallback: committed ledger plus this job's staged overlay."""
from .contracts import VERSION


class OfflineContextProvider:
    mode = 'offline_ledger_only'

    def __init__(self, external_candidates=()):
        self.external_candidates = list(external_candidates)
        self.read_calls = 0
        self.warnings = [{'reason': 'offline_ledger_only', 'detail': 'Explicit offline context; no backend calls made.'}]

    def bundle(self, ledger, digest_budget):
        return {'version': VERSION, 'capability_fallback': 'offline_ledger_only',
                'known_entities': [dict(e, trust='committed' if e.get('state') == 'committed' else 'staged_uncommitted')
                                   for e in ledger.entities()[:64]],
                'approved_aliases': [a for a in ledger.aliases() if a['status'] == 'approved'][:64],
                'known_claims': ledger.claims()[:40],
                'external_memory_candidates': self.external_candidates[:12],
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
                         {'reason': 'native_entities_local_fallback', 'detail': 'Native name lookup is optional; local IDs remain authoritative.'}]

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
