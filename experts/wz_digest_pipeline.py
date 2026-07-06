# expert: wz_digest_pipeline
# description: Универсальный процесс-дайджест: материализует источник из общего стора (тот же резолвер, что у автосгенерированных оркестраторов), разбирает xlsx и во
# params: source_file, work_dir, api_token, api_base, target, source_key

$extens("include.py")
include("import requests", ["extella-pip install requests"])
include("import openpyxl", ["extella-pip install openpyxl"])
include("from cryptography.fernet import Fernet", ["extella-pip install cryptography"])

def wz_digest_pipeline(source_file: str = "", work_dir: str = "", api_token: str = "",
                       api_base: str = "https://api.extella.ai", target: str = "", source_key: str = "") -> dict:
    """Универсальный процесс-дайджест: материализует источник из общего стора (тот же резолвер, что
    у автосгенерированных оркестраторов), разбирает xlsx и возвращает сводку — строк, колонок, числовых
    итогов по колонкам, превью. Годится для ЛЮБОГО табличного источника (демо/пилот).
    Пишет lastrun:digest в KV (для тика планировщика). Параметры как у оркестратора процесса."""
    import json
    import socket
    from pathlib import Path
    from datetime import datetime, timezone

    if not api_token:
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "api_token не передан и нет bridge-конфига"}
    if not source_file:
        return {"status": "error", "message": "source_file обязателен"}
    wd = Path(work_dir) if work_dir else (Path.home() / "extella_wizard" / "digest_work")
    wd.mkdir(parents=True, exist_ok=True)
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    # --- резолвер источника из общего стора (как в _ORCH_TEMPLATE) ---
    _resolve_err = ""
    if source_file and not Path(source_file).exists():
        import base64 as _b64, hashlib as _hl
        _bn = Path(source_file).name
        _base = source_key or ("file:digest:" + _hl.md5(_bn.encode("utf-8")).hexdigest()[:12])
        try:
            _mr = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": _base + ":meta"}, timeout=120).json()
            _mv = _mr.get("value")
            if _mv:
                _meta = json.loads(_mv); _buf = ""
                for _i in range(int(_meta.get("chunks", 0))):
                    _cr = requests.post(api_base.rstrip("/") + "/api/kv/get", headers=headers, json={"key": _base + ":" + str(_i)}, timeout=120).json()
                    _buf += _cr.get("value") or ""
                if _buf:
                    if _meta.get("enc"):
                        _kc = [Path("/opt/extella-listener/extella_wizard/vault.key"), Path.home() / "extella_wizard/app/vault.key", Path.cwd() / "extella_wizard/vault.key"]
                        _kp = next((c for c in _kc if c.exists()), None)
                        _rawf = Fernet(_kp.read_bytes()).decrypt(_buf.encode()) if _kp else None
                        if not _kp:
                            _resolve_err = "зашифрованный источник: локальный vault.key не найден"
                    else:
                        _rawf = _b64.b64decode(_buf)
                    if _rawf:
                        _tmp = wd / _bn
                        _tmp.write_bytes(_rawf)
                        source_file = str(_tmp)
        except Exception as _e:
            _resolve_err = _resolve_err or ("не удалось восстановить источник из стора: " + str(_e)[:120])
    if source_file and not Path(source_file).exists():
        return {"status": "error", "message": _resolve_err or ("источник не найден: " + str(source_file))}

    # --- разбор xlsx → сводка ---
    try:
        wb = openpyxl.load_workbook(source_file, read_only=True, data_only=True)
        ws = wb.active
        rows = []
        for r in ws.iter_rows(values_only=True):
            rows.append(list(r))
        wb.close()
    except Exception as e:
        return {"status": "error", "message": "не удалось прочитать xlsx: " + str(e)[:120]}

    # первая непустая строка = заголовок; числовые итоги по колонкам
    def _nonempty(r):
        return any(c not in (None, "") for c in r)
    data_rows = [r for r in rows if _nonempty(r)]
    header = data_rows[0] if data_rows else []
    body = data_rows[1:] if len(data_rows) > 1 else []
    ncols = max((len(r) for r in data_rows), default=0)
    numeric_totals = {}
    for ci in range(ncols):
        s = 0.0; cnt = 0
        for r in body:
            if ci < len(r):
                v = r[ci]
                if isinstance(v, bool):
                    continue
                if isinstance(v, (int, float)):
                    s += float(v); cnt += 1
        if cnt >= 2:   # колонка «числовая», если ≥2 чисел
            col = str(header[ci]) if ci < len(header) and header[ci] not in (None, "") else ("col%d" % (ci + 1))
            numeric_totals[col] = round(s, 2)
    preview = [[("" if c is None else str(c))[:40] for c in r][:8] for r in data_rows[:5]]
    total_count = len(body)

    result = {"status": "success", "total_count": total_count,
              "columns": [str(c) for c in header if c not in (None, "")][:40],
              "numeric_totals": numeric_totals, "preview": preview,
              "host": socket.gethostname(), "source_file": Path(source_file).name}

    # lastrun:digest — чтобы тик планировщика/дренаж входящих подхватил межустройственно
    try:
        requests.post(api_base.rstrip("/") + "/api/kv/set", headers=headers,
                      json={"key": "lastrun:digest",
                            "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(), "status": "success",
                                                 "total_count": total_count, "total_sum": (sum(numeric_totals.values()) if numeric_totals else None),
                                                 "host": socket.gethostname()}, ensure_ascii=False),
                            "description": "lastrun digest"}, timeout=30)
    except Exception:
        pass
    return result
