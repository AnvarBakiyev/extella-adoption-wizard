# DEV HANDOFF — Adoption Wizard (визард внедрения)

Практическая вводная для разработчика, который перехватывает разработку визарда.
Читается за 10 минут → можно править и деплоить. Продуктовое описание — в `README.md`,
установка в аккаунт Extella — в `INSTALL.md`, архитектурные решения — в `docs/`.

---

## 1. Что это (3 части)

Визард — это **не бандл**, а три живых слоя. `wizard.html` — прямой исходник (правится как есть,
шага сборки нет).

```
┌─ ui/wizard.html ──────────┐   HTTP     ┌─ ui/server.py (мост) ─────┐   run_expert   ┌─ платформа Extella ─┐
│ весь UI: HTML+инлайн       │ ─────────► │ HTTP-сервер 127.0.0.1:8765│ ─────────────► │ эксперты wz_* +      │
│ JS+CSS (одна страница)     │ ◄───────── │ держит токен, эндпоинты   │ ◄───────────── │ агенты (Qwen) + KV  │
└───────────────────────────┘  /x/*       │ /x/*, зовёт эксперты      │                └─────────────────────┘
                                          └───────────────────────────┘
```

- **`ui/wizard.html`** (~300К) — весь интерфейс. Общается с мостом по HTTP на `127.0.0.1:8765` (`/x/*`).
- **`ui/server.py`** (~168К) — локальный **мост**: HTTP-сервер, хранит токен Extella, отдаёт `wizard.html`,
  реализует эндпоинты `/x/*`, зовёт платформенные эксперты через `run_expert`. Версия — `BRIDGE_VERSION`
  (бампать при изменениях сервера). Хелперы моста: `ui/wz_platform.py` (CONFIG, `qwen_agent()`,
  `run_expert`, заголовки), `ui/wz_llm.py`, `ui/wz_build.py`.
- **`experts/wz_*.py`** (42 шт.) — платформенные эксперты (fython/nohup), исполняются НА платформе или на
  устройстве-таргете. Заливаются в аккаунт через `scripts/sync.py`. Источник правды — репо.

---

## 2. Запустить у себя (первый раз, на новой машине)

```bash
git clone https://github.com/AnvarBakiyev/extella-adoption-wizard.git
cd extella-adoption-wizard

# рантайм-папка моста (НЕ в репо — там секреты и деплой-копия)
mkdir -p ~/extella_wizard/app
cp ui/server.py ui/wz_platform.py ui/wz_llm.py ui/wz_build.py ui/wizard.html ~/extella_wizard/app/

# конфиг с твоим токеном Extella (СЕКРЕТ, не коммитить!)
cat > ~/extella_wizard/app/config.json <<'JSON'
{
  "auth_token": "<твой токен с api.extella.ai>",
  "api_base": "https://api.extella.ai",
  "agent_id": "<id платформенного Qwen-агента>",
  "llm_agents": ["<qwen agent 1>", "<qwen agent 2>"]
}
JSON

# зависимости моста (нужен Python 3.12 — в 3.9 нет cryptography → vault падает)
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 -m pip install cryptography requests

# запуск
/Library/Frameworks/Python.framework/Versions/3.12/bin/python3 ~/extella_wizard/app/server.py
# открыть в браузере: http://127.0.0.1:8765
```

Проверка живости: `curl http://127.0.0.1:8765/x/health` → `{"status":"ok","version":"...","pid":...}`.

*(На машине Анвара мост поднят как LaunchAgent `ai.extella.wizard-bridge` — автостарт. Для себя можно
не заводить, а запускать `server.py` руками.)*

---

## 3. Цикл разработки (каждый день)

**Правишь UI (`ui/wizard.html`):**
```bash
cp ui/wizard.html ~/extella_wizard/app/wizard.html   # → обновить страницу в браузере, рестарт не нужен
```

**Правишь мост (`ui/server.py` или `ui/wz_*.py`):**
```bash
# 1) бампни BRIDGE_VERSION в ui/server.py
cp ui/server.py ui/wz_*.py ~/extella_wizard/app/
# 2) рестарт (ОБЯЗАТЕЛЬНО python3.12 — launchd так и настроен):
launchctl kickstart -k gui/501/ai.extella.wizard-bridge
# (если запускаешь руками — просто перезапусти server.py тем же python3.12)
```
> ⚠️ Не рестартуй мост системным `python3` (3.9) — в нём нет `cryptography`, vault упадёт
> `ModuleNotFoundError`. Всегда 3.12.

**Правишь эксперт (`experts/wz_*.py`):**
```bash
python3 scripts/sync.py diff            # что разошлось репо↔платформа
python3 scripts/sync.py push <name> --yes   # прошить эксперт(ы) на платформу (МЕНЯЕТ платформу)
python3 scripts/sync.py pull            # забрать правки, сделанные на платформе, обратно в репо
```
Репо — источник правды по коду. `push` прошивает репо→платформу.

---

## 4. Канон платформы — ГРАБЛИ (нарушение = сломанный прод)

Это правила Extella, не визарда. Их легко нарушить и получить «на моём аккаунте работало, у клиента нет».

- **Клиентская LLM = платформенный Qwen, НИКОГДА не Claude.** Диспетч по `agent_id` В ТЕЛЕ запроса
  через `qwen_agent()` (резолвит живой Qwen из `config.llm_agents`, пропуская Claude и мёртвые id),
  **не** через заголовок `X-Agent-Id`. BYOK/gpt-4o запрещены (нечестный тест).
- **Общий KV — только с `global: true`.** Шаренные каталоги (`composer:catalog`, `_mkt_automations`,
  `_mkt_*`) сеются как global → читать/писать ОБЯЗАТЕЛЬНО с `"global": True`, иначе на свежем аккаунте
  HTTP 500 «Key not found». (Реальный баг, чинили 13.07.) Per-account ключи (`flow:`, `connlog:`,
  `sec:`, `base`/`base_key`, `lastrun:`) — без флага, это нормально.
- **Скоупинг:** эксперты/концепты изолированы per-agent; общее держать global. Один `expert_name` в
  нескольких скоупах = недетерминированный запуск.
- **REST:** `https://api.extella.ai`, заголовки `X-Auth-Token` / `X-Profile-Id: default` / `X-Agent-Id`.
  Пути в ЕД. числе: `/api/agent/*`, `/api/expert/*`; список экспертов — `/api/experts_db/list`.
  `/api/agent/run` требует поле **`input`** (не `message` → 422); ответ = Responses-API (`output=[...]`,
  текст в элементах `type=="message"`).
- **Агенты по API:** правило «**один инструмент за ход**» (баг склейки аргументов); длинные задачи дробить
  (лимит ~50 итераций/ход); HTTP 500/timeout ≠ провал — проверять по артефактам; сообщения агентам делать
  самодостаточными (`previous_response_id` теряется).
- **nohup-эксперты:** сырой python БЕЗ `$extens`/`def`/`return` сверху; `{{placeholder}}` подставляются
  ТОЛЬКО для явно переданных параметров (kwargs-дефолты НЕ подставляются) → фолбэки в коде
  `if not X or X.startswith("{{")`. Секреты — фолбэк из `~/extella_wizard/app/config.json`, иначе честный fail.
- **Зависимости экспертов:** только `include("import X", ["extella-pip install X"])` — голый
  `try/except ImportError` умирает на чистой среде.
- **Синтез Qwen:** для чистого ответа слать `tool_choice:"none"` (иначе агент лезет звать MCP-тулы и жжёт
  токены); формат вывода — plain-text с разделителем, не `json_object` (fine-tune отдаёт кривой JSON).
- **Продовые агенты заморожены (F2).** Изменения процессов — через Строителя с записью решения в сессию
  (`~/extella_wizard/sessions/`). Письма/внешняя запись — только черновики, отправляет человек.

---

## 5. Секреты

- `~/extella_wizard/app/config.json` — токен Extella, **локальный, НЕ в репо, не коммитить.**
- Токен в логи/чаты не печатать (в мосту есть `_scrub`).
- OpenAI-ключ клиентским агентам запрещён (только служебная панель).

---

## 6. Живые объекты и хостинг

- **Мост:** `127.0.0.1:8765`, LaunchAgent `ai.extella.wizard-bridge`, деплой-копия `~/extella_wizard/app/`.
- **Планировщик:** VPS PS.kz `82.115.42.21`, cron `*/15 * * * * tick.py` (гоняет автоматизации визарда по
  расписанию). Не переустанавливать без запроса.
- **Ключевые агенты** (не удалять): Визард `agent_hM0qLHwu-Hw_4sjydTU1g`, Строитель
  `agent_FLYxB0v1qIY2phB5beVP5`.
- Полный канон и список живых объектов — у владельца (файл `CLAUDE.md` в родительском проекте, в этот
  репо он не входит).

---

## 7. Куда смотреть дальше

| Нужно | Файл |
|---|---|
| Продуктовое «что это» | `README.md` |
| Установка в аккаунт Extella (по ссылке) | `INSTALL.md` |
| Архитектурные ТЗ / решения | `docs/` |
| История изменений | `CHANGELOG.md` |
| Эксперты — заливка/сверка | `scripts/sync.py` (diff/pull/push) |
| Смоук-проверка | `scripts/smoke.py` |
