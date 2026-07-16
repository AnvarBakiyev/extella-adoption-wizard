$extens("include.py")
include("import urllib.request", [])

def wz_target_passport(api_token: str = "", api_base: str = "https://api.extella.ai", label: str = "") -> dict:
    """Мультитаргет T1 (docs/MULTITARGET_TZ.md): ЧЕСТНЫЙ паспорт устройства — исполняется
    листенером НА устройстве (локально или с target=...), собирает только проверяемые факты:
    ОС/арх/python, известные приложения по путям, локальные ollama-модели, свободный диск.
    Сам факт успешного прогона = листенер устройства жив. Пишет:
    - KV target:passport:<slug> (default-скоуп) — паспорт с passport_at;
    - RMW-индекс target:passports:__index__ {slugs:[...]} — для /x/targets и реестра.
    Данные, которые нельзя определить автоматически, НЕ выдумываются (ТЗ v2 §13.1)."""
    import json
    import os
    import platform
    import shutil
    import sys
    from datetime import datetime, timezone
    from pathlib import Path

    def _blank(v):
        return (not v) or str(v).startswith("{{")

    if _blank(api_token):
        try:
            api_token = json.loads((Path.home() / "extella_wizard" / "app" / "config.json")
                                   .read_text(encoding="utf-8")).get("auth_token", "")
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "нет api_token и bridge-конфига"}
    if _blank(api_base):
        api_base = "https://api.extella.ai"
    if _blank(label):
        label = ""

    HDRS = {"X-Auth-Token": api_token, "Content-Type": "application/json",
            "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def api(path, payload, timeout=60):
        req = urllib.request.Request(api_base.rstrip("/") + path,
                                     data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers=HDRS, method="POST")
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8"))

    host = platform.node() or "unknown"
    slug = "".join(c if c.isalnum() else "-" for c in host.lower())[:40].strip("-") or "unknown"

    # приложения — только проверяемые пути (не выдумываем)
    apps = []
    _mac_apps = {"Microsoft Excel": "/Applications/Microsoft Excel.app",
                 "LibreOffice": "/Applications/LibreOffice.app",
                 "1C": "/Applications/1cv8.app"}
    _lin_bins = {"LibreOffice": "soffice", "python3.12": "python3.12"}
    if sys.platform == "darwin":
        for name, p in _mac_apps.items():
            if os.path.exists(p):
                apps.append(name)
    else:
        for name, b in _lin_bins.items():
            if shutil.which(b):
                apps.append(name)

    models = []
    try:
        with urllib.request.urlopen("http://localhost:11434/api/tags", timeout=4) as f:
            tags = json.loads(f.read().decode("utf-8"))
        models = [m.get("name") for m in tags.get("models", [])][:20]
    except Exception:
        pass   # ollama нет — честно пустой список

    disk_free_gb = None
    try:
        disk_free_gb = round(shutil.disk_usage(str(Path.home())).free / 1e9, 1)
    except Exception:
        pass

    passport = {
        "slug": slug, "hostname": host, "label": label or host,
        "os": platform.system(), "os_version": platform.release(), "arch": platform.machine(),
        "python": sys.version.split()[0],
        "apps": apps, "ollama_models": models, "disk_free_gb": disk_free_gb,
        "listener_alive": True,   # факт: этот код исполнил листенер устройства
        "passport_at": datetime.now(timezone.utc).isoformat()[:19] + "Z",
    }
    try:
        api("/api/kv/set", {"key": "target:passport:" + slug,
                            "value": json.dumps(passport, ensure_ascii=False),
                            "description": "device passport " + slug})
        idx = []
        try:
            g = api("/api/kv/get", {"key": "target:passports:__index__"})
            idx = (json.loads(g.get("value") or "{}") or {}).get("slugs") or []
        except Exception:
            idx = []
        if slug not in idx:
            idx.append(slug)
            api("/api/kv/set", {"key": "target:passports:__index__",
                                "value": json.dumps({"slugs": idx[:50]}),
                                "description": "device passports index"})
    except Exception as e:
        return {"status": "error", "message": "KV write: " + str(e)[:150], "passport": passport}

    return {"status": "success", "slug": slug, "passport": passport}
