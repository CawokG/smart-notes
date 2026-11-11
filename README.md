# Knowledge ingestion — reviewable V1 prototype

A Python CLI for selected UTF-8 notes: exact snapshots → structured extraction → conservative identity/claim reconciliation → immutable ChangeSet → review → confirmed publication. SQLite keeps identities, provenance and workflow state; pinned MuninnDB stores published readable memories.

This is a **reviewable prototype, not production V1 ready**. The real OpenAI-compatible adapter is implemented and tested against a controlled HTTP server. External LLM credentials are unconfigured; semantic precision/recall on real corpora are **not measured**. Pinned MuninnDB read/write and recovery boundaries were verified on a disposable Linux instance. See [validation results](docs/VALIDATION.md), [known limitations](KNOWN_LIMITATIONS.md), [architecture](ARCHITECTURE.md) and [target SPEC](SPEC.md).

## Clean installation

Python **3.12+**, Git and a writable local directory are required. Runtime uses only the Python standard library; installation downloads the setuptools build dependency when needed.

```sh
git clone https://github.com/CawokG/smart-notes.git smart-notes
cd smart-notes
python -m venv .venv
.venv/bin/python -m pip install .
.venv/bin/ki --help
```

The commands below use `python -m knowledge_ingestion.cli` from the checkout; after activating `.venv`, `ki` is equivalent. For an installed wheel, run `ki` from any directory. A fresh venv installation and the installed console script's entire fake CLI flow were checked from an unrelated working directory.

**Publication/review locking is verified on Linux only.** Windows lacks `fcntl`/the required lock primitives and receives `publication_lock_platform_unsupported`; macOS publication is untested. Scan/plan and the portable HTTP adapter do not require `fcntl`. The full offline suite currently targets Linux.

For the optional pinned MuninnDB binary, `python scripts/setup.py` downloads v0.9.0, verifies its platform-specific checksum, installs it in `.local/bin`, and installs the app in `.venv`. On Windows: `py -3 scripts/setup.py`. Linux/macOS x86-64/ARM64 and Windows x86-64 release artifacts are listed; only Linux x86-64 was executed here. GitHub releases and PyPI access are required. Installation does not create vault-bound observe/full credentials.

## Safe synthetic demonstration

```sh
python scripts/demo.py --output work/my-demo
```

Use a new/empty directory. The script copies only `fixtures/obsidian`, explicitly chooses FakeLLM and the durable fake backend, and runs scan → plan → review decision → commit → status/resume → identical reimport. It writes `report.json`, `report.md`, the original ChangeSet, decisions and per-step JSON artifacts. Expected: **5 files, 57/57 chunks, 2 new fake memories, 2→2 fake POSTs after repeat**, with unresolved/conflicting operations retained. No network or real credentials are used. FakeLLM is scripted by `(document_path, chunk_ordinal)`; unscripted chunks return empty proposals. This tests workflow, not extraction quality.

Checked-in examples: [machine report](examples/report.json), [human summary](examples/report.md), [full ChangeSet](examples/change-set.json). Paths/IDs in those examples belong to a synthetic run; generate fresh artifacts for your own review.

## Scan sources

```sh
python -m knowledge_ingestion.cli scan ./note.md --data ./data
python -m knowledge_ingestion.cli scan ./one/a.md ./two/a.md --data ./data
python -m knowledge_ingestion.cli scan ./vault --data ./data --max-bytes 5242880
```

Select one directory recursively, or one/more explicit files. Supported: UTF-8 `.md`/`.txt` including BOM. Hidden directory trees (`.git`, `.obsidian`, `.cache`, etc.), symbolic links/linked ancestors and the application data tree are excluded. Explicit paths through hidden directory ancestors are also skipped. Hidden filenames in an ordinary directory are allowed. Directory traversal ignores other extensions; unsupported explicitly selected files produce warnings. Links, frontmatter and Obsidian syntax remain uninterpreted text; no links, plugins or source commands are executed.

The **valid subset** is current membership. Missing, oversized, invalid UTF-8/binary and unreadable inputs are skipped with warnings. If none are usable, scan exits **1** with `no_usable_sources`, an empty document list and its warnings. A rescan updates membership even when all inputs become invalid. Documents/revisions/snapshots/evidence and committed memories remain as history; removed/ineligible committed sources produce `possibly_stale_source`, without deleting memory.

Scan and precommit use the **same eligibility policy and original max-bytes**. A skipped file may stay skipped (even if its invalid bytes change). A newly eligible input, an edited/disappeared valid file, or a valid file becoming ineligible makes an old plan `stale_source`. Changes only inside excluded trees do not invalidate it. Run sources from stable storage; hostile/concurrent filesystem edits are outside the atomicity guarantee.

IDs retain the stage1 `prefix_SHA256` encoding: collection identity uses the absolute directory URI or sorted unique explicit file selections; document identity uses collection + locator, revision identity uses document + byte hash. Missing/skipped explicit selections remain part of selection intent, so disappearance does not silently change that collection. Normal stage1 valid-selection IDs remain unchanged. Moving a source creates a new identity candidate. Equal content does not merge documents.

Chunks preserve exact half-open UTF-8 byte ranges, BOM included. Heading/paragraph chunking targets at most 9000 source bytes with bounded whole-segment overlap. This is a byte approximation, not a tokenizer/sentence guarantee. Snapshot files are content-addressed. Schema4 migrations preserve unversioned/stage1/schema2/schema3 data and IDs; historical collections require **rescan** to establish trustworthy current membership. Old plans stay stored but require replanning under current config/code versions.

## Plan: offline by default

Copy `collection_id` from scan JSON:

```sh
python -m knowledge_ingestion.cli plan --collection col_... --data ./data \
  --llm-provider fake --fixture ./fixtures/offline-extraction.json \
  --output ./data/change-set.json
```

The default provider is fake and requires a fixture; there is no automatic real-provider/fake-backend fallback. Plan validates the extraction schema and exact snapshot anchors before staging operations. Initial call + **at most two retries per incomplete chunk per invocation** apply to provider, schema and anchor failures. After exhaustion, the chunk has no completed checkpoint; the job is `needs_attention`, keeps a deferred operation and exits 1. Retrying/resuming is explicit; completed chunks are not resent.

Config pins provider/model/endpoint/env selector/timeout/wire format, schema+prompt versions and hashes, parser/rules/context versions, source policy, request/digest budgets and optional observe backend identity. Secrets are excluded. `--digest-bytes` accepts 512–16384 (default 4096). `--request-bytes` accepts 16384–262144 (default **65536**) and bounds the complete serialized HTTP JSON body, including prompts, schema, source, metadata, feedback and context. It is a byte budget, not a token/cost guarantee. Source text is never silently truncated; if it cannot fit, the chunk stays incomplete without an HTTP call.

Provider context is a shortlist: at most 64 entities, 64 approved aliases, 40 claims, 12 structured external candidates, 7 retrieved snippets and a bounded staged digest. Entries above 2048 serialized bytes are omitted whole; further tail entries are removed to fit the wire budget. Explicit metadata records original/omitted counts. Full local resolution/reconciliation still reads the entire committed ledger plus this job's staging. Numeric usage reported by the real API is retained; cost is null/unmeasured. Events retain request hashes/counts and retrieval IDs, not source/prompt/response bodies.

`approved` in the immutable plan is a local policy result. It does not publish or replace explicit commit confirmation. Negative polarity and known ISO time are preserved; unresolved identity, relative time, nonasserted modalities and conflicts remain deferred. Simple lexical rules/exact anchors do not establish semantic entailment in arbitrary languages.

## Configure a real LLM explicitly

Use environment/settings to provision the secret; never pass the key value as a CLI argument or put it in Git. [config.example.json](config.example.json) documents option names/defaults and environment variable **names only**; the CLI does not implicitly load that file.

```sh
# KNOWLEDGE_LLM_API_KEY is already set securely in this process environment.
python -m knowledge_ingestion.cli plan --collection col_... --data ./data \
  --llm-provider openai-compatible \
  --llm-base-url https://api.openai.com/v1 --llm-model <chosen-model> \
  --llm-api-key-env KNOWLEDGE_LLM_API_KEY --llm-timeout 30 \
  --request-bytes 65536 --output ./data/change-set.json
```

The endpoint must implement `POST <base-url>/chat/completions`, `response_format: {"type":"json_object"}`, `temperature:0`, nonstreaming choices/message/content JSON and `finish_reason:stop`. The supplied JSON Schema is included in the system messages and enforced locally. No tools/functions or write APIs are supplied. Some models/endpoints do not support these options; refusal/truncation/malformed envelopes and HTTP/transport errors remain incomplete, with fixed error codes. There are no SDK/internal retries or redirect forwarding.

An explicitly local loopback endpoint may use HTTP and no API key:

```sh
python -m knowledge_ingestion.cli plan --collection col_... --data ./data \
  --llm-provider openai-compatible --llm-local \
  --llm-base-url http://127.0.0.1:8000/v1 --llm-model <local-model> \
  --output ./data/change-set.json
```

`--llm-local` accepts loopback hosts only. Remote endpoints require HTTPS and a configured env secret. A local server can itself forward data externally; its locality/privacy must be established separately. No real external LLM run is claimed by this repository's current validation.

**Data sent to the selected provider:** exact current chunk text; scoped document locator/file URI, headings, IDs and ordinal; versioned extraction prompt/schema; bounded committed entities/aliases/claims; this job's staged digest/context; optional retrieved Muninn snippets/IDs; fixed validation feedback on retries. A provider can therefore see selected source text, local paths and existing-memory context. It receives no backend tokens, source execution capability, tools or direct database write rights. The LLM API secret appears only in the HTTP Authorization header to the selected endpoint. Reports/ChangeSets intentionally retain validated evidence quotes; keep those and SQLite/snapshots private. Storage is plaintext and unencrypted. There is no content-secret detection/redaction of text you explicitly choose to send.

Optional **read-only backend context** is independent of LLM selection:

```sh
python -m knowledge_ingestion.cli plan --collection col_... --data ./data \
  --llm-provider fake --fixture ./fixtures/offline-extraction.json \
  --context muninn-observe --muninn-url http://127.0.0.1:8475
```

This requires `MUNINN_OBSERVE_TOKEN` (or `--observe-token-env NAME`). It performs bounded health/GET/ACTIVATE with that key and an explicit local-ledger fallback. Full-key reads are not used. Retrieved snippets remain untrusted possible matches. They are **not** translated into a complete structured semantic legacy conflict/dedup gate; semantic legacy reconciliation is partial. Native name aggregates never become canonical local identities. Observe exposed counters were unchanged in a limited live window, but hidden cognitive feedback remains unmeasured.

## Review, commit, status and resume

Keep the original ChangeSet intact. `--change-set` accepts its persisted `chg_...` ID or an exactly matching original JSON file.

```sh
python -m knowledge_ingestion.cli review --change-set ./data/change-set.json --data ./data
python -m knowledge_ingestion.cli review --change-set ./data/change-set.json --data ./data \
  --decisions ./review-decisions.json --actor local-user
```

A batch of decisions is append-only history with actor/rationale/UTC timestamp. Reject/defer dependent operations together. Unsafe defer/conflict operations cannot become publishable by approving a flag; resolve the source/identity and replan. Decisions freeze after commit begins.

```json
{"version":"1.0","decisions":[
  {"operation_id":"op_...","decision":"approved","rationale":"Checked the exact source quote"}
]}
```

For offline publication, select the fake backend **explicitly**:

```sh
python -m knowledge_ingestion.cli commit --change-set ./data/change-set.json --data ./data \
  --backend fake --fake-store ./work/FAKE-backend.sqlite3 --actor local-user --yes
python -m knowledge_ingestion.cli status --job job_... --data ./data
python -m knowledge_ingestion.cli resume --job job_... --data ./data \
  --backend fake --fake-store ./work/FAKE-backend.sqlite3
```

For real publication, provision **separate vault-bound observe/full keys** in `MUNINN_OBSERVE_TOKEN` / `MUNINN_FULL_TOKEN`; select alternate env names with `--observe-token-env` / `--full-token-env`. The plan's `--vault` must match both keys. No admin cookies are used. Remote Muninn requires HTTPS; HTTP is loopback-only.

```sh
python -m knowledge_ingestion.cli commit --change-set ./data/change-set.json --data ./data \
  --backend muninn --muninn-url http://127.0.0.1:8475 --actor local-user --yes
python -m knowledge_ingestion.cli resume --job job_... --data ./data \
  --backend muninn --muninn-url http://127.0.0.1:8475
```

Without `--yes`, a terminal asks for literal `yes`. Noninteractive commit refuses before preparing a session/backend. `resume` continues an already confirmed commit with the original backend identity. Interrupted planning requires the original provider options/fixture and optional observe options:

```sh
python -m knowledge_ingestion.cli resume --job job_... --data ./data \
  --llm-provider fake --fixture ./fixtures/offline-extraction.json
# Real planning: repeat original --llm-provider/--llm-base-url/--llm-model/
# --llm-api-key-env/--llm-local/--llm-timeout and --context/--muninn-url if used.
```

Resume reuses the original request/digest budgets and requires matching model/provider/context/config hashes, source membership, originals, snapshots and committed revision. `--expected-config-hash` pins the value from status. Changed inputs/config require a new scan/plan; source changes during partial commit may require operator reconciliation. `status` lists every unapplied/rejected/deferred operation, outbox attempts/known IDs, session/confirmation, review history and a bounded redacted event timeline. Plan/status JSON can be saved by shell redirection; there is no separate export command or background sync.

Publication durably prepares an outbox operation before POST, records attempts/known IDs, verifies exact vault-bound read-back, then atomically applies local projections. Repeat commit/reimport does not create another engram. Alias/evidence reinforcement is local-only. Previous engrams are never updated, merged, evolved or deleted. `complete_with_deferred` retains unapplied decisions visibly.

**Pinned MuninnDB has no exhaustive marker lookup across lifecycle states.** Unknown POST outcomes remain `needs_manual_repair`; resume never blindly repeats that POST. A trustworthy ID obtained independently can be verified:

```sh
python -m knowledge_ingestion.cli resume --job job_... --data ./data \
  --backend muninn --muninn-url http://127.0.0.1:8475 \
  --repair-operation op_... --engram-id <known-ULID> \
  --actor local-operator --rationale 'Inspected the original operation record'
```

This verifies vault/ID/concept/content/full tag set/marker/confidence; it does not discover a lost ID. Known 4xx refusals are also terminal/manual, with no automatic retry or CLI abandon action. An unresolved attempted outbox blocks new jobs in that vault. Guarantees require **one authoritative ledger per backend/vault**, plus the same-OS-account vault lock. Cross-ledger/host/account coordination is not provided. The fake backend's exhaustive-marker recovery is solely a test guarantee.

To start an optional local MuninnDB after setup: `MUNINN_LOCAL_EMBED=0 .local/bin/muninn --daemon --data ./data/muninn`. It runs in the foreground. Default bootstrap credentials/public vault need separate administration before any real use. Embeddings/enrichment are disabled by this prototype setup; semantic retrieval with a real embedder is unverified. The synthetic smoke scripts perform their own temporary bootstrap and require no existing server.

## Validation

```sh
python -m unittest discover -s tests -v
python -m compileall -q knowledge_ingestion scripts tests
git diff --check
python scripts/smoke_muninn.py --binary work/muninn-spike/muninn-linux-amd64 \
  --output work/capability-report.json
# Run after the preceding smoke has exited; do not run product smokes concurrently.
python scripts/smoke_commit.py --binary work/muninn-spike/muninn-linux-amd64 \
  --output work/product-report.json
```

Live scripts accept no existing URL/store. They checksum the pinned binary, start only their own fresh loopback child with synthetic storage/random transient credentials, disable embeddings/enrichment and remove temporary storage after stopping it. They refuse ambient `~/.muninn/muninn.env` (including symlinks), rather than modifying the user's settings. The capability harness archives/deletes only its newly created synthetic fixtures; the product publisher exposes no destructive operations. See [all 15 scenarios, gates and DoD results](docs/VALIDATION.md) and [pinned API evidence](integrations/muninn-compatibility.md).
