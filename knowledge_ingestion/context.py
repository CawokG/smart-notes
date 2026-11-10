"""Offline capability fallback: committed ledger plus this job's staged overlay."""
from .contracts import VERSION


class OfflineContextProvider:
    def __init__(self, external_candidates=()):
        self.external_candidates = list(external_candidates)

    def bundle(self, ledger, digest_budget):
        return {'version': VERSION, 'capability_fallback': 'offline_ledger_only',
                'known_entities': [dict(e, trust='committed' if e.get('state') == 'committed' else 'staged_uncommitted')
                                   for e in ledger.entities()[:64]],
                'approved_aliases': [a for a in ledger.aliases() if a['status'] == 'approved'][:64],
                'known_claims': ledger.claims()[:40],
                'external_memory_candidates': self.external_candidates[:12],
                'retrieval_trace': [], 'muninn_read_calls': 0, 'muninn_write_calls': 0,
                'staged_digest': ledger.digest(digest_budget)}
