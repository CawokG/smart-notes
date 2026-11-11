# Synthetic offline demo

Result: **PASS_with_documented_limitations**. Explicit FakeLLM and durable fake backend.

- 5 files; 57/57 chunks.
- 2 proposed new claims; 7 defer operations; 1 conflict warnings.
- Publication: `complete_with_deferred`; 2 fake memories; 2→2 fake POSTs after identical reimport.
- Review history retained; exact evidence and deferred operations are in [change-set.json](change-set.json); generate `status.json` with the demo.
- Precision, recall and entity resolution accuracy: **NOT MEASURED**. External LLM: **BLOCKED/unconfigured**.

Only the checked-in synthetic corpus was copied. This run made no MuninnDB or external LLM calls.
The fake backend's exhaustive marker lookup is a test guarantee, not a pinned MuninnDB capability.
See [validation](../docs/VALIDATION.md) and [limitations](../KNOWN_LIMITATIONS.md) before using real sources.
