# Knowledge Ingestion V1 — этап 2: offline plan

Дата проверки: 2026-11-10. Репозиторий `/workspace/smart-notes`, ветка `codex/prototype-stages`.
Базовый SHA: `c0f2acde75041271b812280d62edd46fbc254a02` (merged этап 1, актуальный `origin/main` на начало работы).
Новый commit SHA добавит supervisor после review; исполнитель этапа 2 не выполнял commit или push.
Старый снимок `/workspace/work/pre-sync-smart-notes` не изменялся.

## Реализовано

- `contracts.py`: JSON Schema extraction 1.0, строгая проверка неизвестных полей, типов, ссылок, UTF-8, ограничений размера/глубины, дат и конечных чисел. Нормализация NFKC + casefold + whitespace для aliases; canonical predicate/typed object/qualifiers/polarity/modality/time fingerprint. Числа не округляются контекстом Decimal.
- `storage.py`: транзакционная SQLite migration 0/1→2; существующие `revisions`, scan-таблицы, snapshots и IDs сохраняются. Отдельные committed `entities/aliases/mentions/claims/claim_evidence` и job-scoped `staged_*`; immutable `evidence` получает staged/committed membership через связи. Есть jobs, job_documents, per-chunk checkpoints, run_events, ChangeSets и operations. Планировщик не пишет committed projections. Canonical unique key применяется к proposed rows; deferred proposals сохраняют fingerprint в payload и SQL NULL в unique-колонке, не вытесняют безопасные строки.
- `evidence.py`: проверка snapshot SHA-256/UTF-8 и точной цитаты, global half-open byte offsets, BOM/emoji/разметка, ordinal/occurrence disambiguation. Отсутствующий/неоднозначный якорь не получает выдуманные offsets. Frontmatter fence ranges остаются исходным текстом с metadata trust; из них нельзя получить structured alias/identity/fact. Link targets не разрешаются. Новые revisions получают capture timestamp один раз, evidence сохраняет его; для исторических v1 timestamps остаются NULL.
- `llm.py`: read-only `LLMProvider.generate_structured` и scripted `FakeLLM`. Prompt явно маркирует источник недоверенными данными, отделяет polarity/modality/time и не предоставляет tools/credentials/write API. Конфигурация фиксирует model, fixture hash, schema/prompt/rules/parser versions и temperature=0.
- `context.py`, `resolution.py`: явный `offline_ledger_only` fallback, committed context и staged overlay только текущего job, bounded digest реконструируется из ledger и не доверяет model-written digest. Exact grounded mention, approved scoped aliases с прямым identity context; plain human/account/pet name и близость в документе не доказывают identity. Коллизии/неподтверждённые hints/местоимения/fuzzy shortlist откладываются; custom type сохраняется в `other.type_label`.
- `planner.py`: последовательные документы/chunks, atomic successful-chunk checkpoint; initial call + максимум 2 retries. После исчерпания — `needs_attention`, JSON error и defer с revision/chunk reference; failed chunk не completed. Повтор идентичного plan использует checkpoints. Reconciliation даёт create / add_evidence(reinforce, ledger only) / skip_duplicate / defer / conflict_warning, stable operation IDs, provenance и topological dependencies. Конфликтующие staged стороны обе остаются deferred, повтор одной стороны не снимает review. Multi-valued predicates не становятся конфликтами из-за разных объектов. Старые evidence/revisions не удаляются; исчезнувший committed source quote даёт `possibly_stale_source`.
- CLI `plan --collection ... --data ... --llm-provider fake --fixture ... [--output ...]`; failure/needs_attention возвращает exit 1. `scan` сохраняет прежний интерфейс. README описывает честный offline запуск.
- `fixtures/offline-extraction.json`, прямые body anchors в исходном synthetic corpus и semantic expectations в `fixtures/expected.json`; frontmatter aliases и wikilinks остались raw-only. Новые meaningful tests в `test_plan.py`, lossless migration tests в `test_scan.py`.

## Проверки и воспроизведение

В корне репозитория:

```sh
python -m unittest discover -s tests -v
git diff --check
python -m knowledge_ingestion.cli scan fixtures/obsidian --data work/stage2-cli/data > work/stage2-cli/scan.json
# Взять collection_id из scan.json:
python -m knowledge_ingestion.cli plan --collection col_... --data work/stage2-cli/data --llm-provider fake --fixture fixtures/offline-extraction.json --output work/stage2-cli/change-set.json
```

`work/stage2-cli/` следует создать до shell redirection. `work/` исключён из Git; там лежит только временная генерация/проверка, deliverables — исходники и документация выше.

Результаты:

- Полный `unittest`: **39/39 PASS**, без внешней сети или LLM. Среди них exact schema1→2 migration всех четырёх scan-таблиц и snapshot, повторная migration, отказ от newer schema, UTF-8/BOM/emoji anchors, ambiguous/missing anchors, schema/provider retry exhaustion и checkpoint recovery.
- `git diff --check`: **PASS**.
- Synthetic scan→fake plan: **5 файлов, 57/57 chunks completed, 18/18 evidence spans** с совпадающими snapshot bytes/hash. **4 entities**, из них два разных Алексей; **4 add_alias**, **2 create_claim**, **1 add_evidence**, **1 conflict_warning**, **7 defer**, **4 link_claim_entity**. Оба conflicting `has_status` отложены. Повторный plan и план в другом чистом data directory дают одинаковый JSON при тех же входах и committed projection.
- Source injection не вызывает subprocess/socket; tests запрещают их в scan/plan flow. **0 MuninnDB read calls / 0 write calls**, committed projections не меняются. CLI проверен на JSON/stdout/file и exit 1 для `needs_attention`.
- Existing committed-ledger fixtures проверяют exact duplicate и новое evidence без второго create_claim; это не live commit.

## Осознанные решения и ограничения для следующего этапа

Сохранён stage1 формат `prefix_SHA256`, а не предложенные в SPEC §4 UUIDv7/ULID: это сохраняет deterministic IDs и миграцию без перенумерации. ID зависят от canonical source location/collection; перенос источника создаёт новую identity-кандидатуру. Метаданные wall-clock хранятся в ledger, но исключены из deterministic ChangeSet/fingerprint; `recorded_at` claim пока NULL, job timeline доступен отдельно. `collection_metadata` подготовлена миграцией, но vault выбирается и фиксируется на уровне job; scan пока не предоставляет vault configuration.

FakeLLM — **явно сценарная фикстура**, не NLP extractor. Unconfigured chunks возвращают пустые proposals; длинная заметка проверяет sequencing/digest, не recall. LLM confidence не используется как основание approval. Простые lexical guards и точные quotes не доказывают entailment произвольного языка. Неоднозначность, отличающиеся формы имён, не-ISO даты и неопределённые temporal expressions могут консервативно уменьшать recall. Все non-asserted modalities и unresolved relative dates deferred; asserted negative claims с прямым evidence могут быть предложены отдельно. Отсутствующее время не доказывает single-valued contradiction; противоположная polarity без времени требует temporal review.

`approved` — только локальный policy gate. **Публикации нет.** Не реализованы `review`, `commit`, `status`, полная CLI `resume`, outbox/single-writer locking/read-back, stale-plan checks перед публикацией и cross-store crash recovery. Повтор `plan` восстанавливает только offline chunk checkpoints; новый fixture/config hash даёт новый job. Concurrent planner instances для одного и того же job отдельно не испытаны.

MuninnDB adapter, observe/auth/marker/native entity capability spike и live read/write не выполнялись в этом этапе. External snippets можно передать только как помеченный offline possible-match context; сетевого retrieval нет. Real LLM adapter/credentials/retry rate-limit integration отсутствуют. Новых зависимостей не добавлено и пакеты не устанавливались.

Применимые §12: 1–7, 10–12, 14 покрыты offline (provider outage scripted); 8 — только локальная повторная сверка с существующим committed ledger, без commit; 9 — POST crash recovery отсутствует; 13 — observe access counters не проверены; 15 — собственные alias/name collisions проверены, native MuninnDB collision не проверен. Gates B/C подтверждены для deterministic offline пути. Полные hard gates §13, quality precision/recall на реальных корпусах, Gate D и DoD §14 **не закрыты**.

Следующий исполнитель начинает с этапа 3 `docs/NEXT_TASKS.md` / capability matrix `integrations/muninn-compatibility.md`, затем реализует review/commit/recovery. Нельзя считать scripted tests доказательством live readiness и нельзя повторять неизвестный POST без доказанного exact marker lookup.
