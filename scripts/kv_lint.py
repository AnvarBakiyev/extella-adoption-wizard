#!/usr/bin/env python3
"""F5: статический линт скоупов KV по docs/KV_REGISTRY.md.

Ловит класс бага «глобальный ключ без global:true» (баг Мергуль) и обратный
(«per-account ключ с global:true» — запись в тень). Эвристика по строкам кода:
ищет обращения /api/kv/get|set с литеральным ключом и проверяет наличие
"global" в том же вызове (окно ±3 строки).

Запуск: python3 scripts/kv_lint.py   → выход 1, если есть нарушения.
"""
import re
import sys
import glob

GLOBAL_PREFIXES = ("_mkt_", "composer:catalog")
# легаси-исключение (см. KV_REGISTRY): _mkt_installed — per-account «Мои», НЕ витрина
EXCEPT_LOCAL = ("_mkt_installed",)
# per-account семейства (global у них — ошибка «тень»)
LOCAL_PREFIXES = ("sched:", "inbound:", "inbq:", "hookmap:", "flow:", "digest:",
                  "lastrun:", "connlog:", "sec:", "agent_runs:", "agent_state:", "ci:")

CALL = re.compile(r'/api/kv/(get|set|remove)')
KEY = re.compile(r'"key"\s*:\s*"([^"]+)"')
GLB = re.compile(r'"global"\s*:\s*True')


def lint(path):
    src = open(path, encoding="utf-8").read()
    lines = src.split("\n")
    bad = []
    for i, line in enumerate(lines):
        if not CALL.search(line):
            continue
        window = "\n".join(lines[max(0, i - 1):i + 4])
        km = KEY.search(window)
        if not km:
            continue   # динамический ключ — не судим статически
        key = km.group(1)
        if line.lstrip().startswith(("'", '"')) and "\\n" in line:
            continue   # строка-литерал (генерённый код) — не судим по окну
        has_glb = bool(GLB.search(window))
        if key in EXCEPT_LOCAL:
            if has_glb:
                bad.append((i + 1, key, "легаси per-account ключ с global:True"))
            continue
        if any(key.startswith(p) for p in GLOBAL_PREFIXES) and not has_glb:
            bad.append((i + 1, key, "GLOBAL-ключ без global:True (класс бага Мергуль)"))
        if any(key.startswith(p) for p in LOCAL_PREFIXES) and has_glb:
            bad.append((i + 1, key, "per-account ключ с global:True (запись в тень)"))
    return bad


def main():
    total = 0
    for path in sorted(glob.glob("ui/*.py") + glob.glob("experts/*.py")):
        for ln, key, msg in lint(path):
            print("  ✗ %s:%d  %s — %s" % (path, ln, key, msg))
            total += 1
    print("kv_lint: нарушений: %d" % total)
    sys.exit(1 if total else 0)


if __name__ == "__main__":
    main()
