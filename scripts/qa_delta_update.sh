#!/usr/bin/env bash
# Быстрая QA-дельта Wizard: только изменённые UI/bridge-файлы из codex/prod-hardening.
# Эксперты, концепты, правила, тулбар, Workspace и пользовательские данные не переустанавливаются.
set -euo pipefail

REPO="AnvarBakiyev/extella-adoption-wizard"
BRANCH="codex/prod-hardening"
EXPECTED_VERSION="5.00"
APP_DIR="$HOME/extella_wizard/app"
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
ENC_BRANCH="${BRANCH//\//%2F}"
SHA="$(curl -fsSL "https://api.github.com/repos/$REPO/git/ref/heads/$ENC_BRANCH" |
  "$PY" -c 'import json,sys; print(json.load(sys.stdin)["object"]["sha"])')"
[ -n "$SHA" ] || { echo "Не удалось определить QA-коммит."; exit 1; }

curl -fsSL "https://codeload.github.com/$REPO/tar.gz/$SHA" | tar -xz -C "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"
[ -n "$SRC" ] || { echo "QA-пакет не распакован."; exit 1; }

if ! "$PY" "$SRC/scripts/check_builds_busy.py"; then
  echo "Обновление остановлено: дождитесь завершения стройки и повторите команду."
  exit 2
fi

echo "→ Проверяю скачанную дельту ${SHA:0:7}"
"$PY" -m py_compile "$SRC/ui/server.py" "$SRC/ui/wz_agentic.py" "$SRC/ui/wz_build.py"
if command -v node >/dev/null 2>&1; then
  "$PY" - "$SRC/ui/wizard.html" "$TMP/wizard-inline.js" <<'PY'
import re, sys
html = open(sys.argv[1], encoding="utf-8").read()
parts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
open(sys.argv[2], "w", encoding="utf-8").write("\n;\n".join(parts))
PY
  node --check "$TMP/wizard-inline.js"
fi

BACKUP="$HOME/extella_wizard/backups/qa-${EXPECTED_VERSION}-$(date +%Y%m%d-%H%M%S)"
mkdir -p "$BACKUP" "$APP_DIR"
for name in server.py wz_agentic.py wz_build.py wizard.html; do
  [ ! -f "$APP_DIR/$name" ] || cp "$APP_DIR/$name" "$BACKUP/$name"
  cp "$SRC/ui/$name" "$APP_DIR/$name"
done
echo "  ✓ четыре файла обновлены; backup: $BACKUP"
echo "  ✓ эксперты, концепты и правила не переустанавливались"

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
