$extens("include.py")
include("import requests", ["extella-pip install requests"])

def wz_scheduler_tick(api_token: str = "", api_base: str = "https://api.extella.ai") -> dict:
    """Тик планировщика: читает расписания из KV (ключи sched:*), для каждого,
    у которого подошёл срок, запускает оркестратор процесса, пишет лог прогона
    обратно в KV и переносит next_due. Вешается на cron always-on устройства.
    Параметры: api_token (или из bridge-конфига устройства)."""
    import json
    import re
    import requests
    from datetime import datetime, timezone, timedelta
    from pathlib import Path

    if not api_token:
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "нет api_token и bridge-конфига"}

    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}
    base = api_base.rstrip("/")

    def kv(ep, payload):
        try:
            r = requests.post(base + "/api/kv/" + ep, headers=headers, json=payload, timeout=60)
            return r.json()
        except Exception as e:
            return {"status": "error", "message": str(e)[:120]}

    def now():
        return datetime.now(timezone.utc)

    def _mk_digest(out):
        """Единый отчёт прогона по расписанию: готовый digest → файл report_md (на этом устройстве)
        → синтез из summary (total_count/total_sum/by_*). Раньше бралось только digest_md — у
        процессов-на-файле его нет (там путь к report.md), и результат по расписанию не сохранялся."""
        if not isinstance(out, dict):
            return ""
        dm = out.get("digest_md") or out.get("digest")
        if isinstance(dm, str) and dm.strip() and not dm.strip().startswith("/"):
            return dm[:12000]
        rp = out.get("report_md")
        if isinstance(rp, str) and rp.startswith("/"):
            try:
                p = Path(rp)
                if p.is_file() and p.stat().st_size < 400000:
                    txt = p.read_text(encoding="utf-8", errors="replace").strip()
                    if txt:
                        return txt[:12000]
            except Exception:
                pass
        sm = out.get("summary")
        if isinstance(sm, str) and sm.strip():
            return sm[:12000]
        if isinstance(sm, dict):
            ln = ["## Результат прогона", ""]
            tc, ts = sm.get("total_count"), sm.get("total_sum")
            if tc is not None:
                l = "**Позиций:** %s" % tc
                if ts not in (None, 0, 0.0):
                    l += "  ·  **Сумма:** %s" % ts
                ln += [l, ""]
            for k, v in sm.items():
                if k.startswith("by_") and isinstance(v, dict) and 0 < len(v) <= 25:
                    lbl = k[3:].replace("_", " ")
                    ln.append("### " + lbl.capitalize())
                    ln += ["| %s | Кол-во |" % lbl, "|---|---|"]
                    ln += ["| %s | %s |" % (kk, vv) for kk, vv in list(v.items())[:25]]
                    ln.append("")
            body = "\n".join(ln).strip()
            if body and body != "## Результат прогона":
                return body[:12000]
        return ""

    SCHED_INDEX_KEY = "sched:__index__"
    RECONCILE_MIN = 360   # как часто индекс пересобирается полным сканом (страховка от рассинхрона); 0 = выкл

    def scan_sched_items():
        # ДОРОГОЙ разовый проход всего KV (у kv/list нет префикс-фильтра — тянет и file:* base64-чанки,
        # и sec:*, и чужие данные). Только для бутстрапа/починки индекса, НЕ в штатном тике.
        lst = kv("list", {})
        raw = lst.get("results") or lst.get("items") or []
        out, sids = [], []
        for it in raw:
            k = it.get("kv_key") or it.get("key") or ""
            if not k.startswith("sched:") or k == SCHED_INDEX_KEY:
                continue
            out.append({"key": k, "value": it.get("kv_value") or it.get("value")})
            sids.append(k[len("sched:"):])
        return out, sids

    def write_index(sids):
        kv("set", {"key": SCHED_INDEX_KEY,
                   "value": json.dumps({"sids": sorted(set(sids)), "scan_ts": now().isoformat()},
                                       ensure_ascii=False),
                   "description": "schedule index"})

    # Штатно читаем ТОЛЬКО индекс активных расписаний + точечные sched:<sid>, чтобы не тянуть в память
    # весь стор. Индекс ведёт мост (server.py) на создание/удаление расписания.
    idx = kv("get", {"key": SCHED_INDEX_KEY})
    idx_sids, scan_ts = None, None
    try:
        iv = json.loads(idx.get("value") or "")
        if isinstance(iv, dict):
            idx_sids, scan_ts = iv.get("sids"), iv.get("scan_ts")
        elif isinstance(iv, list):
            idx_sids = iv
    except Exception:
        idx_sids = None

    stale = False
    if RECONCILE_MIN > 0:
        if not scan_ts:
            stale = True
        else:
            try:
                stale = now() >= datetime.fromisoformat(scan_ts.replace("Z", "+00:00")) + timedelta(minutes=RECONCILE_MIN)
            except Exception:
                stale = True

    if not isinstance(idx_sids, list) or stale:
        # индекса нет / устарел → разовый full-scan + пересборка индекса (миграция и самолечение
        # от рассинхрона: потерянный из индекса sid не может молча похоронить расписание навсегда)
        items, _sids = scan_sched_items()
        write_index(_sids)
    else:
        # горячий путь: точечные get по sched:<sid> из индекса, без прохода по всему стору
        items = [{"key": "sched:" + sid, "value": kv("get", {"key": "sched:" + sid}).get("value")}
                 for sid in idx_sids]

    # ===== ВХОДЯЩИЕ (B2): дренаж очереди inbq:<sid> (webhook-шлюз/инъекция) + опрос канала (poll) =====
    # → запуск процесса на ЭТОМ хостинге (пиннинг target) → ответ в чат отправителя. Индекс: inbound:__index__.
    inbound_fired = []
    _dbg = {"isids": None, "polls": []}

    def _j(v, d):
        # KV-значения — JSON (двойные кавычки); РЕЗУЛЬТАТ fython-эксперта — Python-repr (одинарные) →
        # нужен ast-фолбэк, иначе poll/orch-результат не распарсится (был баг: msgs=0).
        if not v:
            return d
        try:
            return json.loads(v)
        except Exception:
            try:
                import ast
                return ast.literal_eval(v)
            except Exception:
                return d

    try:
        iidx = _j(kv("get", {"key": "inbound:__index__"}).get("value"), {})
        isids = iidx.get("sids", []) if isinstance(iidx, dict) else []
        _dbg["isids"] = isids
        for sid in isids:
            ikey = "inbound:" + sid
            ic = _j(kv("get", {"key": ikey}).get("value"), {})
            if not ic or not ic.get("active", True):
                continue
            orch = ic.get("orchestrator")
            src = ic.get("source_file")
            tgt = ic.get("target")
            skey = ic.get("source_key")
            client = ic.get("client", "default")
            channel = ic.get("channel", "telegram")
            if not orch:
                continue
            events = []  # [{chat_id, text, dedup}]
            # (1) очередь: webhook-шлюз или тестовая инъекция кладут события в inbq:<sid>
            qkey = "inbq:" + sid
            qev = _j(kv("get", {"key": qkey}).get("value"), [])
            if isinstance(qev, list) and qev:
                events += [e for e in qev if isinstance(e, dict)]
                kv("set", {"key": qkey, "value": "[]", "description": "inbq drained"})  # идемпотентный дренаж
            # (2) опрос канала (тик клиента сам ходит в канал; курсор offset в конфиге)
            if ic.get("mode") == "poll" and channel == "telegram":
                pbody = {"expert_name": "wz_connector_telegram", "global": True,
                         "params": {"api_token": api_token, "client": client, "mode": "poll",
                                    "offset": int(ic.get("offset", 0) or 0)}}
                if tgt:
                    pbody["target"] = tgt
                try:
                    po = requests.post(base + "/api/expert/run", headers=headers, json=pbody, timeout=90).json()
                    po = po.get("result", po)
                    if isinstance(po, str):
                        po = _j(po, {})
                except Exception:
                    po = {}
                _dbg["polls"].append({"sid": sid, "mode": ic.get("mode"), "chan": channel,
                                      "msgs": len(po.get("messages") or []), "ok": po.get("ok"), "err": po.get("err")})
                for m in (po.get("messages") or []):
                    events.append({"chat_id": m.get("chat_id"), "text": m.get("text"),
                                   "dedup": "tg:" + str(m.get("update_id"))})
                if po.get("next_offset") is not None:
                    ic["offset"] = po["next_offset"]  # двигаем курсор ТОЛЬКО при успешном ответе канала
            # дедуп + фиксация курсора ДО медленного прогона. Иначе deferred-прогон (до 45с/сообщение),
            # наложение тиков или таймаут тика приводят к повторным ответам на одно сообщение = спам.
            # Стратегия at-most-once: лучше не ответить, чем зациклить рассылку.
            seen = ic.get("seen") or []
            fresh = []
            for ev in events[:20]:  # лимит на тик — не зациклить (канон 50 итераций/ход)
                dk = ev.get("dedup") or ""
                if dk and dk in seen:
                    continue
                if dk:
                    seen.append(dk)
                fresh.append(ev)
            ic["seen"] = seen[-200:]
            # C1 (Пауза кабинета): drain_once ставит мост при Возобновлении — этот тик дренирует
            # бэклог, накопленный за паузу, БЕЗ ответов (курсор/seen двигаются, ответы не шлются).
            # Канон at-most-once: лучше не ответить на старое, чем разослать спам по бэклогу.
            _drain = bool(ic.get("drain_once"))
            if _drain:
                ic["drain_once"] = False
                ic["skipped_backlog"] = int(ic.get("skipped_backlog", 0) or 0) + len(fresh)
                ic["drained_at"] = now().isoformat()
            # зафиксировать курсор+seen СРАЗУ (идемпотентность к падению/наложению/таймауту тика)
            kv("set", {"key": ikey, "value": json.dumps(ic, ensure_ascii=False), "description": "inbound " + sid})
            if _drain:
                _dbg["polls"].append({"sid": sid, "drained": len(fresh)})
                continue   # бэклог помечен seen → не ответим и в следующие тики
            processed = 0
            for ev in fresh:
                # УМНЫЙ ОТВЕТ (опционально, обратносовместимо): если в inbound-конфиге задан reply_expert,
                # обработчик получает ТЕКСТ сообщения и сам формирует reply (ветка «Согласовано»/«статус»).
                # Процессы БЕЗ reply_expert идут прежним путём (полный прогон оркестратора).
                rexp = ic.get("reply_expert")
                if rexp:
                    rp = {"api_token": api_token, "message_text": str(ev.get("text") or "")[:2000],
                          "chat_id": str(ev.get("chat_id") or ""), "client": client}
                    rb = {"expert_name": rexp, "params": rp, "global": True}
                    if tgt:
                        rb["target"] = tgt
                    try:
                        rr = requests.post(base + "/api/expert/run", headers=headers, json=rb, timeout=300).json()
                        rr = rr.get("result", rr)
                        if isinstance(rr, str):
                            rr = _j(rr, {})
                    except Exception:
                        rr = {}
                    reply = str((rr or {}).get("reply") or "Готово ✅")[:3800]
                    dbody = {"expert_name": "wz_connector_" + channel, "global": True,
                             "params": {"api_token": api_token, "client": client, "mode": "send",
                                        "text": reply, "chat_id": ev.get("chat_id")}}
                    if tgt:
                        dbody["target"] = tgt
                    try:
                        requests.post(base + "/api/expert/run", headers=headers, json=dbody, timeout=120)
                    except Exception:
                        pass
                    processed += 1
                    continue
                run_params = {"api_token": api_token, "source_file": src}
                if tgt:
                    run_params["target"] = tgt
                if skey:
                    run_params["source_key"] = skey
                rbody = {"expert_name": orch, "params": run_params, "global": True}
                if tgt:
                    rbody["target"] = tgt
                t0 = now()
                try:
                    rout = requests.post(base + "/api/expert/run", headers=headers, json=rbody, timeout=900).json()
                    rout = rout.get("result", rout)
                    if isinstance(rout, str):
                        rout = _j(rout, {})
                except Exception:
                    rout = {}
                # deferred → дочитать lastrun:<ns> (как в расписании)
                if (rout or {}).get("status") != "success":
                    import time as _t2
                    ns2 = orch.replace("_run_pipeline", "")
                    dl = 0
                    while dl < 45:
                        _t2.sleep(5); dl += 5
                        rec = _j(kv("get", {"key": "lastrun:" + ns2}).get("value"), None)
                        if not rec:
                            continue
                        try:
                            frsh = datetime.fromisoformat(rec.get("at", "").replace("Z", "+00:00")) >= t0
                        except Exception:
                            frsh = True
                        if frsh and rec.get("status") == "success":
                            rout = {"status": "success", "total_count": rec.get("total_count"),
                                    "total_sum": rec.get("total_sum")}
                            break
                # ответ отправителю в тот же чат
                ts = (rout or {}).get("total_sum"); tc = (rout or {}).get("total_count")
                if ts is not None:
                    reply = "Готово. Сумма: " + format(ts, ",").replace(",", " ") + " ₸" + (("\nПозиций: " + str(tc)) if tc is not None else "")
                elif (rout or {}).get("status") == "success":
                    reply = "Готово ✅"
                else:
                    reply = "Принял, обрабатываю…"
                dbody = {"expert_name": "wz_connector_" + channel, "global": True,
                         "params": {"api_token": api_token, "client": client, "mode": "send",
                                    "text": reply, "chat_id": ev.get("chat_id")}}
                if tgt:
                    dbody["target"] = tgt
                try:
                    requests.post(base + "/api/expert/run", headers=headers, json=dbody, timeout=120)
                except Exception:
                    pass
                processed += 1
            if processed:
                ic["last_inbound_ts"] = now().isoformat()
                kv("set", {"key": ikey, "value": json.dumps(ic, ensure_ascii=False), "description": "inbound " + sid})
            if processed:
                inbound_fired.append({"sid": sid, "processed": processed})
    except Exception as e:
        inbound_fired.append({"error": str(e)[:120]})

    fired, checked = [], 0
    for it in items:
        # kv/list отдаёт kv_key/kv_value, kv/get — key/value
        key = it.get("kv_key") or it.get("key") or ""
        if not key.startswith("sched:"):
            continue
        checked += 1
        raw = it.get("kv_value") or it.get("value")
        if raw is None:
            raw = kv("get", {"key": key}).get("value", "{}")
        try:
            cfg = json.loads(raw)
        except Exception:
            continue
        if not cfg.get("active", True):
            continue
        interval = int(cfg.get("interval_min", 0) or 0)
        orch = cfg.get("orchestrator")
        src = cfg.get("source_file")
        fid = cfg.get("flow_id")   # composed-задача (Композитор): вместо файла — план flow:<id> в KV
        if not orch or (not src and not fid):
            continue
        # срок?
        nd = cfg.get("next_due_ts")
        _slot = nd or now().isoformat()   # идентичность слота (для идемпотентности доставки) — ДО переноса срока
        due = True
        if nd:
            try:
                due = now() >= datetime.fromisoformat(nd.replace("Z", "+00:00"))
            except Exception:
                due = True
        if not due:
            continue
        # #3 двойное срабатывание: столбим слот СРАЗУ (переносим next_due вперёд и пишем в KV) ДО прогона.
        # Иначе наложенный тик (прогон дольше интервала / параллельный cron) прочитает старый срок и запустит повторно.
        cfg["next_due_ts"] = (now() + timedelta(minutes=max(1, interval))).isoformat()
        kv("set", {"key": key, "value": json.dumps(cfg, ensure_ascii=False),
                   "description": "schedule " + key.split(":", 1)[-1]})
        # запуск оркестратора (пиннинг на хостинг + ключ файла в общем сторе для резолвера)
        t0 = now()
        tgt = cfg.get("target")
        skey = cfg.get("source_key")
        # B3: процесс-на-источнике — свежий pull данных ПЕРЕД прогоном (refresh=per_run).
        # Источник кладёт данные в тот же source_key; при ошибке pull — НЕ запускаем (честный fail).
        _srcinfo = cfg.get("source")
        if isinstance(_srcinfo, dict) and _srcinfo.get("refresh", "per_run") == "per_run":
            _kind = str(_srcinfo.get("kind", "")).replace("src_", "")
            _kind = "".join(ch for ch in _kind.lower() if ch.isalnum() or ch == "_")[:30]
            _pkey = _srcinfo.get("source_key") or skey
            _pt0 = now().timestamp()
            try:
                _pr = requests.post(base + "/api/expert/run", headers=headers, json={
                    "expert_name": "wz_source_" + _kind, "global": True, "target": tgt,
                    "params": {"api_token": api_token, "client": cfg.get("client", "default"),
                               "mode": "pull", "sid": key.split(":", 1)[-1], "source_key": _pkey}}, timeout=600)
                _po = _pr.json().get("result", {})
                if isinstance(_po, str):
                    try:
                        _po = json.loads(_po)
                    except Exception:
                        _po = {}
            except Exception:
                _po = {}
            _pull_ok = isinstance(_po, dict) and _po.get("ok")
            if not _pull_ok:
                # HTTP 500/timeout ≠ провал (канон): источник мог успеть записать данные — проверяем АРТЕФАКТ.
                # Свежая meta (pulled_at >= начала pull) = данные легли, несмотря на хикап ответа.
                try:
                    _mrec = json.loads(kv("get", {"key": _pkey + ":meta"}).get("value") or "{}")
                    if int(_mrec.get("pulled_at", 0)) >= int(_pt0) - 5 and int(_mrec.get("chunks", 0)) > 0:
                        _pull_ok = True
                except Exception:
                    pass
            if not _pull_ok:
                fired.append({"key": key, "status": "source_pull_failed", "err": str((_po or {}).get("err", ""))[:120]})
                continue   # источник не отдал данные — не гоняем оркестратор на устаревших/пустых данных
            # дрифт структуры: колонки источника изменились с привязки → прогон по чужой схеме = мусор.
            # ЛОМАЮЩИЙ дрифт (исчезли колонки) — не гоним автопилот молча, честно помечаем; мягкий (добавились) — ок.
            _base_schema = _srcinfo.get("schema") or []
            _cur_cols = _po.get("columns")
            if not (isinstance(_cur_cols, list) and _cur_cols):
                _prev = _po.get("preview") or _po.get("sample")
                _cur_cols = list(_prev[0].keys()) if (isinstance(_prev, list) and _prev and isinstance(_prev[0], dict)) else []
            _cur_schema = sorted(str(c) for c in (_cur_cols or []))
            if _base_schema and _cur_schema:
                _removed = sorted(set(_base_schema) - set(_cur_schema))
                if _removed:
                    fired.append({"key": key, "status": "source_drift", "removed": _removed})
                    continue
        if fid:
            # composed-задача: раннер flow читает план из KV; agent_id (Qwen клиента) кладёт мост при schedule
            run_params = {"api_token": api_token, "flow_id": fid}
            if cfg.get("agent_id"):
                run_params["agent_id"] = cfg["agent_id"]
            if cfg.get("rules"):
                run_params["rules"] = json.dumps(cfg["rules"], ensure_ascii=False)     # «Правила и поля» владельца
            if cfg.get("fields"):
                run_params["fields"] = json.dumps(cfg["fields"], ensure_ascii=False)
        else:
            run_params = {"api_token": api_token, "source_file": src}
            if skey:
                run_params["source_key"] = skey     # резолвер материализует файл из общего стора
            # F2 (контракт параметров): rules/fields — ТОЛЬКО контрактным оркестраторам
            # (params_contract пишет /x/schedule из builds; старые процессы упали бы на лишних kwargs)
            # A1: карта размещения (стадия → устройство). Мост кладёт её в KV уже развёрнутой.
            if isinstance(cfg.get("placement"), dict) and cfg["placement"]:
                run_params["placement_json"] = json.dumps(cfg["placement"], ensure_ascii=False)
            if int(cfg.get("params_contract", 0) or 0) >= 1:
                # текстовые правила (кодогенным стадиям) + структурные фильтры (оркестратор применяет сам)
                _rp = list(cfg.get("rules") or []) + [r for r in (cfg.get("rules_struct") or []) if isinstance(r, dict)]
                if _rp:
                    run_params["rules_json"] = json.dumps(_rp, ensure_ascii=False)
                if cfg.get("fields"):
                    run_params["fields_json"] = json.dumps(cfg["fields"], ensure_ascii=False)
        if tgt:
            run_params["target"] = tgt          # оркестратор пинит свои стадии на то же устройство
        run_body = {"expert_name": orch, "params": run_params, "global": True}
        if tgt:
            run_body["target"] = tgt            # сам оркестратор исполняется на хостинге
        # Надёжность: ретрай НАЧАЛЬНОГО запуска при транзиенте (обрыв связи / HTTP 5xx) ДО создания задачи —
        # безопасно (задача ещё не создана, дубля прогона не будет). Детерминированный ответ (4xx/готовый
        # результат) не ретраим. Слот уже столблён выше, так что наложения тиков не будет.
        import time as _tr
        resp, out, run_attempts = {}, {"status": "error", "message": "no attempt"}, 0
        for _att in range(1, 4):
            run_attempts = _att
            try:
                r = requests.post(base + "/api/expert/run", headers=headers, json=run_body, timeout=900)
                if r.status_code >= 500 and _att < 3:
                    _tr.sleep(2 * _att)
                    continue
                resp = r.json()
                out = resp.get("result", resp)
                break
            except Exception as e:
                out = {"status": "error", "message": str(e)[:150]}
                if _att < 3:
                    _tr.sleep(2 * _att)
                    continue
                resp = {}

        def as_dict(v):
            if isinstance(v, dict):
                return v
            if isinstance(v, str):
                try:
                    return json.loads(v)
                except Exception:
                    try:
                        import ast
                        return ast.literal_eval(v)
                    except Exception:
                        return {"status": "unknown", "raw": v[:150]}
            return {"status": "unknown"}

        out = as_dict(out)
        # composed-задача (flow): deferred-запуск отдаёт task_id → поллим /api/tasks/check (не lastrun)
        if fid and out.get("status") != "success":
            _tid = resp.get("task_id") if isinstance(resp, dict) else None
            if _tid:
                import time as _t2
                _w = 0
                while _w < 240:
                    _t2.sleep(6); _w += 6
                    try:
                        st = requests.post(base + "/api/tasks/check", headers=headers,
                                           json={"task_id": _tid}, timeout=30).json()
                    except Exception:
                        continue
                    _st = str(st.get("status", "")).lower()
                    _r2 = st.get("result")
                    if _r2 not in (None, "") and not (isinstance(_r2, str) and "deferred" in _r2.lower()):
                        out = as_dict(_r2)
                        break
                    if _st.startswith(("error", "failed", "cancel", "timeout")):
                        out = {"status": "error", "message": _st}
                        break
        # отложенная задача возвращает "deferred" без task_id → оркестратор кладёт итог в
        # KV lastrun:<ns> (межустройственно). Ждём свежую запись новее старта тика.
        if (not fid) and out.get("status") != "success":
            import time as _t
            ns = orch.replace("_run_pipeline", "")
            lk = "lastrun:" + ns
            deadline = 0
            while deadline < 60:
                _t.sleep(5); deadline += 5
                g = kv("get", {"key": lk})
                raw2 = g.get("value")
                if not raw2:
                    continue
                try:
                    rec = json.loads(raw2)
                except Exception:
                    continue
                # запись свежая (после запуска этого прогона)?
                try:
                    fresh = datetime.fromisoformat(rec.get("at", "").replace("Z", "+00:00")) >= t0
                except Exception:
                    fresh = True
                if fresh and rec.get("status") == "success":
                    out = {"status": "success", "total_count": rec.get("total_count"),
                           "total_sum": rec.get("total_sum"), "report_xlsx": rec.get("report_xlsx"),
                           "host": rec.get("host")}
                    break
        _stat = (out or {}).get("status", "unknown")
        if fid:
            _stat = (out or {}).get("run_status") or _stat   # flow честно помечает деградацию (partial)
        run = {"at": now().isoformat(), "status": _stat, "attempts": run_attempts,
               "total_count": (out or {}).get("total_count"), "total_sum": (out or {}).get("total_sum"),
               "report_xlsx": (out or {}).get("report_xlsx"), "host": (out or {}).get("host"),
               "trigger": "schedule"}
        if fid:
            run["digest_source"] = "flow"
            run["flow_id"] = fid
        # Перед записью — ПЕРЕЧИТАТЬ свежий KV: мост мог во время прогона (минуты) поменять поля-владельца
        # (rules/fields/recipients/deliver/message_template). Пишем поверх свежего cfg, не поверх стейл-снимка,
        # иначе тик затрёт правку владельца (lost-update). Прогонные поля (runs/…ts) берём свои.
        try:
            _fresh_raw = kv("get", {"key": key}).get("value")
            _fresh = json.loads(_fresh_raw) if _fresh_raw else None
        except Exception:
            _fresh = None
        if isinstance(_fresh, dict):
            # C2: +flow_id/agent_id — иначе write-back тика откатывал бы чат-доводку композиции,
            # сделанную во время прогона (ревью CABINET_TZ: гонка «тик затирает правку владельца»)
            for _own in ("rules", "rules_struct", "fields", "recipients", "deliver", "message_template", "active", "interval_min", "period", "source", "flow_id", "agent_id", "params_contract"):
                if _own in _fresh:
                    cfg[_own] = _fresh[_own]
            runs = (_fresh.get("runs") or cfg.get("runs") or [])   # прогоны — из свежего (могли дописаться ручным запуском)
        else:
            runs = (cfg.get("runs") or [])
        runs.append(run)
        cfg["runs"] = runs[-10:]
        cfg["last_run_ts"] = run["at"]
        cfg["next_due_ts"] = (now() + timedelta(minutes=max(1, interval))).isoformat()
        kv("set", {"key": key, "value": json.dumps(cfg, ensure_ascii=False),
                   "description": "schedule " + key.split(":", 1)[-1]})
        # C3 (виджет «Последний результат» кабинета): дайджест прогона по расписанию раньше НИГДЕ
        # не сохранялся (в канал уходило 600 симв, файл оставался на исполняющем устройстве) —
        # теперь последний дайджест перезаписью живёт в digest:<sid> (один, не копим — потолок KV)
        _dgp = _mk_digest(out)   # готовый digest → файл report_md → синтез из summary (не только digest_md)
        if _dgp:
            try:
                kv("set", {"key": "digest:" + key.split(":", 1)[-1],
                           "value": json.dumps({"at": run["at"], "digest": str(_dgp)[:12000]}, ensure_ascii=False),
                           "description": "last digest"})
            except Exception:
                pass
        # доставка результата в каналы-получатели (telegram/email/…) — коннектор wz_connector_<канал> на хостинге.
        # Несколько получателей: cfg['recipients'] (список); обратная совместимость — одиночный cfg['deliver'].
        _rc = cfg.get("recipients")
        if isinstance(_rc, list) and _rc:
            recips = [str(x).strip().lower() for x in _rc if str(x).strip()]
        else:
            _d = str(cfg.get("deliver") or "").strip().lower()
            recips = [_d] if _d else []
        recips = [c for c in recips if c.replace("_", "").isalnum()]
        # flow: partial = дайджест есть, но с деградацией — доставляем честно (с пометкой в тексте)
        _deliv = None
        _deliver_ok = run.get("status") == "success" or (fid and run.get("status") == "partial")
        if recips and _deliver_ok:
            tc = run.get("total_count"); ts = run.get("total_sum")
            # шаблон сообщения per-automation (кабина «Шаблон»): {name}{count}{sum}{date}; пустой → дефолт
            _tpl = cfg.get("message_template")
            if isinstance(_tpl, str) and _tpl.strip():
                _fn = (lambda x: format(x, ",").replace(",", " ") if isinstance(x, (int, float)) else "—")
                msg = (_tpl.replace("{name}", str(cfg.get("name") or "процесс"))
                           .replace("{count}", str(tc) if tc is not None else "—")
                           .replace("{sum}", _fn(ts) if ts is not None else "—")
                           .replace("{date}", run["at"][:16].replace("T", " ") + " UTC"))
            else:
                msg = "✅ Extella: процесс отработал по расписанию."
                if tc is not None:
                    msg += "\nПозиций: " + str(tc)
                if ts is not None:
                    msg += "\nСумма: " + format(ts, ",").replace(",", " ") + " ₸"
                msg += "\n" + run["at"][:16].replace("T", " ") + " UTC"
            # composed-задача: главная ценность — сам дайджест → шлём его начало в канал
            _dg = (out or {}).get("digest_md") or (out or {}).get("digest") or ""
            if fid and _dg:
                if run.get("status") == "partial":
                    msg += "\n⚠ прогон с деградацией (часть шагов не отработала)"
                msg += "\n\n" + str(_dg)[:600]
            for deliver in recips:
                # идемпотентность доставки: один отчёт в один канал за ЭТОТ слот расписания — не дважды
                # (страховка от повторной доставки, если тик переиграл после успешной отправки)
                _dedup_k = "delivered:" + str(key) + "|" + str(_slot) + "|" + str(deliver)
                try:
                    if kv("get", {"key": _dedup_k}).get("value"):
                        _deliv = _deliv or []
                        _deliv.append({"channel": deliver, "ok": True, "skipped": "already_delivered"})
                        continue
                except Exception:
                    pass
                dbody = {"expert_name": "wz_connector_" + deliver, "global": True,
                         "params": {"api_token": api_token, "client": cfg.get("client", "default"),
                                    "mode": "send", "text": msg}}
                if cfg.get("target"):
                    dbody["target"] = cfg["target"]
                _dok, _derr = False, None
                try:
                    _dr = requests.post(base + "/api/expert/run", headers=headers, json=dbody, timeout=120)
                    _do = _dr.json().get("result", _dr.json())
                    if isinstance(_do, str):
                        try:
                            _do = json.loads(_do)
                        except Exception:
                            _do = {}
                    _dok = bool(isinstance(_do, dict) and _do.get("ok"))
                    _derr = (_do.get("err") if isinstance(_do, dict) else None)
                except Exception as _de:
                    _derr = str(_de)[:120]
                if _dok:   # ключ дедупа ставим ТОЛЬКО на успешной доставке (иначе — переиграем в след. тик)
                    try:
                        kv("set", {"key": _dedup_k, "value": json.dumps({"at": now().isoformat()}),
                                   "description": "delivery dedup"})
                    except Exception:
                        pass
                _deliv = _deliv or []
                _deliv.append({"channel": deliver, "ok": _dok, "err": _derr})   # #19: доставка больше не молчит — исход в возврат тика/лог
        fired.append({"key": key, "status": run["status"], "total_sum": run["total_sum"], "delivered": _deliv})

    # Capability Registry: суточный ПОЛНЫЙ пересбор (событийные обновления делает мост;
    # тик — страховка, чтобы реестр не протухал без событий). Маркер пишет сам эксперт.
    try:
        gr = requests.post(base + "/api/kv/get", headers=headers,
                           json={"key": "registry:last_rebuild"}, timeout=30).json()
        _lr = str(gr.get("value") or "")
        _stale = True
        if _lr:
            try:
                from datetime import datetime as _rdt
                _stale = (now() - _rdt.fromisoformat(_lr.replace("Z", "+00:00"))).total_seconds() > 86400
            except Exception:
                _stale = True
        if _stale:
            requests.post(base + "/api/expert/run", headers=headers,
                          json={"expert_name": "wz_registry_rebuild", "global": True, "params": {}},
                          timeout=30)   # отложенный запуск; результата не ждём
    except Exception:
        pass

    # Мультитаргет T3: heartbeat устройств = периодический ПЕРЕ-ПАСПОРТ (раз в 6 часов).
    # Свежесть паспорта — сигнал «устройство живо»: протухший паспорт (>48ч) блокирует
    # прогоны с требованиями к устройству (preflight моста) — не запускаем в никуда.
    # Устройства: дефолтный листенер (без target) + уникальные target'ы активных расписаний.
    try:
        gb = requests.post(base + "/api/kv/get", headers=headers,
                           json={"key": "target:passports:last_beat"}, timeout=30).json()
        _hb = str(gb.get("value") or "")
        _hb_stale = True
        if _hb:
            try:
                from datetime import datetime as _bdt
                _hb_stale = (now() - _bdt.fromisoformat(_hb.replace("Z", "+00:00"))).total_seconds() > 6 * 3600
            except Exception:
                _hb_stale = True
        if _hb_stale:
            _tset = [None]
            try:
                gi = requests.post(base + "/api/kv/get", headers=headers,
                                   json={"key": "sched:__index__"}, timeout=30).json()
                for _sid2 in (json.loads(gi.get("value") or "{}") or {}).get("sids", [])[:20]:
                    gc = requests.post(base + "/api/kv/get", headers=headers,
                                       json={"key": "sched:" + str(_sid2)}, timeout=30).json()
                    _cfg2 = json.loads(gc.get("value") or "{}")
                    if _cfg2.get("target") and _cfg2["target"] not in _tset:
                        _tset.append(_cfg2["target"])
            except Exception:
                pass
            for _t in _tset[:6]:
                bb = {"expert_name": "wz_target_passport", "global": True, "params": {}}
                if _t:
                    bb["target"] = _t
                try:
                    requests.post(base + "/api/expert/run", headers=headers, json=bb, timeout=30)
                except Exception:
                    pass   # устройство молчит → его паспорт протухнет — это и есть сигнал
            requests.post(base + "/api/kv/set", headers=headers,
                          json={"key": "target:passports:last_beat", "value": now().isoformat(),
                                "description": "device heartbeat marker"}, timeout=30)
    except Exception:
        pass

    # ── A4: авто-обновление ЗНАНИЙ агентов из живых источников (мозг не устаревает) ──────────────
    # Глобальный индекс knowsrc:__all__ ведёт мост (готовые KV-ключи — тику не считать namespace).
    # Для каждого созревшего источника: verify живых строк → Qwen извлекает факты → СТАРЫЕ факты
    # этого источника удаляем по id и пишем свежие (замена, не накопление) → двигаем next_due.
    knowledge_refreshed = []
    try:
        _all_raw = kv("get", {"key": "knowsrc:__all__"}).get("value")
        _all = json.loads(_all_raw) if _all_raw else []
    except Exception:
        _all = []
    for _ent in (_all if isinstance(_all, list) else [])[:20]:
        try:
            if not isinstance(_ent, dict) or not _ent.get("key"):
                continue
            _rec_raw = kv("get", {"key": _ent["key"]}).get("value")
            _rec = json.loads(_rec_raw) if _rec_raw else None
            if not isinstance(_rec, dict):
                continue
            _nd = _rec.get("next_due")
            if _nd:
                try:
                    if now() < datetime.fromisoformat(str(_nd).replace("Z", "+00:00")):
                        continue   # ещё не созрел
                except Exception:
                    pass
            _aid, _gid = _rec.get("agent_id"), _rec.get("gen_id")
            _llm = _rec.get("llm_agent")
            if not (_aid and _gid and _llm):
                continue
            # СТОЛБИМ СЛОТ до тяжёлой работы (как у расписаний): наложенный тик прочитает уже сдвинутый
            # next_due и пропустит. Иначе два тика делают обновление параллельно и факты ДВОЯТСЯ
            # (проверено вживую: в мозге 38 концептов при 19 отслеживаемых — 19 осиротело).
            try:
                _ivl0 = int(_rec.get("interval_hours", 24) or 24)
                _rec["next_due"] = (now() + timedelta(hours=max(1, _ivl0))).isoformat()
                kv("set", {"key": _ent["key"], "value": json.dumps(_rec, ensure_ascii=False),
                           "description": "knowledge source"})
            except Exception:
                pass
            # 1) свежие живые строки из генеративного источника
            _vr = requests.post(base + "/api/expert/run", headers=headers, json={
                "expert_name": "wz_source_gen_run", "global": True, **({"target": _rec["target"]} if _rec.get("target") else {}),
                "params": {"api_token": api_token, "client": _rec.get("client", "default"), "mode": "verify",
                           "gen_id": _gid, "api_base": base, "limit": 25}}, timeout=180).json()
            # результат эксперта приходит ПИТОНОВСКИМ repr (канон платформы) → только json.loads мало,
            # нужен ast-фолбэк; в тике для этого уже есть _j (иначе источник ложно «молчит»)
            _vo = _j(_vr.get("result", _vr), {}) if isinstance(_vr.get("result", _vr), str) else _vr.get("result", _vr)
            _rows = (_vo or {}).get("preview") if isinstance(_vo, dict) else None
            if not (isinstance(_vo, dict) and _vo.get("ok") and _rows):
                knowledge_refreshed.append({"gen_id": _gid, "status": "source_silent"})
                continue
            # 2) Qwen дробит живые строки на атомарные факты
            _pr = ("Живые данные из источника («" + str(_rec.get("description", ""))[:120] + "»):\n" +
                   json.dumps(_rows[:25], ensure_ascii=False)[:3500] +
                   "\n\nИзвлеки АТОМАРНЫЕ факты для памяти агента (конкретные значения из ДАННЫХ, каждый "
                   'самодостаточен). Верни JSON {"facts":[...]}. Только из данных, без выдумок, 3-20 штук.')
            _ar = requests.post(base + "/api/agent/run", headers=headers, json={
                "agent_id": _llm, "input": _pr, "store": False, "run_timeout": 90}, timeout=140).json()
            _txt = ""
            for _it in (_ar or {}).get("output", []):
                if isinstance(_it, dict) and _it.get("type") == "message":
                    for _c in _it.get("content", []):
                        if isinstance(_c, dict) and _c.get("type") == "output_text":
                            _txt += _c.get("text", "")
            _txt = _txt or (_ar or {}).get("output_text", "")
            _m = re.search(r"\{.*\}", _txt, re.S)
            _facts = []
            if _m:
                try:
                    _facts = [str(f).strip() for f in (json.loads(_m.group(0)).get("facts") or []) if str(f).strip()][:20]
                except Exception:
                    _facts = []
            if not _facts:
                knowledge_refreshed.append({"gen_id": _gid, "status": "no_facts"})
                continue
            # 3) замена: старые факты этого источника — прочь, свежие — в мозг АГЕНТА (X-Agent-Id override)
            _ah = dict(headers)
            _ah["X-Agent-Id"] = _aid
            for _cid in (_rec.get("ids") or []):
                try:
                    requests.post(base + "/api/concept/delete", headers=_ah,
                                  json={"concept_id": _cid}, timeout=30)
                except Exception:
                    pass
            _new_ids = []
            for _f in _facts:
                try:
                    _cr = requests.post(base + "/api/concept/add", headers=_ah,
                                        json={"text": _f[:400]}, timeout=30).json()
                    if isinstance(_cr, dict) and _cr.get("id"):
                        _new_ids.append(_cr["id"])
                except Exception:
                    pass
            _ivl = int(_rec.get("interval_hours", 24) or 24)
            _rec["ids"] = _new_ids
            _rec["refreshed_at"] = now().isoformat()
            _rec["next_due"] = (now() + timedelta(hours=max(1, _ivl))).isoformat()
            kv("set", {"key": _ent["key"], "value": json.dumps(_rec, ensure_ascii=False),
                       "description": "knowledge source"})
            knowledge_refreshed.append({"gen_id": _gid, "status": "refreshed", "facts": len(_new_ids)})
        except Exception as _ke:
            knowledge_refreshed.append({"gen_id": (_ent or {}).get("gen_id"), "status": "error",
                                        "err": str(_ke)[:120]})

    return {"status": "success", "checked": checked, "fired": fired,
            "inbound": inbound_fired, "inbound_dbg": _dbg,
            "knowledge": knowledge_refreshed, "tick_at": now().isoformat()}