# Knowledge ingestion — initial foundation

This is an incomplete implementation. It provides a local snapshot scan only; it does not extract claims or connect to MuninnDB.

The initial scope is individual UTF-8 `.txt` and `.md` files, with entity and relationship extraction planned. Folder ingestion and Obsidian link processing are outside this scope. Each scan accepts one file.

Python 3.12+: `python -m knowledge_ingestion.cli scan ./note.md --data ./data` prints a JSON scan report. Snapshots and a SQLite revision index are stored in the data directory. Run tests with `python -m unittest discover -s tests`.

Source content may contain private information; snapshots retain exact bytes.

## Install MuninnDB and the application

Install Python 3.12 or newer and Git, clone this repository, then run `python scripts/setup.py` (on Windows, `py -3 scripts/setup.py`). The script downloads the pinned upstream MuninnDB v0.9.0 binary, checks its SHA-256 against pinned release checksums, and installs it in `.local/bin`. It creates `.venv` and installs the application there. Internet access to GitHub releases and PyPI is required. Repeat the command to refresh the application; a matching binary is reused. Supported release binaries are Linux and macOS on x86-64/ARM64 and Windows x86-64; only Linux x86-64 has been tested here. Docker is not required.

To start a local server on Linux/macOS, run `MUNINN_LOCAL_EMBED=0 .local/bin/muninn --daemon --data ./data/muninn`. On Windows PowerShell, run `$env:MUNINN_LOCAL_EMBED='0'; .\.local\bin\muninn.exe --daemon --data .\data\muninn`. The process runs in the foreground; stop it with Ctrl+C. Data persists in `data/muninn`. Enrichment and the local embedder are disabled for this setup; semantic search requires a configured embedder. The server defaults to loopback and creates initial administrative credentials and a public default vault; secure these before exposing it to other users or networks. See upstream MuninnDB documentation for authentication and production deployment.

The application currently does not connect to MuninnDB; installing and starting it does not implement ingestion or deployment of a completed application.
