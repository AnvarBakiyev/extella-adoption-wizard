#!/usr/bin/env python3
"""Идёт ли прямо сейчас стройка? Печатает список живых, код 1 — если есть.

Зачем: перезапуск моста ОБРЫВАЕТ живые стройки (треды умирают, F1 помечает их orphaned).
18.07.2026 так была убита стройка на 6-м шаге из 7 при выкатке мелкой правки UI.
Поэтому деплой сначала спрашивает это.
"""
import glob
import json
import os
import sys
import time

live = []
for p in glob.glob(os.path.expanduser("~/extella_wizard/runs/build_*/build_progress.json")):
    try:
        d = json.load(open(p, encoding="utf-8"))
    except Exception:
        continue
    # running + файл свежий (< 3 мин) = стройка действительно жива, а не брошена давно
    if d.get("status") == "running" and time.time() - os.path.getmtime(p) < 180:
        sg = d.get("stages") or []
        done = len([s for s in sg if s.get("status") == "success"])
        live.append("%s (шаг %d из %d)" % (os.path.basename(os.path.dirname(p)), done, len(sg)))

if live:
    print("; ".join(live))
    sys.exit(1)
sys.exit(0)
