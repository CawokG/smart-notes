# Knowledge ingestion — offline planning prototype

This is an incomplete V1 implementation. It provides deterministic snapshots and a local, scripted FakeLLM planning pipeline. It does not connect to MuninnDB or publish knowledge.

The scanner accepts one or more explicitly selected UTF-8 `.txt`/`.md` files, or one directory recursively. It stores exact snapshots and produces deterministic byte ranges for heading/paragraph chunks. `.obsidian`, the data directory and symbolic links are excluded; links and frontmatter remain uninterpreted source text. See [SPEC.md](SPEC.md), [the next tasks](docs/NEXT_TASKS.md) and [the stage handoff](docs/AGENT_HANDOFF.md).

Python 3.12+ examples:

```sh
python -m knowledge_ingestion.cli scan ./note.md --data ./data
python -m knowledge_ingestion.cli scan ./one/a.md ./two/a.md --data ./data
python -m knowledge_ingestion.cli scan ./vault --data ./data --max-bytes 5242880
python -m knowledge_ingestion.cli scan ./fixtures/obsidian --data ./data
# Use collection_id from the scan JSON:
python -m knowledge_ingestion.cli plan --collection col_... --data ./data \
  --llm-provider fake --fixture ./fixtures/offline-extraction.json \
  --output ./data/change-set.json
python -m unittest discover -s tests -v
```

The JSON report includes warnings, collection/document/revision IDs and chunk byte ranges. IDs use SHA-256 over version-independent JSON-encoded identity components: a directory collection uses its absolute file URI, a selected-files collection uses the sorted unique set of absolute file URIs; document identity uses the collection and relative path or file URI, and revision identity uses document ID and the SHA-256 of the source bytes. Editing a file creates a revision; moving it creates a new document identity. Content hashes alone do not merge documents. A BOM is retained in the snapshot and byte offsets; chunks use half-open byte ranges and decode as UTF-8. Chunk sizes and overlap are byte-based approximations, not tokenizer guarantees. Links are never followed as a result of their contents.

SQLite `user_version=2` migrates the stage1 scan tables transactionally without changing their rows or IDs. An existing unversioned `revisions` table and its snapshot files remain intact alongside the new tables. Existing legacy rows are retained as history, not automatically associated with new collections, because their original collection intent cannot be inferred. Rescan sources to index them in the new schema. A newer unsupported schema is rejected. Snapshots may contain private information and are stored unencrypted as exact bytes; avoid scanning a directory while it is being modified by an untrusted process.

`plan` processes chunks sequentially, validates a versioned extraction schema and anchors quotes against the scanned snapshot bytes. It records a job, checkpoints, proposed identities/aliases/claims and a deterministic JSON ChangeSet with operation dependencies, decisions, reason codes and provenance. Committed indexes and other jobs' staging remain isolated. Repeating a completed plan returns the same artifact; repeating an interrupted plan with the same configuration resumes from successful chunk checkpoints. Invalid schema/anchors or provider errors receive at most two retries after the initial call, then produce `needs_attention`, a deferred operation and exit code 1. The failed chunk is not completed.

The fake provider returns proposals explicitly scripted by `(document_path, chunk_ordinal)` in the fixture; other chunks return empty proposals. It does not understand arbitrary notes. The supplied corpus demonstrates direct aliases, cross-file evidence, two distinct people named Алексей, uncertainty, time and conflicting statuses. Its long note tests chunk sequencing and bounded digest, not extraction recall. The fixture's frontmatter aliases and links do not establish identities or relationships. The pipeline makes no network calls and sends no data to an external provider. A future real provider will require explicit configuration and disclosure of transmitted source/context data.

The planner retains polarity, modality and ISO time qualifiers; unresolved relative time, questions, speculation, reported speech, uncertain identities and conflicts are deferred. Exact names and document proximity do not prove that people/accounts are the same; fuzzy matches are shortlists for review. Unknown custom entity types retain `type_label` under `other`. Existing importer claims can be deduplicated or reinforced in a proposed ledger operation, while external snippets remain possible matches. `approved` in a plan is a local policy result, not publication or user approval. `review`, `commit`, `status`, the full `resume` command, outbox recovery, a real LLM and live MuninnDB integration are not implemented.

## Install MuninnDB and the application

Install Python 3.12 or newer and Git, clone this repository, then run `python scripts/setup.py` (on Windows, `py -3 scripts/setup.py`). The script downloads the pinned upstream MuninnDB v0.9.0 binary, checks its SHA-256 against pinned release checksums, and installs it in `.local/bin`. It creates `.venv` and installs the application there. Internet access to GitHub releases and PyPI is required. Repeat the command to refresh the application; a matching binary is reused. Supported release binaries are Linux and macOS on x86-64/ARM64 and Windows x86-64; only Linux x86-64 has been tested here. Docker is not required.

To start a local server on Linux/macOS, run `MUNINN_LOCAL_EMBED=0 .local/bin/muninn --daemon --data ./data/muninn`. On Windows PowerShell, run `$env:MUNINN_LOCAL_EMBED='0'; .\.local\bin\muninn.exe --daemon --data .\data\muninn`. The process runs in the foreground; stop it with Ctrl+C. Data persists in `data/muninn`. Enrichment and the local embedder are disabled for this setup; semantic search requires a configured embedder. The server defaults to loopback and creates initial administrative credentials and a public default vault; secure these before exposing it to other users or networks. See upstream MuninnDB documentation for authentication and production deployment.

The application currently does not connect to MuninnDB; installing and starting it does not implement ingestion or deployment of a completed application.
