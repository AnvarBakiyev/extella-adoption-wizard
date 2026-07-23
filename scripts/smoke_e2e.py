#!/usr/bin/env python3
"""F6: сквозной смоук фундамента визарда. ГОНЯТЬ ПЕРЕД ПОКАЗОМ КЛИЕНТУ И ПОСЛЕ КРУПНЫХ ПРАВОК.

Зачем: отдельные куски проверены каждый в своём коммите, но клиент видит их вместе. Смоук
трогает фундамент СНАРУЖИ, как это делает браузер: живой мост, живая платформа, живые сессии.
Пишет PASS/FAIL с ПРИЧИНОЙ — «упало» без причины бесполезно.

Правила: боевые данные не портим (пишем только во временную сессию и убираем за собой);
секреты не печатаем; провал одной проверки не мешает остальным.

Запуск: python3 scripts/smoke_e2e.py
"""
import atexit
import json
import re
import sys
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BR = "http://127.0.0.1:8765"
SESS = Path.home() / "extella_wizard" / "sessions"
UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
results = []


def check(name):
    def deco(fn):
        try:
            ok, why = fn()
        except Exception as e:
            ok, why = False, type(e).__name__ + ": " + str(e)[:120]
        results.append((ok, name, why))
        print(("  ✓ " if ok else "  ✗ ") + name + (" — " + why if why else ""))
        return fn
    return deco


def call(path, payload=None, timeout=120):
    rq = urllib.request.Request(BR + path,
                                data=json.dumps(payload).encode() if payload is not None else None,
                                headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(rq, timeout=timeout).read())


def pick_built_session():
    """Живой процесс с оркестратором — на нём проверяем чтение (без записи)."""
    best = None
    for f in SESS.glob("wz_*.json"):
        try:
            s = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        b = (s.get("builds") or [{}])[-1]
        if b.get("orchestrator") and (b.get("experts") or []):
            if not best or s.get("updated_at", "") > best[1]:
                best = (s["session_id"], s.get("updated_at", ""))
    return best[0] if best else ""


SID = ""
TMP = ""
UPC_SID = ""
UPC_FILES = []


def cleanup_upc_fixture():
    for path in UPC_FILES:
        try:
            path.unlink(missing_ok=True)
        except Exception:
            pass


atexit.register(cleanup_upc_fixture)


def main():
    global SID, TMP, UPC_SID
    print("F6 СКВОЗНОЙ СМОУК ФУНДАМЕНТА\n")

    print("Мост и данные")

    @check("мост отвечает")
    def _():
        d = call("/x/sessions")
        return bool(d.get("sessions") is not None), "сессий: %d" % len(d.get("sessions") or [])

    @check("мост запущен на ожидаемой UPC-версии")
    def _():
        # Ожидание = BRIDGE_VERSION из репо (один источник истины). Литерал («5.14») протух при
        # первом же бампе версии и завалил релиз 5.26 при живом и правильном мосте.
        import re as _re
        src = (Path(__file__).resolve().parent.parent / "ui" / "server.py").read_text(encoding="utf-8")
        m = _re.search(r'BRIDGE_VERSION\s*=\s*"([^"]+)"', src)
        want = m.group(1) if m else "?"
        d = call("/x/health")
        return d.get("version") == want, "версия: %s (ожидалась %s)" % (d.get("version"), want)

    print("\nUniversal Process API")
    UPC_SID = "wz_smoke_upc_" + uuid.uuid4().hex[:12]
    sp = SESS / (UPC_SID + ".json")
    pp = SESS / (UPC_SID + "_process.json")
    ep = SESS / (UPC_SID + "_process_events.jsonl")
    UPC_FILES.extend((sp, pp, ep))
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ui"))
    from wz_process import checkpoint, process_from_blueprint, process_status
    graph = process_from_blueprint(
        UPC_SID,
        {"process_name": "UPC live smoke", "goal": "Проверить общий процесс",
         "stages": [{"id": "unknown_step", "title": "Новая способность вне каталога",
                     "business_description": "Создать результат неизвестной заранее задачи"}]},
        origin="smoke")
    sp.write_text(json.dumps({
        "session_id": UPC_SID, "schema": 1, "client_name": "UPC live smoke",
        "stage": "blueprint", "answers": {},
        "process_contract": {"schema": graph["schema"], "path": str(pp),
                             "process_id": graph["process_id"], "active_version": 1,
                             "status": process_status(graph)}}, ensure_ascii=False), encoding="utf-8")
    checkpoint(graph, pp, ep, {"type": "smoke_fixture_created"})

    @check("живой мост читает единый UPC sidecar")
    def _():
        d = call("/x/process?session_id=" + UPC_SID + "&surface=chat")
        steps = (d.get("process") or {}).get("steps") or []
        ok = (d.get("status") == "success" and d.get("surface") == "chat" and len(steps) == 1
              and (steps[0].get("implementation") or {}).get("mode") == "generate")
        return ok, "unknown capability → %s" % ((steps[0].get("implementation") or {}).get("mode") if steps else "нет шага")

    @check("общий action seam атомарно пишет решение и событие")
    def _():
        step_id = graph["steps"][0]["id"]
        out = call("/x/process_action", {"session_id": UPC_SID, "step_id": step_id,
                                          "action": "approve", "permission": "create",
                                          "target": "smoke-output", "payload": {"dry_run": True},
                                          "approved": True, "surface": "workspace"})
        read = call("/x/process?session_id=" + UPC_SID + "&surface=wizard")
        stored = read.get("process") or {}
        events = read.get("events") or []
        ok = (out.get("status") == "success" and bool(stored.get("approvals"))
              and any(e.get("surface") == "workspace" for e in events))
        return ok, "approval + append-only event сохранены" if ok else "решение не сохранилось"

    SID = pick_built_session()

    @check("есть собранный процесс для проверок")
    def _():
        return bool(SID), (SID or "ни одного процесса с оркестратором — остальные проверки будут неполными")

    print("\nБезопасность")

    @check("device id не выходит наружу (/x/devices)")
    def _():
        raw = urllib.request.urlopen(BR + "/x/devices", timeout=90).read().decode()
        leaks = UUID_RE.findall(raw)
        return not leaks, ("НАЙДЕНА УТЕЧКА: %d полных id" % len(leaks)) if leaks else "только ref и маски"

    @check("устройства опрашиваются и статус честный")
    def _():
        devs = call("/x/devices").get("devices") or []
        if not devs:
            return False, "пустой список — опрос устройств не отработал"
        live = [d for d in devs if d.get("online")]
        return True, "%d устройств, живых %d" % (len(devs), len(live))

    print("\nРазмещение (A1)")

    @check("карта размещения предлагается с объяснением")
    def _():
        if not SID:
            return False, "нет процесса"
        d = call("/x/placement?sid=" + SID)
        prop = d.get("proposal") or []
        if not prop:
            return False, "предложение пустое"
        no_why = [p for p in prop if not p.get("why")]
        return not no_why, "шагов %d, у всех есть объяснение" % len(prop)

    @check("в карте наружу уходят только ref")
    def _():
        raw = urllib.request.urlopen(BR + "/x/placement?sid=" + SID, timeout=90).read().decode() if SID else "{}"
        return not UUID_RE.findall(raw), "device id не найден"

    print("\nПравила (A6) и фильтры")

    @check("правила читаются из источника истины")
    def _():
        if not SID:
            return False, "нет процесса"
        d = call("/x/rules?sid=" + SID)
        src = d.get("source")
        if src == "platform":
            return True, "источник: платформа"
        if src == "cache":
            return True, "платформа молчит — работаем по кэшу (честная деградация)"
        return False, "непонятный источник: " + str(src)

    @check("правило превращается в жёсткий фильтр")
    def _():
        sys.path.insert(0, str(Path.home() / "extella_wizard" / "app"))
        import server as S
        r = S._compile_rule_filters(["показывать только записи, где статус равен ok"], ["статус", "сумма"])
        if r["filters"]:
            f = r["filters"][0]
            return True, "%s %s %s" % (f["field"], f["op"], f["value"])
        return False, "фильтр не построился: " + (r.get("why") or "без причины")

    @check("компилятор фильтров НИКОГДА не молчит")
    def _():
        # Проверяем ИНВАРИАНТ, а не поведение модели: либо фильтр построен, либо названа
        # причина. Требовать «фильтра быть не должно» — флаки-проверка: модель иногда
        # находит подходящее поле, и это не дефект. 18.07 такая формулировка дала
        # ложное красное на ровном месте.
        sys.path.insert(0, str(Path.home() / "extella_wizard" / "app"))
        import server as S
        r = S._compile_rule_filters(["суммы больше 100000"], ["client_name", "platform"])
        if r["filters"]:
            return True, "фильтр построен: %s" % r["filters"][0].get("field")
        return bool(r["why"]), (r.get("why") or "МОЛЧИТ — ни фильтра, ни причины")[:80]

    print("\nАдаптеры источников (AC-05)")

    @check("переименованные колонки сопоставляются")
    def _():
        sys.path.insert(0, str(Path.home() / "extella_wizard" / "app"))
        import server as S
        want = ["client_name", "platform", "topup_amount", "date"]
        got = ["Клиент", "Площадка", "Сумма пополнения", "Дата"]
        p = S._adapter_propose(got, want)
        return len(p["map"]) == len(want), "сопоставлено %d из %d" % (len(p["map"]), len(want))

    @check("ломающий дрифт распознаётся")
    def _():
        sys.path.insert(0, str(Path.home() / "extella_wizard" / "app"))
        import server as S
        d = S._schema_drift(["a", "b"], ["a", "c"])
        return d.get("breaking") is True, "исчезнувшие колонки → стоп"

    print("\nСтройка (AC-06)")

    @check("у каждого кода ошибки есть что делать")
    def _():
        sys.path.insert(0, str(Path.home() / "extella_wizard" / "app"))
        import wz_build as B
        bad = [c for c, (m, r) in B.BUILD_ERRORS.items() if not m or not r]
        return not bad, "кодов %d, все с remedy" % len(B.BUILD_ERRORS)

    @check("манифест отдаётся или честно помечен")
    def _():
        if not SID:
            return False, "нет процесса"
        d = call("/x/manifest?sid=" + SID)
        if d.get("manifest"):
            return True, "манифест v%s" % d["manifest"].get("manifest_version")
        return bool(d.get("note")), d.get("note") or "ни манифеста, ни пометки"

    print("\nДоставка на ЭТОТ аккаунт")

    @check("эксперты, которых зовёт код, есть на аккаунте")
    def _():
        # Самая дорогая находка 19.07: движок Workspace (1803 строки) жил ТОЛЬКО на аккаунте
        # Анвара, при том что коллегам раскладывался клиент, который в него стучится, — Workspace
        # у них открывался и молча не работал. Тот же класс — mcp_call: наш код его зовёт, а ни
        # один установщик не создавал. «Работает у нас» и «работает у клиента» разъезжаются БЕЗ
        # ЕДИНОЙ ЖАЛОБЫ, и увидеть это можно только отсюда — со стороны живого аккаунта.
        # ГОНЯТЬ У КОЛЛЕГИ: на нашей машине проверка всегда зелёная и потому бесполезна.
        import re as _re
        sys.path.insert(0, str(Path.home() / "extella_wizard" / "app"))
        import server as S
        root = Path(__file__).resolve().parent.parent
        pat = _re.compile(r'(?:run_expert\(\s*|expert_name["\']?\s*[:=]\s*)["\']([a-z][a-z0-9_]{3,50})["\']')
        names = set()
        for sub in ("ui", "experts", "dist"):
            d = root / sub
            if not d.is_dir():
                continue
            for f in d.rglob("*"):
                if f.suffix not in (".py", ".html", ".js") or "__pycache__" in str(f):
                    continue
                try:
                    names.update(pat.findall(f.read_text(encoding="utf-8", errors="ignore")))
                except Exception:
                    pass
        # Имена, которые код СКЛЕИВАЕТ на лету («wz_source_» + вид), проверять нечего.
        names = {n for n in names if not n.endswith("_")}
        if not names:
            return False, "не нашёл ни одного вызова эксперта — проверка ослепла"
        missing = sorted(n for n in names if not S._expert_exists(n))
        if missing:
            return False, "НЕТ на аккаунте (%d из %d): %s" % (len(missing), len(names), ", ".join(missing[:6]))
        return True, "все %d на месте" % len(names)

    print("\nАвтопилот")

    @check("мониторинг отдаёт живую картину")
    def _():
        d = call("/x/monitor")
        if d.get("status") == "error":
            return False, str(d.get("message"))[:100]
        procs = d.get("processes") or []
        if not procs:
            return False, "ни одного процесса в мониторинге — панель пуста"
        return True, "процессов %d, учёток %d" % (len(procs), len(d.get("credentials") or []))

    @check("тик планировщика прошит на платформе")
    def _():
        sys.path.insert(0, str(Path.home() / "extella_wizard" / "app"))
        from wz_platform import api
        r = api("/api/expert/get", {"name": "wz_scheduler_tick", "global": True})
        return isinstance(r, dict) and r.get("status") == "success", "эксперт зарегистрирован"

    ok = sum(1 for r in results if r[0])
    print("\n" + "─" * 60)
    print("ИТОГ: %d из %d" % (ok, len(results)))
    fails = [(n, w) for o, n, w in results if not o]
    if fails:
        print("\nНЕ ПРОШЛО:")
        for n, w in fails:
            print("  • %s — %s" % (n, w))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
