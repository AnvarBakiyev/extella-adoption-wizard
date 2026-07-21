# Universal Process Contract v1

Статус: production contract для standalone Extella. Версия: `upc/1.0`.

UPC — единый договор между интервью, Planner, Builder, Runtime, приёмкой, памятью и четырьмя
поверхностями Extella: Wizard, Chat, Composer и Workspace. Это не формат промпта и не внутренняя
структура конкретной LLM-библиотеки. Один и тот же сохранённый граф должен быть читаем, исполним и
возобновляем независимо от поверхности, с которой пользователь начал работу.

## 1. Решение после аудита существующего контура

| Существующий механизм | Решение | Причина |
|---|---|---|
| Сессия `wz_*.json` и запись через `_update_session` | REUSE | Уже является источником решений и блокировок |
| Task Contract и Source Model из `wz_agentic.py` | REUSE/EXTEND | Собирают интервью, реальные входы, полномочия и фактуру |
| Agentic solve → run → deterministic gate → LLM judge → repair | REUSE AS STEP ENGINE | Уже умеет создавать реального эксперта, запускать и не публиковать ложный success |
| Working memory `concept/rule`, candidate/verified/rejected | EXTEND | Нужны ещё evidence, lesson и artifact, явная область и источник |
| Build progress и owner checkpoint | MIGRATE | Нужны статусы и checkpoint на каждый шаг, а не только на всю стройку |
| Линейный stage-orchestrator | COMPATIBILITY ONLY | Сохраняется для старых releases; новые процессы исполняет UPC Runtime |
| Экспериментальный Contour `_goal_loop` | ABSORB, NOT PARALLEL RUNTIME | Полезны one-step planning, acquire и trace-gate, но текущее состояние недолговечно и build не создаёт production-эксперта |
| Capability Registry | REUSE AS INVENTORY | Каталог даёт кандидатов, но отсутствие записи не означает невозможность задачи |
| Строгий catalog guard в blueprint | REMOVE AS BOUNDARY | Неизвестная стадия должна стать `generate`, `llm_worker`, `acquire` или `human`, а не ошибкой плана |
| Composer flow | ADAPTER | Декларативный flow мигрирует в UPC-граф, не получает отдельный runtime |
| LangChain | DO NOT ADOPT | Не нужен и создаёт лишнюю абстракцию вокруг нативных экспертов Extella |
| LangGraph | DO NOT ADOPT IN PRODUCTION | Его persistence/interrupt дублируют текущую сессию и checkpoint. Допустим только изолированный spike; UPC от него не зависит |

Главный архитектурный вывод: новая система не заменяет доказанный agentic Builder. Она превращает его
из «решить всю задачу одним экспертом» в один из исполнителей шага версионируемого графа.

## 2. Канонические артефакты

На сессию сохраняются:

- `<sid>.json` — пользовательские решения и ссылки на активный процесс;
- `<sid>_process.json` — канонический Process Graph и последний checkpoint;
- `<sid>_process_events.jsonl` — append-only журнал переходов, разрешений и приёмки;
- `runs/<build_id>/process/steps/<step_id>/v<version>/` — входы, StepResult, доказательства,
  артефакты, вердикты и repair history;
- `<sid>_blueprint.json` и `<sid>_build_plan.json` — совместимые представления для старого UI и releases.

Сессия получает только добавочные поля:

```json
{
  "process_contract": {
    "schema": "upc/1.0",
    "path": "..._process.json",
    "process_id": "proc_...",
    "active_version": 1,
    "active_run_id": "run_...",
    "status": "running|succeeded|failed|blocked_human|cancelled",
    "updated_at": "ISO-8601"
  }
}
```

Любая запись сессии остаётся под per-session lock. Сайдкары записываются атомарно через temporary
file + rename. Событийный журнал добавляется только после успешного сохранения нового checkpoint.

## 3. Process Graph v1

```json
{
  "schema": "upc/1.0",
  "process_id": "proc_<stable id>",
  "session_id": "wz_...",
  "version": 1,
  "parent_version": null,
  "title": "Человекочитаемое имя",
  "goal": "Наблюдаемый конечный результат",
  "task_contract_ref": {"sha256": "...", "path": "..."},
  "source_model_ref": {"sha256": "...", "path": "..."},
  "entry_step_ids": ["s001"],
  "terminal_step_ids": ["s005"],
  "steps": [],
  "edges": [],
  "budgets": {
    "max_steps": 40,
    "max_dynamic_steps": 20,
    "max_depth": 5,
    "max_total_attempts": 80,
    "max_step_attempts": 4,
    "max_wall_seconds": 14400,
    "max_llm_calls": 120,
    "max_generated_experts": 40
  },
  "permissions": {},
  "memory_policy": {},
  "created_at": "ISO-8601",
  "updated_at": "ISO-8601"
}
```

`steps` — нормальный DAG; `edges` хранят причинную связь и условие. Для иерархии шаг может содержать
`subgraph_ref`, а не неограниченно раздувать верхний граф. Динамическое добавление допустимо только
как новая версия графа с `parent_version`, причиной и лимитом глубины. Цикл разрешён только как
ограниченный retry одного шага; произвольные циклы графа запрещены валидатором.

## 4. Step Contract v1

Каждый шаг обязан иметь все поля ниже. Поле может быть пустым только там, где это явно разрешено.

```json
{
  "id": "s001",
  "title": "Разобрать входные файлы",
  "purpose": "Как этот шаг приближает бизнес-цель",
  "dependencies": [],
  "input_contract": {
    "artifacts": [],
    "data_schema": {},
    "required": true
  },
  "output_contract": {
    "artifacts": [],
    "data_schema": {},
    "postconditions": []
  },
  "implementation": {
    "mode": "reuse|generate|llm_worker|acquire|human|delegate",
    "capability_ref": null,
    "expert_ref": null,
    "subgraph_ref": null,
    "why": "Почему выбран этот режим"
  },
  "permissions": {
    "read": [],
    "create": [],
    "move": [],
    "modify": [],
    "delete": [],
    "install": [],
    "send": [],
    "external_write": []
  },
  "acceptance": {
    "deterministic_checks": [],
    "semantic_criteria": [],
    "required_artifacts": [],
    "minimum_confidence": 0.7
  },
  "retry_policy": {
    "max_attempts": 4,
    "backoff_seconds": [0, 2, 5, 15],
    "repair_on": ["expert_error", "contract_violation", "acceptance_failed"],
    "human_on": ["permission_required", "ambiguous_owner_decision"]
  },
  "status": "pending",
  "attempts": [],
  "version": 1,
  "output": null,
  "evidence": [],
  "error": null,
  "memory_refs": [],
  "created_at": "ISO-8601",
  "started_at": null,
  "finished_at": null,
  "updated_at": "ISO-8601"
}
```

Режимы:

- `reuse` — вызвать существующего эксперта/плагин/модель по точному capability ref;
- `generate` — Builder создаёт или ремонтирует CSPL/fython-эксперта этого шага;
- `llm_worker` — Qwen является самим исполняемым шагом и возвращает структурированный StepResult;
- `acquire` — подобрать внешнюю способность, но установка/подключение проходит permission gate;
- `human` — шаг невозможно честно автоматизировать без решения или действия человека;
- `delegate` — контролируемый эксперт создаёт bounded subgraph или дочерних экспертов.

Наличие `capability_ref` не обязательно для `generate`, `llm_worker` и `human`. Поэтому неизвестная
задача остаётся исполнимой даже при пустом каталоге.

## 5. Статусы и допустимые переходы

Канонические статусы шага:

`pending`, `ready`, `running`, `succeeded`, `failed`, `repairing`, `blocked_human`, `skipped`,
`stale`, `cancelled`.

Допустимые переходы:

```text
pending -> ready | skipped | cancelled
ready -> running | blocked_human | cancelled
running -> succeeded | repairing | failed | blocked_human | cancelled
repairing -> running | failed | blocked_human | cancelled
succeeded -> stale
stale -> ready | skipped | cancelled
blocked_human -> ready | cancelled
failed -> repairing | ready | cancelled
```

Инварианты:

1. `ready` только если все обязательные зависимости `succeeded` и их версии актуальны.
2. `succeeded` только после успешного StepResult, детерминированных проверок и, если требуется,
   семантической приёмки.
3. Изменение принятого шага создаёт новую версию; старый результат не перезаписывается.
4. Изменение output-контракта или результата помечает зависимые принятые шаги `stale`, но не удаляет их.
5. Независимые `succeeded`-шаги при ремонте другой ветки не переигрываются.
6. `blocked_human` — сохранённый терминальный checkpoint, а не ошибка и не вечный running.
7. После рестарта `running` не объявляется успехом: он становится `ready` для идемпотентного шага или
   `blocked_human` для шага с неподтверждённым внешним эффектом.

## 6. StepResult и три уровня успеха

Listener `completed` означает только доставку ответа. UPC требует три независимых уровня:

1. `transport`: задание доставлено и ответ получен;
2. `expert`: эксперт вернул структурированный `status=success` без runtime error;
3. `acceptance`: наблюдаемые постусловия и артефакты подтверждены.

```json
{
  "schema": "upc-step-result/1.0",
  "step_id": "s001",
  "step_version": 1,
  "attempt": 1,
  "transport": {"status": "completed", "task_id": "..."},
  "expert": {"status": "success|error", "expert_ref": "...", "message": ""},
  "output": {},
  "artifacts": [{"path": "...", "sha256": "...", "bytes": 123}],
  "evidence": [{"criterion": "...", "passed": true, "evidence": "..."}],
  "metrics": {},
  "error": null,
  "started_at": "ISO-8601",
  "finished_at": "ISO-8601"
}
```

Следующие сигналы всегда принудительно делают `expert.status=error`, даже если транспорт завершён:

- строка `[Execution Error]`;
- `Traceback`, `NameError`, `EOFError` и другие необработанные исключения;
- отсутствие обязательного файла или пустой артефакт;
- несовпадение `step_id/version`;
- невалидный JSON StepResult;
- `status=success` при `passed=false` в обязательной проверке.

Детерминированные проверки выполняются первыми: существование/хэш/размер/схема/счётчики/разрешения/
idempotency. LLM judge используется только для смысловых критериев и не может отменить провал
детерминированного гейта.

## 7. Цикл исполнения шага

```text
resolve ready step
  -> resolve implementation mode from Registry inventory
  -> permission preflight
  -> build/reuse/LLM/delegate expert
  -> execute
  -> normalize StepResult
  -> deterministic verify
  -> semantic judge when required
  -> accept OR record lesson and repair OR block human OR fail
  -> atomic checkpoint
  -> unlock newly ready steps
```

Repair локален шагу. Следующая версия получает Task Contract, входы, последний StepResult, точную
диагностику, проверенную память и rejected lessons. Одинаковый code hash после той же диагностики не
считается ремонтом. После исчерпания бюджета шаг становится `failed` или `blocked_human`; весь граф
не начинается заново.

## 8. Память v1

Типы: `evidence`, `lesson`, `concept`, `rule`, `artifact`.

```json
{
  "id": "mem_...",
  "kind": "concept",
  "status": "candidate|verified|rejected|superseded",
  "text": "...",
  "scope": "attempt|step|run|process|agent|workspace",
  "source": {"type": "owner|deterministic_gate|llm_judge|expert", "ref": "..."},
  "evidence_refs": ["..."],
  "confidence": 0.9,
  "step_id": "s001",
  "step_version": 1,
  "attempt": 1,
  "supersedes": null,
  "created_at": "ISO-8601"
}
```

Правила продвижения:

- прямое правило владельца — `verified`, source=`owner`;
- факт из детерминированного гейта — `verified` с evidence ref;
- вывод LLM до приёмки — только `candidate`;
- failed attempt создаёт `lesson/rejected`, но не положительный факт;
- память процесса/агента публикуется только после acceptance соответствующего шага;
- конфликт не затирает память: новая запись `supersedes` старую;
- следующий шаг получает только релевантную память по dependencies/scope, а не весь бесконечный лог.

## 9. Полномочия и внешние эффекты

Каждый шаг декларирует `read/create/move/modify/delete/install/send/external_write`.

- `read` в одобренной области может выполняться без подтверждения;
- `create` в отдельном output_dir — без подтверждения, если не затрагивает пользовательские данные;
- `move/modify/delete/install/send/external_write` требуют preview и явного approval, если ранее не
  выдана точная ограниченная политика;
- approval привязан к step id, version, target и нормализованному payload hash;
- повтор после рестарта проверяет idempotency key и журнал эффекта;
- для `move/modify/delete` сохраняется rollback manifest, где это технически возможно;
- секреты не записываются в Process Graph, events или память.

## 10. Controlled recursive / meta-experts

`delegate` может создать дочерний subgraph или экспертов, если:

- указан `parent_step_id`, глубина и оставшийся бюджет;
- имена экспертов находятся в namespace процесса;
- число динамических шагов, LLM-вызовов и созданных экспертов не превышает budget;
- fingerprint `(goal, input hashes, parent chain)` не встречался в цепочке — защита от цикла;
- каждый дочерний шаг имеет свой контракт, StepResult и acceptance;
- дочерний эксперт не расширяет полномочия родителя;
- установка или внешний эффект всё равно останавливаются на human gate.

Эксперт «создать другого эксперта» не является доказательством выполнения бизнес-шага. Успех наступает
только после прогона и приёмки результата дочернего графа.

## 11. Planner и Capability Registry

Planner сначала декомпозирует бизнес-цель в наблюдаемые шаги и зависимости, затем ищет реализацию.
Registry возвращает кандидатов и evidence их совместимости. Он не фильтрует возможные задачи.

Алгоритм выбора режима:

1. точное доказанное совпадение — `reuse`;
2. вычислимый/интеграционный шаг — `generate`;
3. смысловой шаг, где Qwen и есть исполнитель — `llm_worker`;
4. внешняя модель/репозиторий/инструмент — `acquire`;
5. решение, доступ или опасное действие владельца — `human`;
6. сложная ограниченная подзадача — `delegate` с subgraph.

До 40 статических шагов поддерживаются обязательным контрактом. Большие задачи используют subgraphs.
План без capability из каталога валиден, если каждый шаг получил один из перечисленных modes.

## 12. Единая модель четырёх поверхностей

- Wizard создаёт Task Contract, Process Graph и показывает step ledger.
- Chat может создать/уточнить тот же Process Graph; сообщение пользователя становится решением или
  human answer, а не отдельной автоматизацией.
- Composer — быстрый вход: его flow нормализуется в Process Graph, а отсутствующий блок маршрутизируется
  в `generate/llm_worker/acquire`, не в тупик.
- Workspace показывает и запускает те же releases/runs/checkpoints и Registry refs.

Все поверхности вызывают один planner/runtime API и читают один Process Graph. Поверхность хранится как
`origin`, но не меняет семантику процесса. Эксперт, плагин, CSPL-handler, Qwen или модель, появившиеся в
Registry, доступны всем поверхностям согласно permissions и target passport.

## 13. Обратная совместимость

- Старый blueprint мигрирует: каждая stage становится Step Contract; известные asset/capability → `reuse`,
  неизвестные/пустые → `generate`; knowledge stage → `reuse`; owner gap → `human` или `acquire`.
- Старый Build Plan мигрирует по `depends_on`; `action=build` → `generate`, `reuse/parameterize` → `reuse`.
- Старый agentic one-expert release читается как граф из одного `generate`-шага.
- Старые orchestrators и run records продолжают исполняться без перепаковки.
- Новый runtime не переписывает старый release; новая версия появляется только после явной пересборки.

## 14. UI в существующем Wizard

Новая отдельная поверхность не создаётся. В текущем прогрессе стройки показываются:

- общий счётчик шагов и статус процесса;
- строка на каждый шаг: номер, title, mode, status, attempt/version;
- раскрытие: входы, выходы, эксперт, доказательства, ошибка и память;
- для `blocked_human` — один конкретный вопрос/preview и действия «ответить/подтвердить/отменить»;
- для failed — «ремонтировать этот шаг»; для stale — причина устаревания;
- после рестарта — «продолжить с checkpoint», без повторения `succeeded`.

## 15. Acceptance matrix

Обязательные детерминированные сценарии:

1. Очистка Downloads: unknown task → generate; move/delete блокируются approval; dry-run принимается.
2. Файл → Telegram: обработка проходит, send блокируется без token/approval, после ответа продолжается.
3. Excel/PDF: ошибочная нормализация отклоняется; ремонт только failing step; upstream не повторяется.
4. Параллельные ветки сходятся в merge после обеих зависимостей.
5. Падение середины сохраняет принятые шаги и restart resume.
6. `CSPL=LLM`: llm_worker возвращает StepResult и проходит semantic gate.
7. `delegate`: bounded subgraph, cycle detection и budgets.
8. Restart в `running`: безопасный resume или human reconciliation внешнего эффекта.
9. Негативные сигналы `[Execution Error]`, traceback, missing artifact, permission escalation никогда не success.
10. Старый blueprint/build/orchestrator/run остаётся читаем и исполним.

Вертикальный E2E считается пройденным только если неизвестная задача проходит:

`interview → blueprint → Process Graph → generate/llm_worker → expert creation → execution → acceptance
→ local repair → verified memory → package`, при этом принятые независимые шаги не повторяются.

## 16. LangGraph spike decision

Критерии spike: библиотека должна исполнять существующий UPC без собственного внешнего формата состояния,
не менять экспертов, не становиться вторым источником checkpoint и давать измеримое преимущество в
pause/resume или parallel scheduling. Текущий аудит уже показывает дублирование: Extella имеет сессию,
events, build progress, owner checkpoint, экспертный runtime и recovery.

Решение v1: production runtime реализуется напрямую поверх UPC и существующих примитивов. LangGraph не
добавляется в зависимости и не входит в release. Если позже отдельный spike пройдёт критерии, он сможет
быть внутренним scheduler adapter; Process Graph, StepResult, память и UI от него не изменятся.
