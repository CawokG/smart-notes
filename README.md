# Knowledge ingestion — initial foundation

This is an incomplete implementation. It provides a local snapshot scan only; it does not extract claims or connect to MuninnDB.

The scanner accepts one or more explicitly selected UTF-8 `.txt`/`.md` files, or one directory recursively. It stores exact snapshots and produces deterministic byte ranges for heading/paragraph chunks. `.obsidian`, the data directory and symbolic links are excluded; links and frontmatter remain uninterpreted source text. Extraction and MuninnDB integration are not implemented. See [SPEC.md](SPEC.md) and [the next tasks](docs/NEXT_TASKS.md).

Python 3.12+ examples:

```sh
python -m knowledge_ingestion.cli scan ./note.md --data ./data
python -m knowledge_ingestion.cli scan ./one/a.md ./two/a.md --data ./data
python -m knowledge_ingestion.cli scan ./vault --data ./data --max-bytes 5242880
python -m unittest discover -s tests -v
```

The JSON report includes warnings, collection/document/revision IDs and chunk byte ranges. IDs use SHA-256 over version-independent JSON-encoded identity components: a directory collection uses its absolute file URI, a selected-files collection uses the sorted unique set of absolute file URIs; document identity uses the collection and relative path or file URI, and revision identity uses document ID and the SHA-256 of the source bytes. Editing a file creates a revision; moving it creates a new document identity. Content hashes alone do not merge documents. A BOM is retained in the snapshot and byte offsets; chunks use half-open byte ranges and decode as UTF-8. Chunk sizes and overlap are byte-based approximations, not tokenizer guarantees. Links are never followed as a result of their contents.

SQLite `user_version=1` creates the scan tables transactionally; an existing unversioned `revisions` table and its snapshot files remain intact alongside the new tables. Existing legacy rows are retained as history, not automatically associated with new collections, because their original collection intent cannot be inferred. Rescan sources to index them in the new schema. A newer unsupported schema is rejected. Snapshots may contain private information and are stored unencrypted as exact bytes; avoid scanning a directory while it is being modified by an untrusted process.

## Install MuninnDB and the application

Install Python 3.12 or newer and Git, clone this repository, then run `python scripts/setup.py` (on Windows, `py -3 scripts/setup.py`). The script downloads the pinned upstream MuninnDB v0.9.0 binary, checks its SHA-256 against pinned release checksums, and installs it in `.local/bin`. It creates `.venv` and installs the application there. Internet access to GitHub releases and PyPI is required. Repeat the command to refresh the application; a matching binary is reused. Supported release binaries are Linux and macOS on x86-64/ARM64 and Windows x86-64; only Linux x86-64 has been tested here. Docker is not required.

To start a local server on Linux/macOS, run `MUNINN_LOCAL_EMBED=0 .local/bin/muninn --daemon --data ./data/muninn`. On Windows PowerShell, run `$env:MUNINN_LOCAL_EMBED='0'; .\.local\bin\muninn.exe --daemon --data .\data\muninn`. The process runs in the foreground; stop it with Ctrl+C. Data persists in `data/muninn`. Enrichment and the local embedder are disabled for this setup; semantic search requires a configured embedder. The server defaults to loopback and creates initial administrative credentials and a public default vault; secure these before exposing it to other users or networks. See upstream MuninnDB documentation for authentication and production deployment.

The application currently does not connect to MuninnDB; installing and starting it does not implement ingestion or deployment of a completed application.
