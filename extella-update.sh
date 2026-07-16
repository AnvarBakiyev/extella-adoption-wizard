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

say "Готово. Перезапустите Extella (Cmd+Q → открыть), чтобы применить тулбар и карточки."
