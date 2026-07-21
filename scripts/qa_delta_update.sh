#!/usr/bin/env bash
# Быстрая QA-дельта Wizard: только изменённые UI/bridge-файлы из codex/prod-hardening.
# Неизменённые эксперты, концепты, правила, тулбар и пользовательские данные не переустанавливаются.
set -euo pipefail

REPO="AnvarBakiyev/extella-adoption-wizard"
BRANCH="codex/prod-hardening"
EXPECTED_VERSION="5.12"
APP_DIR="$HOME/extella_wizard/app"
CAT_DIR="$HOME/extella_wizard/catalog"
WS_DIR="$HOME/extella-plugins/workspace"
PY="$(command -v python3.12 || command -v python3 || true)"

[ -n "$PY" ] || { echo "Нет Python 3.12/3 — обновление остановлено."; exit 1; }
[ -f "$APP_DIR/config.json" ] || {
  echo "Extella Wizard ещё не установлен: нет $APP_DIR/config.json"
  echo "Сначала нужна обычная полная установка install-all.sh."
  exit 1
}

TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "→ Проверяю, нет ли живой стройки"
SHA="${EXTELLA_QA_SHA:-}"
if [ -n "$SHA" ]; then
  case "$SHA" in
    *[!0-9a-fA-F]*|'') echo "EXTELLA_QA_SHA должен быть полным git SHA."; exit 1 ;;
  esac
  [ "${#SHA}" -eq 40 ] || { echo "EXTELLA_QA_SHA должен содержать 40 символов."; exit 1; }
else
  ENC_BRANCH="${BRANCH//\//%2F}"
  SHA="$(curl -fsSL "https://api.github.com/repos/$REPO/git/ref/heads/$ENC_BRANCH" |
    "$PY" -c 'import json,sys; print(json.load(sys.stdin)["object"]["sha"])')"
fi
[ -n "$SHA" ] || { echo "Не удалось определить QA-коммит."; exit 1; }

curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$SHA" | tar -xz -C "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -n "$SRC" ] || { echo "QA-пакет не распакован."; exit 1; }

if ! "$PY" "$SRC/scripts/check_builds_busy.py"; then
  echo "Обновление остановлено: дождитесь завершения стройки и повторите команду."
  exit 2
fi

echo "→ Проверяю скачанную дельту ${SHA:0:7}"
"$PY" -m py_compile "$SRC"/ui/*.py
"$PY" "$SRC/scripts/check_blueprint_expert_runtime.py"
# `command -v` недостаточно: Homebrew может оставить node в PATH с потерянной dylib. Такой node
# падал у Гульжан ДО копирования дельты. JS уже прошёл обязательный release-preflight; на клиенте
# повторяем проверку лишь когда бинарник реально запускается.
if command -v node >/dev/null 2>&1 && node --version >/dev/null 2>&1; then
  "$PY" - "$SRC/ui/wizard.html" "$TMP/wizard-inline.js" <<'PY'
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
parts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
open(sys.argv[2], "w", encoding="utf-8").write("\n;\n".join(parts))
PY
  node --check "$TMP/wizard-inline.js"
else
  echo "  ! локальный Node отсутствует или сломан — повторная JS-проверка пропущена"
  echo "    релизный wizard.html уже проверен серверным preflight"
fi

BACKUP="$HOME/extella_wizard/backups/qa-${EXPECTED_VERSION}-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP" "$APP_DIR" "$CAT_DIR"
for name in server.py wz_agentic.py wz_build.py wz_llm.py wz_platform.py wz_process.py wizard.html; do
  [ ! -f "$APP_DIR/$name" ] || cp "$APP_DIR/$name" "$BACKUP/$name"
  cp "$SRC/ui/$name" "$APP_DIR/$name"
done
[ ! -f "$CAT_DIR/catalog.json" ] || cp "$CAT_DIR/catalog.json" "$BACKUP/catalog.json"
cp "$SRC/catalog/catalog.json" "$CAT_DIR/catalog.json"
cp "$SRC/catalog/catalog.json" "$APP_DIR/catalog.json"
echo "  ✓ все модули моста, UI и каталог возможностей обновлены; backup: $BACKUP"

echo "→ Обновляю только изменённые системные эксперты Universal Process"
EXTELLA_DELTA_FILES="experts/wz_generate_blueprint.py,experts/wz_build_plan.py,experts/wz_auto_compose.py" \
  "$PY" "$SRC/install.py"
echo "  ✓ 3 изменённых эксперта обновлены; остальные эксперты, концепты и правила не переустанавливались"

echo "→ Обновляю read/action-адаптер Workspace к тому же Process Contract"
if [ -d "$WS_DIR" ]; then
  mkdir -p "$BACKUP/workspace"
  for name in server.py index.html VERSION; do
    [ ! -f "$WS_DIR/$name" ] || cp "$WS_DIR/$name" "$BACKUP/workspace/$name"
    cp "$SRC/dist/workspace/$name" "$WS_DIR/$name"
  done
  WS_PID="$(lsof -tiTCP:34767 -sTCP:LISTEN 2>/dev/null || true)"
  if [ -n "$WS_PID" ]; then
    kill "$WS_PID" 2>/dev/null || true
    sleep 1
    (cd "$WS_DIR" && nohup "$PY" server.py >"$WS_DIR/workspace.log" 2>&1 &)
    echo "  ✓ Workspace v1.1.0 обновлён и перезапущен"
  else
    echo "  ✓ Workspace v1.1.0 обновлён; поднимется из тулбара"
  fi
else
  echo "  ! Workspace не установлен — адаптер пропущен; Wizard обновляется независимо"
fi

echo "→ Перезапускаю только мост Wizard"
if launchctl print "gui/$(id -u)/ai.extella.wizard-bridge" >/dev/null 2>&1; then
  launchctl kickstart -k "gui/$(id -u)/ai.extella.wizard-bridge"
else
  PID="$(lsof -tiTCP:8765 -sTCP:LISTEN 2>/dev/null || true)"
  [ -z "$PID" ] || kill "$PID"
  (cd "$APP_DIR" && nohup "$PY" server.py >"$HOME/extella_wizard/bridge.log" 2>&1 &)
fi

for _ in 1 2 3 4 5 6 7 8 9 10; do
  VERSION="$(curl -fsS --max-time 2 http://127.0.0.1:8765/x/health 2>/dev/null |
    "$PY" -c 'import json,sys; print(json.load(sys.stdin).get("version", ""))' 2>/dev/null || true)"
  [ "$VERSION" != "$EXPECTED_VERSION" ] || {
    echo "Готово: Wizard v$VERSION, QA ${SHA:0:7}. Перезагрузите страницу Extella."
    exit 0
  }
  sleep 1
done

echo "Файлы установлены, но мост v$EXPECTED_VERSION не ответил. Лог: ~/extella_wizard/bridge.log"
exit 3
