# Обновление Extella для коллег и Мергуль

Единый апдейтер тянет свежие **визард + workspace + карточки приложений** и (если есть доступ
к приватному репо) **тулбар**. Идемпотентен, пользовательские данные (сессии, config.json,
vault.key, логи) не трогает.

## Быстрый способ (одна строка)

```bash
curl -fsSL https://raw.githubusercontent.com/AnvarBakiyev/extella-adoption-wizard/main/extella-update.sh | bash
```

После завершения — **Cmd+Q Extella и открыть заново** (применятся тулбар и карточки на
Plugins-главной: Композитор, Студия языков, Команда).

## Что делает скрипт

1. **Визард** (публичный репо) — git pull/clone, выкладка `ui/*.py` + `wizard.html` в
   `~/extella_wizard/app/`, регистрация карточек приложений, перезапуск моста (порт 8765).
2. **Workspace** — выкладка `dist/workspace/*` в `~/extella-plugins/workspace/`, перезапуск
   сервера (порт 34767).
3. **Тулбар** (приватный репо) — только если есть клон/доступ: git pull → `node build.js` →
   выкладка `toolbar.js` в Extella Desktop. Сборка занимает 1–2 минуты — это нормально.

## Требования
- Python 3.12 (`python3.12` или `python3`).
- Для шага тулбара — Node.js и **доступ к приватному репо** `AnvarBakiyev/extella-toolbar-src`.

## Тулбар без доступа к приватному репо
Если доступа нет, скрипт обновит визард+workspace и честно скажет про тулбар. Чтобы получить
тулбар, Анвар выдаёт read-доступ:
```bash
gh api -X PUT repos/AnvarBakiyev/extella-toolbar-src/collaborators/<GITHUB_USERNAME> -f permission=pull
```
После этого коллега один раз клонирует и дальше обновляется скриптом:
```bash
git clone https://github.com/AnvarBakiyev/extella-toolbar-src.git ~/extella-toolbar-src
```

## Настройка путей (необязательно)
Скрипт читает переменные окружения, если клоны лежат не по умолчанию:
- `EXTELLA_WIZARD_SRC` — клон визарда (по умолчанию `~/extella-adoption-wizard`).
- `EXTELLA_TOOLBAR_SRC` — клон тулбара (по умолчанию `~/extella-toolbar-src`).
