# expert: wz_source_1c_winrm
# description: Источник данных 1С — ЖИВОЙ сервер через WinRM+VBScript (V83.Application), трек B3. Исполняется на устройстве в СЕТИ клиента (WinRM-доступ к серверу 1С
# params: api_token, client, mode, sid, source_key, api_base, limit

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import winrm", ["extella-pip install pywinrm"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_source_1c_winrm(api_token: str = "", client: str = "default", mode: str = "validate",
                       sid: str = "", source_key: str = "", api_base: str = "https://api.extella.ai",
                       limit: int = 0) -> dict:
    """Источник данных 1С — ЖИВОЙ сервер через WinRM+VBScript (V83.Application), трек B3. Исполняется
    на устройстве в СЕТИ клиента (WinRM-доступ к серверу 1С). Секрет sec:<client>:src_1c_winrm из vault:
    {server_ip, login, password, base_path, object_type(catalog|document), object_name, fields[]}.
    mode='validate' → WinRM-проверка связи; mode='pull' → читает записи справочника/документа выбранными
    полями → xlsx → УКЛАДЫВАЕМ в общий стор под source_key (шифр vault.key) как _sync_file_to_store.
    Пароль НИКОГДА не логируется (scrub). Механика WinRM+VBScript реюзнута из onec_verify_and_report."""
    import json
    import socket
    import re
    import hashlib
    import time
    import io
    from pathlib import Path

    def ns(s):
        s = str(s)
        return re.sub(r"[^A-Za-z0-9_.-]", "_", s)[:40] + "_" + hashlib.sha256(s.encode("utf-8")).hexdigest()[:12]

    _ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f]")
    _NAME = re.compile(r"[^0-9A-Za-zА-Яа-яЁё_]")   # имена объектов/полей 1С: буквы(вкл. кириллицу)/цифры/_

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    cands = [Path("/opt/extella-listener/extella_wizard/vault.key"),
             Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
    kp = next((c for c in cands if c.exists()), None)
    if not kp:
        return {"ok": False, "err": "vault.key не найден на устройстве (провижининг ключа не выполнен)"}
    fkey = Fernet(kp.read_bytes())
    key = "sec:" + ns(client) + ":" + ns("src_1c_winrm")
    try:
        g = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": key}, timeout=60).json()
        ct = g.get("value")
    except Exception as e:
        return {"ok": False, "err": "чтение секрета: " + str(e)[:100]}
    if not ct:
        return {"ok": False, "err": "источник 1С (WinRM) не подключён (нет секрета)"}
    try:
        env = json.loads(fkey.decrypt(ct.encode()).decode())
        if env.get("k") != "src_1c_winrm":
            return {"ok": False, "err": "привязка секрета не совпала (ожидался src_1c_winrm)"}
        creds = json.loads(env.get("v", "{}"))
    except Exception as e:
        return {"ok": False, "err": "расшифровка/формат секрета: " + str(e)[:100]}

    server_ip = str(creds.get("server_ip", "")).strip()
    login = str(creds.get("login", "")).strip()
    password = str(creds.get("password", ""))
    base_path = str(creds.get("base_path", "")).strip()
    obj_type = str(creds.get("object_type", "catalog")).strip().lower()
    obj_name = str(creds.get("object_name", "")).strip()
    fields = creds.get("fields") or ["Code", "Description"]
    if not isinstance(fields, list) or not fields:
        fields = ["Code", "Description"]
    if not server_ip or not login or not base_path:
        return {"ok": False, "err": "в секрете нет server_ip/login/base_path"}

    def scrub(s):
        s = str(s)
        if password and len(password) >= 3:
            s = s.replace(password, "<pw>")
        return s

    def clean(v):
        return _ILLEGAL.sub("", str(v)) if v is not None else ""

    try:
        session = winrm.Session("http://%s:5985/wsman" % server_ip, auth=(login, password), transport="ntlm")

        if mode == "validate":
            r = session.run_ps('Write-Output "wz_ok"')
            out = (r.std_out or b"").decode("cp866", errors="replace")
            if "wz_ok" in out:
                return {"ok": True, "host": socket.gethostname(), "source": "1c_winrm", "server": server_ip}
            return {"ok": False, "err": scrub("winrm: нет ответа (" + (out or (r.std_err or b"").decode("cp866", errors="replace"))[:80] + ")")}

        # mode == pull: сгенерировать VBScript чтения объекта выбранными полями
        if not obj_name:
            return {"ok": False, "err": "не указан object_name (имя справочника/документа 1С)"}
        cap = int(limit) if (limit and int(limit) > 0) else 50000
        coll = "Documents" if obj_type == "document" else "Catalogs"
        name_esc = _NAME.sub("", obj_name)
        if not name_esc:
            return {"ok": False, "err": "имя объекта 1С пустое после проверки (допустимы буквы/цифры/_)"}
        if not re.match(r"^[A-Za-z]:\\", base_path) or any(c in base_path for c in '"\'`|<>*?\n\r'):
            return {"ok": False, "err": "недопустимый путь к базе (ожидается вида C:\\1C\\Base)"}
        fnames = [_NAME.sub("", str(x)) for x in fields][:40]
        fnames = [x for x in fnames if x] or ["Description"]
        bp = base_path.replace("\\", "\\\\")
        # нейтрализуем делимитеры/переводы строк в ЗНАЧЕНИЯХ полей, иначе поедут колонки/строки при парсинге
        extract = "\n".join(
            'v%d=""\nOn Error Resume Next\nv%d=Replace(Replace(Replace(CStr(obj.%s),"|","/"),Chr(13)," "),Chr(10)," ")\nIf Err.Number<>0 Then v%d="": Err.Clear' % (i, i, fn, i)
            for i, fn in enumerate(fnames))
        joinexpr = ' & "|" & '.join("v%d" % i for i in range(len(fnames)))
        vbs = ('On Error Resume Next\n'
               'Set a=CreateObject("V83.Application")\n'
               'If Err.Number<>0 Then WScript.Echo "ERR:" & Err.Description: WScript.Quit End If\n'
               'a.Connect "File=""%s"""\n'
               'If Err.Number<>0 Then WScript.Echo "ERR:" & Err.Description: WScript.Quit End If\n'
               'Set m=a.%s.%s\n'
               'If IsNull(m) Or IsEmpty(m) Then WScript.Echo "ERR:Объект %s не найден": WScript.Quit\n'
               'Set sel=m.Select()\n'
               'count=0:lines=""\n'
               'Do While sel.Next()\n'
               'Set obj=sel.GetObject()\n'
               'line=""\n'
               '%s\n'
               'line=%s\n'
               'If lines<>"" Then lines=lines & "|||"\n'
               'lines=lines & line\n'
               'count=count+1\n'
               'Set obj=Nothing\n'
               'If count>=%d Then Exit Do End If\n'
               'Loop\n'
               'a.Exit(False)\n'
               'WScript.Echo "COUNT:" & count\n'
               'WScript.Echo "ROWS:" & lines\n'
               'WScript.Echo "SUCCESS"\n') % (bp, coll, name_esc, name_esc, extract, joinexpr, cap)

        import uuid
        vbs_path = "C:\\wz_1c_src_%s.vbs" % uuid.uuid4().hex   # уникально на вызов → нет гонки при параллельных pull на одном устройстве
        try:
            session.run_ps('Set-Content -Path "%s" -Value "" -Encoding UTF8' % vbs_path)
            time.sleep(0.2)
            for ln in vbs.strip().split("\n"):
                ln_esc = ln.replace('"', '`"').replace("'", "''")
                session.run_ps('Add-Content -Path "%s" -Value "%s" -Encoding UTF8' % (vbs_path, ln_esc))
            result = session.run_cmd('cscript //NoLogo %s 2>&1' % vbs_path)
            output = (result.std_out or b"").decode("cp866", errors="replace").strip()
        finally:
            try:
                session.run_ps('Remove-Item -Path "%s" -ErrorAction SilentlyContinue' % vbs_path)   # свой файл всегда убираем, даже при ошибке
            except Exception:
                pass

        if "ERR:" in output and "SUCCESS" not in output:
            errl = [l for l in output.split("\n") if "ERR:" in l]
            return {"ok": False, "err": scrub("1c: " + (errl[0] if errl else output[:150]))}
        # маркер завершения обязателен: без него вывод неполный (обрыв WinRM/Add-Content) — иначе тихий ноль строк как «успех»
        if "SUCCESS" not in output:
            return {"ok": False, "err": "неполный вывод 1С (нет маркера завершения) — прогон не засчитан, повторите"}

        allrows = []
        for line in output.split("\n"):
            line = line.strip()
            if line.startswith("ROWS:") and line != "ROWS:":
                raw = line[len("ROWS:"):].strip()
                for row_str in raw.split("|||"):
                    parts = row_str.split("|")
                    rd = {fnames[i]: clean(parts[i]) if i < len(parts) else "" for i in range(len(fnames))}
                    allrows.append(rd)
        # сверка заявленного числа строк с фактически разобранным — ловит частичный вывод/обрыв
        _cnt = None
        for _l in output.split("\n"):
            _l = _l.strip()
            if _l.startswith("COUNT:"):
                try:
                    _cnt = int(_l[len("COUNT:"):].strip())
                except Exception:
                    _cnt = None
        if _cnt is not None and _cnt != len(allrows):
            return {"ok": False, "err": "1С: разобрано строк " + str(len(allrows)) + " из заявленных " + str(_cnt) + " — вывод неполный, прогон не засчитан"}
    except Exception as e:
        return {"ok": False, "err": "1c_winrm: " + scrub(str(e))[:150]}

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "data"
    ws.append([clean(c) for c in fnames])
    for rd in allrows:
        ws.append([rd.get(fn, "") for fn in fnames])
    buf = io.BytesIO()
    wb.save(buf)
    raw = buf.getvalue()
    if len(raw) > 25 * 1024 * 1024:
        return {"ok": False, "err": "выгрузка слишком большая (>25 МБ) — задайте limit"}

    # === укладка в общий стор ТОЧНО как _sync_file_to_store ===
    basename = "1c_winrm_pull.xlsx"
    base_key = source_key or ("file:" + str(sid) + ":" + hashlib.md5(basename.encode("utf-8")).hexdigest()[:12])
    try:
        old_n = int(json.loads((requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers,
                                               json={"key": base_key + ":meta"}, timeout=60).json() or {}).get("value") or "{}").get("chunks", 0))
    except Exception:
        old_n = 0
    payload = fkey.encrypt(raw).decode()
    FILE_CHUNK = 8000
    parts = [payload[i:i + FILE_CHUNK] for i in range(0, len(payload), FILE_CHUNK)]
    for i, pt in enumerate(parts):
        done = False
        for _ in range(4):
            if requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                             json={"key": base_key + ":" + str(i), "value": pt, "description": "filechunk " + str(sid)},
                             timeout=25).json().get("status") == "success":
                done = True
                break
        if not done:
            return {"ok": False, "err": "не удалось записать чанк источника в стор"}
    m_ok = False
    for _ in range(4):
        if requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                         json={"key": base_key + ":meta",
                               "value": json.dumps({"name": basename, "chunks": len(parts), "bytes": len(raw),
                                                    "enc": True, "pulled_at": int(time.time())}),
                               "description": "filemeta " + str(sid)}, timeout=25).json().get("status") == "success":
            m_ok = True
            break
    if not m_ok:
        return {"ok": False, "err": "не удалось записать meta источника в стор"}
    for i in range(len(parts), old_n):
        requests.post(api_base.rstrip("/") + "/api/kv/remove", headers=headers, json={"key": base_key + ":" + str(i)}, timeout=25)
    return {"ok": True, "rows": len(allrows), "source_key": base_key, "basename": basename,
            "bytes": len(raw), "host": socket.gethostname(), "source": "1c_winrm"}
