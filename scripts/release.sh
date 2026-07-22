#!/usr/bin/env bash
# ЕДИНСТВЕННЫЙ способ отправлять работу наружу. Не запускать git push руками.
#
# Зачем: 18.07 я ТРИЖДЫ прошёл мимо собственных проверок — они печатали «не деплоить»
# и «НЕ ПРОШЛО», а следующая команда в цепочке всё равно копировала файлы и пушила.
# Проверка, результат которой ничего не блокирует, проверкой не является.
set -euo pipefail
cd "$(dirname "$0")/.."

branch="$(git branch --show-current)"
# Разделение ролей (утверждено 20.07): скрипт пушит ТОЛЬКО текущую ветку, поэтому hardening-ветка
# физически не может отправить main. Прежний тотальный запрет релиза из main ломал канонический
# релиз владельца (main — мой путь выпуска) — снят, полный preflight ниже остаётся обязательным.

echo "── ПОЛНЫЙ PREFLIGHT ──"
if ! bash scripts/preflight_ui.sh; then
  echo
  echo "РЕЛИЗ ОТМЕНЁН: preflight красный. Ничего не закоммичено и не отправлено."
  exit 1
fi

echo "── СМОУК ФУНДАМЕНТА ──"
if ! python3 scripts/smoke_e2e.py; then
  echo
  echo "РЕЛИЗ ОТМЕНЁН: смоук красный. Ничего не закоммичено и не отправлено."
  exit 1
fi

if [ -z "$(git status --porcelain)" ]; then
  echo; echo "нечего коммитить — отправляю то, что уже готово"
else
  if [ $# -lt 1 ]; then
    echo; echo "нужен текст коммита: ./scripts/release.sh \"сообщение\""; exit 2
  fi
  git add -A && git commit -q -m "$1"
  echo; echo "закоммичено: $(git log --oneline -1)"
fi

git push origin "$branch" -q
echo "отправлено: $(git log --oneline -1)"
