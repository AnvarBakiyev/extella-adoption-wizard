# expert: wz_connector_sms
# description: Коннектор SMS (Twilio). Исполняется на ХОСТИНГЕ: расшифровывает {account_sid, auth_token, from, to} из vault и шлёт SMS. validate=проверка аккаунта (G
# params: api_token, client, mode, text, api_base

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_connector_sms(api_token: str = "", client: str = "default", mode: str = "validate",
                     text: str = "", api_base: str = "https://api.extella.ai") -> dict:
    """Коннектор SMS (Twilio). Исполняется на ХОСТИНГЕ: расшифровывает {account_sid, auth_token, from, to}
    из vault и шлёт SMS. validate=проверка аккаунта (GET), send=SMS. auth_token не логируется."""
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
    key = "sec:" + ns(client) + ":" + ns("sms")
    try:
        ct = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json().get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "коннектор SMS не подключён (нет секрета)"}
    try:
        env = json.loads(Fernet(kp.read_bytes()).decrypt(ct.encode()).decode())
        if env.get("c") != client:
            return {"ok": False, "err": "привязка секрета к клиенту не совпала"}   # #8 client-isolation: проверка привязки на ЧТЕНИИ
        if env.get("k") != "sms":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался sms)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}
    sid = creds.get("account_sid", "")
    tok = creds.get("auth_token", "")
    frm = creds.get("from", "")
    to = creds.get("to", "")
    if not sid or not tok:
        return {"ok": False, "err": "нет account_sid/auth_token"}
    base = "https://api.twilio.com/2010-04-01/Accounts/" + sid

    def _log(res):
        if mode != "validate":
            try:
                from datetime import datetime, timezone
                requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                              json={"key": "connlog:" + ns(client) + ":sms",
                                    "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(), "mode": "send", "ok": res.get("ok"), "err": res.get("err")}),
                                    "description": "connlog"}, timeout=30)
            except Exception:
                pass
        return res

    try:
        if mode == "validate":
            r = requests.get(base + ".json", auth=(sid, tok), timeout=20)
            if r.status_code == 200:
                return {"ok": True, "host": socket.gethostname(), "channel": "sms"}
            return {"ok": False, "err": "twilio: HTTP %s %s" % (r.status_code, (r.text or "")[:80])}
        if not frm or not to:
            return _log({"ok": False, "err": "нет from/to"})
        r = requests.post(base + "/Messages.json", auth=(sid, tok),
                          data={"From": frm, "To": to, "Body": text or "Тест Extella"}, timeout=20)
        j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code in (200, 201) and j.get("sid"):
            return _log({"ok": True, "message_id": j.get("sid"), "host": socket.gethostname(), "channel": "sms"})
        return _log({"ok": False, "err": "twilio: " + str(j.get("message", (r.text or "")[:80]))})
    except Exception as e:
        return _log({"ok": False, "err": "сеть twilio: " + str(e)[:100]})
