# SESSION_SCHEMA v1 — контракт сессии `~/extella_wizard/sessions/wz_*.json`

Сессия — **стержень всей системы**: единственный источник решений и истории процесса.
Схема собрана **по фактическим данным** (union полей 22 живых сессий, 16.07.2026) + код.
Проверка: `python3 scripts/validate_sessions.py`.

## Правила эволюции (нарушать нельзя)
1. **Только добавление полей.** Переименование/удаление — через явную миграцию с бампом `schema`.
2. Новое поле — **опциональное** (читатели обязаны переживать его отсутствие: `s.get(...)`).
3. Запись — **только через `_update_session(sid, mutate)`** (per-sid lock; прямой `write_text` сессии
   вне моста запрещён — гонки с тиком/чатом).
4. Любое изменение схемы — строка в этот файл в том же PR.

## Идентичность и жизненный цикл
| Поле | Тип | Кто пишет | Смысл |
|---|---|---|---|
| `session_id` | str | wz_session create | `wz_YYYYMMDD_<hex>`; SAFE_ID `[A-Za-z0-9_-]+` |
| `schema` | int | wz_session create | версия схемы (v1; отсутствие = легаси v0, читается как v1) |
| `client_name` | str | create / доводка C2 | имя процесса (может нести ведущий эмодзи) |
| `stage` | str | set_stage | `interview → blueprint → built → launched` (+`intake/spec/audited/test/slice_accepted`) |
| `created_at`/`updated_at` | str ISO | все писатели | |
| `log` | list | wz_session | событийный журнал `{ts, event}` |

## Интервью (этапы 0–2)
| Поле | Тип | Кто пишет | Смысл |
|---|---|---|---|
| `answers` | dict | wz_session save_answers | `{qid: {question, answer, updated_at}}`; qid свободный (P2: кастомные вопросы легальны) |
| `questionnaire` | list | /x/gen_questions (P2) | адаптивные вопросы `[{id,q,hint}]`; отсутствие = статичный QUESTIONS |
| `questionnaire_task` | str | /x/gen_questions | задача, под которую сгенерён конспект |
| `comments` | list | UI-треды | комментарии к пунктам |
| `files` | list | /x/upload | файлы-образцы |
| `blueprint_path`/`spec_path`/`build_plan_path` | str | генераторы | пути сайдкаров (`<sid>_blueprint.json` и т.п.) |

## Стройка и версии (этапы 3–4, C4.2/C6/F1)
| Поле | Тип | Кто пишет | Смысл |
|---|---|---|---|
| `builds` | list | _run_build / flow_save | версии сборок; `builds[-1]` — действующая: `{build_id, at/built_at, experts[], orchestrator, audit{verdict}, source_file?, flow_id?, components_human?, composed?, revised_at?}` |
| `decisions` | list | /x/rebuild, /x/rollback | **канон F2**: журнал решений `{at, change, stage_id?, stage_title?, decision, by: builder-chat|rollback}` |
| `blueprint_history` | list | /x/rebuild (C6) | снапшоты blueprint ДО правок (последние 5) `{at, blueprint, before_change}` — фундамент отката |
| `building` | str | rebuild/rollback (F1) | build_id активной стройки; гард от параллельной; снимается в finally / recovery |
| `audit`, `data_check`, `tasks` | dict | стройка/аудит | вспомогательные артефакты стройки |

## Прод и жизнь процесса (этапы 6–7, C1–C3)
| Поле | Тип | Кто пишет | Смысл |
|---|---|---|---|
| `schedule` | dict | /x/schedule | `{period, interval_min, set_at, orchestrator, target}`; исполнение — KV `sched:<sid>` |
| `paused`/`paused_at`/`resumed_at` | bool/str | /x/pause (C1) | источник статуса паузы для UI (KV active — исполнение) |
| `recipients` | list | /x/recipients | каналы доставки `[telegram, email, …]` |
| `message_template` | str | /x/message_template | шаблон доставки, плейсхолдеры `{name}{count}{sum}{date}` |
| `rules`/`fields` | list/dict | /x/rules | правила словами + поля владельца (композиции применяют на лету; Мастер-процессы — при пересборке) |
| `rules_struct` | list | /x/rules (F2) | скомпилированные из `rules` машинные фильтры `[{field, op: >|>=|<|<=|==|contains, value}]` — Qwen интерпретирует ОДИН раз при записи, оркестратор применяет детерминированно на каждом прогоне |
| `target_requirements` | dict | /x/target_requirements (T2) | требования процесса к устройству `{apps:[...], local_only: bool, device: <slug>}` — preflight (`_target_preflight`) проверяет ДО прогона/расписания против паспортов `target:passport:*` |
| `source` | dict/None | /x/source_bind | привязанный источник данных `{kind, …}` |
| `inbound` | dict | /x/inbound | приём входящих `{mode, channel, target, …}`; исполнение — KV `inbound:<sid>` |
| `runs` | list | /x/run_process | ручные прогоны `{at, status, findings?/total_*?, digest_source?}`; расписание пишет в `sched:<sid>.runs` |
| `production_agent` | dict | wz_deploy_agent | продовый Qwen-агент `{agent_id, …}` (заморожен F2) |
| `panel_url`/`panel_name` | str | пак | родная доменная панель (Travel-класс) |
| `panel_manifest` | dict | /x/gen_panel | сгенерированные доменные поля |
| `published` | dict | /x/publish | `{pack_id, repo_url, at}` |
| `goal` | str | flow_save / доводка | описание для карточек |
| `demo_runs` | list | демо-раннер | исторический артефакт |


## Run-record v1 (F3 — единая история исполнения)
Прогоны пишутся в два хранилища (ручные — `s.runs` мостом; по расписанию — `sched:<sid>.runs` тиком),
но читаются ТОЛЬКО через `_runs_unified(s, skv)` (мост): dedup по `at[:19]`, нормализация. Контракт записи:
`{at: ISO, status: success|partial|error|…, trigger: manual|schedule|inbound, …счётчики (findings/total_count/total_sum), digest_source?, flow_id?, report_xlsx?}`.
Старые записи без `trigger` читаются как `manual`. Дайджест прогона — отдельно в KV `digest:<sid>` (см. KV_REGISTRY).
Новые читатели истории обязаны использовать `_runs_unified`, не склеивать сами.

## Сайдкары (та же папка, тот же sid)
`<sid>_blueprint.json` `{session_id, generated_at, blueprint{process_name, archetype, goal, stages[], …}}` ·
`<sid>_build_plan.json` · `<sid>_chat.json` (стенограмма Помощника) · `sessions_archive/` (удалённые — архив, не hard-delete).
