"""Conservative pinned REST adapter. Only create_engram publishes.

No admin cookies, native mutations, automatic POST retry, or active-list recovery.
HTTP exceptions contain fixed codes only: backend bodies/credentials stay private.
"""
import json
import http.client
import re
import urllib.error
import urllib.request
from urllib.parse import urlencode, urlsplit

from .contracts import canonical_json, content_hash

PINNED_VERSION = 'v0.9.0'


class UnsupportedOperation(RuntimeError):
    pass


class BackendError(RuntimeError):
    def __init__(self, code, status=None, unknown=False):
        self.code, self.status, self.unknown = code, status, unknown
        super().__init__(code)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class MuninnAdapter:
    capabilities = {'exact_exhaustive_marker_lookup': False, 'observe_get': True,
                    'observe_activate': True, 'native_entity_candidates': False}

    def __init__(self, base_url, vault, observe_token, write_token=None, timeout=10):
        parsed = urlsplit(base_url)
        if (parsed.scheme not in {'http', 'https'} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment or parsed.path not in {'', '/'}):
            raise ValueError('invalid_muninn_base_url')
        if parsed.scheme == 'http' and parsed.hostname not in {'127.0.0.1', 'localhost', '::1'}:
            raise ValueError('remote_muninn_requires_https')
        if not isinstance(vault, str) or not vault.strip() or len(vault) > 100:
            raise ValueError('invalid_vault')
        for token in (observe_token, write_token):
            if token is not None and (not isinstance(token, str) or not token or any(c.isspace() for c in token)):
                raise ValueError('invalid_muninn_credential')
        if not observe_token or write_token == observe_token:
            raise ValueError('separate_observe_and_full_credentials_required')
        if not 0 < timeout <= 30:
            raise ValueError('invalid_backend_timeout')
        self.base_url, self.vault = base_url.rstrip('/'), vault
        self._observe_token, self._write_token = observe_token, write_token
        self.timeout = timeout
        self.read_calls, self.write_calls = 0, 0
        # No CookieProcessor, credential-bearing redirects, ambient cookie jar.
        self._client = urllib.request.build_opener(NoRedirect())
        self.identity = 'muninn:' + content_hash({'url': self.base_url, 'vault': vault, 'version': PINNED_VERSION})

    def _vault(self, vault):
        if vault != self.vault:
            raise ValueError('adapter_vault_mismatch')

    def _request(self, method, route, payload=None, write=False):
        token = self._write_token if write else self._observe_token
        if not token:
            raise BackendError('full_credential_required')
        headers = {'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'}
        request = urllib.request.Request(self.base_url + route,
            data=canonical_json(payload).encode('utf-8') if payload is not None else None,
            headers=headers, method=method)
        if write:
            self.write_calls += 1
        else:
            self.read_calls += 1
        try:
            with self._client.open(request, timeout=self.timeout) as response:
                status = response.status
                raw = response.read(2 * 1024 * 1024 + 1)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError('oversized')
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError('not_object')
            return status, value
        except urllib.error.HTTPError as error:
            # 4xx is a known refusal, but remains terminal for this operation.
            raise BackendError('backend_http_' + str(error.code), error.code,
                               unknown=write and (error.code >= 500 or error.code < 400 or error.code == 408)) from None
        except (OSError, ValueError, TimeoutError, http.client.HTTPException):
            raise BackendError('unknown_write_outcome' if write else 'backend_read_failed',
                               unknown=write) from None

    def health(self):
        status, value = self._request('GET', '/api/health')
        if status != 200 or value.get('status') != 'ok' or value.get('version') != PINNED_VERSION:
            raise BackendError('unsupported_backend_version_or_health')
        # A bounded empty list request validates observe credential/vault binding.
        status, listing = self._request('GET', '/api/engrams?' + urlencode({'vault': self.vault, 'limit': 1}))
        if status != 200 or not isinstance(listing.get('engrams'), list):
            raise BackendError('observe_auth_or_shape_failed')
        return {'version': PINNED_VERSION, 'capabilities': dict(self.capabilities)}

    def get_engram(self, vault, engram_id):
        self._vault(vault)
        if not isinstance(engram_id, str) or not re.fullmatch(r'[0-9A-HJKMNP-TV-Z]{26}', engram_id):
            raise ValueError('invalid_engram_id')
        try:
            status, value = self._request('GET', '/api/engrams/' + engram_id + '?' + urlencode({'vault': vault}))
        except BackendError as error:
            if error.status == 404:
                return None
            raise
        if status != 200 or value.get('id') != engram_id or value.get('vault', vault) != vault:
            raise BackendError('invalid_engram_readback')
        # Pinned get omits vault; server-enforced query+vault-bound bearer is binding.
        return {**value, 'vault': vault}

    def create_engram(self, vault, concept, content, tags, confidence):
        self._vault(vault)
        status, value = self._request('POST', '/api/engrams?' + urlencode({'vault': vault}),
            {'vault': vault, 'concept': concept, 'content': content, 'tags': tags, 'confidence': confidence}, write=True)
        engram_id = value.get('id')
        if status != 201 or not isinstance(engram_id, str) or not re.fullmatch(r'[0-9A-HJKMNP-TV-Z]{26}', engram_id):
            raise BackendError('unknown_write_outcome', unknown=True)
        return engram_id

    def list_by_marker(self, vault, marker):
        self._vault(vault)
        raise UnsupportedOperation('pinned_marker_lookup_not_exhaustive')

    def activate_observe(self, vault, contexts, limit=5):
        self._vault(vault)
        if (not isinstance(contexts, list) or not 1 <= len(contexts) <= 3
                or any(not isinstance(s, str) or not s.strip() or len(s) > 240 for s in contexts)
                or type(limit) is not int or not 1 <= limit <= 12):
            raise ValueError('invalid_bounded_activation')
        status, value = self._request('POST', '/api/activate?' + urlencode({'vault': vault}),
            {'vault': vault, 'context': contexts, 'max_results': limit, 'threshold': 0.0, 'disable_hops': True})
        hits = value.get('activations')
        if status != 200 or not isinstance(hits, list) or len(hits) > limit:
            raise BackendError('invalid_activation_response')
        # Bound untrusted response fields before they can enter a prompt.
        return [{'id': str(h.get('id', ''))[:100], 'concept': str(h.get('concept', ''))[:240],
                 'content': str(h.get('content', ''))[:1800], 'trust': 'external_possible_match'}
                for h in hits if isinstance(h, dict)]

    def entity_candidates(self, vault, query):
        self._vault(vault)
        raise UnsupportedOperation('native_entities_optional_use_local_ledger')
