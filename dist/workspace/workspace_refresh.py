#!/usr/bin/env python3
# Локальный сборщик состояния курируемого Workspace — «кнопка обновления».
# По каждому контуру собирает СВЕЖУЮ АКТИВНОСТЬ (git-коммиты / недавно изменённые файлы) и пишет её
# в contour.state.recent через wz_workspace op=set_state. Курируемые списки (done/next/blockers/waiting)
# НЕ трогает — их ведёт архитектор. Живые сигналы — на машине пользователя, поэтому сборщик ЛОКАЛЬНЫЙ.
#
# Использование:  python3 workspace_refresh.py [ws_id]
#   ws_id по умолчанию — «Работа с Экстеллой» (ws_0d08f06535).
# Тулбар вызывает этот скрипт по нажатию «↻ Обновить», затем перечитывает op=get и перерисовывает кокпит.
import json, os, sys, subprocess, urllib.request
from pathlib import Path
from datetime import datetime

BASE = "https://api.extella.ai"
DEFAULT_WS = "ws_0d08f06535"

def _token():
    cfg = Path.home() / "extella_wizard" / "app" / "config.json"
    try:
        return json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "")
    except Exception:
        return ""

def _post(path, body, t=60):
    tok = _token()
    if not tok:
        raise SystemExit("нет api_token (~/extella_wizard/app/config.json)")
    H = {"X-Auth-Token": tok, "Content-Type": "application/json",
         "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    req = urllib.request.Request(BASE + path, data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=H, method="POST")
    with urllib.request.urlopen(req, timeout=t) as r:
        return json.loads(r.read().decode("utf-8"))

def _run(params, t=60):
    r = _post("/api/expert/run", {"expert_name": "wz_workspace", "params": params, "global": True}, t)
    res = r.get("result")
    return json.loads(res) if isinstance(res, str) and res.strip().startswith("{") else res

def _git(folder):
    try:
        top = subprocess.run(["git", "-C", folder, "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, timeout=8)
        if top.returncode != 0:
            return None
        log = subprocess.run(["git", "-C", folder, "log", "-4", "--pretty=%h · %s"],
                             capture_output=True, text=True, timeout=8).stdout.strip().splitlines()
        dirty = subprocess.run(["git", "-C", folder, "status", "--porcelain"],
                               capture_output=True, text=True, timeout=8).stdout.strip().splitlines()
        out = ["коммит " + l for l in log[:4]]
        if dirty:
            out.append("незакоммичено файлов: %d" % len(dirty))
        return out
    except Exception:
        return None

def _files(folder):
    try:
        entries = []
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if os.path.isfile(p) and not n.startswith("."):
                entries.append((os.path.getmtime(p), n))
        entries.sort(reverse=True)
        return ["изменён %s · %s" % (n, datetime.fromtimestamp(mt).strftime("%d.%m %H:%M"))
                for mt, n in entries[:4]]
    except Exception:
        return []

def recent_for(folder):
    folder = os.path.expanduser(folder or "")
    if not folder or not os.path.isdir(folder):
        return ["папка недоступна: " + (folder or "—")]
    return _git(folder) or _files(folder) or ["нет свежей активности"]

def main():
    ws_id = sys.argv[1] if len(sys.argv) > 1 else DEFAULT_WS
    ws = (_run({"op": "get", "ws_id": ws_id}) or {}).get("workspace")
    if not ws or ws.get("kind") != "curated":
        raise SystemExit("курируемый воркспейс не найден: " + ws_id)
    print("↻ Обновляю «%s» (%d контуров)" % (ws.get("name"), len(ws.get("contours", []))))
    for c in ws.get("contours", []):
        rec = recent_for(c.get("folder"))
        _run({"op": "set_state", "ws_id": ws_id, "contour": c["key"],
              "state": json.dumps({"recent": rec}, ensure_ascii=False)})
        print("  ✓ %-11s → %d строк активности" % (c["key"], len(rec)))
    print("Готово. Тулбар перечитывает op=get и рисует кокпит.")

if __name__ == "__main__":
    main()
