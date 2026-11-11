# Stage 5 validation

Verified **11 November 2026, Europe/Moscow (UTC+3)**; machine timestamps below remain UTC. Branch `codex/prototype-stages`, incoming HEAD/origin `72d1228fea4d4d033bbd1634225ece9fa0b64250`. Results describe this reviewable prototype and synthetic tests; **production V1 readiness is not established**.

Status vocabulary: **PASS** = verified within the stated scope; **PARTIAL** = a named requirement/measurement remains incomplete; **BLOCKED** = a required live dependency is unconfigured; **NOTMEASURED** = no valid measurement. Scripted fake/HTTP responses do not measure semantic precision/recall or real-model behavior.

## Executed checks

| Check | Actual result | Command / evidence |
|---|---|---|
| Offline suite | **PASS, 97/97**, 6.333 s executor final run | `python -m unittest discover -s tests -v`; 78 inherited + 19 stage5 regressions. No external LLM/backend required; controlled HTTP tests use synthetic loopback traffic only. |
| Python compilation | **PASS** | `python -m compileall -q knowledge_ingestion scripts tests` |
| Diff whitespace | **PASS** | `git diff --check` |
| Fake full CLI demo | **PASS** | `python scripts/demo.py --output work/stage5-demo`; 5 files, 57/57 chunks, 18 evidence spans, 2 published fake memories, 2→2 fake POSTs on reimport; review history and all deferred operations retained. Supervisor independent replay also PASS. |
| Clean install | **PASS, Python 3.12.14/Linux x86-64** | `python -m venv work/stage5-clean-venv`; `work/stage5-clean-venv/bin/python -m pip install .`; installed `ki --help` and scan/plan/review/commit/status/resume/reimport from an unrelated cwd: all 9 CLI calls exit 0, 57 chunks, 2→2 fake POSTs. Import verified from venv `site-packages`, not checkout. Supervisor compared all 14 installed modules byte-for-byte and checked `ki --help` from `/tmp`. |
| Real HTTP LLM protocol | **PASS, controlled fixture only** | [HTTPProviderTest / RequestBudgetTest](../tests/test_llm_http.py): real urllib POST `/v1/chat/completions`, auth header, JSON schema/messages, exact anchors, numeric usage, malformed/null envelopes, redirect refusal, 429/timeout/schema/anchor exhaustion/resume and complete wire budget. |
| External live LLM | **BLOCKED/unconfigured** | No external credentials configured. No outbound real-provider call, no user-secret request, no precision/recall inference from fixtures. `OpenAICompatibleLLM` fails with `llm_secret_unconfigured_use_environment_settings` when remote credentials are absent. |
| Pinned Muninn capability smoke | **PASS_with_documented_limitations** | `python scripts/smoke_muninn.py --binary work/muninn-spike/muninn-linux-amd64 --output work/stage5-capability/report.json`; UTC `2026-11-10T20:59:07.231435+00:00`, **530 HTTP calls**, active marker 205/205, exhaustive lifecycle lookup UNSUPPORTED; child stopped and temporary storage removed. |
| Pinned product smoke | **PASS_with_documented_limitations** | `python scripts/smoke_commit.py --binary work/muninn-spike/muninn-linux-amd64 --output work/stage5-product/report.json`; UTC `2026-11-10T20:59:28.069749+00:00`; 5 real successful POSTs/committed claims, 1 separate intentional full-key 401 refusal, 38 observe reads. One-fact repeat/reimport totals 1 POST; every recovery adds 0 POST. Child stopped/temp storage removed. Supervisor independent replay PASS. |

The live commands ran **sequentially**, using only their own fresh temporary loopback instances, synthetic sources and random transient credentials. No existing store/server or private source was scanned. Enrichment/embeddings were disabled. Pinned MuninnDB v0.9.0 Linux/amd64 SHA-256: `4da4c959a6482c7608809a75501cd3c746875c1dcd9c7caf7e6d3052dbdefa5b`. [API capability matrix](../integrations/muninn-compatibility.md) preserves exact supported/unsupported boundaries. Raw scratch reports/logs live under ignored `work/`; [sanitized machine validation summary](../examples/validation-report.json) and [demo report](../examples/report.json) are checked in.

## SPEC §12: all 15 scenarios

These statuses assess the tested pipeline behavior. Items 1–7 with scripted proposals verify deterministic auditing/reconciliation; they do **not** show that a real model would extract those proposals correctly.

| # | Scenario | Status | Test/command evidence and practical limit |
|---|---|---|---|
| 1 | Grounded/scoped Макс/Максим/@max_dev alias | **PASS** | [`test_alias_normalization_scopes_and_collisions`, `test_unsafe_identity_statements_never_approve_alias_or_hint`](../tests/test_plan.py); corpus retains evidence/scope. Real extraction quality NOTMEASURED. |
| 2 | Cross-file staged context, links not relations | **PASS** | [`test_corpus_semantics_determinism_provenance_and_no_network`, `test_frontmatter_and_links_do_not_establish_identity_or_claims`](../tests/test_plan.py); selected files share one job; no wikilink traversal. |
| 3 | Same-name people remain separate/unresolved | **PASS** | [`test_homonym_and_fuzzy_candidate_do_not_resolve_by_name`, `test_same_document_is_not_identity_proof_for_homonyms`, `test_alias_collision_in_committed_registry_is_ambiguous`](../tests/test_plan.py); two synthetic Алексей IDs retained. |
| 4 | Duplicate fact, separate evidence | **PASS** | [`test_committed_repeat_is_duplicate_new_span_only_reinforces_locally`, `test_same_evidence_is_duplicate_not_reinforcement`](../tests/test_plan.py); corpus two-source canonical claim reinforcement, no extra engram. |
| 5 | Uncertainty/plans/reported/questions | **PASS** | [`test_mismarked_modality_polarity_and_relative_time_are_deferred`, `test_corpus_semantics_determinism_provenance_and_no_network`](../tests/test_plan.py); fixtures stay deferred, not plain positive facts. General-language semantics remain limited. |
| 6 | Past/negative time, no superseding | **PASS** | [`test_negation_preserved_and_temporal_intervals_do_not_supersede`](../tests/test_plan.py), [`test_negative_published_statement_explicitly_preserves_negation`](../tests/test_commit.py), product `negative_exact_product_readback`; prior memory unchanged. |
| 7 | Known-time incompatible status warning | **PASS** | [`test_conflicts_known_time_block_both_staged_sides_multivalued_does_not`](../tests/test_plan.py); both staged sides remain deferred, no silent overwrite. |
| 8 | Identical reimport, no new engram/evidence duplicate | **PASS** | [`test_repeat_commit_and_reimport_no_second_engram`](../tests/test_commit.py), `scripts/demo.py`, product `scan_fake_plan_review_commit_status_resume_reimport`: real one-fact total **1 POST**, fake corpus **2→2**. Requires one authoritative ledger. |
| 9 | Crash after POST/before SQLite, manual fallback | **PASS** | [`test_crash_after_post_unsupported_stops_without_retry_then_manual_known_id`, `test_known_id_persisted_before_crash_and_recovery_needs_no_marker`](../tests/test_commit.py); product real service-boundary crash/response-loss/read-back injection → 0 additional POST; known-ID exact repair. Real exhaustive absence proof UNSUPPORTED. |
| 10 | Prompt injection cannot execute tools/mutate | **PASS** | [`test_corpus_commit_remains_offline_and_source_injection_is_inert`](../tests/test_commit.py), [`test_corpus_semantics_determinism_provenance_and_no_network`](../tests/test_plan.py), [`test_wire_protocol_schema_anchors_usage_and_secret_redaction`](../tests/test_llm_http.py). Publisher has no destructive API; LLM request has no tools. Not an empirical real-model jailbreak benchmark. |
| 11 | UTF-8/Cyrillic/emoji/BOM/markup spans | **PASS** | [`test_utf8_bom_emoji_markup_and_frozen_snapshot`, `test_quote_occurrence_index_and_global_snapshot_offsets`](../tests/test_plan.py), [`test_directory_bytes_ranges_and_exclusions`](../tests/test_scan.py); exact byte/hash evidence remains verifiable. |
| 12 | Rate limit/timeout, incomplete chunk/resume | **PASS** | [`test_rate_limit_exhaustion_and_real_adapter_resume_same_hash`, `test_timeout_schema_anchor_and_malformed_envelopes_remain_incomplete`, `test_http_resume_keeps_successful_chunks_and_retries_only_incomplete_chunk`](../tests/test_llm_http.py); initial+2 bound, completed chunks not resent. External vendor failure behavior untested. |
| 13 | Observe counters/read-only phase | **PARTIAL** | Capability smoke real hits: exposed `access_count 0→0` and last_access unchanged in a two-second window; product observe context 0 writes; [`test_observe_reads_and_full_post_use_distinct_bearers_without_cookies`](../tests/test_commit.py). Hidden cognitive feedback unmeasured; full-key counter control also unchanged. No total cognitive immutability claim. |
| 14 | File/multiple files/folder/exclusions/links | **PASS** | [ScanTest](../tests/test_scan.py), [SourceMembershipTest](../tests/test_sources.py): hidden service trees, symlinks, selected-set identity, custom limit, all-invalid error/warnings, latest membership, preserved history, stale newly-valid/edited/missing sources. Wikilinks remain raw. |
| 15 | Native name collision preserves local identity | **PASS** | Capability smoke `native_candidates.same_name_distinct_memories_share_native_entity`: two different Алексей memories in one native aggregate; local [homonym/alias tests](../tests/test_plan.py) preserve IDs. Product native lookup intentionally uses local fallback; this is not complete legacy reconciliation. |

## SPEC §13: quality gates

| Criterion | Status | Evidence / limit |
|---|---|---|
| 13.1: 100% published fixture claims have valid evidence/resolved IDs | **PASS** | `policy_gate`, publication `check_preconditions`, `test_corpus_commit_remains_offline_and_source_injection_is_inert`, exact product read-back; 5 real synthetic committed claims. Arbitrary real-model semantic correctness is not implied. |
| 13.1: zero automatic homonym merges | **PASS** | Scenario 3/15; no merge API in publisher. |
| 13.1: zero duplicate engrams after repeat/recovery or honest manual block | **PASS** | Scenario 8/9; durable outbox, no blind POST retry, unsupported marker handled manually. Single-ledger scope applies. |
| 13.1: zero destructive changes to existing engrams | **PASS** | Adapter exposes only create/read/observe in product path; `test_add_evidence_and_alias_are_local_only_with_readable_existing_entity`, real product smoke. Capability harness destructive calls affect its own disposable fixtures only. |
| 13.1: scan/plan produce zero backend writes | **PASS** | `test_corpus_semantics_determinism_provenance_and_no_network`; product `explicit_observe_context` 0 writes; model has no write API. Hidden read feedback is separate PARTIAL observation. |
| 13.1: deterministic/fake suite without external service | **PASS** | 97/97; loopback HTTP fixture is controlled and synthetic, with no external LLM/backend dependency. |
| 13.1: every unapplied operation inspectable/recoverable or manual | **PASS** | Status/review/outbox/deferred IDs; `test_definitive_4xx_is_terminal_and_preserves_all_unapplied`, `test_unknown_outbox_blocks_fresh_plan_marker_for_same_vault`; no abort UI yet. |
| 13.2: precision ≥0.95 on authorized gold corpora | **NOTMEASURED** | No external live LLM/authorized real corpus. Scripted response success is not precision. |
| 13.2: recall ≥0.80 | **NOTMEASURED** | Long note unscripted portions intentionally return no proposals. No recall claim. |
| 13.2: nonhomonym entity accuracy ≥0.90 | **NOTMEASURED** | Rule fixtures do not measure end-to-end extraction/entity accuracy. |
| 13.2: tokens/cost per 1000 source tokens, per-file runtime | **PARTIAL** | Safe provider numeric usage retained when supplied; demo records per-command seconds. No real usage/cost or per-file/source-token normalized benchmark; cost null. |
| Phase Gate A: pinned capability/fallback evidence | **PASS** | Both real live smokes and API matrix; exhaustive recovery explicitly unsupported. |
| Phase Gate B: deterministic scan/IDs/evidence/history/migrations | **PASS** | Scan/source/anchor tests plus schema0/1/2/3→4 lossless migration checks; removed/ineligible sources cannot reenter current plans. |
| Phase Gate C: fixture plan/no writes/validated evidence | **PASS** | Offline corpus, schema/anchor tests, bounded context; real semantic extraction remains unmeasured. |
| Phase Gate D: repeat/recovery/publication hard checks | **PASS**, tested prototype scope | Real pinned read/write and fault-injected service recovery; single-ledger/Linux limits and manual fallbacks retained. This does not close semantic §13.2 or overall DoD. |

## SPEC §14: Definition of Done

| # | Requirement | Status | Evidence / remaining work |
|---|---|---|---|
| 1 | Clean setup/config/migrations | **PASS**, Linux | Clean venv/install/installed CLI verification; source/schema migration tests; [README](../README.md). Other publication platforms untested/unsupported. |
| 2 | Full scan→plan→review→commit→status/resume | **PASS** | `test_full_cli_scan_plan_review_commit_status_resume`, demo, clean installed CLI, real product smoke. |
| 3 | Deterministic fake + real configurable LLM | **PARTIAL** | Real adapter implemented; actual HTTP wire fixture PASS. External vendor/model live run **BLOCKED/unconfigured**. |
| 4 | Real Muninn context or explicit fallback | **PASS**, bounded scope | Product real observe reads and explicit local fallback; hidden feedback unmeasured. |
| 5 | Pinned Muninn read/write verified or honest block | **PASS** | v0.9.0 capability and product smokes; no production integration claim. |
| 6 | Validation/exact anchors/alias collisions/conservative reconciliation | **PARTIAL** | Deterministic rules PASS; complete external legacy semantic reconciliation not implemented; general model entailment/quality NOTMEASURED. |
| 7 | JSON ChangeSet/provenance/outbox/recovery | **PASS** | [ChangeSet example](../examples/change-set.json), immutable hashes, review history, real/fake recovery tests and status. |
| 8 | All 15 scenarios have tests/results, failures disclosed | **PASS** as coverage/accounting | All listed above; scenario13 remains explicitly PARTIAL rather than silently accepted. |
| 9 | README/architecture/API matrix/known limits | **PASS** | Linked documents, [config example](../config.example.json), [demo](../scripts/demo.py), [machine/human reports](../examples/report.md). |
| 10 | No unapproved destructive commit actions | **PASS** | Product exposes no delete/evolve/merge/update API; explicit commit confirmation and actor required. |

**Overall: PARTIAL prototype acceptance.** Available engineering/wire/live-backend checks pass. External real-model validation, semantic quality targets, full legacy reconciliation and hidden cognitive-feedback measurement remain open; no production V1 declaration is made. No further stage or agent is required to review this delivered implementation; remaining live/quality work needs separately configured credentials and authorized data.
