#!/usr/bin/env bash
# Единый апдейтер Extella для коллег и Мергуль (визард + workspace + карточки + тулбар).
# Скачивание одной строкой (публичный репо визарда):
#   curl -fsSL https://raw.githubusercontent.com/AnvarBakiyev/extella-adoption-wizard/main/extella-update.sh | bash
# Идемпотентен. Пользовательские данные (сессии, config.json, vault.key, логи) НЕ трогает.
set -uo pipefail

WIZ_REPO="https://github.com/AnvarBakiyev/extella-adoption-wizard.git"
WIZ_DIR="${EXTELLA_WIZARD_SRC:-$HOME/extella-adoption-wizard}"
APP_DIR="$HOME/extella_wizard/app"
WS_DIR="$HOME/extella-plugins/workspace"
# приватный тулбар: путь к клону (если у коллеги есть доступ). Пусто → шаг пропускается.
TB_DIR="${EXTELLA_TOOLBAR_SRC:-$HOME/extella-toolbar-src}"
TB_DEPLOY="$HOME/Library/Application Support/extella-desktop/toolbar.js"

say(){ printf "\n\033[1m▸ %s\033[0m\n" "$*"; }
ok(){ printf "  \033[32m✓\033[0m %s\n" "$*"; }
warn(){ printf "  \033[33m!\033[0m %s\n" "$*"; }

PY="$(command -v python3.12 || command -v python3)"
[ -z "$PY" ] && { echo "нет python3 — установите Python 3.12"; exit 1; }

# ── 1. ВИЗАРД (публичный репо) ─────────────────────────────────────────────
say "Визард: обновляю исходники"
if [ -d "$WIZ_DIR/.git" ]; then
  git -C "$WIZ_DIR" pull --ff-only --quiet && ok "git pull ($WIZ_DIR)"
else
  git clone --quiet "$WIZ_REPO" "$WIZ_DIR" && ok "склонирован в $WIZ_DIR"
fi
mkdir -p "$APP_DIR"
cp "$WIZ_DIR"/ui/*.py "$WIZ_DIR"/ui/wizard.html "$APP_DIR"/ && ok "выложены ui/*.py + wizard.html → $APP_DIR"
"$PY" "$WIZ_DIR"/scripts/register_app_cards.py >/dev/null 2>&1 && ok "карточки приложений зарегистрированы" || warn "карточки: пропущено (нет ~/extella-plugins?)"

# перезапуск моста визарда (порт 8765)
if launchctl print "gui/$(id -u)/ai.extella.wizard-bridge" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/ai.extella.wizard-bridge" && ok "мост визарда перезапущен (launchd)"
else
  pid="$(lsof -tiTCP:8765 -sTCP:LISTEN 2>/dev/null)"; [ -n "$pid" ] && kill $pid 2>/dev/null; sleep 1
  ( cd "$APP_DIR" && nohup "$PY" server.py >/dev/null 2>&1 & ) && ok "мост визарда поднят (nohup)"
fi

# ── 2. WORKSPACE (завендорен в визард-репо) ────────────────────────────────
say "Workspace: обновляю"
if [ -d "$WIZ_DIR/dist/workspace" ]; then
  mkdir -p "$WS_DIR"
  cp "$WIZ_DIR"/dist/workspace/* "$WS_DIR"/ && ok "выложен → $WS_DIR"
  pid="$(lsof -tiTCP:34767 -sTCP:LISTEN 2>/dev/null)"; [ -n "$pid" ] && { kill $pid 2>/dev/null; sleep 1; ( cd "$WS_DIR" && nohup "$PY" server.py >/dev/null 2>&1 & ); ok "workspace-сервер перезапущен"; } || warn "workspace-сервер не запущен — поднимется из тулбара"
else
  warn "dist/workspace не найден — обновите визард-репо"
fi

# ── 3. ТУЛБАР (приватный репо — только если есть доступ/клон) ───────────────
say "Тулбар: обновляю (если есть доступ к приватному репо)"
if [ -d "$TB_DIR/.git" ]; then
  if git -C "$TB_DIR" pull --ff-only --quiet 2>/dev/null; then
    if command -v node >/dev/null 2>&1; then
      ( cd "$TB_DIR/toolbar" && node build.js >/dev/null 2>&1 ) \
        && cp "$TB_DIR/toolbar/build/toolbar.js" "$TB_DEPLOY" \
        && ok "тулбар собран и выложен → Extella Desktop" \
        || warn "сборка тулбара не удалась (node build.js)"
    else
      warn "нет node — тулбар не собрать. Установите Node.js и повторите."
    fi
  else
    warn "git pull тулбара не прошёл (нет доступа/расхождение) — обновите вручную"
  fi
else
  warn "клон приватного тулбара не найден ($TB_DIR)."
  warn "Нужен доступ к github.com/AnvarBakiyev/extella-toolbar-src (private)."
  warn "После доступа: git clone <repo> \"$TB_DIR\" && повторите этот скрипт."
fi

# ── 4. ACTIVITY CENTER: виджет «что делает Extella» ────────────────────────
# Панель приезжает вместе с тулбаром, а ДАННЫЕ для неё дают мост :8799 и наблюдатель, вшитый
# в среду листенера. Этот шаг ставил только install-all.sh (первичная установка) — коллеги,
# обновлявшиеся апдейтером, получали панель без источника: виджет молча показывал «фоновых
# задач нет» при работающем листенере (Гульжан, 19.07). Ставим здесь и ГОВОРИМ результат.
say "Activity Center: виджет «что делает Extella»"
if [ "$(uname)" != "Darwin" ]; then
  warn "только macOS — пропускаю"
else
  # Тянем НЕ по имени ветки, а по хешу коммита. raw.githubusercontent кэширует ветку на минуты:
  # 19.07 шаг скачал версию ДО только что сделанного пуша и поставил её поверх более свежей —
  # то есть откатил рабочий наблюдатель. Ссылка на коммит неизменяема и кэша не имеет.
  AC_REPO="AnvarBakiyev/extella-marketplace-pack"
  AC_SHA="$(curl -fsSL "https://api.github.com/repos/$AC_REPO/commits/main" 2>/dev/null \
            | "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("sha",""))' 2>/dev/null || true)"
  if [ -n "${AC_SHA:-}" ]; then
    ok "версия наблюдателя: ${AC_SHA:0:7}"
  else
    AC_SHA="main"
    warn "не удалось узнать точную версию (нет доступа к api.github.com) — беру ветку main;"
    warn "если только что был пуш, GitHub может отдать копию из кэша."
  fi
  AC_RAW="https://raw.githubusercontent.com/$AC_REPO/$AC_SHA/device"
  AC_TMP="$(mktemp -d)"
  mkdir -p "$AC_TMP/activity-center/bridge" "$AC_TMP/activity-center/instrumentation" "$AC_TMP/boot"
  ac_ok=1
  # Список = ровно то, что раскладывает install.py (см. sources в нём). Пропустить хоть один —
  # установщик упадёт на copy2; boot-скрипт необязателен, его отсутствие не ошибка.
  for f in activity-center/install.py activity-center/bridge/server.py \
           activity-center/bridge/activity_model.py activity-center/bridge/service_manager.py \
           activity-center/bridge/task_state.py \
           activity-center/instrumentation/extella_activity_hook.py boot/restart_local_servers.py; do
    curl -fsSL "$AC_RAW/$f" -o "$AC_TMP/$f" || { [ "$f" = "boot/restart_local_servers.py" ] || ac_ok=0; }
  done
  # Список выше должен покрывать всё, что install.py реально копирует. Если Codex добавит модуль,
  # а этот скрипт о нём не узнает — установщик упадёт на copy2. Спрашиваем сам установщик.
  if [ "$ac_ok" = "1" ]; then
    AC_MISS="$("$PY" - "$AC_TMP/activity-center" <<'PYEOF' 2>/dev/null || true
import os, re, sys
root = sys.argv[1]
src = open(os.path.join(root, "install.py"), encoding="utf-8").read()
m = re.search(r"sources\s*=\s*\((.*?)\)", src, re.S)
want = re.findall(r'root\s*/\s*"([^"]+)"\s*/\s*"([^"]+)"', m.group(1) if m else "")
print(" ".join(a + "/" + b for a, b in want if not os.path.exists(os.path.join(root, a, b))))
PYEOF
)"
    [ -n "${AC_MISS:-}" ] && { ac_ok=0; warn "установщику нужны файлы, которых нет в списке: $AC_MISS"; }
  fi
  if [ "$ac_ok" != "1" ]; then
    warn "не скачались файлы наблюдателя — виджет останется пустым. Проверьте сеть и повторите."
  else
    AC_OUT="$("$PY" "$AC_TMP/activity-center/install.py" 2>&1)"
    # «(N listener hooks)» — сколько сред листенера реально прошито. Ноль = виджет НЕ ЗАРАБОТАЕТ,
    # и молчать об этом нельзя: снаружи это неотличимо от «задач просто нет».
    AC_HOOKS="$(printf '%s' "$AC_OUT" | sed -n 's/.*(\([0-9]*\) listener hooks).*/\1/p')"
    if [ -z "$AC_HOOKS" ]; then
      warn "установщик наблюдателя отработал непонятно: ${AC_OUT:-нет вывода}"
    elif [ "$AC_HOOKS" = "0" ]; then
      warn "мост :8799 поставлен, но наблюдатель НЕ привязан к листенеру (0 сред)."
      warn "Причина: среда листенера ещё не создана. Запустите Extella и дайте ей отработать"
      warn "любую задачу, затем повторите этот скрипт — виджет молчит именно из-за этого."
    else
      ok "наблюдатель привязан к листенеру (сред: $AC_HOOKS)"
    fi
    sleep 2
    if curl -fsS --max-time 5 http://127.0.0.1:8799/api/health >/dev/null 2>&1; then
      ok "мост Activity Center отвечает (:8799)"
    else
      warn "мост :8799 не отвечает — виджет будет пуст. Лог: ~/.extella/activity-center/bridge.error.log"
    fi
  fi
  rm -rf "$AC_TMP"
fi

say "Готово. Перезапустите Extella (Cmd+Q → открыть) — без этого наблюдатель не подхватится."
