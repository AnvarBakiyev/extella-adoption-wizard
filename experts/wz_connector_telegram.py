$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_connector_telegram(api_token: str = "", client: str = "default", mode: str = "validate",
                          text: str = "", api_base: str = "https://api.extella.ai", offset: int = 0,
                          chat_id: str = "", file_path: str = "") -> dict:
    """Коннектор Telegram (вывод результата процесса). Исполняется на устройстве-ХОСТИНГЕ:
    читает шифротекст секрета sec:<client>:telegram из общего KV, расшифровывает ЛОКАЛЬНЫМ vault.key,
    проверяет привязку конверта, достаёт {token, chat_id} и вызывает Telegram Bot API.
    mode='validate' → getMe; mode='send' → sendMessage(text);
    mode='send_document' → sendDocument(file_path, caption=text) — файл должен быть ЛОКАЛЬНЫМ на устройстве исполнения.
    Токен НИКОГДА не возвращается/не логируется. Шаблон для остальных коннекторов."""
    import json
    import socket
    import re
    import hashlib
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
    key = "sec:" + ns(client) + ":" + ns("telegram")
    try:
        g = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json()
        ct = g.get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "коннектор Telegram не подключён (нет секрета)"}
    try:
        env = json.loads(Fernet(kp.read_bytes()).decrypt(ct.encode()).decode())
        if env.get("c") != client:
            return {"ok": False, "err": "привязка секрета к клиенту не совпала"}   # #8 client-isolation: проверка привязки на ЧТЕНИИ
        if env.get("k") != "telegram":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался telegram)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}

    # РЕЖИМ MTProto (пользовательская сессия Telethon) — если в секрете есть session.
    # Мощнее бота: шлёт как аккаунт, не нужен бот в канале, можно писать людям/читать входящие.
    if creds.get("mode") == "mtproto" or creds.get("session"):
        try:
            import asyncio
            from telethon import TelegramClient
            from telethon.sessions import StringSession
        except Exception as e:
            return {"ok": False, "err": "telethon не установлен на хостинге: " + str(e)[:80]}

        async def _mt():
            cl = TelegramClient(StringSession(creds.get("session", "")), int(creds.get("api_id", 0)), creds.get("api_hash", ""))
            await cl.connect()
            try:
                if not await cl.is_user_authorized():
                    return {"ok": False, "err": "сессия Telegram не авторизована — переавторизуйте (tg_login.py)"}
                if mode == "validate":
                    me = await cl.get_me()
                    return {"ok": True, "bot": (me.username or me.first_name), "host": socket.gethostname(), "channel": "mtproto"}
                m = await cl.send_message(creds.get("target") or "me", text or "Тест Extella")
                return {"ok": True, "message_id": getattr(m, "id", None), "host": socket.gethostname(), "channel": "mtproto"}
            finally:
                await cl.disconnect()
        try:
            res = asyncio.run(_mt())
        except Exception as e:
            res = {"ok": False, "err": "mtproto: " + str(e)[:120]}
        if mode != "validate":
            try:
                from datetime import datetime, timezone
                requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                              json={"key": "connlog:" + ns(client) + ":telegram",
                                    "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                         "mode": "send", "ok": res.get("ok"), "err": res.get("err")}),
                                    "description": "connlog"}, timeout=30)
            except Exception:
                pass
        return res

    tok = creds.get("token", "")
    chat = creds.get("chat_id", "")
    if not tok:
        return {"ok": False, "err": "в секрете нет token"}
    base = "https://api.telegram.org/bot" + tok
    try:
        if mode == "webhook_info":
            # диагностика: стоит ли на боте webhook (он блокирует getUpdates/опрос входящих)
            r = requests.get(base + "/getWebhookInfo", timeout=20).json()
            wi = r.get("result", {}) if r.get("ok") else {}
            return {"ok": bool(r.get("ok")), "webhook_url": wi.get("url", ""),
                    "pending": wi.get("pending_update_count"),
                    "last_error": wi.get("last_error_message"), "host": socket.gethostname()}
        if mode == "clear_webhook":
            # снять webhook, чтобы заработал опрос (drop_pending_updates=False — входящие сообщения НЕ теряем)
            r = requests.post(base + "/deleteWebhook", json={"drop_pending_updates": False}, timeout=20).json()
            return {"ok": bool(r.get("ok")), "cleared": bool(r.get("result")),
                    "desc": str(r.get("description", ""))[:120], "host": socket.gethostname()}
        if mode == "set_webhook":
            # восстановить webhook (url передаётся в text). ВНИМАНИЕ: секретный токен вебхука неизвестен —
            # если приложение его использует, надёжнее перевыкатить это приложение (оно само переустановит webhook).
            wh = str(text or "").strip()
            if not wh:
                return {"ok": False, "err": "нет url для set_webhook (передайте в text)"}
            r = requests.post(base + "/setWebhook", json={"url": wh}, timeout=20).json()
            return {"ok": bool(r.get("ok")), "set": bool(r.get("result")),
                    "desc": str(r.get("description", ""))[:120], "host": socket.gethostname()}
        if mode == "poll":
            # входящие (B2): getUpdates с курсором offset; возвращаем текстовые сообщения + next_offset
            r = requests.get(base + "/getUpdates", params={"offset": offset, "timeout": 0, "allowed_updates": '["message"]'}, timeout=25).json()
            if not r.get("ok"):
                desc = str(r.get("description", ""))
                # опрос несовместим с webhook. НЕ снимаем молча (бот может принадлежать другому приложению —
                # иначе Extella и тот сервис будут по кругу отбирать бота). Возвращаем честную ошибку, снятие — явным действием.
                if "webhook" in desc.lower():
                    return {"ok": False, "webhook_conflict": True,
                            "err": "на этом боте активен webhook другого приложения — опрос входящих невозможен; снимите webhook в «Подключениях» или используйте отдельного бота для Extella"}
                return {"ok": False, "err": "telegram getUpdates: " + desc}
            msgs = []
            maxu = int(offset) - 1
            for u in r.get("result", []):
                uid = u.get("update_id", 0)
                maxu = max(maxu, uid)
                m = u.get("message") or {}
                if m.get("text"):
                    msgs.append({"update_id": uid, "chat_id": (m.get("chat") or {}).get("id"),
                                 "text": m.get("text"), "from": (m.get("from") or {}).get("username")})
            return {"ok": True, "messages": msgs, "next_offset": maxu + 1, "host": socket.gethostname()}
        if mode == "validate":
            r = requests.get(base + "/getMe", timeout=20).json()
            if r.get("ok"):
                return {"ok": True, "bot": r.get("result", {}).get("username"), "host": socket.gethostname()}
            return {"ok": False, "err": "telegram: " + str(r.get("description", "invalid token"))}
        if mode == "send_document":
            # отправка файла (реестр рисков/протокол): файл ЛОКАЛЬНЫЙ на устройстве исполнения коннектора
            from pathlib import Path as _P
            fp = _P(str(file_path)).expanduser()
            if not str(file_path) or not fp.is_file():
                return {"ok": False, "err": "file_path не найден на устройстве исполнения: " + str(file_path)[:120]}
            if fp.stat().st_size > 45 * 1024 * 1024:
                return {"ok": False, "err": "файл больше лимита Telegram (50 МБ)"}
            chat = str(chat_id).strip() or chat
            if not chat:
                return {"ok": False, "err": "в секрете нет chat_id для отправки"}
            with open(fp, "rb") as fh:
                r = requests.post(base + "/sendDocument",
                                  data={"chat_id": chat, "caption": (text or "")[:1000]},
                                  files={"document": (fp.name, fh)}, timeout=90).json()
            if r.get("ok"):
                res = {"ok": True, "message_id": r.get("result", {}).get("message_id"),
                       "file": fp.name, "host": socket.gethostname()}
            else:
                res = {"ok": False, "err": "telegram: " + str(r.get("description", ""))}
            try:
                from datetime import datetime, timezone
                requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                              json={"key": "connlog:" + ns(client) + ":telegram",
                                    "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                         "mode": "send_document", "ok": res.get("ok"), "err": res.get("err")}),
                                    "description": "connlog"}, timeout=30)
            except Exception:
                pass
            return res
        chat = str(chat_id).strip() or chat   # для входящих (B2) отвечаем в чат отправителя, иначе — chat_id из секрета
        if not chat:
            return {"ok": False, "err": "в секрете нет chat_id для отправки"}
        # #13 ретраи на ВРЕМЕННЫХ сбоях (429/5xx/сеть): до 3 попыток, экспон. backoff + Retry-After.
        # Постоянные ошибки (400 «chat not found» и т.п.) НЕ ретраим. При исчерпании — transient:True,
        # чтобы оркестратор/тик переочередил, а не записал как окончательный провал.
        import time as _t
        r = None
        _last_transient = None
        for _att in range(3):
            _wait = None
            try:
                _resp = requests.post(base + "/sendMessage", json={"chat_id": chat, "text": text or "Тест Extella"}, timeout=20)
                if _resp.status_code == 429 or _resp.status_code >= 500:
                    try:
                        _ra = int(_resp.headers.get("Retry-After") or (_resp.json().get("parameters", {}) or {}).get("retry_after") or 0)
                    except Exception:
                        _ra = 0
                    _last_transient = "telegram временный сбой (HTTP %d)" % _resp.status_code
                    _wait = max(_ra, 2 ** _att)
                else:
                    r = _resp.json()
                    break
            except Exception as _ne:
                _last_transient = "сеть telegram: " + (str(_ne).replace(tok, "<token>") if tok else str(_ne))[:100]
                _wait = 2 ** _att
            if _wait is not None and _att < 2:
                _t.sleep(min(_wait, 10))
        if r is None:
            return {"ok": False, "transient": True, "err": _last_transient or "telegram: временный сбой, отправка не подтверждена"}
        if r.get("ok"):
            res = {"ok": True, "message_id": r.get("result", {}).get("message_id"), "host": socket.gethostname()}
        else:
            res = {"ok": False, "err": "telegram: " + str(r.get("description", ""))}
        # журнал доставки (для UI «доставлено ✓/✗»): пишем компактную запись в KV, БЕЗ текста/токена
        try:
            from datetime import datetime, timezone
            requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                          json={"key": "connlog:" + ns(client) + ":telegram",
                                "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                     "mode": "send", "ok": res.get("ok"), "err": res.get("err")}),
                                "description": "connlog"}, timeout=30)
        except Exception:
            pass
        return res
    except Exception as e:
        # чистим токен из текста ошибки: сетевое исключение несёт URL вида /bot<token>/ (не полагаемся на усечение)
        return {"ok": False, "err": "сеть telegram: " + (str(e).replace(tok, "<token>") if tok else str(e))[:120]}