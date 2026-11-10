"""Explicitly fake, durable offline backend for workflow/recovery exercises.

This is not MuninnDB and supplies exhaustive-marker guarantees Muninn lacks.
Never select it implicitly when a live backend is unavailable.
"""
import json
import sqlite3
from pathlib import Path

from .contracts import canonical_json, content_hash
from .muninn import BackendError, UnsupportedOperation
from .storage import contains_symlink


class FakeBackend:
    def __init__(self, path, vault='personal', exhaustive=True):
        self.path = Path(path).absolute()
        if contains_symlink(self.path) or self.path.name == 'ledger.sqlite3':
            raise ValueError('invalid_fake_backend_path')
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.vault, self.exhaustive = vault, exhaustive
        self.identity = 'FAKE:' + content_hash({'path': str(self.path), 'vault': vault, 'exhaustive': exhaustive})
        self.capabilities = {'exact_exhaustive_marker_lookup': exhaustive, 'observe_get': True,
                             'observe_activate': False, 'native_entity_candidates': False}
        self.fail = None
        with self._db() as db:
            app_id = db.execute('PRAGMA application_id').fetchone()[0]
            tables = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if app_id != 1263097420 and (app_id or tables):
                raise ValueError('not_a_fake_backend_store')
            db.execute('PRAGMA application_id=1263097420')
            db.execute('CREATE TABLE IF NOT EXISTS memories(id TEXT PRIMARY KEY,vault TEXT NOT NULL,payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS calls(seq INTEGER PRIMARY KEY,kind TEXT NOT NULL)')

    def _db(self):
        return sqlite3.connect(self.path)

    def _vault(self, vault):
        if vault != self.vault:
            raise ValueError('adapter_vault_mismatch')

    def health(self):
        return {'version': 'FAKE-offline-1', 'capabilities': self.capabilities}

    @property
    def post_count(self):
        with self._db() as db:
            return db.execute("SELECT COUNT(*) FROM calls WHERE kind='POST'").fetchone()[0]

    def create_engram(self, vault, concept, content, tags, confidence):
        self._vault(vault)
        with self._db() as db:
            db.execute("INSERT INTO calls(kind) VALUES ('POST')")
            if self.fail == '4xx':
                error = BackendError('backend_http_403', 403)
            else:
                number = db.execute('SELECT COUNT(*) FROM memories').fetchone()[0] + 1
                engram_id = 'fake_' + str(number)
                payload = {'id': engram_id, 'vault': vault, 'concept': concept,
                           'content': content, 'tags': tags, 'confidence': confidence}
                db.execute('INSERT INTO memories VALUES (?,?,?)', (engram_id, vault, canonical_json(payload)))
                error = BackendError('unknown_write_outcome', unknown=True) if self.fail == 'response_lost' else None
        if error:
            raise error
        return engram_id

    def get_engram(self, vault, engram_id):
        self._vault(vault)
        if self.fail == 'readback':
            raise BackendError('backend_read_failed')
        with self._db() as db:
            row = db.execute('SELECT payload FROM memories WHERE id=? AND vault=?', (engram_id, vault)).fetchone()
        return json.loads(row[0]) if row else None

    def list_by_marker(self, vault, marker):
        self._vault(vault)
        if not self.exhaustive:
            raise UnsupportedOperation('FAKE_marker_lookup_disabled')
        with self._db() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT payload FROM memories WHERE vault=?', (vault,))]
        return [r for r in rows if marker in r['tags']]
