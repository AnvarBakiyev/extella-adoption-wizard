# expert: wz_open_wizard
# description: Adoption Wizard: native entry point for chat agents - opens the wizard UI on the user's device with one secret-free call. Designed to be invoked by an
# params: 

def wz_open_wizard(
    client_name: str = "",
    session_id: str = "",
    port: int = 8765,
    auto_open: bool = True,
    reuse_existing: bool = True
) -> dict:
    import json, os, subprocess, sys, time, uuid
    import urllib.request
    from pathlib import Path
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat()

    base = "http://127.0.0.1:" + str(int(port))
    app = Path.home() / "extella_wizard" / "app"
    sess_dir = Path.home() / "extella_wizard" / "sessions"
    sess_dir.mkdir(parents=True, exist_ok=True)

    # ── 1. Ensure the bridge is running ───────────────────────────────
    def alive():
        try:
            with urllib.request.urlopen(base + "/x/sessions", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    bridge = "already-running"
    if not alive():
        if not (app / "server.py").exists() or not (app / "config.json").exists():
            return {"status": "error",
                    "message": "Wizard app is not deployed on this device yet. "
                               "Run wz_wizard_serve once (with auth_token and llm_api_key) to install it."}
        log = open(app / "server.log", "a")
        subprocess.Popen([sys.executable, str(app / "server.py")],
                         stdout=log, stderr=log,
                         start_new_session=True, cwd=str(app))
        for _ in range(10):
            time.sleep(1)
            if alive():
                break
        if not alive():
            return {"status": "error", "message": "Bridge did not start; check " + str(app / "server.log")}
        bridge = "started"

    # ── 2. Resolve the session ────────────────────────────────────────
    def load(p):
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return None

    sid = ""
    if session_id:
        if not (sess_dir / (session_id + ".json")).exists():
            return {"status": "error", "message": "session not found: " + session_id}
        sid = session_id
    else:
        if reuse_existing and client_name:
            candidates = []
            for p in sess_dir.glob("wz_*.json"):
                if p.name.endswith("_blueprint.json"):
                    continue
                s = load(p)
                if s and str(s.get("client_name", "")).strip().lower() == client_name.strip().lower():
                    candidates.append(s)
            if candidates:
                sid = sorted(candidates, key=lambda s: s.get("updated_at", ""))[-1]["session_id"]
        if not sid:
            sid = "wz_" + datetime.now(timezone.utc).strftime("%Y%m%d") + "_" + uuid.uuid4().hex[:6]
            s = {"session_id": sid,
                 "client_name": client_name or "Без названия",
                 "created_at": now(), "updated_at": now(),
                 "stage": "interview",
                 "answers": {}, "comments": [], "blueprint_path": "",
                 "log": [{"ts": now(), "event": "session created by wz_open_wizard (agent call)"}]}
            (sess_dir / (sid + ".json")).write_text(
                json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

    url = base + "/?session=" + sid

    # ── 3. Open the default browser on this device ────────────────────
    opened = False
    # runtime drops falsy params: accept string forms to disable auto-open
    if str(auto_open).strip().lower() in ("0", "false", "no", "off"):
        auto_open = False
    if auto_open:
        try:
            if sys.platform == "darwin":
                subprocess.Popen(["open", url])
            elif os.name == "nt":
                os.startfile(url)
            else:
                subprocess.Popen(["xdg-open", url])
            opened = True
        except Exception:
            opened = False

    return {"status": "success", "url": url, "session_id": sid,
            "bridge": bridge, "opened": opened,
            "hint": "Визард открыт в браузере устройства" if opened else "Откройте ссылку в браузере: " + url}