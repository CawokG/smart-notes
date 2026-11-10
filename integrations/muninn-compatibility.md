# MuninnDB v0.9.0 capability spike

Checked **2026-11-10**, Linux x86-64, against the installed release binary:

- Release: `https://github.com/scrypster/muninndb/releases/download/v0.9.0/muninn-linux-amd64`
- `--version` and REST health version: **`v0.9.0`**.
- Binary SHA-256: **`4da4c959a6482c7608809a75501cd3c746875c1dcd9c7caf7e6d3052dbdefa5b`**, checked before execution; agrees with `scripts/setup.py`.
- Source research used **tag `v0.9.0`**, peeled commit `3eed40c928450b93a237a1b15f847363ac6e3a0c` (annotated tag object `d50b719a58c77511c1ca4646a1346590e893626f`). Source observations below are explicitly distinguished from binary smoke results. No `develop` documentation is used as verification.

**Result:** read/write and optional candidate lookup are verified. Exact marker lookup works for the quiescent active fixture, but **exhaustive marker recovery across lifecycle states is UNSUPPORTED by the tested observe/full API**. An unknown POST outcome must become **`needs_manual_repair`**; absence from the active list never permits another POST.

## Reproduce

Python 3.12+ standard library only; no dependency installation, Docker, LLM, model download or remote embedding service:

```sh
# Downloads the pinned binary into ignored work/ if absent, verifies its checksum:
python scripts/smoke_muninn.py

# Or reuse an already downloaded binary (it is still checksum-checked):
python scripts/smoke_muninn.py --binary work/muninn-spike/muninn-linux-amd64 \
  --output work/muninn-spike/report.json

# Offline safety checks and the existing application tests:
python -m unittest discover -s tests -v
git diff --check
python -m compileall -q knowledge_ingestion scripts tests
```

Executor run: **530 HTTP calls**, approximately 11 seconds, `PASS_with_documented_limitations`; an independent supervisor replay also passed. Exit 0 means that the characterization assertions passed, including expected unsupported behavior; it does **not** mean every capability is supported or that V1 is complete. Other platforms are untested. Download/startup/transport/assertion failure exits nonzero and leaves unverified capabilities unverified.

The script accepts neither an existing server URL nor an existing data directory. It creates fresh temporary storage under ignored `work/muninn-spike/`, uses random free loopback ports for REST/MCP/UI/MBP/gRPC and a foreground `--daemon` child, confirms that child's bind logs, and stops only that process in `finally`. Its archive/delete operations apply only to the synthetic fixtures it just created. Credentials and database files are transient; the child stops and the temporary tree is removed. The JSON report retains response **shapes**, synthetic examples, checks and a redacted log tail.

All inherited `MUNINN*` variables are removed from the child environment. It sets `MUNINN_LOCAL_EMBED=0`, empty `MUNINN_ENRICH_URL`, a fresh `MUNINNDB_DATA`, and random `MUNINN_ADMIN_PASSWORD` / `MUNINN_MCP_TOKEN`. Startup logs confirm no embedder/enricher, and `GET /api/admin/plugins` returns `[]`. v0.9.0 automatically reads `~/.muninn/muninn.env`; the script **refuses to run if that file or a symlink exists**. Use a clean OS account/container in that case; it does not rewrite HOME or user configuration. An explicit MCP token prevents reuse of `~/.muninn/mcp.token`.

## Installed binary results

| Capability | Result | Evidence / limit |
|---|---|---|
| Startup / health | PASS | `GET /mcp/health` → `200 {"status":"ok"}`; `GET /api/health` → `200 {"db_writable":true,"status":"ok","uptime_seconds":0,"version":"v0.9.0"}`. Uptime varies. |
| Admin bootstrap / private vault / API keys | PASS | UI login issues session cookie; REST admin calls create two private synthetic vaults and separate vault-bound `observe` / `full` keys. |
| Vault authentication | PASS | Private vault without token or with invalid token → 401; query vault different from key vault → 401 `VAULT_KEY_MISMATCH`; conflicting body vault on POST → 400. GET without a vault uses the key's vault. |
| Create / exact get by ID / read-back | PASS | Full POST → 201 with ULID; observe GET → 200 with exact concept, content, tags and confidence; unknown valid ID → 404. |
| Observe write denial | PASS | REST POST → 403; no row is created. MCP `muninn_remember` → HTTP 200 with JSON-RPC error `-32001`. |
| Observe GET / ACTIVATE counters | PASS, bounded observation only | Three explicit GETs and three ACTIVATEs returning the fixture, plus two seconds of observe GET polling: `access_count 0→0`, `last_access` unchanged. Full-key control also gives `0→0`; full/observe contrast is **not** established by that counter. |
| Counter exposure positive control | PASS | A deliberately acknowledged duplicate-content POST returns the same ID, and `access_count` becomes 1. This is not a transport retry or a guarantee of idempotent publication. |
| ACTIVATE without embeddings | PASS, lexical fixture only | Returns the expected synthetic hit. Observe response query IDs are `q-...`; full response query IDs are ULIDs. Semantic embedding search is disabled and unverified. |
| Exact tag filter on active fixture | PASS within tested scope | All **205/205** known marker IDs; prefix/suffix, uppercase and content-only decoys excluded; another vault's marker excluded. Two tags use AND: 204 match both `ki_v1` and marker. |
| Offset pagination / list cap | PASS on quiescent fixture | Pages at offsets `0,200,205` return `200,5,0`, stable `total=205`; page size 7 also covers every ID (31 calls including terminal empty page). Requested limit 999 is clamped to 200. |
| Archived marker | Partial | Default list omits it; explicit `state=archived` finds the one archived fixture. |
| Soft-deleted marker / completeness | UNSUPPORTED | Of **101** known tombstones, `/api/deleted?limit=9999` returns only 100; `count=100` is returned count. Offset 100 returns the same IDs. `state=soft_deleted` on `/api/engrams` is silently ignored and returns the 205 active marker rows. The omitted tombstone is readable by known ID with `state=127` and its exact tag. |
| General exhaustive `list_by_marker` | **UNSUPPORTED** | Default list is incomplete across lifecycle states; deleted listing has no usable pagination/complete total. No snapshot token/concurrent pagination guarantee established. Recovery flag must be false. |
| MCP native entity tools | PASS, optional shortlist | `muninn_entity`, `muninn_find_by_entity`, `muninn_similar_entities` advertised and invoked successfully using observe bearer auth. Details below. |
| Native identity separation | Not provided by name lookup | Two synthetic memories explicitly representing different people named Алексей appear in a single native aggregate (`mention_count=2`, two engram IDs). Preserve separate local identities. |
| `idempotent_id` / MCP `op_id` receipts | UNVERIFIED | Fields exist in pinned source/schema; receipt persistence, crash behavior and payload binding were not smoke-tested. Do not use them to relax recovery. |
| Product adapter / outbox / publication / crash recovery | NOT IMPLEMENTED | These remain stage 4 work. This script is an isolated characterization harness. |

## Auth, endpoints and actual shapes for the next adapter

Use independent credentials for observation and publication. Tokens never go to the LLM or logs. A vault key is scoped by the backend; specify the same vault consistently and fail on mismatch. Use a fresh request/client context without an admin cookie in read-only application code: admin session cookies can bypass vault-key restrictions (source observation, not an intended adapter auth path).

Bootstrap is needed **only for this disposable instance**:

1. UI port: `POST /api/auth/login` with `{"username":"root","password":"<MUNINN_ADMIN_PASSWORD>"}` → `200 {"status":"ok"}` and `Set-Cookie: muninn_session=...`.
2. REST port with that cookie: `PUT /api/admin/vaults/config` with `{"name":"ki-spike","public":false}` → 200 with that config.
3. REST port with cookie: `POST /api/admin/keys` with `{"vault":"ki-spike","label":"synthetic-spike-observe","mode":"observe"}` (separately `full`) → `201 {"token":"<shown once>","key":{"id":...,"vault":"ki-spike","label":...,"mode":"observe","created_at":...,"storage_hash":null}}`.

Application REST calls use **`Authorization: Bearer <vault key>`**, without the admin cookie:

```text
POST /api/engrams?vault=ki-spike             # full key only
{"vault":"ki-spike","concept":"Synthetic claim",
 "content":"Synthetic readable claim with provenance reference.",
 "tags":["ki_v1","ki_op:<stable operation marker>"],"confidence":0.75}
→ 201 {"id":"<26-character ULID>","created_at":<integer>}

GET /api/engrams/<id>?vault=ki-spike         # observe key
→ 200 {"id":...,"concept":...,"content":...,"confidence":0.75,
       "relevance":0,"stability":30,"access_count":0,
       "tags":[...],"state":1,"created_at":...,"updated_at":...,
       "last_access":...,"memory_type":0,"trust":2,"entities":[...]}

POST /api/activate?vault=ki-spike            # observe key
{"vault":"ki-spike","context":["Synthetic quasar 000"],
 "max_results":5,"threshold":0.0,"disable_hops":true}
→ 200 {"query_id":...,"total_found":1,
       "activations":[{"id":...,"concept":...,"content":...,"score":...,
                       "confidence":...,"score_components":{...},...}],
       "latency_ms":...,"brief":[...]}

GET /api/engrams?vault=ki-spike&tags=ki_op%3Astage3-exact&sort=created&limit=200&offset=0
→ 200 {"engrams":[{"id":...,"concept":...,"content":...,"confidence":...,
                      "tags":[...],"vault":"ki-spike","created_at":...}],
       "total":205,"limit":200,"offset":0}
```

Observed `created_at` on create/get and `last_access`/`updated_at` on get are epoch **nanoseconds**; list `created_at` is epoch **seconds**. Do not assume consistent units across these endpoints. Optional fields can be omitted. The create payload above is verified; arbitrary relation types and extra metadata are not part of the tested minimum.

Tags are case-sensitive complete strings. List query `tags` is a comma-separated AND list; URL-encode the value and use marker syntax without commas (the tested colon/hyphen form is accepted). Read all pages, verify metadata, unique IDs, exact tags and stable total, and check a terminal empty page. `collect_tag_pages` rejects truncated, repeated, changing or ignored-filter pages. These checks do **not** solve missing lifecycle states or concurrent writers. The active fixture result is a possible positive match, never a general proof of absence.

MCP uses the separate MCP port, the same observe bearer token and `POST /mcp` with ordinary JSON-RPC (no session initialization required for these unary calls):

```json
{"jsonrpc":"2.0","id":1,"method":"tools/call",
 "params":{"name":"muninn_entity",
           "arguments":{"vault":"ki-spike","name":"Алексей","limit":20}}}
```

Success is `{"jsonrpc":"2.0","id":1,"result":{"content":[{"type":"text","text":"<JSON string>"}]}}`; parse the inner JSON. HTTP 200 alone does not establish success: check `error` and `result.isError`. Mismatched vault → JSON-RPC error `-32602`; observe write → `-32001`.

| Tool / tested arguments | Parsed response |
|---|---|
| `muninn_entity {vault,name,limit:20}` | `{name,type,confidence,state,mention_count,first_seen,updated_at,engrams:[{id,concept,created_at}],relationships:[...],co_occurring:[...]}`; both Алексей memories returned. |
| `muninn_find_by_entity {vault,entity_name,limit:20}` | `{count:2,engrams:[{id,concept,state,type}],entity:"Алексей",matched_entity:"Алексей"}`. Exact-name branch verified; fuzzy fallback branch unverified. |
| `muninn_similar_entities {vault,threshold:0.5,top_n:20}` | `{count:1,similar:[{entity_a:"Quasar Engine",entity_b:"Quasar Engines",similarity:0.9565217391304348}]}`. This produces pairs within a vault, not a canonical identity lookup or a query-string search. |

These fixtures supplied `entities:[{name,type}]` on REST writes; no automatic NLP extraction was tested. Same-name native aggregation does not authorize merging distinct local `Entity` IDs. Global name-key behavior across different vaults is indicated by pinned source but was not separately smoke-tested.

## Safe stage 4 contract and limitations

- Enable the verified health/create/get, bounded observe ACTIVATE and optional MCP candidate capabilities for this exact release/checksum. Candidate failures can fall back to the local identity ledger / empty native shortlist with an explicit capability status.
- **`list_by_marker` must raise explicit `UnsupportedOperation` for exhaustive recovery** on this tested contract. Do not represent an active-only list or the capped deleted list as complete. Admin exports, embedded SDK access and receipt crash guarantees are outside this test and remain unverified.
- After an unknown POST outcome, or an exhausted bounded read-back failure without a trustworthy known-ID match, retain the outbox operation and use **`needs_manual_repair`**. Do not issue another create POST. Even a known content-duplicate POST can change counters; it is not a substitute for reconciliation.
- Verify known-ID read-back against the expected vault, concept, content and exact marker before recording application success. Use a single writer, vault lock and transactional outbox; this spike implements none of those product services.
- Preserve the local identity/alias/evidence ledger. Do not call native merge/evolve/enrich/delete in the product path; archive/delete here are disposable fixture controls only.

The smoke verifies unchanged **exposed access counters during a two-second window**, with real hits and a working counter positive control. It does not prove complete cognitive immutability. Pinned-source-only caution: `internal/engine/engine.go` `Read` fires `scoring.RecordFeedback` without an observe guard, while `Activate` guards several write paths; hidden scoring feedback is not measured by `access_count`. Use the observe key, document this limitation, and keep bounded context calls. Do not label all GET side effects as absent.

The offline planner remains network-free and still performs zero backend writes. Real adapter wiring, review/commit/status/resume, locks/outbox, stale-plan validation, recovery tests and a real LLM remain unfinished. This stage establishes API facts and a conservative recovery boundary, not production integration readiness.
