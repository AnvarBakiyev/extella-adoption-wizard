#!/usr/bin/env python3
# Автопилот воркспейса — САМОХОДНОСТЬ: выполняет следующие capability-задачи по очереди,
# пока не упрётся в задачу, где нужен человек (kind=question|human) или в то, что движок пока не умеет.
#
# Архитектура (как сборщик workspace_refresh.py): локальный ДРАЙВЕР прогоняет задачи через движок
# (op=run_capability с key=id задачи → способность делает работу и САМА закрывает свою задачу через _advance).
# Многошаговая оркестрация живёт здесь, а не внутри одного вызова эксперта (иначе таймаут).
#
# Использование:  python3 workspace_autopilot.py <ws_id> [макс_шагов]
# Тулбар (когда автопилот включён) запускает этот драйвер, затем перечитывает get и рисует.
import json, os, sys, time, urllib.request
from pathlib import Path

try:
    sys.stdout.reconfigure(line_buffering=True)   # журнал живой: строки видны сразу, а не после завершения
except Exception:
    pass

BASE = "https://api.extella.ai"

def _token():
    cfg = Path.home() / "extella_wizard" / "app" / "config.json"
    try: return json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception: return ""

def _post(path, body, t=180):
    tok = _token()
    if not tok: raise SystemExit("нет api_token (~/extella_wizard/app/config.json)")
    H = {"X-Auth-Token": tok, "Content-Type": "application/json",
         "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    req = urllib.request.Request(BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read().decode("utf-8"))

def _run(params, t=180, expert="wz_workspace"):
    r = _post("/api/expert/run", {"expert_name": expert, "params": params, "global": True}, t)
    res = r.get("result"); tid = r.get("task_id")
    if isinstance(res, str) and "deferred" in res.lower() and tid:
        for _ in range(90):
            time.sleep(2)
            c = _post("/api/tasks/check", {"task_id": tid}, 60)
            rr = c.get("result")
            if rr and (not isinstance(rr, str) or "deferred" not in rr.lower()):
                return rr
    return res

def _obj(res):
    try: return json.loads(res) if isinstance(res, str) else res
    except Exception: return {}

def _scrub(s):
    # журнал автопилота — файл на диске: итоги выращенных способностей чистим от секретов (канон)
    import re as _re
    s = str(s or "")
    try:
        toks = [_token()]
        tp = Path.home() / ".extella_test_token"
        if tp.exists(): toks.append(tp.read_text(encoding="utf-8").strip())
        for t in toks:
            if t and len(t) > 8 and t in s: s = s.replace(t, "•••")
    except Exception: pass
    return _re.sub(r"\bsk-[A-Za-z0-9_\-]{10,}", "sk-•••", s)

def _kv(op, key, val=None):
    try:
        if op == "get":
            r = _post("/api/kv/get", {"key": key}, 30)
            return json.loads((r or {}).get("value") or "null")
        _post("/api/kv/set", {"key": key, "value": json.dumps(val, ensure_ascii=False), "description": "autopilot stats"}, 30)
    except Exception:
        return None

def drive(ws_id, max_steps=12):
    ws = _obj(_run({"op": "get", "ws_id": ws_id})).get("workspace")
    if not ws:
        print("воркспейс не найден:", ws_id); return
    if not (ws.get("autopilot") or {}).get("enabled"):
        print("автопилот выключен — включи set_autopilot enabled=true"); return
    # СТОП-УСЛОВИЯ (P0-5): суточный лимит прогонов + кулдаун между прогонами
    import datetime as _dt
    today = _dt.date.today().isoformat()
    st = _kv("get", "ws:%s:autopilot_stats" % ws_id) or {}
    DAILY_LIMIT = 25   # страховка от «сорвавшегося» автопилота; для стройки поднято с 10 (16.07). Ручной запуск через чат не ограничен.
    if st.get("date") == today and int(st.get("runs", 0)) >= DAILY_LIMIT:
        print("⛔ Суточный лимит прогонов автопилота (%d) исчерпан — до завтра или запусти вручную нужную способность." % DAILY_LIMIT); return
    if st.get("last_ts") and (time.time() - float(st["last_ts"])) < 60:
        print("⏳ Кулдаун: прошлый прогон был меньше минуты назад — подожди чуть-чуть."); return
    _kv("set", "ws:%s:autopilot_stats" % ws_id,
        {"date": today, "runs": (int(st.get("runs", 0)) + 1) if st.get("date") == today else 1, "last_ts": time.time()})
    print("🤖 Автопилот «%s» — веду план сам, остановлюсь где нужен ты." % ws.get("name"))
    done = []
    prev_id = None
    stop_hint = ""
    for _ in range(max_steps):
        ws = _obj(_run({"op": "get", "ws_id": ws_id})).get("workspace") or {}
        todo = [t for t in ws.get("tasks", []) if t.get("status") != "done"]
        if not todo:
            print("✅ Все задачи закрыты. Цели:", [g.get("progress") for g in ws.get("goals", [])]); break
        nxt = todo[0]
        if prev_id and nxt.get("id") == prev_id:   # задача не закрылась после выполнения → жёсткий стоп (не крутимся)
            print("⏸  Стоп: задача «%s» не закрылась после выполнения — посмотри вручную." % nxt.get("title")); break
        prev_id = nxt.get("id")
        if nxt.get("kind") in ("question", "human"):
            tag = "ответь на вопрос" if nxt["kind"] == "question" else "нужно твоё действие"
            print("⏸  Стоп — %s: «%s»" % (tag, nxt.get("title"))); break
        # capability → выполняем
        print("▶  Делаю: «%s»…" % nxt.get("title"))
        d = _obj(_run({"op": "run_capability", "ws_id": ws_id, "capability": nxt.get("title"), "key": nxt.get("id")}))
        if d.get("kind") == "cap_dispatch":
            # выращенная способность: движок дал направление — исполняем сами и закрываем задачу
            print("   ⚙️  Своя способность «%s» — запускаю…" % d.get("cap_name"))
            out = _obj(_run(dict(d.get("params") or {}), t=240, expert=d.get("expert_name")))
            if isinstance(out, dict) and out.get("status") == "success":
                print("   " + _scrub(out.get("note") or "готово")[:180])
                d = _obj(_run({"op": "cap_result", "ws_id": ws_id, "key": nxt.get("id"),
                               "value": _scrub(out.get("note") or "")[:200], "capability": nxt.get("title")}))
            else:
                print("   ⏸  способность «%s» упала: %s. Стоп." % (d.get("cap_name"), _scrub((out or {}).get("message") or (out or {}).get("note") or out)[:120])); break
        if d.get("advanced"):
            done.append(nxt.get("title"))
            print("   ✓ готово → дальше: «%s»" % (d.get("task_next") or "—"))
        elif d.get("gap") or d.get("queued"):
            # ТУПИК → система проектирует себе способность сама; регистрирует только человек (канон)
            print("   🧠 Такого не умею — проектирую новую способность…")
            pv = _obj(_run({"op": "cap_design", "ws_id": ws_id, "value": nxt.get("title"), "n": "auto"}, t=300))
            if pv.get("kind") == "cap_exists":
                # похожая уже выращена, но триггеры не совпали — запускаем её напрямую
                print("   ⚙️  «%s» уже выращена — запускаю её…" % pv.get("cap_name"))
                out = _obj(_run(dict(pv.get("params") or {}), t=240, expert=pv.get("expert_name")))
                if isinstance(out, dict) and out.get("status") == "success":
                    print("   " + _scrub(out.get("note") or "готово")[:180])
                    d2 = _obj(_run({"op": "cap_result", "ws_id": ws_id, "key": nxt.get("id"),
                                    "value": _scrub(out.get("note") or "")[:200], "capability": nxt.get("title")}))
                    if d2.get("advanced"):
                        done.append(nxt.get("title")); prev_id = None
                        print("   ✓ готово → дальше: «%s»" % (d2.get("task_next") or "—")); continue
                print("   ⏸  способность «%s» не закрыла задачу. Стоп." % pv.get("cap_name")); break
            if pv.get("kind") == "cap_preview":
                print("   💡 Спроектировал: «%s» — %s" % (pv.get("cap_name"), (pv.get("what") or "")[:120]))
                print("   ⏸  Скажи «разрешаю» в кокпите (вопрос в Обзоре или карточка в чате) и нажми ▶ Запустить ещё раз — продолжу с этого места. Стоп.")
                stop_hint = "Спроектировал способность «%s» — скажи «разрешаю» в кокпите и запусти меня ещё раз." % pv.get("cap_name")
            else:
                print("   ⏸  спроектировать не вышло: %s. Стоп." % str(pv.get("message") or "")[:100])
            break
        else:
            print("   ⏸  не могу выполнить автоматически: «%s» (%s). Стоп." % (nxt.get("title"), (d.get("note") or "")[:70])); break
    print("\nИтог: автопилот сделал %d задач(и): %s" % (len(done), "; ".join(done) or "—"))
    if stop_hint:
        print(stop_hint)   # последняя строка журнала = тост кокпита: несём следующий шаг, а не «0 задач»
    # наблюдаемость (P1-7): компактные счётчики воркспейса — без содержимого и секретов
    st2 = _kv("get", "ws:%s:stats" % ws_id) or {}
    _kv("set", "ws:%s:stats" % ws_id, {"runs": int(st2.get("runs", 0)) + 1,
        "caps_done": int(st2.get("caps_done", 0)) + len(done), "last_run": time.strftime("%Y-%m-%dT%H:%M:%S")})

if __name__ == "__main__":
    if len(sys.argv) < 2:
        raise SystemExit("укажи ws_id:  python3 workspace_autopilot.py <ws_id> [макс_шагов]")
    drive(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 12)
