#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# Плагин Extella «Workspace» — локальный сервер кокпита (порт 34767).
# Роль: (1) отдаёт UI (index.html); (2) прокси к движку wz_workspace — ТОКЕН ЖИВЁТ ТУТ, в браузер не уходит,
# deferred-задачи доводятся до результата на сервере; (3) локальные действия: запуск автопилота (Popen драйвера),
# приём файлов (upload → в папку проекта), обзор папок для пикера.
import json, os, sys, time, base64, subprocess, socket, urllib.request, urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PORT = 34767
HERE = os.path.dirname(os.path.abspath(__file__))
BASE = "https://api.extella.ai"
HOME = os.path.expanduser("~")

def _token():
    try:
        cfg = os.path.join(HOME, "extella_wizard", "app", "config.json")
        return json.loads(open(cfg, encoding="utf-8").read()).get("auth_token", "")
    except Exception:
        return ""

def _api(path, body, t=120):
    tok = _token()
    req = urllib.request.Request(BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers={"X-Auth-Token": tok, "Content-Type": "application/json",
                                          "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}, method="POST")
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read().decode("utf-8"))

def ws_call(params, wait=180, expert="wz_workspace"):
    """Вызов движка (или выращенной способности) + доведение deferred до результата (поллинг на сервере)."""
    r = _api("/api/expert/run", {"expert_name": expert, "params": params, "global": True})
    res = r.get("result"); tid = r.get("task_id")
    if isinstance(res, str) and "deferred" in res.lower() and tid:
        deadline = time.time() + wait
        while time.time() < deadline:
            time.sleep(2)
            try:
                c = _api("/api/tasks/check", {"task_id": tid}, 60)
                rr = c.get("result")
                if rr and (not isinstance(rr, str) or "deferred" not in rr.lower()):
                    res = rr; break
            except Exception:
                pass
        else:
            return {"status": "error", "error": "timeout", "message": "движок думает дольше обычного — повтори"}
    try:
        return json.loads(res) if isinstance(res, str) else (res or {})
    except Exception:
        return {"status": "error", "message": "нечитаемый ответ движка"}


WIZARD_BASE = "http://127.0.0.1:8765"


def _wizard_call(path, body=None, timeout=20):
    """Read/mutate the canonical Wizard Process Contract; Workspace owns no process copy."""
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(WIZARD_BASE + path, data=data,
                                 headers={"Content-Type": "application/json"},
                                 method="GET" if body is None else "POST")
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def process_list(limit=12):
    """Compact read model for existing Workspace: inputs, artifacts, checks and step history."""
    try:
        sessions = (_wizard_call("/x/sessions") or {}).get("sessions") or []
    except Exception as exc:
        return {"status": "error", "message": "Wizard недоступен: " + str(exc)[:120], "processes": []}
    rows = []
    for session in sorted(sessions, key=lambda x: str(x.get("updated_at") or ""), reverse=True):
        if not session.get("process_id") or len(rows) >= max(1, min(int(limit or 12), 30)):
            continue
        sid = str(session.get("session_id") or "")
        try:
            doc = _wizard_call("/x/process?" + urllib.parse.urlencode({
                "session_id": sid, "surface": "workspace"}))
            graph = doc.get("process") if isinstance(doc, dict) else None
            if not isinstance(graph, dict):
                continue
            steps = []
            for step in graph.get("steps") or []:
                if not isinstance(step, dict):
                    continue
                steps.append({
                    "id": step.get("id"), "title": step.get("title"),
                    "status": step.get("status"), "version": step.get("version"),
                    "mode": (step.get("implementation") or {}).get("mode"),
                    "expert_ref": (step.get("implementation") or {}).get("expert_ref"),
                    "input_contract": step.get("input_contract"),
                    "output_contract": step.get("output_contract"),
                    "artifacts": step.get("artifact_refs") or [],
                    "evidence": step.get("evidence") or [], "error": step.get("error"),
                    "human_gate": step.get("human_gate"),
                    "attempts_count": len(step.get("attempts") or []),
                })
            rows.append({
                "session_id": sid, "client_name": session.get("client_name"),
                "updated_at": session.get("updated_at"), "process_id": graph.get("process_id"),
                "process_version": graph.get("version"), "process_status": doc.get("process_status"),
                "title": graph.get("title"), "goal": graph.get("goal"), "steps": steps,
                "events": (doc.get("events") or [])[-30:],
            })
        except Exception:
            continue
    return {"status": "success", "source": "wizard-upc/1.0", "processes": rows}


def process_action(body):
    payload = {k: body.get(k) for k in ("session_id", "action", "step_id", "reason",
                                                   "answer", "permission", "target", "payload", "approved")
               if k in body}
    payload["surface"] = "workspace"
    try:
        return _wizard_call("/x/process_action", payload, timeout=30)
    except Exception as exc:
        return {"status": "error", "message": "действие не принято Wizard: " + str(exc)[:160]}

REG_KW = ("реестр", "трекер", "консолид", "единый", "портфел", "собери все", "registry")
WRITE_KW = ("запиш", "запис", "сохран", "выгруз", "экспорт", "подтвержда")

def registry_chunked(ws_id):
    """Большая сборка реестра — кусочками через registry_chunk (без платформенных таймаутов)."""
    off = 0; last = {}
    for _ in range(30):
        d = ws_call({"op": "registry_chunk", "ws_id": ws_id, "n": str(off)}, 240)
        if d.get("status") != "success":
            return d
        last = d
        if d.get("need_owner"):
            # владелец не задан — отдаём обычному run_capability, он положит вопрос в инбокс
            return ws_call({"op": "run_capability", "ws_id": ws_id, "capability": "собери реестр"}, 240)
        if d.get("done"): break
        off = d.get("next_offset") or (off + 6)
    dr = ws_call({"op": "get", "ws_id": ws_id}, 60)
    return {"status": "success", "kind": "registry", "rows": [], "note": last.get("note", "Реестр собран"),
            "chunked": True, "rows_so_far": last.get("rows_so_far"), "workspace": (dr or {}).get("workspace")}

def maybe_heavy(params):
    """Прозрачный апгрейд: сборка реестра на большой папке из чата → кусочками."""
    import re as _re
    if params.get("op") != "run_capability": return None
    text = str(params.get("capability") or "").lower()
    if _re.match(r"^\s*(научись|научи себя|выучи)\b", text): return None   # «научись: собери реестр…» — это про способность, движку
    if not any(k in text for k in REG_KW): return None
    if any(k in text for k in WRITE_KW): return None   # запись/подтверждение — обычным путём
    w = ws_call({"op": "get", "ws_id": params.get("ws_id", "")}, 60)
    ws = (w or {}).get("workspace") or {}
    if int(ws.get("sources_total") or 0) > 12 and ws.get("folder"):
        return registry_chunked(params.get("ws_id"))
    return None

def _ap_paths(ws_id):
    drv = os.path.join(HOME, "extella-plugins", "workspace", "workspace_autopilot.py")
    if not os.path.isfile(drv):
        drv = os.path.join(HERE, "workspace_autopilot.py")
    logdir = os.path.join(os.path.dirname(drv), "logs")
    sid = str(ws_id)[-8:]
    return drv, os.path.join(logdir, "autopilot_%s.log" % sid), os.path.join(logdir, "autopilot_%s.pid" % sid)

AP_PROCS = {}   # живые Popen текущего сервера: poll() подбирает зомби и честно говорит «закончился»

def _ap_running(ws_id, pidf):
    p = AP_PROCS.get(str(ws_id))
    if p is not None:
        return p.poll() is None
    try:   # после рестарта сервера — по pid-файлу; зомби (Z) считаем завершённым
        pid = int(open(pidf).read().strip())
        st = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
        return bool(st) and not st.startswith("Z")
    except Exception:
        return False

def _scrub(s):
    """итог выращенной способности — недоверенный текст: чистим секреты перед чатом/логами (канон)."""
    import re as _re
    s = str(s or "")
    try:
        toks = [_token()]
        tp = os.path.join(HOME, ".extella_test_token")
        if os.path.isfile(tp): toks.append(open(tp, encoding="utf-8").read().strip())
        for t in toks:
            if t and len(t) > 8 and t in s: s = s.replace(t, "•••")
    except Exception: pass
    return _re.sub(r"\bsk-[A-Za-z0-9_\-]{10,}", "sk-•••", s)

def cap_sanity(expert_name, ws_id):
    """ПОСТ-ПРОВЕРКА (вариант А): СРАЗУ после «разрешаю» прогнать одобренную способность ОДИН раз и показать
    результат. Код уже одобрен человеком (канон соблюдён). Задачи НЕ трогаем — только показываем note, чтобы
    человек увидел числа и при абсурде нажал «Разучиться». KV-запись способности (cap_data) — её штатный вывод."""
    if not (expert_name and ws_id):
        return {"status": "error", "message": "нет способности для проверки"}
    out = ws_call({"ws_id": ws_id}, 200, expert=expert_name)
    if isinstance(out, dict) and out.get("status") == "success":
        # UI показывает только note; data наружу не отдаём (защита от утечки секрета мимо скраба — находка ревью)
        return {"status": "success", "note": _scrub(out.get("note") or "")[:400]}
    return {"status": "error", "message": _scrub((out or {}).get("message") or (out or {}).get("note") or "прогон не дал результата")[:200]}

def run_dispatch(d):
    """kind:cap_dispatch из чата: прогнать выращенную способность и закрыть её задачу через cap_result."""
    prm = d.get("params") or {}
    out = ws_call(prm, 240, expert=d.get("expert_name") or "")
    if not (isinstance(out, dict) and out.get("status") == "success"):
        return {"status": "error", "message": "способность «%s» упала: %s" % (d.get("cap_name"), _scrub((out or {}).get("message") or "")[:120])}
    note = _scrub(out.get("note") or "")[:300]
    rr = ws_call({"op": "cap_result", "ws_id": prm.get("ws_id", ""), "key": d.get("key") or "",
                  "value": note[:200], "capability": d.get("cap_name") or ""}, 60)
    return {"status": "success", "kind": "cap_run", "cap_name": d.get("cap_name"), "note": note,
            "data": out.get("data"), "advanced": (rr or {}).get("advanced"), "task_next": (rr or {}).get("task_next")}

def autopilot(ws_id):
    drv, logf, pidf = _ap_paths(ws_id)
    if not os.path.isfile(drv):
        return {"status": "error", "message": "драйвер автопилота не найден — запусти deploy_drivers.py"}
    if _ap_running(ws_id, pidf):   # замок: повторный клик не плодит второй процесс
        return {"status": "success", "already": True, "note": "Автопилот уже работает — показываю его журнал"}
    os.makedirs(os.path.dirname(logf), exist_ok=True)
    with open(logf, "w") as lf:
        p = subprocess.Popen([sys.executable or "python3", "-u", drv, str(ws_id)], cwd=os.path.dirname(drv),
                             stdout=lf, stderr=subprocess.STDOUT, start_new_session=True)
    AP_PROCS[str(ws_id)] = p
    with open(pidf, "w") as pf:
        pf.write(str(p.pid))
    return {"status": "success", "log": logf, "note": "Автопилот запущен"}

def autopilot_log(ws_id):
    _, logf, pidf = _ap_paths(ws_id)
    try:
        lines = open(logf, encoding="utf-8", errors="ignore").read().strip().splitlines()
    except Exception:
        lines = []
    return {"status": "success", "lines": lines[-14:], "running": _ap_running(ws_id, pidf)}

def reveal(path, mode):
    p = os.path.realpath(os.path.expanduser(path or ""))
    if not p.startswith(os.path.realpath(HOME)) or not os.path.exists(p):
        return {"status": "error", "message": "файл не найден"}
    subprocess.Popen(["open", "-R", p] if mode == "folder" else ["open", p])
    return {"status": "success"}

def browse(path):
    p = os.path.realpath(os.path.expanduser(path or HOME))
    if not p.startswith(os.path.realpath(HOME)):   # только домашний каталог
        p = os.path.realpath(HOME)
    dirs, nfiles = [], 0
    try:
        for n in sorted(os.listdir(p)):
            if n.startswith("."): continue
            fp = os.path.join(p, n)
            if os.path.isdir(fp): dirs.append(n)
            else: nfiles += 1
    except Exception:
        pass
    parent = os.path.dirname(p) if os.path.realpath(p) != os.path.realpath(HOME) else ""
    return {"status": "success", "path": p, "parent": parent, "dirs": dirs[:200], "files_count": nfiles}

def upload(ws_id, folder, filename, b64):
    # сохранить присланный файл в папку проекта (подпапка «Прикреплённые») и вернуть путь
    tgt_dir = os.path.expanduser(folder or "") or os.path.join(HOME, "Downloads")
    if not os.path.isdir(tgt_dir): tgt_dir = os.path.join(HOME, "Downloads")
    dstdir = os.path.join(tgt_dir, "Прикреплённые"); os.makedirs(dstdir, exist_ok=True)
    safe = os.path.basename(filename or "файл")
    dst = os.path.join(dstdir, safe); n = 2
    while os.path.exists(dst):
        r, e = os.path.splitext(safe); dst = os.path.join(dstdir, "%s_%d%s" % (r, n, e)); n += 1
    with open(dst, "wb") as f:
        f.write(base64.b64decode(b64 or ""))
    return {"status": "success", "path": dst}

LOGF = os.path.join(HOME, "extella-plugins", "workspace", "logs", "plugin.log")
def _plog(event, detail=""):
    # журнал плагина: события без содержимого файлов и без токенов; ротация по 500КБ
    try:
        os.makedirs(os.path.dirname(LOGF), exist_ok=True)
        if os.path.exists(LOGF) and os.path.getsize(LOGF) > 500_000:
            os.replace(LOGF, LOGF + ".1")
        with open(LOGF, "a", encoding="utf-8") as f:
            f.write("%s %s %s\n" % (time.strftime("%Y-%m-%dT%H:%M:%S"), event, str(detail)[:140]))
    except Exception:
        pass

class H(BaseHTTPRequestHandler):
    def log_message(self, *a): pass
    def _send(self, code, body, ctype="application/json"):
        b = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype + "; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers(); self.wfile.write(b)
    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        try: return json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception: return {}
    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                self._send(200, open(os.path.join(HERE, "index.html"), "rb").read(), "text/html")
            except Exception as e:
                self._send(500, {"error": str(e)[:120]})
        elif path == "/health":
            self._send(200, {"ok": True, "token": bool(_token())})
        else:
            self._send(404, {"error": "not found"})
    def do_POST(self):
        path = self.path.split("?")[0]
        b = self._body()
        try:
            if path == "/ws":
                prm = b.get("params") or {}
                _plog("op:" + str(prm.get("op", "?")), (str(prm.get("ws_id", ""))[-10:] + " " + str(prm.get("capability", ""))[:60]).strip())
                hv = maybe_heavy(prm)
                res = hv if hv is not None else ws_call(prm, int(b.get("wait") or 180))
                if isinstance(res, dict) and res.get("kind") in ("cap_dispatch", "cap_exists"):
                    res = run_dispatch(res)   # выращенная способность: исполняем тут же, чат получает готовый итог
                self._send(200, res)
            elif path == "/cap_sanity":
                _plog("cap_sanity", str(b.get("ws_id", ""))[-10:] + " " + str(b.get("expert_name", ""))[:40])
                self._send(200, cap_sanity(b.get("expert_name", ""), b.get("ws_id", "")))
            elif path == "/autopilot":
                _plog("autopilot:run", str(b.get("ws_id", ""))[-10:])
                self._send(200, autopilot(b.get("ws_id", "")))
            elif path == "/autopilot_log": self._send(200, autopilot_log(b.get("ws_id", "")))
            elif path == "/browse":    self._send(200, browse(b.get("path", "")))
            elif path == "/reveal":    self._send(200, reveal(b.get("path", ""), b.get("mode", "file")))
            elif path == "/upload":    self._send(200, upload(b.get("ws_id", ""), b.get("folder", ""), b.get("filename", ""), b.get("b64", "")))
            elif path == "/processes": self._send(200, process_list(b.get("limit", 12)))
            elif path == "/process_action": self._send(200, process_action(b))
            else:                      self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(500, {"status": "error", "message": str(e)[:160]})

def main():
    try:
        with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
            print("port %d busy — уже запущен" % PORT); return
    except Exception:
        pass
    print("workspace plugin on http://127.0.0.1:%d" % PORT)
    ThreadingHTTPServer(("127.0.0.1", PORT), H).serve_forever()

if __name__ == "__main__":
    main()
