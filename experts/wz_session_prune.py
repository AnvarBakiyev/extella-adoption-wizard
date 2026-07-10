# expert: wz_session_prune
# description: Удаление сессий Визарда — одной по id или старых по возрасту; безопасно (превью по умолчанию, щадит живые процессы)
# params: session_id?, older_than_days?, apply?, include_built?, force?
$extens("include.py")
include("import json", [])

def wz_session_prune(session_id="", older_than_days=14, apply=False, include_built=False, force=False):
    """Чистка сессий Визарда на устройстве (~/extella_wizard/sessions).
    Режимы: (1) одна сессия — передай session_id; (2) старые — older_than_days (по created_at, иначе mtime).

    ВАЖНО (канон платформы): рантайм ОТБРАСЫВАЕТ falsy-параметры, поэтому все опасные переключатели
    сделаны «выкл по умолчанию, включаются truthy-значением» — иначе `false` просто потерялся бы:
      - apply         — по умолчанию НЕТ (только ПОКАЗАТЬ, что удалилось бы). Чтобы реально удалить, передай apply=true.
      - include_built — по умолчанию НЕТ (чистим только заброшенные: 0 ответов интервью и нет собранного процесса).
                        Передай include_built=true, чтобы удалять и с ответами/сборкой.
      - force         — по умолчанию НЕТ (сессии с живым production_agent НЕ трогаются). Передай force=true, чтобы удалить и их.
    Удаляет файл сессии И все сопутствующие (_blueprint/_build_plan/_build_manifest/_spec/_audit/_files/...).
    НЕ удаляет собранных на платформе экспертов процесса — только записи сессий на устройстве."""
    import os, json, glob, shutil
    from pathlib import Path
    from datetime import datetime, timezone, timedelta

    def as_bool(v, d):
        if isinstance(v, bool): return v
        s = str(v).strip().lower()
        if s in ("true", "1", "yes", "да", "on", "y"): return True
        if s in ("false", "0", "no", "нет", "off", "n", ""): return False
        return d
    apply = as_bool(apply, False)              # truthy → реально удалить; иначе превью
    include_built = as_bool(include_built, False)
    force = as_bool(force, False)
    dry_run = not apply
    only_empty = not include_built
    try: days = int(str(older_than_days))
    except Exception: days = 14
    session_id = str(session_id or "").strip()
    if session_id.startswith("{{"): session_id = ""

    SD = Path(os.path.expanduser("~/extella_wizard/sessions"))
    if not SD.is_dir():
        return {"status": "error", "message": "каталог сессий не найден: " + str(SD)}

    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    def created_of(doc, path):
        c = doc.get("created_at")
        if c:
            try: return datetime.fromisoformat(str(c).replace("Z", "+00:00"))
            except Exception: pass
        return datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)

    def is_built(doc, sid):
        return bool(doc.get("production_agent")) or (SD / (sid + "_build_manifest.json")).exists()

    mains = [p for p in sorted(SD.glob("*.json"))
             if not any(p.name.endswith(s) for s in ("_blueprint.json", "_build_plan.json", "_build_manifest.json"))]

    result, deleted = [], 0
    for p in mains:
        sid = p.stem
        try: doc = json.loads(p.read_text(encoding="utf-8"))
        except Exception: doc = {}
        if session_id:
            if sid != session_id: continue
        else:
            if created_of(doc, p) > cutoff: continue
            if only_empty and (len(doc.get("answers") or {}) > 0 or is_built(doc, sid)): continue
        entry = {"session_id": sid, "client_name": doc.get("client_name"),
                 "created_at": doc.get("created_at"), "answers": len(doc.get("answers") or {}),
                 "built": is_built(doc, sid)}
        if doc.get("production_agent") and not force:
            entry["skipped"] = "живой production_agent — force=true, если точно надо"
            result.append(entry); continue
        related = sorted(set(glob.glob(str(SD / (sid + ".json"))) + glob.glob(str(SD / (sid + "_*")))))
        entry["files"] = [os.path.basename(f) for f in related]
        if not dry_run:
            for f in related:
                try:
                    shutil.rmtree(f) if os.path.isdir(f) else os.remove(f)
                except Exception as e:
                    entry["error"] = str(e)[:100]
            if not entry.get("error"):
                entry["deleted"] = True; deleted += 1
        result.append(entry)

    return {"status": "success", "mode": "single" if session_id else "prune",
            "dry_run": dry_run, "older_than_days": days, "only_empty": only_empty,
            "candidates": sum(1 for e in result if not e.get("skipped")),
            "deleted": deleted, "skipped": sum(1 for e in result if e.get("skipped")),
            "sessions": result}
