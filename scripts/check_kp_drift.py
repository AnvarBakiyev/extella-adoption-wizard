#!/usr/bin/env python3
"""Четыре эксперта kp_* живут В ДВУХ репозиториях сразу — и в визарде, и в дистрибутиве.
Установщик ставит пак, потом визард: версия визарда молча затирает версию пака.

Сегодня они идентичны. Когда однажды разойдутся, у клиента окажется не та версия,
что мы ожидаем, и ловить это будем по симптомам. Поэтому расхождение должно быть
видно СРАЗУ — на предполётной проверке, а не у клиента.

Пак может быть не склонирован (на чужой машине) — тогда проверка молча пропускается.
"""
import hashlib
import os
import sys

WIZ = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "experts")
PACK = os.path.expanduser("~/extella_tools/extella-marketplace-pack/experts")
SHARED = ["kp_ask", "kp_ingest", "kp_install_pack", "kp_resolver"]


def digest(p):
    try:
        with open(p, "rb") as f:
            return hashlib.sha256(f.read()).hexdigest()
    except Exception:
        return None


if not os.path.isdir(PACK):
    sys.exit(0)   # дистрибутив не склонирован — сравнивать не с чем, это не ошибка

drift = []
for name in SHARED:
    a = digest(os.path.join(WIZ, name + ".py"))
    b = digest(os.path.join(PACK, name + ".py"))
    if a and b and a != b:
        drift.append(name)

if drift:
    print("; ".join(drift))
    sys.exit(1)
sys.exit(0)
