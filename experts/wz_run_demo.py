# expert: wz_run_demo
# description: Эксперт wz_run_demo (Adoption Wizard).
# params: api_token, llm_api_key, run_id, session_id, industry, n_dialogues, sample_n, checklist_path, llm_base_url, gen_model, judge_model, findings_model, company_context, month, seed, columns_json, api_base

def wz_run_demo(
    api_token: str = "",
    llm_api_key: str = "",
    run_id: str = "",
    session_id: str = "",
    industry: str = "insurance",
    n_dialogues: int = 40,
    sample_n: int = 10,
    checklist_path: str = "",
    llm_base_url: str = "https://api.openai.com/v1",
    gen_model: str = "gpt-4o-mini",
    judge_model: str = "gpt-4o-mini",
    findings_model: str = "gpt-4o",
    company_context: str = "Демо-компания, синтетические данные",
    month: str = "2026-06",
    seed: int = 7,
    columns_json: str = '{"id_column": "ID обращения", "id_col": "ID обращения"}',
    api_base: str = "https://api.extella.ai"
) -> dict:
    import json, time, ast, uuid
    import urllib.request, urllib.error
    from pathlib import Path
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat()

    # ── Validate ───────────────────────────────────────────────────────
    if not api_token:
        return {"status": "error", "message": "api_token is required (Extella API token for stage calls)"}
    if not llm_api_key:
        return {"status": "error", "message": "llm_api_key is required"}
    try:
        n_dialogues = max(10, min(500, int(n_dialogues)))
        sample_n = max(1, min(50, int(sample_n)))
    except Exception:
        return {"status": "error", "message": "n_dialogues and sample_n must be integers"}

    if not run_id:
        run_id = "demo_" + datetime.now(timezone.utc).strftime("%Y%m%d_%H%M") + "_" + uuid.uuid4().hex[:4]
    run_dir = Path.home() / "extella_wizard" / "runs" / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    # ── Embedded reference checklist (used when checklist_path empty) ──
    DEFAULT_CHECKLIST = json.loads("""__CHECKLIST_JSON__""")
    if not checklist_path:
        checklist_path = str(run_dir / "checklist.json")
        Path(checklist_path).write_text(
            json.dumps(DEFAULT_CHECKLIST, ensure_ascii=False, indent=1), encoding="utf-8")
    elif not Path(checklist_path).exists():
        return {"status": "error", "message": "checklist_path not found: " + checklist_path}
    try:
        checklist_version = str(json.loads(Path(checklist_path).read_text(encoding="utf-8")).get("checklist_id", "ref")) + "-" + \
                            str(json.loads(Path(checklist_path).read_text(encoding="utf-8")).get("version", "1.0"))
    except Exception:
        checklist_version = "ref-v1.0"

    # ── Progress file ──────────────────────────────────────────────────
    STEPS = [
        {"id": "generate", "title": "Генерация синтетических данных"},
        {"id": "pipeline", "title": "Конвейер: разбор → ПДн → аналитика → ИИ-оценка"},
        {"id": "adapter", "title": "Сборка данных витрины"},
        {"id": "findings", "title": "ИИ-аналитик пишет выводы"},
    ]
    progress = {"run_id": run_id, "session_id": session_id,
                "started_at": now(), "updated_at": now(),
                "status": "running", "error": None,
                "params": {"industry": industry, "n_dialogues": n_dialogues,
                           "sample_n": sample_n, "judge_model": judge_model,
                           "checklist_version": checklist_version},
                "steps": [dict(s, status="pending", seconds=0, info=None) for s in STEPS]}

    def save_progress():
        progress["updated_at"] = now()
        (run_dir / "progress.json").write_text(
            json.dumps(progress, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    def step_rec(sid):
        return next(s for s in progress["steps"] if s["id"] == sid)

    save_progress()

    # ── REST helper (pattern from cx_run_pipeline: deferred -> artifact wait)
    endpoint = api_base.rstrip("/") + "/api/expert/run"
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def artifact_wait(out_file, wait_s):
        t0 = time.time()
        while time.time() - t0 < wait_s:
            p = Path(out_file)
            if p.exists():
                s1 = p.stat().st_size
                time.sleep(5)
                if p.exists() and p.stat().st_size == s1 and s1 > 0:
                    return {"status": "success", "deferred": True, "output": str(p)}
            time.sleep(8)
        return {"status": "error", "message": "output " + str(out_file) + " not seen within " + str(wait_s) + "s"}

    def run(name, params, out_file=None, wait_s=1800):
        payload = {"expert_name": name, "name": name, "params": params, "global": True}
        req = urllib.request.Request(endpoint, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except Exception as e:
            # network timeout while the stage may still be running server-side:
            # fall back to waiting for the expected artifact
            if out_file:
                return artifact_wait(out_file, wait_s)
            return {"status": "error", "message": "Request to " + name + " failed: " + str(e)[:300]}
        raw = resp.get("result", resp) if isinstance(resp, dict) else resp
        if isinstance(raw, str) and "deferred" in raw:
            if not out_file:
                return {"status": "error", "message": name + " deferred but no artifact to wait for"}
            return artifact_wait(out_file, wait_s)
        if isinstance(raw, str):
            for loader in (json.loads, ast.literal_eval):
                try:
                    v = loader(raw)
                    if isinstance(v, dict):
                        return v
                except Exception:
                    continue
            return {"status": "error", "message": "Unparseable result from " + name + ": " + raw[:300]}
        if not isinstance(raw, dict):
            return {"status": "error", "message": "Non-dict result from " + name}
        return raw

    def exec_step(sid, name, params, out_file=None, wait_s=1800):
        rec = step_rec(sid)
        rec["status"] = "running"
        save_progress()
        t0 = time.time()
        res = run(name, params, out_file=out_file, wait_s=wait_s)
        rec["seconds"] = round(time.time() - t0, 1)
        ok = isinstance(res, dict) and res.get("status") == "success"
        rec["status"] = "success" if ok else "error"
        rec["info"] = {k: v for k, v in res.items() if k != "status" and not isinstance(v, (list, dict))} if ok else None
        if not ok:
            rec["error"] = str(res.get("message", res))[:400]
            progress["status"] = "error"
            progress["error"] = "Шаг «" + rec["title"] + "»: " + rec["error"]
        save_progress()
        return ok, res

    fail = lambda: {"status": "error", "message": progress["error"], "run_id": run_id,
                    "progress_path": str(run_dir / "progress.json")}

    # ── Step 1: synthetic data ────────────────────────────────────────
    xlsx = str(run_dir / "demo_export.xlsx")
    ok, res_gen = exec_step("generate", "cx_generate_synthetic_dialogues", {
        "output_path": xlsx, "industry": industry, "n_dialogues": n_dialogues,
        "api_key": llm_api_key, "base_url": llm_base_url, "model": gen_model,
        "month": month, "seed": int(seed)}, out_file=xlsx, wait_s=2400)
    if not ok:
        return fail()

    # ── Step 2: full pipeline ─────────────────────────────────────────
    manifest_path = str(run_dir / "manifest.json")
    ok, res_pipe = exec_step("pipeline", "cx_run_pipeline", {
        "api_token": api_token, "api_base": api_base,
        "work_dir": str(run_dir), "file_paths_json": json.dumps([xlsx]),
        "llm_api_key": llm_api_key, "llm_base_url": llm_base_url,
        "llm_model": judge_model, "checklist_path": checklist_path,
        "checklist_version": checklist_version,
        "company_context": company_context, "sample_n": sample_n,
        "columns_json": columns_json},
        out_file=manifest_path, wait_s=3000)
    if not ok:
        return fail()
    try:
        manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except Exception:
        manifest = {}
    eval_ok = any(s.get("name") == "cx_evaluate_checklist" and s.get("status") == "success"
                  for s in manifest.get("stages", []))
    if not eval_ok:
        stage_errors = ["{}: {}".format(s.get("name"), (s.get("error") or "")[:160])
                        for s in manifest.get("stages", []) if s.get("status") == "error"]
        step_rec("pipeline")["status"] = "error"
        progress["status"] = "error"
        progress["error"] = ("Стадия ИИ-оценки не удалась. Ошибки конвейера: "
                             + ("; ".join(stage_errors) if stage_errors else "см. manifest.json"))
        save_progress()
        return fail()

    # ── Step 3: dashboard data adapter ────────────────────────────────
    ok, res_ad = exec_step("adapter", "cx_build_dashboard_data", {
        "eval_json_path": str(run_dir / "eval_sample.json"),
        "parsed_pkl_path": str(run_dir / "parsed.pkl"),
        "output_dir": str(run_dir), "checklist_path": checklist_path},
        out_file=str(run_dir / "llm_results.json"), wait_s=900)
    if not ok:
        return fail()

    # ── Step 4: findings (verified-quotes pattern) ────────────────────
    findings_path = str(run_dir / "findings.json")
    ok, res_f = exec_step("findings", "cx_generate_findings", {
        "aggregates_json": json.dumps({"llm_results": str(run_dir / "llm_results.json"),
                                       "by_day": str(run_dir / "by_day.json")}),
        "evidence_samples_path": str(run_dir / "checklist_quotes.json"),
        "api_key": llm_api_key, "base_url": llm_base_url, "model": findings_model,
        "company_context": company_context, "max_findings": 4,
        "output_path": findings_path}, out_file=findings_path, wait_s=900)
    if not ok:
        return fail()

    # ── Assemble result.json ──────────────────────────────────────────
    def jload(p):
        try:
            return json.loads(Path(p).read_text(encoding="utf-8"))
        except Exception:
            return {}

    llm_results = jload(run_dir / "llm_results.json")
    findings_doc = jload(findings_path)
    anon_report = jload(run_dir / "anonymization_report.json")
    parse_out = next((s.get("output") or {} for s in manifest.get("stages", [])
                      if s.get("name") == "cx_parse_dialogues"), {})
    anon_out = next((s.get("output") or {} for s in manifest.get("stages", [])
                     if s.get("name") == "cx_anonymize_dataset"), {})

    total_seconds = round(sum(s["seconds"] for s in progress["steps"]), 1)
    result = {
        "run_id": run_id, "session_id": session_id,
        "finished_at": now(), "total_seconds": total_seconds,
        "params": progress["params"],
        "generated": {"rows": res_gen.get("rows") or parse_out.get("total_dialogues") or n_dialogues,
                      "languages": parse_out.get("languages"),
                      "queues": parse_out.get("queues_list"),
                      "csat_avg": parse_out.get("csat_avg"),
                      "avg_wait_sec": parse_out.get("avg_wait_sec")},
        "anonymization": {"replacements": (anon_out.get("replacements")
                                           or anon_report.get("replacements")),
                          "verification": (anon_out.get("verification")
                                           or anon_report.get("verification"))},
        "evaluation": {"total_evaluated": (llm_results.get("meta") or {}).get("total_evaluated"),
                       "avg_score_pct": (llm_results.get("checklist") or {}).get("avg_score_pct"),
                       "criteria": (llm_results.get("checklist") or {}).get("criteria"),
                       "fail_examples": ((llm_results.get("checklist") or {}).get("fail_examples") or [])[:6],
                       "by_queue": (llm_results.get("checklist") or {}).get("by_queue"),
                       "checklist_version": checklist_version},
        "findings": {"items": findings_doc.get("findings", []),
                     "warnings": findings_doc.get("warnings", []),
                     "model": findings_doc.get("model_version")},
        "artifacts_dir": str(run_dir),
    }
    (run_dir / "result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=1, default=str), encoding="utf-8")

    progress["status"] = "success"
    save_progress()

    # ── Attach run to wizard session ──────────────────────────────────
    if session_id:
        try:
            sp = Path.home() / "extella_wizard" / "sessions" / (session_id + ".json")
            if sp.exists():
                s = json.loads(sp.read_text(encoding="utf-8"))
                s.setdefault("demo_runs", []).append(
                    {"run_id": run_id, "finished_at": now(), "status": "success",
                     "avg_score_pct": result["evaluation"]["avg_score_pct"],
                     "total_seconds": total_seconds})
                s.setdefault("log", []).append({"ts": now(), "event": "demo run finished: " + run_id})
                s["updated_at"] = now()
                sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:
            pass

    return {"status": "success", "run_id": run_id,
            "total_seconds": total_seconds,
            "avg_score_pct": result["evaluation"]["avg_score_pct"],
            "total_evaluated": result["evaluation"]["total_evaluated"],
            "findings_count": len(result["findings"]["items"]),
            "result_path": str(run_dir / "result.json")}
