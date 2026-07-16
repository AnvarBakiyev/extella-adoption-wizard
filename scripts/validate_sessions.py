#!/usr/bin/env python3
"""F4: валидатор сессий против SESSION_SCHEMA v1 (docs/SESSION_SCHEMA.md).

Мягкий контракт: обязательные поля + типы известных полей + предупреждение о неизвестных
(неизвестное поле = кандидат на внесение в схему, не ошибка).
Запуск: python3 scripts/validate_sessions.py [--dir ~/extella_wizard/sessions]
Выход: 0 = ошибок нет (warnings допустимы), 1 = есть ошибки контракта.
"""
import json
import re
import sys
import glob
import os

REQUIRED = {"session_id": str, "client_name": str, "stage": str, "updated_at": str}
KNOWN = {
    "session_id": str, "schema": int, "client_name": str, "stage": str,
    "created_at": str, "updated_at": str, "log": list,
    "answers": dict, "questionnaire": list, "questionnaire_task": str,
    "comments": list, "files": list,
    "blueprint_path": str, "spec_path": (str, type(None)), "build_plan_path": (str, type(None)),
    "builds": list, "decisions": list, "blueprint_history": list, "building": str,
    "audit": dict, "data_check": dict, "tasks": dict,
    "schedule": dict, "paused": bool, "paused_at": str, "resumed_at": str,
    "recipients": list, "message_template": str, "rules": list, "rules_struct": list, "fields": dict,
    "source": (dict, type(None)), "inbound": dict, "runs": list,
    "production_agent": dict, "panel_url": str, "panel_name": str, "panel_manifest": dict,
    "published": dict, "goal": str, "demo_runs": list,
}
SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
STAGES = {"interview", "intake", "blueprint", "spec", "audited", "test", "slice_accepted", "built", "launched", "launch"}


def check(path):
    errs, warns = [], []
    try:
        s = json.load(open(path, encoding="utf-8"))
    except Exception as e:
        return ["не парсится: " + str(e)[:80]], []
    for k, t in REQUIRED.items():
        if k not in s:
            errs.append("нет обязательного поля " + k)
        elif not isinstance(s[k], t):
            errs.append("поле %s: ожидался %s, найден %s" % (k, t.__name__, type(s[k]).__name__))
    if s.get("session_id") and not SAFE_ID.match(str(s["session_id"])):
        errs.append("session_id не SAFE_ID")
    if s.get("stage") and s["stage"] not in STAGES:
        warns.append("неизвестный stage: " + str(s["stage"]))
    for k, v in s.items():
        if k in KNOWN:
            t = KNOWN[k]
            if not isinstance(v, t):
                errs.append("поле %s: тип %s вне контракта" % (k, type(v).__name__))
        else:
            warns.append("неизвестное поле «%s» — внесите в SESSION_SCHEMA.md" % k)
    # builds[-1] — действующая сборка
    for b in (s.get("builds") or []):
        if not isinstance(b, dict):
            errs.append("builds: элемент не dict")
        elif not b.get("orchestrator"):
            warns.append("builds: запись без orchestrator")
    return errs, warns


def main():
    d = os.path.expanduser("~/extella_wizard/sessions")
    if "--dir" in sys.argv:
        d = os.path.expanduser(sys.argv[sys.argv.index("--dir") + 1])
    bad = 0
    total = 0
    import re as _re
    _SESS = _re.compile(r"^wz_[0-9]{8}_[A-Za-z0-9]+\.json$")
    for p in sorted(glob.glob(os.path.join(d, "wz_*.json"))):
        if not _SESS.match(os.path.basename(p)):
            continue   # сайдкары (_blueprint/_build_plan/_chat/_build_manifest/…) — не сессии
        total += 1
        errs, warns = check(p)
        name = os.path.basename(p)
        for e in errs:
            print("  ✗ %s: %s" % (name, e))
        for w in warns:
            print("  ⚠ %s: %s" % (name, w))
        if errs:
            bad += 1
    print("проверено сессий: %d · с ошибками контракта: %d" % (total, bad))
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
