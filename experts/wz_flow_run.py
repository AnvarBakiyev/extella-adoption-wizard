# expert: wz_flow_run
# description: Универсальный раннер автоматизаций композитора: читает декларативный план из KV flow:<flow_id> (шаги из проверенных блоков + synthesis-промпт), исполняет шаги (дожидаясь отложенных), синтезирует бриф на Qwen fine-tune, доставляет. Честно помечает деградацию (warnings/run_status). Возвращает digest_md для показа в приложении.

def wz_flow_run(flow_id="", agent_id="", deliver="", deliver_client="",
                api_token="", api_base="https://api.extella.ai", work_dir="",
                source_file="", source_key="", target="", client="",
                rules="", fields="") -> dict:
    # хвостовые параметры — толерантность к вызову планировщиком/Визардом (игнорируются)
    import json, re, time, urllib.request
    from pathlib import Path
    from datetime import datetime, timezone

    def _b(v):
        return (not v) or str(v).startswith("{{")

    if _b(flow_id):
        return json.dumps({"status": "error", "message": "flow_id обязателен"}, ensure_ascii=False)
    if _b(api_base):
        api_base = "https://api.extella.ai"
    if _b(agent_id):
        # дефолт — свой Qwen клиента из конфига Визарда (прежний хардкод agent_iVWW… удалён с платформы → 404)
        _cfgp = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            _c = json.loads(_cfgp.read_text(encoding="utf-8")) if _cfgp.exists() else {}
        except Exception:
            _c = {}
        _ch = _c.get("llm_agents") or []
        agent_id = ((_ch[0] if isinstance(_ch, list) and _ch else "") or _c.get("llm_agent_id") or _c.get("agent_id") or "")
    if _b(agent_id):
        return json.dumps({"status": "error", "message": "нет agent_id (Qwen): передай параметром или заполни config.json Визарда"}, ensure_ascii=False)
    if _b(api_token):
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return json.dumps({"status": "error", "message": "нет api_token"}, ensure_ascii=False)

    # КАНОН: KV/expert-run/tasks — служебный скоуп agent_extella_default (иначе flow:/lastrun не видны
    # серверу/другим); Qwen (agent_id) — ТОЛЬКО на /api/agent/run (синтез). Это скоуп-заголовок, не платный вызов.
    SVC = "agent_extella_default"
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": SVC}

    def _post(path, body, t=600, agent=None):
        h = dict(headers)
        if agent:
            h["X-Agent-Id"] = agent
        req = urllib.request.Request(api_base.rstrip("/") + path,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=h, method="POST")
        with urllib.request.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode("utf-8"))

    def _parse(raw):
        out = raw
        if isinstance(raw, str):
            try:
                out = json.loads(raw)
            except Exception:
                try:
                    import ast
                    out = ast.literal_eval(raw)
                except Exception:
                    out = {"raw": raw[:2000]}
        return out

    # --- план из KV ---
    try:
        raw = _post("/api/kv/get", {"key": "flow:" + str(flow_id)}, t=60).get("value")
        flow = json.loads(raw) if raw else None
    except Exception as e:
        return json.dumps({"status": "error", "message": "не прочитал план: " + str(e)[:120]}, ensure_ascii=False)
    if not isinstance(flow, dict) or not flow.get("steps"):
        return json.dumps({"status": "error", "message": "план flow:%s пуст/не найден" % flow_id}, ensure_ascii=False)

    if _b(deliver):
        deliver = flow.get("deliver", "")
    if _b(deliver_client):
        deliver_client = flow.get("deliver_client", "default")

    wd = Path(work_dir) if not _b(work_dir) else (Path.home() / "extella_wizard" / "flows" / str(flow_id))
    wd.mkdir(parents=True, exist_ok=True)

    # --- прогон эксперта с ожиданием отложенной задачи (зеркалит server.run_expert) ---
    def _await_expert(expert_name, params, wait=200):
        res = _post("/api/expert/run", {"expert_name": expert_name, "params": params, "global": True})
        task_id = res.get("task_id") if isinstance(res, dict) else None
        # deferred-признак: строка result содержит "deferred" и внутри лежит настоящий uuid task_id
        if not task_id and isinstance(res, dict) and isinstance(res.get("result"), str) and "deferred" in res["result"].lower():
            cand = _parse(res.get("result"))
            cid = cand.get("task_id") if isinstance(cand, dict) else None
            if isinstance(cid, str) and len(cid) >= 20 and "-" in cid:
                task_id = cid
        if task_id:
            t0 = time.time()
            while time.time() - t0 < wait:
                time.sleep(5)
                try:
                    st = _post("/api/tasks/check", {"task_id": task_id}, t=30)
                except Exception:
                    continue
                status = str(st.get("status", "")).lower()
                _r = st.get("result")
                _has = _r not in (None, "") and not (isinstance(_r, str) and "deferred" in _r.lower())
                if status.startswith(("success", "completed", "done", "finished", "ok", "error", "failed", "cancel", "timeout")) or _has:
                    return _parse(st.get("result", st))
            return {"status": "error", "message": "шаг завис (timeout)", "task_id": task_id}
        return _parse(res.get("result", res))

    # --- признак провала шага (не только status==error): честный fail, не молчаливый успех ---
    def _step_failed(out):
        if out is None:
            return True
        if isinstance(out, dict):
            if out.get("status") == "error":
                return True
            if out.get("ok") is False:
                return True
            if out.get("error") or out.get("traceback"):
                return True
            if set(out.keys()) == {"raw"}:            # _parse-фолбэк = нераспарсенный/битый ответ
                return True
            if "task_id" in out and "status" not in out and "result" not in out:  # неразрешённый deferred
                return True
        return False

    # --- {{save_as}} / {{save_as.field}} подстановки из прошлых результатов ---
    results = {}
    stages = []
    warnings = []

    def _resolve(k1, k2):
        base = results.get(k1)
        if k2 is not None:
            base = base.get(k2) if isinstance(base, dict) else None   # .field на не-словаре -> нет данных (не весь объект)
        return base

    def _subst(v):
        if isinstance(v, str):
            m = re.fullmatch(r"\{\{(\w+)(?:\.(\w+))?\}\}", v.strip())
            if m:   # плейсхолдер занимает всю строку — отдаём значение как есть (сохраняем тип)
                base = _resolve(m.group(1), m.group(2))
                if base is None:
                    return v
                return base if not isinstance(base, (dict, list)) else json.dumps(base, ensure_ascii=False)
            # инлайн-подстановка: {{key}} / {{key.field}} ВНУТРИ большого текста
            # (напр. question у cap_local_ask: "...detected: {{subscriptions_raw}}. Analyze...")
            def _fill(mo):
                base = _resolve(mo.group(1), mo.group(2))
                if base is None:
                    return mo.group(0)   # нет данных — оставляем плейсхолдер видимым (честно)
                return base if isinstance(base, str) else json.dumps(base, ensure_ascii=False)
            return re.sub(r"\{\{(\w+)(?:\.(\w+))?\}\}", _fill, v)
        if isinstance(v, dict):
            return {k: _subst(x) for k, x in v.items()}   # params должны остаться dict для /api/expert/run
        if isinstance(v, list):
            return [_subst(x) for x in v]
        return v

    for st in flow["steps"]:
        name = st.get("save_as") or st.get("expert")
        rec = {"name": name, "expert": st.get("expert"), "status": "running"}
        stages.append(rec)
        try:
            out = _await_expert(st["expert"], _subst(st.get("params") or {}))
        except Exception as e:
            out = {"status": "error", "message": str(e)[:200]}
        if _step_failed(out):
            emsg = str((out or {}).get("message") or (out or {}).get("error") or (out or {}).get("raw") or out)[:200] if isinstance(out, dict) else "нет валидного результата"
            rec["status"] = "error"; rec["error"] = emsg
            if not st.get("optional"):
                return json.dumps({"status": "error", "message": "шаг %s: %s" % (name, emsg), "stages": stages}, ensure_ascii=False)
            warnings.append("шаг %s не отработал: %s" % (name, emsg))
            continue
        results[name] = out
        rec["status"] = "success"

    # --- синтез брифа на Qwen fine-tune ---
    digest_md = ""
    degraded = bool(warnings)
    syn = flow.get("synthesis_prompt") or ""
    if syn:
        data_blob = json.dumps(results, ensure_ascii=False, default=str)[:16000]
        # «Правила и поля» владельца (кабинет автоматизации): правила ОБЯЗАТЕЛЬНЫ для синтеза,
        # поля — доп. контекст процесса. Приходят JSON-строками из моста/тика (sессия автоматизации).
        owner_block = ""
        try:
            _r = json.loads(rules) if rules and not str(rules).startswith("{{") else []
            if isinstance(_r, list) and _r:
                owner_block += "\nOWNER RULES (обязательные требования владельца процесса — соблюдать строго):\n" + \
                               "\n".join("- " + str(x)[:200] for x in _r[:20]) + "\n"
        except Exception:
            pass
        try:
            _f = json.loads(fields) if fields and not str(fields).startswith("{{") else {}
            if isinstance(_f, dict) and _f:
                owner_block += "\nCONTEXT FIELDS (факты о процессе от владельца):\n" + \
                               "\n".join("- " + str(k)[:60] + ": " + str(v)[:200] for k, v in list(_f.items())[:20]) + "\n"
        except Exception:
            pass
        msg = ("You are an analyst agent inside a composed automation. Work ONLY from DATA below — no tools, no browsing.\n"
               "TASK:\n" + syn + "\n" + owner_block + "\nDATA (JSON, step results):\n" + data_blob + "\n\n"
               "Output: clean MARKDOWN only (## headings, tables, bullet lists). No preamble, no code fences.")
        stages.append({"name": "Синтез (Qwen)", "status": "running"})
        try:
            resp = _post("/api/agent/run", {"agent_id": agent_id, "input": msg, "store": False,
                                            "temperature": 0, "run_timeout": 180,
                                            "tool_choice": "none", "max_output_tokens": 4000}, agent=agent_id)
            parts = []
            for it in (resp.get("output") or []):
                if isinstance(it, dict) and it.get("type") == "message":
                    for c in it.get("content", []):
                        if isinstance(c, dict) and c.get("text"):
                            parts.append(c["text"])
            digest_md = ("\n".join(parts) or resp.get("output_text") or "").strip()
            stages[-1]["status"] = "success"
        except Exception as e:
            stages[-1]["status"] = "error"; stages[-1]["error"] = str(e)[:200]
            warnings.append("синтез брифа не выполнился: " + str(e)[:150])
            degraded = True
    if not digest_md:  # честный фолбэк — сырые результаты
        degraded = True
        digest_md = "# " + (flow.get("name") or str(flow_id)) + " — raw results\n\n```\n" + \
                    json.dumps(results, ensure_ascii=False, indent=2, default=str)[:6000] + "\n```"

    digest_path = wd / "brief.md"
    digest_path.write_text(digest_md, encoding="utf-8")

    # --- доставка (опционально) ---
    delivered = False
    delivery_error = ""
    if deliver and str(deliver).lower() in ("slack", "email", "telegram"):
        exp = "wz_connector_" + str(deliver).lower()
        try:
            d = _parse(_post("/api/expert/run", {"expert_name": exp, "global": True,
                             "params": {"api_token": api_token, "client": deliver_client or "default",
                                        "mode": "send", "text": digest_md[:38000]}}).get("result"))
            delivered = bool(isinstance(d, dict) and d.get("ok"))
            if not delivered:
                delivery_error = str((d or {}).get("error") or (d or {}).get("message") or "доставка не подтверждена")[:200]
        except Exception as ex:
            delivery_error = str(ex)[:200]
        if delivery_error:
            warnings.append("доставка (%s) не прошла: %s" % (deliver, delivery_error))
            degraded = True

    run_status = "partial" if degraded else "success"
    try:
        _post("/api/kv/set", {"key": "lastrun:flow:" + str(flow_id),
                              "value": json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                                   "status": run_status, "delivered": delivered,
                                                   "warnings": warnings[:10]},
                                                  ensure_ascii=False)}, t=60)
    except Exception:
        pass

    return json.dumps({"status": "success", "run_status": run_status, "degraded": degraded,
            "warnings": warnings[:10], "flow_id": flow_id, "digest_md": digest_md[:20000],
            "digest_path": str(digest_path), "delivered": delivered, "delivery_error": delivery_error,
            "stages": stages}, ensure_ascii=False)
