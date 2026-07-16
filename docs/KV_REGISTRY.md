# KV_REGISTRY v1 — реестр KV-ключей платформы (визард и связанные)

**Зачем.** KV Extella скоупится по `X-Profile-Id` + `X-Agent-Id`. Один и тот же ключ под разными
агентами — **разные записи** («тени»). Этот класс ошибок уже стрелял трижды: баг Мергуль
(500 «Key not found» — чтение global-ключа без `global:true`), карточки-тени `_mkt_automations`
(запись под агентом пака → витрина не видит), тест C1 в скоупе Визард-агента. Реестр = единственный
источник правды о скоупе каждого ключа. Проверка кода: `python3 scripts/kv_lint.py`.

## Правила (нарушать нельзя)
1. **Global-ключи** читаются и пишутся ТОЛЬКО с `"global": True` **и под `X-Agent-Id: agent_extella_default`**
   (иначе агент-тень; урок `_mkt_automations`).
2. **Per-account ключи** — БЕЗ `global` (флаг не нужен и вреден).
3. Новый ключ — строка в этот реестр в том же PR + префикс из существующих семейств (или новое семейство осознанно).
4. Потолок значения ~28КБ: большие каталоги шардировать (урок `_mkt_*` 614 карточек), истории — обрезать (`runs[-10:]`, `seen[-200:]`, `blueprint_history[-5:]`).
5. RMW общих лент (`_mkt_automations`) — best-effort кэш; истина остаётся в первоисточнике (sched/сессия).

## Global-скоуп (`global:true` + agent_extella_default)
| Ключ | Писатели | Читатели | Значение |
|---|---|---|---|
| `_mkt_automations` | wz_auto_compose, wz_publish_pack, /x/pause (кэш), /x/automation_delete | тулбар-витрина, /x/automations (identity) | `{items:[card]}`; карточки автоматизаций/паков |
| `_mkt_installed` | wz_capability_install | вкладка «Мои» | `{items:[…60]}` — установленное Композитором (per-account по смыслу, живёт global — легаси) |
| `_mkt_models`, `_mkt_cli_catalog`, `_mkt_programs`, `_mkt_skills`, … | харвестеры VPS | тулбар | каталоги витрины (шардированы) |
| `composer:catalog` | сидер | wz_auto_compose | whitelist блоков Композитора |
| `capability:registry` (+`:0..N`) | эксперт wz_registry_rebuild (у клиента; событийно из моста `_registry_refresh_async` + суточно тиком) | /x/registry (мост → все 4 поверхности) | Capability Registry v0: meta `{chunks, enc:"b64", count, generated_at}` + b64-шарды по 8000 (kv/set строит эмбеддинг — крупные значения бьются об его лимит). Скоуп: под default-агентом БЕЗ флага global (писатель и читатель в одном скоупе). Зеркала для людей: docs/CAPABILITIES.md (наш git) и `~/extella_wizard/registry/CAPABILITIES.md` (устройство клиента) |
| `registry:last_rebuild` | wz_registry_rebuild | тик (суточная страховка пересбора) | ISO ts последнего полного пересбора реестра |

## Per-account скоуп (без global; писатель указан)
| Ключ | Писатели | Читатели | Значение |
|---|---|---|---|
| `sched:<sid>` | /x/schedule, /x/pause, тик (write-back с whitelist полей владельца: rules, fields, recipients, deliver, message_template, active, interval_min, period, source, flow_id, agent_id) | тик, /x/automations (_sched_kv_batch), монитор | конфиг расписания + `runs[-10:]`, `next_due_ts`, `active` |
| `sched:__index__` | /x/schedule, delete | тик | `{sids:[…]}` активных расписаний |
| `inbound:<sid>` | /x/inbound, /x/pause, тик (offset/seen/drain) | тик, монитор | приём входящих: `{mode, channel, active, offset, seen[-200:], drain_once?, skipped_backlog?}` |
| `inbound:__index__` | /x/inbound, /x/automation_delete | тик | `{sids:[…]}` |
| `inbq:<sid>` | webhook-шлюз / инъекция | тик (дренаж) | очередь событий |
| `hookmap:<token>` | /x/inbound webhook, /x/pause | шлюз | маршрут вебхука `{active,…}` |
| `flow:<id>` | wz_auto_compose (стабильный id при reuse_flow_id — C2) | wz_flow_run, /x/flow, publish C5 | план композиции `{name, task, steps[], synthesis_prompt, installed, missing, composed_at}` |
| `digest:<sid>` | тик, /x/run_process (_save_digest) | /x/digest → виджет «Последний результат» | `{at, digest[:12000]}` — перезапись последнего |
| `lastrun:<имя>` (`lastrun:ci`, `lastrun:digest`, `lastrun:flow:<id>`) | оркестраторы | тик, монитор | счётчики последнего прогона |
| `connlog:<ns>:<канал>` | wz_connector_* | монитор (здоровье доставки) | `{ok, err?, at}` |
| `sec:<client>:<коннектор>` | vault (/x/secret_set) | коннекторы/источники | секреты (шифрованные; в логи не печатать) |
| `agent_runs:<id>` | wz_agent_runlog, тулбар | кабинет агента (тулбар) | история запусков (cap 200) |
| `agent_state:<id>` | тулбар | тулбар | клиентский слой состояния карточки |
| `ci:config`, `ci:knowledge_gaps` | ci_configure, ci_run_pipeline | ci_run_pipeline | конфиг и learn-петля Competitor Intelligence |

## Легаси-исключения (знать, не плодить)
- `sched:ci` — расписание Competitor Intelligence ставилось до штатного механизма: ключ НЕ `sched:<sid>`.
  `/x/pause` для CI гасит только флаг сессии. При случае — мигрировать на `sched:wz_20260708_cintel`.
- `_mkt_installed` — по смыслу per-account, живёт в global-скоупе (историческое). Не копировать паттерн.

## Как обращаться из кода
- Мост/UI-хелперы: `api("/api/kv/get|set", {...})` — заголовки уже `agent_extella_default` (wz_platform.HEADERS).
- Эксперты (fython): свои `_post` — **обязаны** передавать `"global": True` для global-семейств (см. правило 1).
- Новый код моста: используйте `kv_get(key)` / `kv_set(key, value, description)` из `wz_platform` —
  скоуп определяется по реестру автоматически (карта `KV_GLOBAL_PREFIXES`).
