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

echo "→ гейты графа стройки"
python3 scripts/check_build_graph_gates.py \
  && echo "   ✓ нелинейный DAG и частичная сборка не маскируются" \
  || { echo "   ✗ Строитель снова выдаёт частичную цепочку за готовую"; fail=1; }

echo "→ агентная стройка сложных задач"
python3 scripts/check_agentic_builder.py \
  && echo "   ✓ Qwen получает полное ТЗ и все входы, результат проверяется до упаковки" \
  || { echo "   ✗ агентный solve-run-repair контракт сломан"; fail=1; }

echo "→ универсальная матрица Source Model / repair / memory"
python3 scripts/check_agentic_universal.py \
  && echo "   ✓ 15 классов задач, legacy 4/4 и отрицательные stop-сценарии доказаны синтетически" \
  || { echo "   ✗ универсальный агентный механизм или repair budget сломан"; fail=1; }

echo "→ Universal Process Contract v1"
python3 scripts/check_universal_process_runtime.py \
  && python3 scripts/check_universal_process_matrix.py \
  && python3 scripts/check_upc_orchestrator_runtime.py \
  && python3 scripts/check_universal_process_vertical.py \
  && echo "   ✓ 10/10 сценариев, DAG/checkpoint/resume/HITL, generated orchestrator и вертикальный repair доказаны" \
  || { echo "   ✗ пошаговый runtime, локальный repair или checkpoint сломан"; fail=1; }

echo "→ диалог владельца внутри стройки"
python3 scripts/check_build_owner_dialogue.py \
  && echo "   ✓ технические ссылки чинятся без человека, бизнес-вопрос продолжает ту же сессию" \
  || { echo "   ✗ need_human снова превратился в тупик или отдельный чат"; fail=1; }

echo "→ защита точечного ремонта от повторного клика"
python3 scripts/check_build_start_idempotency.py \
  && echo "   ✓ кнопка отвечает сразу, одна сессия запускает только одного worker" \
  || { echo "   ✗ повторный клик снова может запустить конкурирующие стройки"; fail=1; }

echo "→ единый процесс во всех четырёх поверхностях"
python3 scripts/check_process_surfaces.py \
  && echo "   ✓ Wizard, Chat, Composer и Workspace читают/чинят один UPC sidecar" \
  || { echo "   ✗ одна из поверхностей завела отдельное состояние процесса"; fail=1; }

echo "→ длинный и медленный мозг агента"
python3 scripts/check_brain_modal.py \
  && echo "   ✓ мозг прокручивается, закрывается и не открывается снова запоздавшим ответом" \
  || { echo "   ✗ окно мозга снова может заблокировать Wizard"; fail=1; }

echo "→ безопасная граница экспериментального Контура"
python3 scripts/check_goal_loop_safety.py \
  && echo "   ✓ fail-closed; нет fake expert/install и записи недоказанной памяти" \
  || { echo "   ✗ Контур снова маскирует локальный шаг или загрязняет мозг"; fail=1; }

echo "→ адаптивное интервью и проверка данных"
python3 scripts/check_adaptive_data_check.py \
  && echo "   ✓ аудит файла читает фактически показанные вопросы, а не старую анкету" \
  || { echo "   ✗ адаптивные ответы снова потерялись перед аудитом файла"; fail=1; }

echo "→ упаковка рабочего агента"
python3 scripts/check_agentic_packaging.py \
  && echo "   ✓ эксперт, концепты и правила образуют один пакет" \
  || { echo "   ✗ агент снова развёртывается без мозга/правил"; fail=1; }

echo "→ устойчивость чата"
python3 scripts/check_chat_resilience.py \
  && echo "   ✓ временный пустой ответ повторяется один раз" \
  || { echo "   ✗ чат снова падает на первом флапе"; fail=1; }

echo "→ QA-дельта установки"
python3 scripts/check_delta_install.py \
  && echo "   ✓ неизменённые эксперты/концепты не переустанавливаются" \
  || { echo "   ✗ delta-фильтр установки сломан"; fail=1; }
bash -n scripts/qa_delta_update.sh \
  && grep -q 'check_builds_busy.py' scripts/qa_delta_update.sh \
  && grep -q 'codex/prod-hardening' scripts/qa_delta_update.sh \
  && grep -q 'node --version >/dev/null 2>&1' scripts/qa_delta_update.sh \
  && echo "   ✓ короткий QA-апдейтер синтаксически цел и защищает живую стройку" \
  || { echo "   ✗ короткий QA-апдейтер сломан или обходит защиту стройки"; fail=1; }

echo "→ каталог возможностей на чистом Mac"
python3 scripts/check_catalog_install.py \
  && echo "   ✓ полная/дельта-установка кладут каталог, мост восстанавливает резерв" \
  || { echo "   ✗ новый Mac снова остановится на шаге «План»"; fail=1; }

echo "→ атомарность плана"
python3 scripts/check_blueprint_atomicity.py \
  && echo "   ✓ неполный blueprint не выглядит готовым и не открывает стройку" \
  || { echo "   ✗ формальный success снова может маскировать отсутствие плана"; fail=1; }

echo "→ runtime эксперта плана"
python3 scripts/check_blueprint_expert_runtime.py \
  && python3 scripts/check_build_plan_upc.py \
  && echo "   ✓ prompt строится и blueprint действительно записывается" \
  || { echo "   ✗ эксперт плана компилируется, но падает до сохранения результата"; fail=1; }

echo "→ маршрутизация локальных экспертов"
python3 scripts/check_local_expert_routing.py \
  && echo "   ✓ сессия/план/ТЗ исполняются на устройстве открытого Wizard" \
  || { echo "   ✗ несколько Listener'ов снова могут разнести файлы сессии по разным Mac"; fail=1; }

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

echo "→ интерфейс Workspace"
python3 - <<'PY' > /tmp/_workspace_inline.js
import re
html = open('dist/workspace/index.html', encoding='utf-8').read()
parts = re.findall(r'<script(?![^>]*\bsrc=)[^>]*>(.*?)</script>', html, re.S)
print("\n;\n".join(parts))
PY
python3 -m py_compile dist/workspace/server.py \
  || { echo "   ✗ Workspace server.py — СИНТАКСИС"; fail=1; }
if command -v node >/dev/null 2>&1; then
  node --check /tmp/_workspace_inline.js \
    && echo "   ✓ Workspace server.py + index.html" \
    || { echo "   ✗ Workspace index.html — СИНТАКСИС JS"; fail=1; }
else
  echo "   ⚠ node не найден — Workspace JS не проверен"
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
