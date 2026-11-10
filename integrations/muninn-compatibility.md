# MuninnDB compatibility

The setup script pins MuninnDB v0.9.0 release binaries and published SHA-256 checksums. Linux x86-64 binary SHA-256: `4da4c959a6482c7608809a75501cd3c746875c1dcd9c7caf7e6d3052dbdefa5b`. On Linux x86-64 the binary started with an isolated temporary data directory, enrichment and local embeddings disabled; `GET /mcp/health` returned `{"status":"ok"}`. Other supported platforms have not been tested here. GHCR image pull was denied in this environment; binary installation succeeded.

No read/write, observe authentication, Entity Graph, or exact idempotency-marker lookup has been verified. The application does not yet implement the adapter or write to MuninnDB. Do not treat startup health as integration verification.
