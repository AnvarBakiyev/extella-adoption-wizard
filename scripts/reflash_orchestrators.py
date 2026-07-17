#!/usr/bin/env python3
"""Одноразовая миграция: применить data-integrity фикс к БОЕВЫМ файловым оркестраторам без
полной пересборки (стадии не трогаем). Патчим ровно 2 строки в коде каждого оркестратора:
  1) перед стадией удаляем stage-файл прошлого прогона (иначе упавшая стадия «успешна» на чужих данных);
  2) гейт стадии по фактическому ok, а не только по наличию файла.
Идемпотентно (повторный запуск пропускает уже пропатченные). Использует санкционированный клиент sync.py.

Запуск ИЗ КАТАЛОГА репозитория:  python3 scripts/reflash_orchestrators.py
"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import sync

# только файловые оркестраторы из _ORCH_TEMPLATE (API/композитные/ручные — не задеты)
ORCHESTRATORS = ["uc_run_pipeline", "p1aa_run_pipeline", "hvk_run_pipeline", "hr_run_pipeline"]

UNLINK_ANCHOR = 'outp = str(wd / ("stage%d.json" % i))'
GATE_OLD = "if not Path(outp).exists():"
GATE_NEW = "if not ok or not Path(outp).exists():"


def patch(code):
    if "Path(outp).unlink()" in code:
        return code, False                      # уже пропатчен
    if UNLINK_ANCHOR not in code or GATE_OLD not in code:
        return code, None                       # якорь не найден — не наш шаблон, не трогаем
    out = []
    for ln in code.split("\n"):
        out.append(ln)
        if UNLINK_ANCHOR in ln:
            ind = ln[:len(ln) - len(ln.lstrip())]
            out += [ind + "try:",
                    ind + "    Path(outp).unlink()   # не тащим stage-файл прошлого прогона: упавшая стадия иначе «успешна» на чужих данных",
                    ind + "except OSError:",
                    ind + "    pass"]
    return "\n".join(out).replace(GATE_OLD, GATE_NEW), True


def main():
    tok = sync.token()
    if not tok:
        print("нет токена"); sys.exit(1)
    for name in ORCHESTRATORS:
        r = sync.api("/api/expert/get", {"name": name, "global": True}, tok)
        code = r.get("expert_code") or ""
        if not code:
            print("  ✗ %s: код не получен" % name); continue
        code2, changed = patch(code)
        if changed is None:
            print("  ⚠️  %s: якорь шаблона не найден — пропуск (не наш оркестратор)" % name); continue
        if not changed:
            print("  ~ %s: уже пропатчен" % name); continue
        if not ("Path(outp).unlink()" in code2 and GATE_NEW in code2):
            print("  ✗ %s: патч не наложился — НЕ сохраняю" % name); continue
        sv = sync.api("/api/expert/save", {
            "name": name, "code": code2, "cspl": r.get("cspl", "fython"), "global": True,
            "description": r.get("expert_description") or (name + " orchestrator"),
            "kwargs": r.get("expert_params") or {}}, tok)
        print("  %s %s: save=%s" % ("✅" if sv.get("status") == "success" else "❌", name, sv.get("status")))
    print("\n== верификация ==")
    for name in ORCHESTRATORS:
        c = sync.platform_code(name, tok) or ""
        print("  %s: unlink=%s  честный-гейт=%s" % (
            name, "✓" if "Path(outp).unlink()" in c else "✗",
            "✓" if GATE_NEW in c else "✗"))


if __name__ == "__main__":
    main()
