# expert: wz_project_spec
# description: Эксперт wz_project_spec (Adoption Wizard).
# params: session_id, base_dir, output_path

def wz_project_spec(
    session_id: str = "",
    base_dir: str = "",
    output_path: str = ""
) -> dict:
    import json
    from pathlib import Path
    from datetime import datetime, timezone

    def now():
        return datetime.now(timezone.utc).isoformat()

    if not session_id:
        return {"status": "error", "message": "session_id is required"}
    root = Path(base_dir) if base_dir else Path.home() / "extella_wizard" / "sessions"
    sp = root / (session_id + ".json")
    if not sp.exists():
        return {"status": "error", "message": "session not found: " + str(sp)}
    s = json.loads(sp.read_text(encoding="utf-8"))

    def jload(p):
        try:
            pp = Path(p)
            return json.loads(pp.read_text(encoding="utf-8")) if p and pp.exists() else None
        except Exception:
            return None

    bdoc = jload(s.get("blueprint_path"))
    bp = (bdoc or {}).get("blueprint", {})
    plan_doc = jload(s.get("build_plan_path"))
    plan = (plan_doc or {}).get("plan", {})
    manifest = jload(str(root / (session_id + "_build_manifest.json")))

    L = []
    A = L.append
    A("# ТЗ проекта: " + str(s.get("client_name", session_id)))
    A("")
    A("_Живой документ; собран автоматически из сессии `" + session_id + "` " + now()[:16].replace("T", " ") + " UTC. Источник правды — артефакты сессии; правки вносятся через интервью/комментарии/решения, не в этот файл._")
    A("")
    A("## 1. Статус")
    A("- Стадия: **" + str(s.get("stage", "?")) + "**")
    A("- Ответов интервью: " + str(len(s.get("answers", {}))) +
      " · Открытых комментариев: " + str(sum(1 for c in s.get("comments", []) if not c.get("resolved"))) +
      " · Решений: " + str(len(s.get("decisions", []))))
    if s.get("production_agent"):
        A("- Продовый агент: `" + str(s["production_agent"].get("agent_id")) + "` (" + str(s["production_agent"].get("name")) + ")")
    if s.get("demo_runs"):
        d = s["demo_runs"][-1]
        A("- Последний демо-прогон: " + str(d.get("run_id")) + " · скор " + str(d.get("avg_score_pct")) + "% · " + str(d.get("total_seconds")) + " с")
    A("")
    A("## 2. Интервью (что сказал клиент)")
    for qid, a in (s.get("answers") or {}).items():
        A("- **" + str(a.get("question") or qid) + "** — " + str(a.get("answer", "")))
    A("")
    if bp:
        su = bp.get("suitability") or {}
        A("## 3. Blueprint (утверждаемый план процесса)")
        A("**" + str(bp.get("process_name", "")) + "** — " + str(bp.get("goal", "")))
        ar = bp.get("archetype") or {}
        if ar.get("id"):
            A("- Архетип: " + str(ar["id"]) + (" · адаптация: " + str(ar.get("adaptation", "")) if ar.get("adaptation") else ""))
        A("- Пригодность: " + str(su.get("score")) + "/100 · риск " + str(su.get("risk_level")) +
          " · self-serve: " + ("да" if su.get("self_serve_allowed") else "нужна ИТ/ИБ-проверка"))
        A("")
        A("Стадии:")
        for i, st in enumerate(bp.get("stages") or []):
            A(str(i + 1) + ". **" + str(st.get("title")) + "** — " + str(st.get("business_description", "")) +
              " _(возможности: " + ", ".join(st.get("capability_ids") or []) + ")_")
        gaps = bp.get("gaps") or []
        if gaps:
            A("")
            A("Разрывы (честно):")
            for g in gaps:
                ext = (" [расширение: " + str(g.get("extension_id")) + "]") if g.get("extension_id") else ""
                A("- **" + str(g.get("title")) + "**" + ext + " — " + str(g.get("proposal", "")))
        oq = bp.get("open_questions") or []
        if oq:
            A("")
            A("Открытые вопросы: " + " · ".join(str(q) for q in oq))
        A("")
    if plan:
        A("## 4. План стройки")
        for t in plan.get("tasks") or []:
            status = ""
            if manifest:
                m = (manifest.get("tasks") or {}).get(t.get("id"), {})
                status = " → **" + str(m.get("status", "pending")) + "**"
            A("- `" + str(t.get("expert_name")) + "` [" + str(t.get("action")) + ", " + str(t.get("cspl")) + "] — " +
              str(t.get("purpose", ""))[:140] + status)
        orch = (plan.get("orchestrator") or {}).get("expert_name")
        if orch:
            A("- Оркестратор: `" + str(orch) + "`")
        pa = plan.get("production_agent") or {}
        if pa.get("name"):
            A("- Продовый агент (эскиз): " + str(pa.get("name")) + " — " + str(pa.get("role_summary", "")))
        A("")
    decs = s.get("decisions") or []
    if decs:
        A("## 5. Журнал решений")
        for d in decs:
            A("- " + str(d.get("created_at", ""))[:16].replace("T", " ") + " · **" + str(d.get("decision")) + "**" +
              (" — " + str(d.get("reason")) if d.get("reason") else "") + " _(" + str(d.get("author", "")) + ")_")
        A("")
    comments = s.get("comments") or []
    open_c = [c for c in comments if not c.get("resolved")]
    if open_c:
        A("## 6. Открытые комментарии команды")
        for c in open_c:
            A("- [" + str(c.get("block_ref")) + "] " + str(c.get("author")) + ": " + str(c.get("text")))
        A("")
    if s.get("audit"):
        A("## 7. Аудит")
        A("Вердикт: **" + str(s["audit"].get("verdict", "?")) + "** · " + str(s["audit"].get("summary", ""))[:400])
        A("")
    A("---")
    A("_Гейты: blueprint утверждает владелец → срез принимает владелец → деплой после аудита и подтверждения._")

    out = Path(output_path) if output_path else root / (session_id + "_spec.md")
    out.write_text("\n".join(L), encoding="utf-8")

    s.setdefault("log", []).append({"ts": now(), "event": "project spec assembled: " + str(out)})
    s["spec_path"] = str(out)
    s["updated_at"] = now()
    sp.write_text(json.dumps(s, ensure_ascii=False, indent=2), encoding="utf-8")

    return {"status": "success", "spec_path": str(out),
            "sections": {"answers": len(s.get("answers", {})), "stages": len(bp.get("stages", []) if bp else []),
                         "tasks": len(plan.get("tasks", []) if plan else []), "decisions": len(decs),
                         "open_comments": len(open_c)}}
