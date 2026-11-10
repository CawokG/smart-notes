# Knowledge Ingestion V1 — этап 4: review, publication и recovery

Проверено **2026-11-10**, live product run **20:38 UTC / 23:38 Europe/Moscow**. Репозиторий `/workspace/smart-notes`, ветка **`codex/prototype-stages`**.
Входной SHA этапа 4: **`c0a96f5f1e0a3787255c6dd9df89ae0a93480e1e`** (`HEAD=origin/codex/prototype-stages`, этап 3 reviewed/committed/pushed; дерево на входе чистое). Этап 2: `8daf7afc1a392495add5da19a8a0349fd78c026e`; base main `c0f2acde75041271b812280d62edd46fbc254a02`.
Исполнитель этапа 4 **не выполнял commit/push**. Итоговый SHA после review фиксирует supervisor. Пользовательские stores, существующие backend и `/workspace/work/pre-sync-smart-notes` не изменялись.

## Что завершено в этапе 4

- `publication.py`: immutable original ChangeSet и supplied-file equality/hash/version, согласованность persisted job/config/manifest/staging/evidence/operations, current code versions, исходная ledger revision и operation preconditions. Проверка originals + snapshots и полного source manifest перед writes; отсутствующие/изменённые/hidden/unlisted source fail closed. Существующее stage1 кодирование stable IDs сохранено: для job identity восстанавливается исходный порядок manifest keys, operation payload сравнивается с независимым job_operations.
- `commit.py`: `review` по operation_id с actor/rationale/UTC timestamp/append-only history, quotes/filename/heading; batch dependency validation и запрет approval unsafe defer/conflicts. Review decisions не изменяют исходный JSON и замораживаются после старта commit. `status` показывает decisions, все unapplied IDs, outbox attempts/outcome/known IDs, manual-repair IDs, confirmation/session и bounded redacted event timeline.
- Подтверждаемый `commit`: `--yes` или literal terminal `yes`, обязательный actor; noninteractive отказ до подготовки/backend. `resume` продолжает оригинальный fake planning с совпадающим fixture/config/context либо ранее подтверждённый commit; повторное подтверждение для recovery не требуется. Manual known-ID repair требует operation ID, actor/rationale и точного read-back.
- Единственный writer под vault `flock` для одного OS account (Linux). Schema3 additive transactional 2→3 migration добавляет review_decisions/commit_sessions/outbox, сохраняя прежние таблицы/данные/IDs. Commit session фиксирует backend identity, config/artifact hashes, confirmation и ожидаемую ledger revision, атомарно обновляемую после собственного local apply; внешние изменения дают `replan_required`.
- Transactional outbox `prepared` до POST, durable `attempting` до вызова, stable `ki_op:<operation_id>`, attempts/outcome/known-ID/read-back, atomic/idempotent local apply. После crash/unknown сначала exact reconciliation. Real pinned capability false → `needs_manual_repair`, **без повторного POST**. Known-ID GET проверяет vault, ID, concept/content, полный tag set/marker и confidence. Новая session блокируется, пока в vault есть unresolved attempted outbox, чтобы новый plan не обходил unknown outcome свежим marker.
- `muninn.py`: pinned v0.9.0 health и capability contract, раздельные observe/full bearer credentials, consistent vault, no admin cookies/redirect forwarding, no token/body exception logging, GET, bounded observe ACTIVATE, единственный create endpoint. General `list_by_marker` explicit `UnsupportedOperation`; native entity capability optional/local fallback. HTTP truncation/transport/3xx/408/5xx после POST считаются unknown; definitive4xx terminal refusal; никакой автоматический POST retry.
- `fake_backend.py`: **явно fake** durable SQLite backend для offline workflow/recovery, с отдельным именем/identity и проверкой формата store. Его exhaustive marker guarantee не приписывается MuninnDB. No automatic fake fallback для live failures.
- Readable engrams сохраняют negative polarity в самом утверждении, valid time, typed object/unit и qualifiers; имена берутся из committed+staged ledger. Native names не являются IDs. Alias/evidence reinforcement меняет только локальный ledger, никакого merge/delete/evolve/update прошлых engrams.
- Opt-in `plan --context muninn-observe`: bounded observe GET/ACTIVATE, explicit local-ledger fallback, external snippets как untrusted context; default fake plan остаётся полностью offline. Observe/full secrets не попадают в prompts/config/artifacts/status. Не заявляется полная legacy semantic reconciliation или cognitive immutability.
- `scripts/smoke_commit.py` использует isolation harness stage3 с отдельным characterization callback; создаёт только собственный temporary loopback instance, synthetic sources и random transient keys. Ни existing URL, ни existing data-directory параметра нет. Fault injection после реального POST/до SQLite, lost response и read-back failure явно обозначены в отчёте. README обновлён для реализованного CLI; NEXT_TASKS §4 закрыт с ограничениями, §5 остаётся открытым.

## Проверки и результаты этапа 4

```sh
python -m unittest discover -s tests -v
python -m compileall -q knowledge_ingestion scripts tests
git diff --check
python scripts/smoke_commit.py --binary work/muninn-spike/muninn-linux-amd64 \
  --output work/commit-smoke/report.json
```

- **78/78 offline tests PASS**: 47 прежних + 31 adapter/publication/recovery. Нет внешней сети/LLM; fake workflow tests запрещают sockets/source subprocess. Полный CLI scan→fake plan→review→commit→status→resume, repeat/reimport, local aliases/reinforcement и synthetic corpus с deferred/conflicts проходят.
- Проверены review history/dependency rejection/unsafe override refusal, noninteractive confirmation, prepared-before-POST, crash после успешного POST до ID/SQLite, supported fake exact recovery и unsupported fallback, lost response, terminal4xx, known-ID persistence/read-back failure/mismatch, manual verified ID, stale original/snapshot/config/ledger/backend, outbox/artifact tamper, same-account concurrent process, replan обход unknown и lossless schema2→3 migration. Unapplied/deferred записи доступны через status, старые source revisions/evidence сохраняются.
- **Product live smoke: `PASS_with_documented_limitations`**, установленный Linux x86-64 binary v0.9.0 с checksum `4da4c959a6482c7608809a75501cd3c746875c1dcd9c7caf7e6d3052dbdefa5b`. Primary product adapter: **5 full POST**, **38 observe reads**; отдельный observe-context adapter: **4 reads/0 writes**; invalid-full fixture: **1 rejected POST (401)**. Ledger: **5 committed claims, 5 applied outbox**, 1 terminal-refusal manual-repair outbox. Повтор/reimport создаёт всего 1 engram; все resume/manual recovery дают **0 extra POST**. Daemon stopped и temporary storage removed подтверждены отчётом.
- **Independent supervisor live replay: PASS** по всем этим сценариям; финальный independent supervisor offline replay также 78/78 PASS, compileall и diff-check PASS. Local reports/work assets ignored и не входят в Git. Compileall и diff-check PASS.

## Реальные ограничения и следующие задачи — этап 5

1. **Real exhaustive marker lookup UNSUPPORTED** по фактам stage3. Не использовать active-only search, capped deleted list, native content dedup или непроверенные idempotent_id/op_id receipts как доказательство отсутствия. Lost ID автоматически не обнаруживается; unknown POST сохраняется manual. Reliable known-ID может завершить application только после точного read-back.
2. Known4xx — terminal/manual, без повторного POST. CLI abandonment/refusal resolution пока отсутствует; unresolved attempted outbox блокирует новые sessions vault. История/исходные данные сохраняются для ручного разбора. Source/config changes во время partial publication также требуют operator-led reconciliation; простое редактирование artifact/outbox запрещено integrity checks.
3. Надёжная идемпотентность требует **одного authoritative ledger на backend/vault**. Lock действует в одном OS account/host, Linux/flock; cross-host/cross-account/cross-ledger coordination и Windows publication не реализованы. Source/snapshot должны оставаться неизменными в течение preflight/commit; защиты от враждебных concurrent filesystem writers нет.
4. **Inherited scanner defects не исправлены:** `.git`/other hidden dirs обходятся; all-invalid scan возвращает empty-success; удалённый документ остаётся last_revision и planner включает его после rescan. Precommit отвергает missing/hidden/unlisted material; all-invalid planning не имеет пригодных документов. Directory collections с skipped eligible files также отклоняются консервативным полным manifest check. Этап 5 обязан исправить scanner, добавить регрессии и заново проверить **полный Gate B**. Не скрывать это за успешным offline baseline.
5. Native optional candidate tools не подключены к product canonical matching; bounded REST context и explicit local fallback есть, полноценная semantic reconciliation внешних legacy snippets остаётся за границей прототипа. ContextBundle уже ограничивает entities≤64, aliases≤64, claims≤40 и digest budget, но большой единичный claim/evidence payload требует дальнейшей оценки prompt budgets на real provider.
6. Observe exposed counters stage3 были 0→0, hidden scoring feedback не измерен. **Не обещать полную cognitive immutability.** В product smoke callbacks фиксируют искусственные service-boundary faults после настоящего HTTP success; это не simulated Muninn storage crash или distributed durability proof.
7. Этап 5: configurable real LLM, data-disclosure/secret discipline, полный README/install/demo/ARCHITECTURE/KNOWN_LIMITATIONS/report, все §12 scenarios и честные §13–14 quality gates. Никакой real LLM/финальный qualityreport в этапе 4 не выполнялся. **V1 в целом не готов.**

---

## Исторический handoff этапа 3: pinned MuninnDB capability spike

Дата проверки: **2026-11-10**. Репозиторий `/workspace/smart-notes`, ветка **`codex/prototype-stages`**.
Входной SHA этапа 3: **`8daf7afc1a392495add5da19a8a0349fd78c026e`** (`HEAD=origin/codex/prototype-stages`, этап 2 reviewed/committed/pushed; дерево на входе чистое).
Исполнитель этапа 3 **не выполнял commit или push**. Итоговый SHA после review добавляет supervisor в следующий handoff/prompt. Старые/пользовательские хранилища и `/workspace/work/pre-sync-smart-notes` не изменялись.

## Что завершено в этапе 3

- `scripts/smoke_muninn.py`: воспроизводимый Linux x86-64 smoke на **release v0.9.0**, SHA-256 **`4da4c959a6482c7608809a75501cd3c746875c1dcd9c7caf7e6d3052dbdefa5b`**. Проверка checksum до исполнения; при отсутствии default binary — pinned download в ignored `work/`.
- Только собственный временный loopback instance и synthetic vaults; отдельные случайные admin/MCP/observe/full credentials, отключённые local embeddings и enrichment, контроль startup bind logs, graceful shutdown и удаление временного data tree. Нет параметра existing backend URL/data. При наличии `~/.muninn/muninn.env` smoke отказывается от запуска, чтобы не применять пользовательский config; HOME не изменяется. Credentials не записываются в отчёт.
- Проверены installed binary health/version, private-vault auth и vault binding, full POST → observe GET с точным read-back, write denial для observe, ACTIVATE с реальными lexical hits и counters, MCP candidate lookup, name collision и marker pagination/lifecycle completeness. Ни исходники tag, ни `develop` docs не считаются smoke-подтверждением.
- `tests/test_muninn_smoke.py`: 8 offline safety checks — окончательная пустая страница, отказ от truncated/duplicate/changing/ignored-filter/offset pages, redaction, отказ от ambient config/provider variables, checksum pin и отсутствие retry после неизвестного POST.
- `integrations/muninn-compatibility.md`: матрица PASS/UNSUPPORTED/UNVERIFIED, воспроизведение, реальные auth/endpoints/JSON shapes, ограничения счётчиков и безопасный контракт этапа 4. В `docs/NEXT_TASKS.md` закрыт только этап 3; этапы 4–5 остаются открытыми.
- Зависимости не добавлялись и не устанавливались. Application scan/plan и schema2 не изменялись; product adapter/commit service не реализованы.

## Проверки и фактические результаты

```sh
python scripts/smoke_muninn.py --binary work/muninn-spike/muninn-linux-amd64 \
  --output work/muninn-spike/report.json
python -m unittest discover -s tests -v
git diff --check
python -m compileall -q knowledge_ingestion scripts tests
```

- Executor live smoke: **`PASS_with_documented_limitations`**, 530 HTTP calls, около 11 секунд. **Independent supervisor replay: PASS**, включая shutdown/temp cleanup, marker205 и homonym aggregate. Отчёты `work/` — временные локальные артефакты, в Git не входят; воспроизводимые исходники и факты находятся в script/matrix.
- **47/47 offline tests PASS** (39 прежних + 8 новых); supervisor независимо подтвердил 47/47, compileall и diff-check. Детерминированный stage2 scan→fake plan baseline остаётся 57/57 chunks, 18/18 exact evidence, повтор JSON побайтово идентичен, 0 backend reads/writes; код этого пути не менялся.
- Private no-token/invalid-token → 401; чужой query vault → 401; несовпадающий body vault → 400; observe POST → 403 без созданной записи; full POST → 201; observe GET/read-back → 200 с точными concept/content/tags/confidence. Ключ привязан к vault, GET без vault использует его scope.
- Observe 3 GET + 3 ACTIVATE с hit и двухсекундным polling: `access_count 0→0`, `last_access` неизменён. Full-контроль также `0→0`: нельзя заявлять positive contrast или полное отсутствие cognitive side effects. Отдельный **намеренный подтверждённый** duplicate-content POST возвращает тот же ID и даёт counter 1; это positive control доступности счётчика, не retry неизвестной записи.
- Active marker fixture: **205/205**, страницы **200+5+empty**, page size 7 также полон; exact case-sensitive tags, AND (204 записи), prefix/suffix/case/content decoys и cross-vault exclusion подтверждены.
- **Exhaustive marker lookup: UNSUPPORTED.** Default list пропускает archived/deleted; `state=archived` находит архивный fixture, а `state=soft_deleted` игнорируется и выдаёт active205. `/api/deleted?limit=9999` возвращает 100 из **101** известных tombstones; `count=100` — returned count, offset100 игнорируется. Скрытый tombstone читается по известному ID с `state=127` и marker. Отсутствие в этих списках не доказывает отсутствие записи.
- Native `muninn_entity` и `muninn_find_by_entity` с observe key возвращают обе synthetic записи разных людей по имени Алексей в одном aggregate; `similar_entities` возвращает пару Quasar Engine/Quasar Engines. Это candidate shortlist, не canonical identity. Cross-vault global name-key — source-only observation, отдельно не smoke-tested.

## Точные рекомендации следующему исполнителю — этап 4

1. Начать с `docs/NEXT_TASKS.md` §4 и полной матрицы `integrations/muninn-compatibility.md`; SPEC — целевые требования, её исторический implementation snapshot не заменяет этот handoff. Не выполнять этап 5 и не расширять native mutation API в critical path без отдельной задачи.
2. Adapter: REST `Authorization: Bearer <vault-bound key>`; отдельный observe key для чтения и full key только для публикации, без admin cookies. Verified minimum — `/api/health`, `POST /api/engrams`, `GET /api/engrams/{id}`, bounded `POST /api/activate`. Проверять query/body vault и ключ; POST/GET timestamps — nanoseconds, list timestamps — seconds. MCP optional через отдельный port `/mcp`; HTTP 200 требует проверки JSON-RPC `error` / `result.isError` и разбора JSON внутри `content[].text`.
3. Установить **`exact_exhaustive_marker_lookup=false`**; general `list_by_marker` возвращает explicit `UnsupportedOperation`. Active hits могут быть диагностическими кандидатами, но не полным доказательством отсутствия/уникальности. При неизвестном результате POST или исчерпании bounded read-back без надёжного known-ID match — **`needs_manual_repair`**, outbox сохранить, **POST не повторять**. `idempotent_id` / MCP `op_id` receipt crash/payload guarantees не проверены; native content dedup не заменяет recovery.
4. Реализовать `review` по operation ID с историей, `status`, полную CLI `resume`, подтверждаемый `commit`, source/snapshot/dependency/config/stale-plan checks, один writer, lock на vault, SQLite outbox и exact read-back concept/content/marker до `applied`. Schema2 уже содержит jobs/events/ChangeSets и staged/committed projections; полноценного outbox пока нет. Не переписывать прежние IDs или evidence/revisions.
5. Проверить повторный commit, crash после POST до SQLite, потерянный ответ/read-back, manual-repair fallback, stale plan и конкуренцию. Новые synthetic smoke fixtures не означают, что product crash recovery готов. Прежние engrams не менять/не удалять/не merge-ить; reinforce/add_evidence — локальный ledger.
6. Native names не объединяют local identities. Сохранить отдельные local Entity IDs, alias scope и evidence; candidate lookup optional с явным local-ledger fallback. Source-only caution: pinned `Engine.Read` вызывает `scoring.RecordFeedback` без observe guard. Измеренный access counter не показывает hidden scoring feedback; не обещать полную cognitive immutability чтения.

§12.13 проверен лишь в пределах exposed counters/окна; §12.15 теперь имеет live homonym aggregate + прежние offline local-identity collision tests. §12.8–9 (product повторный commit/crash recovery), весь Gate D, полные hard gates §13 и DoD §14 **не закрыты**. Real LLM, quality metrics на реальных корпусах, полные CLI/demo/ARCHITECTURE/KNOWN_LIMITATIONS остаются этапом 5. V1 в целом не готов.

## Исторический подробный отчёт этапа 2 — offline plan

Дата проверки: 2026-11-10. Репозиторий `/workspace/smart-notes`, ветка `codex/prototype-stages`.
Базовый SHA: `c0f2acde75041271b812280d62edd46fbc254a02` (merged этап 1, актуальный `origin/main` на начало работы).
Commit этапа 2 после supervisor review: `8daf7afc1a392495add5da19a8a0349fd78c026e`; executor этапа 2 не выполнял commit или push.
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

На конец этапа 2 следующим был capability spike этапа 3; он завершён и описан выше. Теперь следующий исполнитель начинает с этапа 4 review/commit/recovery. Нельзя считать scripted tests доказательством live readiness и нельзя повторять неизвестный POST без доказанного exact marker lookup.
