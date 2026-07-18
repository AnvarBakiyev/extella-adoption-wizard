#!/usr/bin/env bash
# Пред-деплойная проверка визарда. ГОНЯТЬ ПЕРЕД КАЖДЫМ `cp ui/* ~/extella_wizard/app/`.
#
# Зачем: wizard.html — один инлайн-скрипт на ~350 КБ. Одна забытая запятая в словаре роняет
# ВЕСЬ скрипт, и страница открывается ПУСТОЙ — при этом мост жив и в логах тихо.
# Повод: 18.07 ровно так едва не уехал в прод тулбар (поймала Элла синтакс-чеком до пуша);
# у неё это теперь в цикле сборки — у нас тоже.
set -u
cd "$(dirname "$0")/.."
fail=0

echo "→ python-модули моста"
for f in ui/*.py; do
  python3 -c "import ast,sys; ast.parse(open('$f',encoding='utf-8').read())" \
    && echo "   ✓ $f" || { echo "   ✗ $f — СИНТАКСИС"; fail=1; }
done

echo "→ инлайн-скрипт wizard.html"
python3 - <<'PY' > /tmp/_wz_inline.js
import re
html = open('ui/wizard.html', encoding='utf-8').read()
parts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
print("\n;\n".join(parts))
PY
if command -v node >/dev/null 2>&1; then
  node --check /tmp/_wz_inline.js && echo "   ✓ wizard.html (JS)" || { echo "   ✗ wizard.html — СИНТАКСИС JS"; fail=1; }
else
  echo "   ⚠ node не найден — JS не проверен (поставьте node или проверьте в браузере: консоль без ошибок)"
fi

echo "→ эксперты (fython: срезаем \$extens)"
for f in experts/*.py; do
  python3 - "$f" <<'PY' || { echo "   ✗ $f — СИНТАКСИС"; fail=1; }
import ast, sys
src = "\n".join(l for l in open(sys.argv[1], encoding='utf-8').read().splitlines()
                if not l.strip().startswith('$extens'))
ast.parse(src)
PY
done
[ $fail -eq 0 ] && echo "   ✓ эксперты"

echo
[ $fail -eq 0 ] && echo "ВСЁ ЧИСТО — можно деплоить" || echo "ЕСТЬ ОШИБКИ — НЕ ДЕПЛОИТЬ"
exit $fail
