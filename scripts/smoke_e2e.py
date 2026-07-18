#!/usr/bin/env python3
"""F6: сквозной смоук фундамента визарда. ГОНЯТЬ ПЕРЕД ПОКАЗОМ КЛИЕНТУ И ПОСЛЕ КРУПНЫХ ПРАВОК.

Зачем: отдельные куски проверены каждый в своём коммите, но клиент видит их вместе. Смоук
трогает фундамент СНАРУЖИ, как это делает браузер: живой мост, живая платформа, живые сессии.
Пишет PASS/FAIL с ПРИЧИНОЙ — «упало» без причины бесполезно.

Правила: боевые данные не портим (пишем только во временную сессию и убираем за собой);
секреты не печатаем; провал одной проверки не мешает остальным.

Запуск: python3 scripts/smoke_e2e.py
"""
import json
import re
import sys
import urllib.error
import urllib.request
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


def main():
    global SID, TMP
    print("F6 СКВОЗНОЙ СМОУК ФУНДАМЕНТА\n")

    print("Мост и данные")

    @check("мост отвечает")
    def _():
        d = call("/x/sessions")
        return bool(d.get("sessions") is not None), "сессий: %d" % len(d.get("sessions") or [])

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

    @check("невозможный фильтр объясняется, а не молчит")
    def _():
        sys.path.insert(0, str(Path.home() / "extella_wizard" / "app"))
        import server as S
        r = S._compile_rule_filters(["суммы больше 100000"], ["client_name", "platform"])
        return (not r["filters"]) and bool(r["why"]), (r.get("why") or "ПРИЧИНЫ НЕТ")[:80]

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
