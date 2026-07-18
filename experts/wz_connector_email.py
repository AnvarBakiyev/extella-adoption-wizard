# expert: wz_connector_email
# description: Коннектор Email (вывод результата процесса по SMTP). Исполняется на устройстве-ХОСТИНГЕ: читает шифротекст секрета sec:<client>:email из общего KV, ра
# params: api_token, client, mode, text, api_base

$extens("include.py")
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_connector_email(api_token: str = "", client: str = "default", mode: str = "validate",
                       text: str = "", api_base: str = "https://api.extella.ai",
                       file_path: str = "", subject: str = "") -> dict:
    """Коннектор Email (вывод результата процесса по SMTP). Исполняется на устройстве-ХОСТИНГЕ:
    читает шифротекст секрета sec:<client>:email из общего KV, расшифровывает ЛОКАЛЬНЫМ vault.key,
    проверяет привязку конверта, достаёт SMTP-креды и шлёт письмо. SMTP через stdlib (без внешних deps).
    mode='validate' → connect+login (проверка); mode='send' → письмо с text. Пароль не логируется/не возвращается."""
    import json
    import socket
    import re
    import hashlib
    import smtplib
    import ssl
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    from email.mime.application import MIMEApplication
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
        return {"ok": False, "err": "vault.key не найден на устройстве (провижининг ключа не выполнен)"}
    import requests
    key = "sec:" + ns(client) + ":" + ns("email")
    try:
        g = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json()
        ct = g.get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "коннектор Email не подключён (нет секрета)"}
    try:
        env = json.loads(Fernet(kp.read_bytes()).decrypt(ct.encode()).decode())
        if env.get("c") != client:
            return {"ok": False, "err": "привязка секрета к клиенту не совпала"}   # #8 client-isolation: проверка привязки на ЧТЕНИИ
        if env.get("k") != "email":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался email)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}

    host = creds.get("host", "")
    port = int(creds.get("port", 587) or 587)
    user = creds.get("username", "")
    pw = creds.get("password", "")
    frm = creds.get("from") or user
    to = creds.get("to") or user
    use_tls = creds.get("use_tls", True)
    if not host or not user:
        return {"ok": False, "err": "в секрете нет host/username"}

    def _log(res):
        if mode != "validate":
            try:
                from datetime import datetime, timezone
                requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                              json={"key": "connlog:" + ns(client) + ":email",
                                    "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                         "mode": "send", "ok": res.get("ok"), "err": res.get("err")}),
                                    "description": "connlog"}, timeout=30)
            except Exception:
                pass
        return res

    try:
        if port == 465:
            srv = smtplib.SMTP_SSL(host, port, timeout=25, context=ssl.create_default_context())
        else:
            srv = smtplib.SMTP(host, port, timeout=25)
            if use_tls:
                srv.starttls(context=ssl.create_default_context())
        srv.login(user, pw)
        if mode == "validate":
            srv.quit()
            return {"ok": True, "host": socket.gethostname(), "channel": "email", "smtp": host, "to": to}
        # Тема письма — НЕ наша: письмо уходит заказчикам клиента, и «Extella» в теме
        # там так же неуместна, как наш логотип в его отчёте (замечание Анвара про документы).
        _subj = (subject or "").strip() or "Отчёт процесса"
        _att = str(file_path or "").strip()
        if _att and not _att.startswith("{{"):
            import os as _os
            if not _os.path.isfile(_att):
                return {"ok": False, "err": "файл вложения не найден на устройстве исполнения: " + _att[:120]}
            if _os.path.getsize(_att) > 20 * 1024 * 1024:
                return {"ok": False, "err": "вложение больше 20 МБ — почтовые серверы такое режут; "
                                            "пришлите ссылку вместо файла"}
            msg = MIMEMultipart()
            msg.attach(MIMEText(text or "", "plain", "utf-8"))
            with open(_att, "rb") as _fh:
                part = MIMEApplication(_fh.read(), Name=_os.path.basename(_att))
            part["Content-Disposition"] = 'attachment; filename="%s"' % _os.path.basename(_att)
            msg.attach(part)
        else:
            msg = MIMEText(text or "Проверка связи", "plain", "utf-8")
        msg["Subject"] = _subj
        msg["From"] = frm
        msg["To"] = to
        srv.sendmail(frm, [to], msg.as_string())
        srv.quit()
        return _log({"ok": True, "host": socket.gethostname(), "channel": "email", "to": to})
    except Exception as e:
        return _log({"ok": False, "err": "smtp: " + str(e)[:130]})
