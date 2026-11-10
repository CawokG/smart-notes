# Knowledge ingestion — initial foundation

This is an incomplete implementation. It provides a local snapshot scan only; it does not extract claims or connect to MuninnDB.

The initial scope is individual UTF-8 `.txt` and `.md` files, with entity and relationship extraction planned. Folder ingestion and Obsidian link processing are outside this scope. Each scan accepts one file.

Python 3.12+: `python -m knowledge_ingestion.cli scan ./note.md --data ./data` prints a JSON scan report. Snapshots and a SQLite revision index are stored in the data directory. Run tests with `python -m unittest discover -s tests`.

Source content may contain private information; snapshots retain exact bytes.
