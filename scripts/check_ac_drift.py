#!/usr/bin/env python3
"""Наблюдатель Activity Center («что делает Extella») тоже живёт в ДВУХ местах: канон пишет
Codex в репо интеграции, а раздаётся он клиентам ИЗ ДИСТРИБУТИВА (пак → raw.githubusercontent,
откуда его тянет extella-update.sh).

19.07 разошлись молча: у Анвара работал наблюдатель на 245 строк с гашением задач, а пак
раздавал 214-строчную версию БЕЗ task_state.py. Коллега ставила по инструкции и получала
заведомо устаревший мост — при этом виджет не жалуется, он просто говорит «фоновых задач нет».
Ровно тот же класс, что и расхождение kp_-экспертов, — поэтому та же защита.

Обе стороны могут быть не склонированы (чужая машина) — тогда проверка молча пропускается.
"""
import hashlib
import os
import sys

CANON = os.path.expanduser(
    "~/Documents/Codex/extella-toolbar-activity-center-integration/device/activity-center")
PACK = os.path.expanduser("~/extella_tools/extella-marketplace-pack/device/activity-center")

# Ровно то, что install.py раскладывает на устройство, плюс он сам: если разойдётся любой
# из этих файлов — клиент получит не тот наблюдатель, что мы проверяли.
WATCHED = [
    "install.py",
    "bridge/server.py",
    "bridge/activity_model.py",
    "bridge/service_manager.py",
    "bridge/task_state.py",
    "instrumentation/extella_activity_hook.py",
]


def digest(p):
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except FileNotFoundError:
        return None


if not os.path.isdir(CANON) or not os.path.isdir(PACK):
    sys.exit(0)   # одной из сторон нет — сравнивать не с чем, это не ошибка

drift = []
for rel in WATCHED:
    a = digest(os.path.join(CANON, rel))
    b = digest(os.path.join(PACK, rel))
    if a is None and b is None:
        continue
    if a is not None and b is None:
        drift.append(rel + " (нет в паке)")
    elif a is None and b is not None:
        drift.append(rel + " (нет в каноне)")
    elif a != b:
        drift.append(rel)

if drift:
    print("; ".join(drift))
    sys.exit(1)
sys.exit(0)
