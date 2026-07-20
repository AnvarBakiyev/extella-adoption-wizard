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

# Перезапуск моста ОБРЫВАЕТ живые стройки (треды умирают, F1 помечает их orphaned).
# 18.07 так была убита стройка Анвара на 6-м шаге из 7 при выкатке мелкой правки UI.
echo "→ идут ли стройки прямо сейчас"
if _busy=$(python3 "$(dirname "$0")/check_builds_busy.py"); then
  echo "   ✓ живых строек нет"
else
  echo "   ✗ ИДЁТ СТРОЙКА: $_busy"
  echo "     Перезапуск моста её оборвёт — дождитесь конца."
  fail=1
fi

# kp_*-эксперты живут в ДВУХ репозиториях: визард ставится после пака и молча затирает
# его версию. Пока файлы идентичны — беды нет; расхождение должно всплыть здесь, а не у клиента.
echo "→ общие kp_-эксперты (визард ↔ дистрибутив)"
if _kp=$(python3 "$(dirname "$0")/check_kp_drift.py"); then
  echo "   ✓ версии совпадают"
else
  echo "   ✗ РАСХОЖДЕНИЕ: $_kp — визард затрёт версию пака у клиента"
  fail=1
fi

# Наблюдатель Activity Center: канон пишет Codex, а раздаём его МЫ — из пака, через
# extella-update.sh. Отставание пака не видно ниоткуда: виджет на устаревшем мосту не
# ругается, он просто говорит «фоновых задач нет» — так и потеряли время у коллеги.
echo "→ наблюдатель Activity Center (канон ↔ дистрибутив)"
if _ac=$(python3 "$(dirname "$0")/check_ac_drift.py"); then
  echo "   ✓ версии совпадают"
else
  echo "   ✗ РАСХОЖДЕНИЕ: $_ac — клиент получит не тот наблюдатель"
  fail=1
fi

echo "→ python-модули моста"
for f in ui/*.py; do
  python3 -c "import ast,sys; ast.parse(open('$f',encoding='utf-8').read())" \
    && echo "   ✓ $f" || { echo "   ✗ $f — СИНТАКСИС"; fail=1; }
done

echo "→ человеческие названия шагов карты"
python3 scripts/check_placement_labels.py \
  && echo "   ✓ карта не показывает владельцу только expert_name" \
  || { echo "   ✗ карта снова техническая"; fail=1; }

echo "→ долговечность отчётов"
python3 scripts/check_report_persistence.py \
  && echo "   ✓ отчёт переживает /tmp и другое устройство" \
  || { echo "   ✗ отчёт снова привязан к временной папке"; fail=1; }

echo "→ роли шагов стройки"
python3 scripts/check_build_task_roles.py \
  && echo "   ✓ runtime-обвязка не блокирует конвейер данных" \
  || { echo "   ✗ watcher/autostart снова попал в data-DAG"; fail=1; }

echo "→ устойчивость чата"
python3 scripts/check_chat_resilience.py \
  && echo "   ✓ временный пустой ответ повторяется один раз" \
  || { echo "   ✗ чат снова падает на первом флапе"; fail=1; }

echo "→ QA-дельта установки"
python3 scripts/check_delta_install.py \
  && echo "   ✓ неизменённые эксперты/концепты не переустанавливаются" \
  || { echo "   ✗ delta-фильтр установки сломан"; fail=1; }

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
