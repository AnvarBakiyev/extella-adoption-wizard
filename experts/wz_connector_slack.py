# expert: wz_connector_slack
# description: Коннектор Slack (вывод результата через Incoming Webhook). Исполняется на ХОСТИНГЕ: расшифровывает {webhook_url} из vault локальным ключом и постит те
# params: api_token, client, mode, text, api_base

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_connector_slack(api_token: str = "", client: str = "default", mode: str = "validate",
                       text: str = "", api_base: str = "https://api.extella.ai") -> dict:
    """Коннектор Slack (вывод результата через Incoming Webhook). Исполняется на ХОСТИНГЕ:
    расшифровывает {webhook_url} из vault локальным ключом и постит текст. validate=пробный пост, send=результат."""
    import json, socket, re, hashlib
    from pathlib import Path

    def ns(s):
        s = str(s)
        return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:40] + "_" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    cands = [Path("/opt/extella-listener/extella_wizard/vault.key"),
             Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
    kp = next((c for c in cands if c.exists()), None)
    if not kp:
        return {"ok": False, "err": "vault.key не найден на устройстве"}
    key = "sec:" + ns(client) + ":" + ns("slack")
    try:
        ct = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json().get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "коннектор Slack не подключён (нет секрета)"}
    try:
        env = json.loads(Fernet(kp.read_bytes()).decrypt(ct.encode()).decode())
        if env.get("c") != client:
            return {"ok": False, "err": "привязка секрета к клиенту не совпала"}   # #8 client-isolation: проверка привязки на ЧТЕНИИ
        if env.get("k") != "slack":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался slack)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}
    url = creds.get("webhook_url", "")
    if not url.startswith("http"):
        return {"ok": False, "err": "нет корректного webhook_url"}

    def _log(res):
        if mode != "validate":
            try:
                from datetime import datetime, timezone
                requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                              json={"key": "connlog:" + ns(client) + ":slack",
                                    "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(), "mode": "send", "ok": res.get("ok"), "err": res.get("err")}),
                                    "description": "connlog"}, timeout=30)
            except Exception:
                pass
        return res

    body = text or ("✅ Extella подключён к Slack" if mode == "validate" else "Тест Extella")
    try:
        r = requests.post(url, json={"text": body}, timeout=20)
        if r.status_code == 200 and (r.text or "").strip() == "ok":
            res = {"ok": True, "host": socket.gethostname(), "channel": "slack"}
        else:
            res = {"ok": False, "err": "slack: HTTP %s %s" % (r.status_code, (r.text or "")[:80])}
    except Exception as e:
        res = {"ok": False, "err": "сеть slack: " + str(e)[:100]}
    return _log(res) if mode != "validate" else res
