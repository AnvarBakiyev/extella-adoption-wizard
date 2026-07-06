# expert: wz_connector_whatsapp
# description: Коннектор WhatsApp (WhatsApp Business Cloud API, Meta). Исполняется на ХОСТИНГЕ: расшифровывает {phone_id, access_token, to} из vault и шлёт текстовое
# params: api_token, client, mode, text, api_base

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_connector_whatsapp(api_token: str = "", client: str = "default", mode: str = "validate",
                          text: str = "", api_base: str = "https://api.extella.ai") -> dict:
    """Коннектор WhatsApp (WhatsApp Business Cloud API, Meta). Исполняется на ХОСТИНГЕ:
    расшифровывает {phone_id, access_token, to} из vault и шлёт текстовое сообщение.
    validate=проверка метаданных номера (GET), send=сообщение. Токен не логируется."""
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
    key = "sec:" + ns(client) + ":" + ns("whatsapp")
    try:
        ct = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json().get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "коннектор WhatsApp не подключён (нет секрета)"}
    try:
        env = json.loads(Fernet(kp.read_bytes()).decrypt(ct.encode()).decode())
        if env.get("k") != "whatsapp":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался whatsapp)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}
    to = str(creds.get("to", ""))
    # провайдер: GREEN-API (обычный номер по QR, популярно в СНГ) или Meta Cloud (официальный Business API)
    provider = creds.get("provider") or ("green" if creds.get("id_instance") else "meta")

    def _log(res):
        if mode != "validate":
            try:
                from datetime import datetime, timezone
                requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                              json={"key": "connlog:" + ns(client) + ":whatsapp",
                                    "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(), "mode": "send", "ok": res.get("ok"), "err": res.get("err")}),
                                    "description": "connlog"}, timeout=30)
            except Exception:
                pass
        return res

    if provider == "green":
        idi = str(creds.get("id_instance", ""))
        atok = creds.get("api_token", "")
        gbase = str(creds.get("api_url", "https://api.green-api.com")).rstrip("/") + "/waInstance" + idi
        if not idi or not atok:
            return {"ok": False, "err": "нет id_instance/api_token (GREEN-API)"}
        def _gj(resp):
            try:
                return resp.json()
            except Exception:
                return {"_http": resp.status_code, "_text": (resp.text or "")[:80]}
        try:
            if mode == "validate":
                r = _gj(requests.get(gbase + "/getStateInstance/" + atok, timeout=20))
                if r.get("stateInstance") == "authorized":
                    return {"ok": True, "host": socket.gethostname(), "channel": "whatsapp", "provider": "green"}
                if r.get("stateInstance"):
                    return {"ok": False, "err": "green-api: инстанс не авторизован (" + str(r.get("stateInstance")) + ") — привяжите номер по QR"}
                return {"ok": False, "err": "green-api: HTTP %s %s (проверьте idInstance/token)" % (r.get("_http"), r.get("_text", ""))}
            if not to:
                return _log({"ok": False, "err": "нет номера получателя (to)"})
            chat = to if "@" in to else (re.sub(r"\D", "", to) + "@c.us")
            r = _gj(requests.post(gbase + "/sendMessage/" + atok, json={"chatId": chat, "message": text or "Тест Extella"}, timeout=20))
            if r.get("idMessage"):
                return _log({"ok": True, "message_id": r.get("idMessage"), "host": socket.gethostname(), "channel": "whatsapp", "provider": "green"})
            return _log({"ok": False, "err": "green-api: " + str(r)[:100]})
        except Exception as e:
            return _log({"ok": False, "err": "сеть green-api: " + str(e)[:100]})

    # Meta Cloud API
    pid = str(creds.get("phone_id", ""))
    tok = creds.get("access_token", "")
    if not pid or not tok:
        return {"ok": False, "err": "нет phone_id/access_token"}
    hh = {"Authorization": "Bearer " + tok}
    graph = "https://graph.facebook.com/v20.0/"
    try:
        if mode == "validate":
            r = requests.get(graph + pid, params={"fields": "id"}, headers=hh, timeout=20).json()
            if r.get("id"):
                return {"ok": True, "host": socket.gethostname(), "channel": "whatsapp", "provider": "meta"}
            return {"ok": False, "err": "whatsapp: " + str(r.get("error", {}).get("message", r))[:100]}
        if not to:
            return _log({"ok": False, "err": "нет номера получателя (to)"})
        payload = {"messaging_product": "whatsapp", "to": to, "type": "text", "text": {"body": text or "Тест Extella"}}
        r = requests.post(graph + pid + "/messages", json=payload, headers=hh, timeout=20).json()
        if r.get("messages"):
            return _log({"ok": True, "message_id": r["messages"][0].get("id"), "host": socket.gethostname(), "channel": "whatsapp", "provider": "meta"})
        return _log({"ok": False, "err": "whatsapp: " + str(r.get("error", {}).get("message", r))[:100]})
    except Exception as e:
        return _log({"ok": False, "err": "сеть whatsapp: " + str(e)[:100]})
