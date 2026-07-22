"""Universal Process Contract v1: deterministic graph/state/policy core.

The module has no platform or LLM dependency. Planner, Builder, Wizard, Chat, Composer and Workspace
must exchange this contract instead of creating their own execution state. Platform experts remain the
execution primitive; this module decides what is ready, what was actually accepted and what must be
repaired or approved by a human.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path


SCHEMA = "upc/1.0"
STEP_RESULT_SCHEMA = "upc-step-result/1.0"
STEP_STATUSES = (
    "pending", "ready", "running", "succeeded", "failed", "repairing",
    "blocked_human", "skipped", "stale", "cancelled",
)
IMPLEMENTATION_MODES = ("reuse", "generate", "llm_worker", "acquire", "human", "delegate")
FAILURE_CLASSES = (
    "transient_transport", "workload_timeout", "implementation_defect",
    "output_contract_missing", "semantic_acceptance_failed", "no_progress",
    "semantic_ambiguity", "capability_mismatch",
)
CONTROL_ACTIONS = (
    "retry_transient", "repair_code", "repair_output_contract", "replace_implementation",
    "split_step", "ask_owner", "stop_fail_closed",
)
MEMORY_KINDS = ("evidence", "lesson", "concept", "rule", "artifact")
MEMORY_STATUSES = ("candidate", "verified", "rejected", "superseded")
PERMISSION_KINDS = ("read", "create", "move", "modify", "delete", "install", "send", "external_write")
DANGEROUS_PERMISSIONS = ("move", "modify", "delete", "install", "send", "external_write")
TERMINAL_STATUSES = ("succeeded", "failed", "blocked_human", "skipped", "cancelled")

TRANSITIONS = {
    "pending": {"ready", "skipped", "cancelled"},
    "ready": {"running", "blocked_human", "cancelled"},
    "running": {"succeeded", "repairing", "failed", "blocked_human", "cancelled"},
    "repairing": {"running", "failed", "blocked_human", "cancelled"},
    "succeeded": {"stale"},
    "stale": {"ready", "skipped", "cancelled"},
    "blocked_human": {"ready", "cancelled"},
    "failed": {"repairing", "ready", "cancelled"},
    "skipped": set(),
    "cancelled": set(),
}

ERROR_MARKERS = (
    "[execution error]", "traceback (most recent call last)", "nameerror:", "typeerror:",
    "valueerror:", "keyerror:", "eoferror", "syntaxerror:", "runtimeerror:",
    "modulenotfounderror:", "permissionerror:", "filenotfounderror:",
)


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def _clip(value, limit=1200):
    text = str(value if value is not None else "").replace("\x00", "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _safe_id(value, prefix="s"):
    raw = re.sub(r"[^a-zA-Z0-9_-]+", "_", str(value or "").strip()).strip("_-").lower()
    if not raw:
        raw = prefix + "_" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:8]
    if not re.match(r"^[a-z]", raw):
        raw = prefix + "_" + raw
    return raw[:80]


def _stable_hash(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def file_sha256(path):
    path = Path(path)
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def input_fingerprint(paths):
    """Content fingerprint for a file set; names alone never validate a checkpoint."""
    rows = []
    for raw in paths or []:
        path = Path(str(raw))
        if path.is_file():
            rows.append({"name": path.name, "bytes": path.stat().st_size, "sha256": file_sha256(path)})
        elif path.is_dir():
            for child in sorted((x for x in path.rglob("*") if x.is_file()), key=lambda x: str(x)):
                rows.append({"name": str(child.relative_to(path)), "bytes": child.stat().st_size,
                             "sha256": file_sha256(child)})
        else:
            rows.append({"name": path.name or str(path), "missing": True})
    return _stable_hash(rows)


def step_contract_fingerprint(step):
    """Hash only contract-bearing fields; runtime status/history must not invalidate itself."""
    step = step if isinstance(step, dict) else {}
    return _stable_hash({k: step.get(k) for k in (
        "id", "version", "purpose", "dependencies", "input_contract", "output_contract",
        "implementation", "permissions", "acceptance", "retry_policy", "execution_role",
    )})


def infer_execution_role(stage=None, task=None):
    """Classify what a step *does* without tying the runtime to one business domain.

    The planner may provide an explicit role.  Legacy plans did not, so a conservative fallback
    separates data/reasoning work from runtime configuration and delivery.  The distinction is a
    safety boundary: schedule/watch/send wrappers must never receive the original business files
    or be decomposed into one child per PDF/XLSX.
    """
    stage = stage if isinstance(stage, dict) else {}
    task = task if isinstance(task, dict) else {}
    explicit = str(task.get("execution_role") or task.get("stage_role") or task.get("role") or
                   stage.get("execution_role") or stage.get("stage_role") or stage.get("role") or "")
    normalized = re.sub(r"[^a-z]+", "_", explicit.casefold()).strip("_")
    aliases = {
        "data": "data", "transform": "data", "extract": "data", "compute": "data",
        "reason": "reasoning", "reasoning": "reasoning", "llm": "reasoning",
        "merge": "integration", "join": "integration", "integration": "integration",
        "schedule": "runtime_setup", "trigger": "runtime_setup", "runtime": "runtime_setup",
        "runtime_setup": "runtime_setup", "watcher": "runtime_setup", "monitor": "runtime_setup",
        "delivery": "delivery", "send": "delivery", "notify": "delivery",
        "control": "control", "orchestrator": "control", "human": "human",
    }
    if normalized in aliases:
        return aliases[normalized]
    mode = str(task.get("implementation_mode") or stage.get("implementation_mode") or "").casefold()
    if mode == "human":
        return "human"
    permissions = task.get("permissions") if isinstance(task.get("permissions"), dict) else {}
    permissions = permissions or (stage.get("permissions") if isinstance(stage.get("permissions"), dict) else {})
    if permissions.get("send") or permissions.get("external_write"):
        return "delivery"
    name = str(task.get("expert_name") or stage.get("expert_name") or "").casefold().replace("-", "_")
    text = " ".join(str((task.get(key) or stage.get(key) or ""))
                    for key in ("title", "purpose", "business_description")).casefold()
    if any(marker in name for marker in (
            "schedule", "cron", "autostart", "launchd", "systemd", "daemon", "watcher",
            "folder_monitor", "monitor_folder", "folder_watch", "watch_folder", "setup_monitor")) or \
       any(marker in text for marker in (
            "расписан", "регулярный запуск", "автозапуск", "фоновый демон", "демон мониторинга",
            "launchagent", "launchd", "systemd", "scheduled trigger", "watch folder")):
        return "runtime_setup"
    if any(marker in name for marker in ("send_", "_send_", "deliver", "notif_")) or \
       any(marker in text for marker in ("доставка отч", "отправить отч", "отправка отч", "send report")):
        return "delivery"
    if any(marker in name for marker in ("orchestr", "pipeline")) or \
       any(marker in text for marker in ("оркестратор процесса", "управляет шагами", "координирует шаги")):
        return "control"
    if any(marker in text for marker in ("объединить принятые", "свести результаты", "merge accepted")):
        return "integration"
    return "reasoning" if mode == "llm_worker" else "data"


def step_can_partition(step):
    """Only data/integration work may be split by physical input boundaries."""
    step = step if isinstance(step, dict) else {}
    contract = step.get("input_contract") if isinstance(step.get("input_contract"), dict) else {}
    if contract.get("allow_partition") is False:
        return False
    if str(contract.get("scope") or "") in ("map_partition", "runtime_config", "dependency_bundle"):
        return False
    return str(step.get("execution_role") or "data") in ("data", "reasoning", "integration")


def upgrade_process_graph(graph):
    """Bring an in-flight UPC sidecar to the current execution-boundary contract.

    QA users often update the bridge while a long build is paused.  Restarting from a clean graph
    would throw away accepted experts; blindly resuming an older graph would keep the exact bug we
    fixed (a PDF partition asking for the Excel owned by its sibling).  This migration is deliberately
    narrow: infer missing execution roles everywhere, and rewrite only recognizable, unfinished
    failure-map children.  Role-only changes preserve already accepted checkpoints.
    """
    if not isinstance(graph, dict):
        return {"changed": [], "unblocked": []}
    changed, unblocked, contract_rewrites = [], [], False
    steps = [x for x in (graph.get("steps") or []) if isinstance(x, dict)]
    by_id = {str(x.get("id") or ""): x for x in steps}

    def rehash_checkpoint(step):
        checkpoint = step.get("checkpoint") if isinstance(step.get("checkpoint"), dict) else None
        if not checkpoint or checkpoint.get("schema") != "upc-checkpoint/1.0":
            return
        checkpoint["step_contract_sha256"] = step_contract_fingerprint(step)
        checkpoint["checkpoint_sha256"] = _stable_hash({
            k: v for k, v in checkpoint.items() if k not in ("accepted_at", "checkpoint_sha256")})

    for step in steps:
        role_only = False
        if not step.get("execution_role"):
            step["execution_role"] = infer_execution_role(step, step)
            role_only = True
            changed.append(str(step.get("id") or "") + ":execution_role")
        if step.get("status") != "succeeded":
            acceptance = step.get("acceptance") if isinstance(step.get("acceptance"), dict) else {}
            output = step.get("output_contract") if isinstance(step.get("output_contract"), dict) else {}
            current_required = _text_list(acceptance.get("required_artifacts"))
            canonical_required = canonical_artifact_requirements(
                current_required, output.get("artifacts"), ensure_step_result=bool(
                    current_required or output.get("artifacts")))
            if canonical_required != current_required:
                if current_required:
                    acceptance["artifact_labels"] = current_required
                acceptance["required_artifacts"] = canonical_required
                step["acceptance"] = acceptance
                changed.append(str(step.get("id") or "") + ":artifact_contract")
                contract_rewrites = True
        contract = step.get("input_contract") if isinstance(step.get("input_contract"), dict) else {}
        title = str(step.get("title") or "").casefold()
        delegation = step.get("delegation") if isinstance(step.get("delegation"), dict) else {}
        is_legacy_map = bool(delegation and contract.get("source_refs") and
                             ("ограниченную партию" in title or "bounded batch" in title))
        if is_legacy_map and contract.get("scope") != "map_partition" and \
                step.get("status") != "succeeded":
            parent = by_id.get(str(delegation.get("parent_step_id") or "")) or {}
            parent_goal = str(parent.get("purpose") or parent.get("title") or "исходный бизнес-шаг")
            contract.update({
                "scope": "map_partition", "allow_partition": False,
                "parent_step_id": str(parent.get("id") or delegation.get("parent_step_id") or ""),
                "parent_business_goal": parent_goal,
            })
            step["input_contract"] = contract
            step["execution_role"] = "data"
            step["purpose"] = (
                "Прочитать только объявленную партию, доказанно извлечь её факты и нормализовать "
                "их в канонический промежуточный результат. Не выполнять глобальное сопоставление, "
                "не делать итоговый бизнес-вывод и не требовать файлы соседних партий.")
            step["output_contract"] = {
                "artifacts": ["source_facts.json"], "data_schema": {"type": "object"},
                "postconditions": ["локальные факты содержат provenance", "нет глобального вывода"],
            }
            step["acceptance"] = {
                "required_artifacts": ["step_result_json", "source_facts.json"],
                "deterministic_checks": ["обработаны все и только source_refs этой партии"],
                "semantic_criteria": [], "minimum_confidence": 0.7,
            }
            question = str(((step.get("human_gate") or {}).get("question") or "")).casefold()
            asks_for_sibling = any(marker in question for marker in (
                "предостав", "прилож", "загруз", "отсутствует файл", "не хватает файл",
                "оба файла", "два excel", "provide", "attach", "upload", "missing file"))
            if step.get("status") == "blocked_human" and asks_for_sibling:
                step["status"] = "pending"
                step["human_gate"] = None
                step["error"] = None
                step["status_reason"] = "ложный запрос соседнего входа снят миграцией контракта"
                unblocked.append(str(step.get("id") or ""))
            changed.append(str(step.get("id") or "") + ":map_partition")
            contract_rewrites = True
            role_only = False
        if role_only and step.get("status") == "succeeded":
            # The execution role did not alter the code, input, output or acceptance proof.
            rehash_checkpoint(step)
        if changed:
            step["updated_at"] = now_iso()

    for step in steps:
        deps = [by_id.get(str(x)) for x in (step.get("dependencies") or [])]
        is_legacy_merge = bool(deps and all(
            dep and str((dep.get("input_contract") or {}).get("scope") or "") == "map_partition"
            for dep in deps))
        if is_legacy_merge and str((step.get("input_contract") or {}).get("scope") or "") != "map_merge":
            step.setdefault("input_contract", {})["scope"] = "map_merge"
            step["input_contract"]["allow_partition"] = False
            step["execution_role"] = "integration"
            if step.get("status") == "succeeded":
                rehash_checkpoint(step)
            step["updated_at"] = now_iso()
            changed.append(str(step.get("id") or "") + ":map_merge")
            contract_rewrites = True

    if changed:
        if not contract_rewrites:
            # A role-only schema migration must not make every downstream accepted checkpoint look
            # stale merely because its predecessor checkpoint was rehashed with the new field.
            for step in steps:
                if step.get("status") != "succeeded":
                    continue
                checkpoint = step.get("checkpoint") if isinstance(step.get("checkpoint"), dict) else {}
                if checkpoint.get("schema") != "upc-checkpoint/1.0":
                    continue
                checkpoint["dependency_checkpoint_sha256"] = [
                    str(((by_id.get(str(dep)) or {}).get("checkpoint") or {}).get("checkpoint_sha256") or "")
                    for dep in (step.get("dependencies") or [])]
                rehash_checkpoint(step)
        graph["version"] = int(graph.get("version") or 1) + 1
        graph["parent_version"] = graph["version"] - 1
        graph["updated_at"] = now_iso()
        refresh_ready(graph)
        valid = validate_process(graph)
        if not valid["ok"]:
            raise ValueError("invalid migrated UPC graph: " + "; ".join(valid["errors"]))
    return {"changed": changed, "unblocked": unblocked, "graph_version": graph.get("version")}


def _words(value):
    text = _flatten_text(value, limit=20000) if "_flatten_text" in globals() else str(value).casefold()
    return {x for x in re.findall(r"[a-zа-яё0-9_]{3,}", text) if x not in {
        "для", "или", "как", "the", "and", "with", "from", "this", "that", "step", "process",
        "input", "output", "data", "файл", "файлы", "шаг", "процесс",
    }}


def assess_reuse_compatibility(step, capability):
    """Fail-closed semantic gate before a supposedly reusable expert spends run budget.

    Exact name equality proves identity, not suitability.  The candidate must also describe the
    requested purpose/I/O and must not require permissions which the step did not declare.
    """
    step = step if isinstance(step, dict) else {}
    capability = capability if isinstance(capability, dict) else {}
    implementation = step.get("implementation") if isinstance(step.get("implementation"), dict) else {}
    requested = str(implementation.get("expert_ref") or implementation.get("capability_ref") or "")
    names = {str(capability.get(k) or "") for k in (
        "name", "id", "expert", "expert_name", "capability_id", "capability_ref",
    )}
    names.discard("")
    identity_ok = bool(requested and requested in names)
    if not capability:
        return {"ok": False, "score": 0.0, "failure_class": "capability_mismatch",
                "reasons": ["описание reusable-способности отсутствует"], "requested": requested}

    need = _words({"purpose": step.get("purpose"), "input": step.get("input_contract"),
                   "output": step.get("output_contract"), "acceptance": step.get("acceptance")})
    offers = _words(capability)
    overlap = sorted(need & offers)
    semantic_score = len(overlap) / max(1, min(12, len(need)))
    explicit_caps = {str(x) for x in (capability.get("capabilities") or []) if str(x)}
    requested_cap = str(implementation.get("capability_ref") or "")
    if requested_cap and requested_cap in explicit_caps:
        semantic_score = max(semantic_score, 0.75)

    required_permissions = capability.get("permissions") if isinstance(capability.get("permissions"), dict) else {}
    declared_permissions = step.get("permissions") if isinstance(step.get("permissions"), dict) else {}
    missing_permissions = sorted(kind for kind in DANGEROUS_PERMISSIONS
                                 if required_permissions.get(kind) and not declared_permissions.get(kind))
    reasons = []
    if not identity_ok:
        reasons.append("ссылка шага не совпадает с id/name найденной способности")
    if semantic_score < 0.18:
        reasons.append("назначение и I/O шага не подтверждены описанием способности")
    if missing_permissions:
        reasons.append("не объявлены полномочия: " + ", ".join(missing_permissions))
    return {"ok": not reasons, "score": round(semantic_score, 3),
            "failure_class": "" if not reasons else "capability_mismatch",
            "reasons": reasons, "requested": requested, "matched_terms": overlap[:20]}


def adaptive_failure_decision(observation):
    """Classify an observed failure and select the next bounded action.

    This is deliberately deterministic.  Qwen may propose code or a semantic verdict, but it may
    not silently choose to repeat the same strategy, consume more budget, or ask a person to fix a
    technical failure.
    """
    observation = observation if isinstance(observation, dict) else {}
    text = _flatten_text({
        "issues": observation.get("issues"), "message": observation.get("message"),
        "status": observation.get("status"), "error": observation.get("error"),
    }, limit=40000)
    prior = [str(x) for x in (observation.get("previous_classes") or [])]
    input_count = max(0, int(observation.get("input_count") or 0))
    formats = {str(x).casefold() for x in (observation.get("input_formats") or []) if str(x)}
    repeated_code = bool(observation.get("code_seen"))
    repeated_strategy = bool(observation.get("strategy_seen"))
    owner_question = _clip(observation.get("owner_question") or "", 1200)
    semantic_ambiguity = bool(observation.get("semantic_ambiguity") and owner_question)
    can_split = bool(observation.get("can_split", input_count > 1 or len(formats) > 1))

    def verdict(failure_class, action, evidence, confidence=0.95):
        if failure_class not in FAILURE_CLASSES or action not in CONTROL_ACTIONS:
            raise ValueError("invalid failure controller verdict")
        # A human is only a semantic oracle, never a substitute for retry/repair/decomposition.
        question = owner_question if action == "ask_owner" and failure_class == "semantic_ambiguity" else ""
        return {"failure_class": failure_class, "action": action, "confidence": confidence,
                "duration_seconds": round(float(observation.get("duration_seconds") or 0), 1),
                "evidence": [_clip(x, 500) for x in evidence if str(x).strip()],
                "owner_question": question}

    if observation.get("reuse_compatible") is False:
        return verdict("capability_mismatch", "replace_implementation",
                       observation.get("reuse_reasons") or ["reusable expert is not compatible"])
    if semantic_ambiguity:
        return verdict("semantic_ambiguity", "ask_owner", ["решение зависит от отсутствующего бизнес-правила"])
    if repeated_code or repeated_strategy:
        action = "split_step" if can_split else "replace_implementation"
        return verdict("no_progress", action,
                       ["повторён уже испытанный code hash" if repeated_code else
                        "повторена уже испытанная стратегия без нового доказательства"])

    missing_markers = ("missing required artifact", "required artifact is missing", "обязательн", "artifact")
    if any(x in text for x in missing_markers) and any(x in text for x in ("missing", "отсутств", "empty", "пуст")):
        return verdict("output_contract_missing", "repair_output_contract",
                       ["исполнение завершилось, но обязательный выходной артефакт не доказан"])

    timed_out = any(x in text for x in ("timed out", "timeout", "time out", "превышено время", "таймаут"))
    duration = float(observation.get("duration_seconds") or 0)
    timeout_limit = float(observation.get("run_timeout_seconds") or 0)
    repeated_timeout = prior.count("workload_timeout") + prior.count("transient_transport") > 0
    if timed_out:
        workload = (input_count >= 6 or len(formats) > 1 or repeated_timeout or
                    (timeout_limit > 0 and duration >= timeout_limit * 0.8))
        if workload:
            return verdict("workload_timeout", "split_step" if can_split else "replace_implementation",
                           ["таймаут коррелирует с объёмом/смешанными входами или уже повторялся"])
        return verdict("transient_transport", "retry_transient",
                       ["первый единичный таймаут без признаков перегруженного шага"], 0.8)

    if any(x in text for x in ("connection", "network", "read operation timed out", "http 429", "http 502",
                               "http 503", "temporarily unavailable", "eoferror")):
        if "transient_transport" in prior:
            return verdict("transient_transport", "replace_implementation",
                           ["транспортный сбой повторился после одного разрешённого повтора"])
        return verdict("transient_transport", "retry_transient", ["временный транспортный сбой"], 0.85)

    semantic_failed = bool(observation.get("semantic_failed")) or "semantic acceptance failed" in text
    if semantic_failed:
        return verdict("semantic_acceptance_failed", "repair_code",
                       ["бизнес-приёмка отвергла фактический результат; вопрос владельцу не сформирован"])
    if any(x in text for x in ("traceback", "nameerror", "typeerror", "valueerror", "syntaxerror",
                               "execution error", "expert_error", "exception", "contract_violation")):
        return verdict("implementation_defect", "repair_code", ["наблюдается дефект реализации"])
    return verdict("implementation_defect", "repair_code",
                   ["результат не прошёл проверку и не относится к более узкому классу"], 0.65)


def atomic_write_json(path, value):
    """Crash-safe sidecar write. Session mutation itself remains the bridge's responsibility."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, default=str)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    finally:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass


def append_event(path, event):
    """Append a compact journal entry after the matching state checkpoint was saved."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    item = dict(event or {})
    item.setdefault("at", now_iso())
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(item, ensure_ascii=False, sort_keys=True, default=str) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def default_budgets():
    return {
        "max_steps": 40,
        "max_dynamic_steps": 20,
        "max_depth": 5,
        "max_total_attempts": 80,
        "max_step_attempts": 4,
        "max_wall_seconds": 14400,
        "max_llm_calls": 120,
        "max_total_tokens": 2000000,
        "max_cost_usd": 20.0,
        "estimated_cost_per_1k_tokens_usd": 0.01,
        "max_generated_experts": 40,
    }


def empty_permissions():
    return {kind: [] for kind in PERMISSION_KINDS}


def _text_list(value, limit=40):
    if isinstance(value, list):
        return [_clip(v, 500) for v in value[:limit] if str(v or "").strip()]
    if value is None or value == "":
        return []
    return [_clip(value, 500)]


def canonical_artifact_requirements(required=None, outputs=None, ensure_step_result=False):
    """Translate planner prose into identifiers the runtime can actually verify.

    Qwen sometimes describes an output artifact with a sentence (for example
    ``"structured data ready for validation"``).  Treating that sentence as a filename makes a
    perfectly good expert enter an expensive repair loop.  Every such logical/non-file output lives
    in the canonical ``step_result.json`` envelope.  Concrete requested documents remain separate
    artifacts and are still checked fail-closed.
    """
    raw = _text_list(required)
    if not raw:
        raw = _text_list(outputs)
    out = ["step_result_json"] if ensure_step_result or raw else []
    known = {
        "step_result": "step_result_json", "step_result.json": "step_result_json",
        "step_result_json": "step_result_json", "result_json": "step_result_json",
        "report.md": "report_md", "report_md": "report_md",
        "report.xlsx": "report_xlsx", "report_xlsx": "report_xlsx",
        "report.pdf": "report_pdf", "report_pdf": "report_pdf",
        "report.docx": "report_docx", "report_docx": "report_docx",
        "report.pptx": "report_pptx", "report_pptx": "report_pptx",
    }
    document_suffixes = {
        ".json": "json", ".csv": "csv", ".xlsx": "xlsx", ".xls": "xls",
        ".pdf": "pdf", ".docx": "docx", ".pptx": "pptx", ".md": "md",
        ".html": "html", ".txt": "txt",
    }
    for value in raw:
        text = str(value or "").strip()
        folded = text.casefold().replace("-", "_").replace(" ", "_")
        token = known.get(folded)
        if not token:
            # A concrete filename is an enforceable artifact. Keep the basename so generated
            # output directories may differ between attempts and devices.
            suffix = Path(text).suffix.casefold()
            if suffix in document_suffixes:
                token = Path(text).name
            elif any(word in text.casefold() for word in ("отчёт", "отчет", "report")):
                token = "report_md"
            elif re.fullmatch(r"[a-z][a-z0-9_]{2,80}", folded) and any(
                    marker in folded for marker in ("artifact", "manifest", "report", "result", "facts")):
                token = folded
            else:
                # Tables, records, facts, classifications and other logical outputs are fields of
                # step_result.json, not imaginary files named after the planner's prose.
                token = "step_result_json"
        if token not in out:
            out.append(token)
    return out


def _legacy_permissions(stage, task):
    """Conservative migration only; the new planner must provide explicit permissions."""
    text = " ".join(str((task or {}).get(k) or (stage or {}).get(k) or "")
                    for k in ("title", "purpose", "business_description")).casefold()
    out = empty_permissions()
    out["read"] = ["declared_inputs"]
    out["create"] = ["run_output_dir"]
    markers = {
        "move": ("перемест", "move ", "relocate"),
        "modify": ("измен", "редакт", "modify", "update file"),
        "delete": ("удал", "очист", "delete", "cleanup"),
        "install": ("установ", "install", "brew ", "pip "),
        "send": ("telegram", "email", "письм", "отправ", "send ", "slack"),
        "external_write": ("записать в", "создать в crm", "publish", "опубликов"),
    }
    for kind, words in markers.items():
        if any(word in text for word in words):
            out[kind] = ["requires_runtime_target"]
    return out


def _implementation(stage, task):
    stage = stage or {}
    task = task or {}
    requested = str(task.get("implementation_mode") or stage.get("implementation_mode") or "").lower()
    action = str(task.get("action") or "").lower()
    ref = task.get("reuse_of") or task.get("capability_ref") or stage.get("capability_ref")
    known_caps = [str(x) for x in (stage.get("capability_ids") or []) if str(x)]
    known_assets = [str(x) for x in (stage.get("asset_names") or []) if str(x)]
    if requested not in IMPLEMENTATION_MODES:
        if action in ("reuse", "parameterize") and ref:
            requested = "reuse"
        elif ref or known_caps or known_assets:
            requested = "reuse"
        else:
            requested = "generate"
    if requested == "reuse" and not ref:
        ref = (known_assets or known_caps or [None])[0]
    return {
        "mode": requested,
        "capability_ref": ref,
        "expert_ref": (task.get("expert_ref") or task.get("expert_name") or
                       stage.get("expert_ref") or stage.get("expert_name")) if requested == "reuse" else None,
        "subgraph_ref": task.get("subgraph_ref"),
        "why": _clip(task.get("implementation_why") or stage.get("implementation_why") or
                     ("matched inventory" if requested == "reuse" else "build from the step contract"), 500),
    }


def make_step(stage, task=None, index=1):
    stage = stage if isinstance(stage, dict) else {}
    task = task if isinstance(task, dict) else {}
    stamp = now_iso()
    sid = _safe_id(task.get("id") or stage.get("id") or ("s%03d" % index))
    title = str(task.get("title") or stage.get("title") or task.get("purpose") or
                stage.get("business_description") or ("Шаг %d" % index)).strip()
    purpose = str(task.get("purpose") or stage.get("business_description") or title).strip()
    acceptance = task.get("acceptance") if isinstance(task.get("acceptance"), dict) else {}
    legacy_accept = task.get("acceptance_test") if isinstance(task.get("acceptance_test"), dict) else {}
    deterministic = acceptance.get("deterministic_checks") or task.get("deterministic_checks") or []
    semantic = acceptance.get("semantic_criteria") or task.get("semantic_criteria") or []
    if not semantic and legacy_accept.get("expected"):
        semantic = [legacy_accept.get("expected")]
    if not semantic and legacy_accept.get("description"):
        semantic = [legacy_accept.get("description")]
    permissions = task.get("permissions") if isinstance(task.get("permissions"), dict) else None
    permissions = permissions or (stage.get("permissions") if isinstance(stage.get("permissions"), dict) else None)
    if not permissions:
        permissions = _legacy_permissions(stage, task)
    permissions = {kind: _text_list(permissions.get(kind)) for kind in PERMISSION_KINDS}
    retry = task.get("retry_policy") if isinstance(task.get("retry_policy"), dict) else {}
    output_contract = task.get("output_contract") or {
        "artifacts": _text_list(stage.get("outputs") or stage.get("output")),
        "data_schema": {}, "postconditions": _text_list(stage.get("postconditions"))}
    required_artifacts = canonical_artifact_requirements(
        acceptance.get("required_artifacts") or task.get("required_artifacts"),
        (output_contract.get("artifacts") if isinstance(output_contract, dict) else []))
    return {
        "id": sid,
        "title": _clip(title, 240),
        "purpose": _clip(purpose, 1000),
        "execution_role": infer_execution_role(stage, task),
        "dependencies": [_safe_id(x) for x in (task.get("depends_on") or stage.get("depends_on") or [])],
        "input_contract": task.get("input_contract") or {
            "artifacts": _text_list(stage.get("inputs")), "data_schema": {}, "required": True},
        "output_contract": output_contract,
        "implementation": _implementation(stage, task),
        "permissions": permissions,
        "acceptance": {
            "deterministic_checks": deterministic if isinstance(deterministic, list) else [],
            "semantic_criteria": _text_list(semantic),
            "required_artifacts": required_artifacts,
            "minimum_confidence": acceptance.get("minimum_confidence", 0.7),
        },
        "retry_policy": {
            "max_attempts": max(1, min(int(retry.get("max_attempts") or 4), 10)),
            "backoff_seconds": retry.get("backoff_seconds") or [0, 2, 5, 15],
            "repair_on": retry.get("repair_on") or
                         ["expert_error", "contract_violation", "acceptance_failed"],
            "human_on": retry.get("human_on") or
                        ["permission_required", "ambiguous_owner_decision"],
        },
        "status": "pending",
        "attempts": [],
        "version": 1,
        "output": None,
        "evidence": [],
        "error": None,
        "memory_refs": [],
        "created_at": stamp,
        "started_at": None,
        "finished_at": None,
        "updated_at": stamp,
    }


def process_from_blueprint(session_id, blueprint, build_plan=None, origin="wizard", process_id=""):
    """Migrate both legacy and UPC-aware plans into one deterministic graph."""
    blueprint = blueprint if isinstance(blueprint, dict) else {}
    build_plan = build_plan if isinstance(build_plan, dict) else {}
    raw_stages = [x for x in (blueprint.get("stages") or []) if isinstance(x, dict)]
    raw_tasks = [x for x in (build_plan.get("tasks") or []) if isinstance(x, dict)]
    by_stage = {str(x.get("id")): x for x in raw_stages if x.get("id")}
    steps = []
    if raw_tasks:
        for index, task in enumerate(raw_tasks, 1):
            stage = by_stage.get(str(task.get("stage_id"))) or {}
            steps.append(make_step(stage, task, index))
    else:
        for index, stage in enumerate(raw_stages, 1):
            steps.append(make_step(stage, None, index))
    if not steps:
        # A useful unknown task is still a valid generative process, never an empty/catalog error.
        request = blueprint.get("goal") or blueprint.get("process_name") or "Выполнить задачу пользователя"
        steps = [make_step({"id": "s001", "title": request, "business_description": request,
                            "implementation_mode": "generate"}, None, 1)]

    # Build-plan ids may have been sanitized; dependencies need the same mapping.
    raw_nodes = raw_tasks or raw_stages
    source_ids = [str((raw_nodes[i].get("id") if i < len(raw_nodes) else steps[i].get("id")) or
                      "s%03d" % (i + 1)) for i in range(len(steps))]
    id_map = {source_ids[i]: steps[i]["id"] for i in range(len(steps))}
    for index, step in enumerate(steps):
        raw = raw_nodes[index] if index < len(raw_nodes) else {}
        step["dependencies"] = [id_map.get(str(dep), _safe_id(dep)) for dep in (raw.get("depends_on") or [])]
    ids = {x["id"] for x in steps}
    edges = [{"from": dep, "to": step["id"], "condition": "succeeded"}
             for step in steps for dep in step["dependencies"] if dep in ids]
    children = {sid: [] for sid in ids}
    for edge in edges:
        children[edge["from"]].append(edge["to"])
    entry = [x["id"] for x in steps if not x["dependencies"]]
    terminal = [x["id"] for x in steps if not children[x["id"]]]
    stamp = now_iso()
    seed = {"session_id": session_id, "goal": blueprint.get("goal"), "steps": [x["id"] for x in steps]}
    graph = {
        "schema": SCHEMA,
        "process_id": process_id or ("proc_" + _stable_hash(seed)[:14]),
        "session_id": str(session_id or ""),
        "origin": str(origin or "wizard"),
        "version": 1,
        "parent_version": None,
        "title": str(blueprint.get("process_name") or build_plan.get("process_name") or "Процесс")[:240],
        "goal": str(blueprint.get("goal") or "")[:2000],
        "task_contract_ref": {},
        "source_model_ref": {},
        "entry_step_ids": entry,
        "terminal_step_ids": terminal,
        "steps": steps,
        "edges": edges,
        "budgets": default_budgets(),
        "permissions": empty_permissions(),
        "approvals": [],
        "memory_policy": {"promote_only_after_acceptance": True, "max_entries": 500},
        "memory": [],
        "run": {"run_id": "", "status": "pending", "attempts_used": 0,
                "llm_calls_used": 0, "tokens_used": 0, "estimated_cost_usd": 0.0,
                "generated_experts_used": 0, "dynamic_steps_used": 0,
                "started_at": None, "finished_at": None, "updated_at": stamp},
        "created_at": stamp,
        "updated_at": stamp,
    }
    valid = validate_process(graph)
    if not valid["ok"]:
        raise ValueError("invalid UPC graph: " + "; ".join(valid["errors"]))
    refresh_ready(graph)
    return graph


def project_runtime_contract(graph=None, session=None, build=None, blueprint=None, recipients=None):
    """Project the user-facing runtime I/O from the canonical process facts.

    The cabinet used to infer ``no connector == uploaded file`` and rendered delivery/finance
    controls for every process.  That is false for local folders, device applications, pure
    reasoning tasks and processes without delivery.  Keep this projection deterministic so all
    surfaces can show the same facts without asking an LLM or inventing business fields.
    """
    graph = graph if isinstance(graph, dict) else {}
    session = session if isinstance(session, dict) else {}
    build = build if isinstance(build, dict) else {}
    blueprint = blueprint if isinstance(blueprint, dict) else {}
    steps = [x for x in (graph.get("steps") or []) if isinstance(x, dict)]
    by_id = {str(x.get("id") or ""): x for x in steps}
    entry = [by_id[x] for x in (graph.get("entry_step_ids") or []) if x in by_id]
    terminal = [by_id[x] for x in (graph.get("terminal_step_ids") or []) if x in by_id]
    if not entry:
        entry = [x for x in steps if not x.get("dependencies")]
    if not terminal:
        depended_on = {str(d) for x in steps for d in (x.get("dependencies") or [])}
        terminal = [x for x in steps if str(x.get("id") or "") not in depended_on]

    task_contract = build.get("task_contract") if isinstance(build.get("task_contract"), dict) else {}
    operation = task_contract.get("operation") if isinstance(task_contract.get("operation"), dict) else {}
    source = session.get("source") if isinstance(session.get("source"), dict) else {}
    inbound = session.get("inbound") if isinstance(session.get("inbound"), dict) else {}

    context = {
        "goal": blueprint.get("goal") or session.get("goal") or session.get("questionnaire_task"),
        "answers": session.get("answers"),
        # Permissions contain technical keys such as ``read``.  They are intentionally excluded
        # from input classification: otherwise "create a PDF" + the default read permission looks
        # like "read/upload a PDF" and invents a manual file input.
        "entry_steps": [{k: x.get(k) for k in ("title", "purpose", "input_contract")}
                        for x in entry],
        "source_configuration": task_contract.get("source_configuration"),
    }
    try:
        raw_input_text = json.dumps(context, ensure_ascii=False, default=str)[:30000]
    except Exception:
        raw_input_text = str(context)[:30000]
    input_text = raw_input_text.casefold()

    def _real_source(path):
        value = str(path or "").strip()
        folded = value.replace("\\", "/").casefold()
        if not value or "/build_fixture/" in folded:
            return ""
        if Path(value).name.casefold() in ("task_input.json", "synthetic_input.json"):
            return ""
        return value

    source_files = []
    for raw in list(build.get("source_files") or []) + [build.get("source_file")]:
        value = _real_source(raw)
        if value and value not in source_files:
            source_files.append(value)

    if source.get("kind"):
        runtime_input = {"kind": "connector", "label": str(source.get("label") or source.get("kind")),
                         "manual_upload": False, "connectable": True}
    elif inbound and str(inbound.get("mode") or "off") != "off":
        channel = str(inbound.get("channel") or "").strip()
        runtime_input = {"kind": "inbound", "label": "Входящие сообщения" +
                         ((" · " + channel) if channel else ""),
                         "manual_upload": False, "connectable": True}
    else:
        # Prefer an explicit path.  A folder/path in the task is a live local resource, not the
        # synthetic task_input.json fixture created solely to test a non-file process.
        path_match = re.search(r"(?:~|/(?:Users|home)/[^\s/]+)/(?:[^\s,;:)\"'{}\[\]]+)", raw_input_text,
                               flags=re.IGNORECASE)
        local_words = ("папк", "директори", "folder", "directory", "downloads", "рабочем стол")
        has_local_folder = bool(path_match) or any(word in input_text for word in local_words)
        if has_local_folder:
            raw_path = path_match.group(0) if path_match else ""
            if "downloads" in input_text and not raw_path:
                raw_path = "~/Downloads"
            label = ("Папка %s на этом устройстве" % raw_path) if raw_path else "Локальная папка на этом устройстве"
            runtime_input = {"kind": "local_folder", "label": label,
                             "path": raw_path, "manual_upload": False, "connectable": False}
        elif source_files:
            runtime_input = {"kind": "manual_file", "label": "Файл при запуске",
                             "manual_upload": True, "connectable": True}
        else:
            entry_input_text = _flatten_text([x.get("input_contract") for x in entry])
            file_kinds = ("xlsx", "excel", "csv", "pdf", "документ", "файл", "изображен", "видео")
            input_actions = ("прилож", "загруз", "на вход", "прочит", "обработ", "анализ", "parse", "read", "upload")
            looks_like_file_input = (any(word in entry_input_text for word in file_kinds) or
                                     (any(word in input_text for word in file_kinds) and
                                      any(word in input_text for word in input_actions)))
            if looks_like_file_input:
                runtime_input = {"kind": "manual_file", "label": "Файл при запуске",
                                 "manual_upload": True, "connectable": True}
            else:
                runtime_input = {"kind": "none", "label": "Отдельный источник не требуется",
                                 "manual_upload": False, "connectable": False}

    resolved_recipients = [str(x) for x in (recipients if isinstance(recipients, list)
                                             else (session.get("recipients") or [])) if str(x)]
    send_targets = []
    for step in steps:
        permissions = step.get("permissions") if isinstance(step.get("permissions"), dict) else {}
        send_targets.extend(str(x) for x in (permissions.get("send") or []) if str(x))
    explicit_delivery = operation.get("delivery")
    if isinstance(explicit_delivery, dict):
        explicit_delivery_enabled = str(explicit_delivery.get("mode") or "").casefold() not in ("", "off", "none") or bool(
            explicit_delivery.get("channel") or explicit_delivery.get("target"))
    else:
        explicit_delivery_enabled = str(explicit_delivery or "").strip().casefold() not in ("", "off", "none", "нет", "no")
    delivery_enabled = bool(resolved_recipients or send_targets or explicit_delivery_enabled)

    output_context = {
        "terminal_steps": [{k: x.get(k) for k in ("title", "purpose", "output_contract")}
                           for x in terminal],
        "delivery": explicit_delivery,
    }
    output_text = _flatten_text(output_context)
    supports_sum = any(word in output_text for word in
                       ("total_sum", "amount", "currency", "сумм", "стоимост", "тенге", "₸"))
    supports_count = any(word in output_text for word in
                         ("total_count", "count", "колич", "число", "сколько", "items", "records", "files"))
    return {
        "schema": "runtime-cabinet/1.0",
        "input": runtime_input,
        "delivery": {"enabled": delivery_enabled,
                     "channels": resolved_recipients or send_targets},
        "output": {"supports_count": supports_count, "supports_sum": supports_sum},
    }


def validate_process(graph):
    errors = []
    if not isinstance(graph, dict) or graph.get("schema") != SCHEMA:
        return {"ok": False, "errors": ["schema must be " + SCHEMA]}
    steps = graph.get("steps") if isinstance(graph.get("steps"), list) else []
    max_steps = int((graph.get("budgets") or {}).get("max_steps") or 40)
    if not steps:
        errors.append("process has no steps")
    if len(steps) > max_steps:
        errors.append("static step budget exceeded: %d > %d" % (len(steps), max_steps))
    ids = [str(x.get("id") or "") for x in steps if isinstance(x, dict)]
    if len(ids) != len(steps) or any(not sid for sid in ids):
        errors.append("every step needs id")
    if len(set(ids)) != len(ids):
        errors.append("duplicate step ids")
    known = set(ids)
    deps = {}
    for step in steps:
        if not isinstance(step, dict):
            continue
        sid = str(step.get("id") or "")
        status = str(step.get("status") or "")
        mode = str((step.get("implementation") or {}).get("mode") or "")
        if status not in STEP_STATUSES:
            errors.append("%s: invalid status %s" % (sid, status))
        if mode not in IMPLEMENTATION_MODES:
            errors.append("%s: invalid implementation mode %s" % (sid, mode))
        ds = [str(x) for x in (step.get("dependencies") or [])]
        unknown = [x for x in ds if x not in known]
        if unknown:
            errors.append("%s: unknown dependencies %s" % (sid, unknown))
        if sid in ds:
            errors.append("%s: self dependency" % sid)
        deps[sid] = ds
    visiting, visited = set(), set()

    def walk(sid):
        if sid in visiting:
            return False
        if sid in visited:
            return True
        visiting.add(sid)
        for dep in deps.get(sid, []):
            if not walk(dep):
                return False
        visiting.remove(sid)
        visited.add(sid)
        return True

    for sid in ids:
        if not walk(sid):
            errors.append("cycle detected at " + sid)
            break
    return {"ok": not errors, "errors": errors}


def step_map(graph):
    return {str(x.get("id")): x for x in (graph.get("steps") or []) if isinstance(x, dict)}


def refresh_ready(graph):
    """Only dependency state controls readiness; accepted steps never replay implicitly."""
    by_id = step_map(graph)
    changed = []
    for step in graph.get("steps") or []:
        if step.get("status") not in ("pending", "stale"):
            continue
        deps = [by_id.get(x) for x in step.get("dependencies") or []]
        if all(dep and dep.get("status") in ("succeeded", "skipped") for dep in deps):
            step["status"] = "ready"
            step["updated_at"] = now_iso()
            changed.append(step["id"])
    graph["updated_at"] = now_iso()
    return changed


def ready_steps(graph):
    refresh_ready(graph)
    return [x for x in (graph.get("steps") or []) if x.get("status") == "ready"]


def budget_preflight(graph, reserve=None, at=None):
    """Fail closed before allocating another step; all counters survive checkpoints/restarts."""
    budgets = graph.get("budgets") if isinstance(graph.get("budgets"), dict) else {}
    run = graph.get("run") if isinstance(graph.get("run"), dict) else {}
    reserve = reserve if isinstance(reserve, dict) else {}
    checks = (
        ("attempts", "attempts_used", "max_total_attempts", int),
        ("llm_calls", "llm_calls_used", "max_llm_calls", int),
        ("tokens", "tokens_used", "max_total_tokens", int),
        ("cost_usd", "estimated_cost_usd", "max_cost_usd", float),
        ("generated_experts", "generated_experts_used", "max_generated_experts", int),
    )
    exceeded = []
    remaining = {}
    for public, used_key, max_key, cast in checks:
        limit = cast(budgets.get(max_key) or 0)
        used = cast(run.get(used_key) or 0)
        extra = cast(reserve.get(public) or 0)
        remaining[public] = max(0, limit - used) if limit > 0 else None
        if limit > 0 and used + extra > limit:
            exceeded.append({"resource": public, "used": used, "reserve": extra, "limit": limit})
    wall_limit = int(budgets.get("max_wall_seconds") or 0)
    started = str(run.get("started_at") or "")
    wall_used = 0.0
    if started:
        try:
            current = at if isinstance(at, datetime) else datetime.now(timezone.utc)
            parsed = datetime.fromisoformat(started.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            wall_used = max(0.0, (current - parsed).total_seconds())
        except Exception:
            exceeded.append({"resource": "wall_seconds", "used": "invalid_started_at",
                             "reserve": 0, "limit": wall_limit})
    remaining["wall_seconds"] = max(0.0, wall_limit - wall_used) if wall_limit > 0 else None
    if wall_limit > 0 and wall_used >= wall_limit:
        exceeded.append({"resource": "wall_seconds", "used": wall_used,
                         "reserve": 0, "limit": wall_limit})
    return {"ok": not exceeded, "code": "" if not exceeded else "budget_exhausted",
            "message": "" if not exceeded else "process budget exhausted: " +
                       ", ".join(str(x["resource"]) for x in exceeded),
            "exceeded": exceeded, "remaining": remaining}


def record_usage(graph, attempts=0, llm_calls=0, tokens=0, cost_usd=None,
                 generated_experts=0, estimated=True):
    """Record provider usage or a conservative estimate; never silently disables the cost ceiling."""
    run = graph.setdefault("run", {})
    budgets = graph.get("budgets") if isinstance(graph.get("budgets"), dict) else {}
    attempts = max(0, int(attempts or 0))
    llm_calls = max(0, int(llm_calls or 0))
    tokens = max(0, int(tokens or 0))
    if cost_usd is None:
        rate = max(0.0, float(budgets.get("estimated_cost_per_1k_tokens_usd") or 0))
        cost_usd = tokens * rate / 1000.0
        estimated = True
    cost_usd = max(0.0, float(cost_usd or 0))
    run["attempts_used"] = int(run.get("attempts_used") or 0) + attempts
    run["llm_calls_used"] = int(run.get("llm_calls_used") or 0) + llm_calls
    run["tokens_used"] = int(run.get("tokens_used") or 0) + tokens
    run["estimated_cost_usd"] = round(float(run.get("estimated_cost_usd") or 0) + cost_usd, 6)
    run["generated_experts_used"] = int(run.get("generated_experts_used") or 0) + max(
        0, int(generated_experts or 0))
    run["usage_estimated"] = bool(run.get("usage_estimated") or estimated)
    run["updated_at"] = now_iso()
    graph["updated_at"] = run["updated_at"]
    return budget_preflight(graph)


def transition_step(graph, step_id, new_status, reason="", extra=None):
    by_id = step_map(graph)
    step = by_id.get(str(step_id))
    if not step:
        raise KeyError("unknown step: " + str(step_id))
    old = str(step.get("status") or "pending")
    if new_status not in TRANSITIONS.get(old, set()):
        raise ValueError("invalid transition %s -> %s for %s" % (old, new_status, step_id))
    stamp = now_iso()
    step["status"] = new_status
    step["updated_at"] = stamp
    if new_status == "running" and not step.get("started_at"):
        step["started_at"] = stamp
    if new_status in TERMINAL_STATUSES:
        step["finished_at"] = stamp
    if reason:
        step["status_reason"] = _clip(reason, 1200)
    if isinstance(extra, dict):
        step.update(copy.deepcopy(extra))
    graph["updated_at"] = stamp
    return {"step_id": step_id, "from": old, "to": new_status, "reason": reason, "at": stamp}


def _flatten_text(value, limit=30000):
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = str(value)
    return text.casefold()[:limit]


def normalize_step_result(raw, step_id, step_version=1, attempt=1, task_id=""):
    """Transport completion is not expert success. Convert every return into a fail-closed result."""
    stamp = now_iso()
    source = raw
    if isinstance(raw, str):
        try:
            source = json.loads(raw)
        except Exception:
            source = {"status": "error", "message": raw}
    if not isinstance(source, dict):
        source = {"status": "error", "message": "non-object expert result: " + _clip(source, 300)}
    nested = source.get("result") if isinstance(source.get("result"), dict) else source
    text = _flatten_text(source)
    marker = next((x for x in ERROR_MARKERS if x in text), "")
    expert_status = str(nested.get("status") or source.get("status") or "").lower()
    if expert_status not in ("success", "error"):
        expert_status = "error"
    error = None
    if marker:
        expert_status = "error"
        error = {"code": "execution_error", "message": "runtime marker: " + marker}
    elif expert_status != "success":
        error = {"code": "expert_error", "message": _clip(
            nested.get("message") or nested.get("error") or source.get("message") or "expert did not return success")}
    artifacts = nested.get("artifacts") if isinstance(nested.get("artifacts"), list) else []
    for key in ("report_md", "report_xlsx", "report_pdf", "report_docx", "report_pptx"):
        if nested.get(key):
            artifacts.append({"path": str(nested[key]), "kind": key})
    result = {
        "schema": STEP_RESULT_SCHEMA,
        "step_id": str(step_id),
        "step_version": int(step_version or 1),
        "attempt": int(attempt or 1),
        "transport": {"status": "completed", "task_id": str(task_id or source.get("task_id") or "")},
        "expert": {"status": expert_status, "expert_ref": nested.get("expert_ref") or "",
                   "message": _clip(nested.get("message") or source.get("message") or "")},
        "output": nested.get("output") if "output" in nested else nested.get("summary"),
        "artifacts": artifacts,
        "evidence": ((nested.get("evidence") or {}).get("acceptance_checks")
                     if isinstance(nested.get("evidence"), dict) else nested.get("evidence")) or [],
        "metrics": nested.get("metrics") or {},
        "error": error,
        "started_at": nested.get("started_at") or stamp,
        "finished_at": nested.get("finished_at") or stamp,
        "raw_sha256": _stable_hash(source),
    }
    return result


def artifact_facts(artifacts):
    out = []
    for raw in artifacts or []:
        item = dict(raw) if isinstance(raw, dict) else {"path": str(raw)}
        path = Path(str(item.get("path") or ""))
        if path.exists() and path.is_file():
            item["bytes"] = path.stat().st_size
            h = hashlib.sha256()
            with path.open("rb") as handle:
                for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                    h.update(chunk)
            item["sha256"] = h.hexdigest()
            item["exists"] = True
        else:
            item["exists"] = False
        out.append(item)
    return out


def verify_step_result(step, result):
    issues = []
    if not isinstance(result, dict) or result.get("schema") != STEP_RESULT_SCHEMA:
        return {"ok": False, "issues": ["invalid StepResult schema"], "artifacts": []}
    if str(result.get("step_id")) != str(step.get("id")):
        issues.append("StepResult belongs to another step")
    if int(result.get("step_version") or 0) != int(step.get("version") or 0):
        issues.append("StepResult belongs to another step version")
    if (result.get("transport") or {}).get("status") != "completed":
        issues.append("transport did not complete")
    if (result.get("expert") or {}).get("status") != "success" or result.get("error"):
        issues.append(_clip((result.get("error") or {}).get("message") or "expert did not succeed", 500))
    artifacts = artifact_facts(result.get("artifacts") or [])
    required = [str(x).casefold() for x in ((step.get("acceptance") or {}).get("required_artifacts") or [])]
    for need in required:
        matches = [x for x in artifacts if need in (str(x.get("kind") or "") + " " +
                                                     str(x.get("path") or "")).casefold()]
        if not matches:
            issues.append("missing required artifact: " + need)
        elif not any(x.get("exists") and int(x.get("bytes") or 0) > 0 for x in matches):
            issues.append("required artifact is missing or empty: " + need)
    checks = result.get("evidence") if isinstance(result.get("evidence"), list) else []
    failed_checks = [x for x in checks if isinstance(x, dict) and x.get("passed") is False]
    if failed_checks:
        issues.append("mandatory acceptance check failed")
    return {"ok": not issues, "issues": issues, "artifacts": artifacts,
            "needs_semantic_judge": bool((step.get("acceptance") or {}).get("semantic_criteria"))}


def approval_hash(step_id, step_version, permission, target, payload):
    return _stable_hash({"step_id": step_id, "step_version": step_version, "permission": permission,
                         "target": target, "payload": payload})


def permission_preflight(graph, step, request):
    request = request if isinstance(request, dict) else {}
    permission = str(request.get("permission") or "")
    target = str(request.get("target") or "")
    if permission not in PERMISSION_KINDS:
        return {"ok": False, "code": "unknown_permission", "message": "unknown permission: " + permission}
    declared = (step.get("permissions") or {}).get(permission) or []
    if not declared:
        return {"ok": False, "code": "permission_not_declared",
                "message": "step did not declare permission " + permission}
    digest = approval_hash(step.get("id"), step.get("version"), permission, target, request.get("payload"))
    approvals = graph.get("approvals") if isinstance(graph.get("approvals"), list) else []
    approved = any(x.get("hash") == digest and x.get("status") == "approved" for x in approvals if isinstance(x, dict))
    if permission in DANGEROUS_PERMISSIONS and not approved:
        return {"ok": False, "code": "approval_required", "approval_hash": digest,
                "message": "human approval required for " + permission,
                "preview": {"step_id": step.get("id"), "step_version": step.get("version"),
                            "permission": permission, "target": target, "payload": request.get("payload")}}
    return {"ok": True, "approval_hash": digest}


def record_approval(graph, step_id, permission, target, payload, approved, by="owner"):
    step = step_map(graph).get(str(step_id))
    if not step:
        raise KeyError("unknown step: " + str(step_id))
    digest = approval_hash(step_id, step.get("version"), permission, target, payload)
    item = {"hash": digest, "step_id": step_id, "step_version": step.get("version"),
            "permission": permission, "target": target, "payload_sha256": _stable_hash(payload),
            "status": "approved" if approved else "rejected", "by": by, "at": now_iso()}
    graph.setdefault("approvals", []).append(item)
    graph["updated_at"] = now_iso()
    return item


def memory_entry(kind, text, status="candidate", scope="step", source=None, evidence_refs=None,
                 confidence=0.5, step_id="", step_version=0, attempt=0, supersedes=None):
    kind = kind if kind in MEMORY_KINDS else "lesson"
    status = status if status in MEMORY_STATUSES else "candidate"
    item = {
        "kind": kind,
        "status": status,
        "text": _clip(text, 1200),
        "scope": scope if scope in ("attempt", "step", "run", "process", "agent", "workspace") else "step",
        "source": source if isinstance(source, dict) else {"type": str(source or "expert"), "ref": ""},
        "evidence_refs": [str(x) for x in (evidence_refs or []) if str(x)],
        "confidence": max(0.0, min(1.0, float(confidence or 0))),
        "step_id": str(step_id or ""),
        "step_version": int(step_version or 0),
        "attempt": int(attempt or 0),
        "supersedes": supersedes,
        "created_at": now_iso(),
    }
    item["id"] = "mem_" + _stable_hash({k: v for k, v in item.items() if k != "created_at"})[:16]
    return item


def add_memory(graph, entries, accepted=False):
    current = {str(x.get("id")): x for x in (graph.get("memory") or []) if isinstance(x, dict)}
    added = []
    for raw in entries or []:
        if not isinstance(raw, dict) or not raw.get("text"):
            continue
        item = copy.deepcopy(raw)
        item.setdefault("id", "mem_" + _stable_hash(item)[:16])
        if item.get("status") == "verified" and not accepted and (item.get("source") or {}).get("type") != "owner":
            item["status"] = "candidate"
        if accepted and item.get("status") == "candidate" and item.get("evidence_refs"):
            item["status"] = "verified"
        current[item["id"]] = item
        added.append(item["id"])
    limit = int((graph.get("memory_policy") or {}).get("max_entries") or 500)
    graph["memory"] = list(current.values())[-limit:]
    graph["updated_at"] = now_iso()
    return added


def _accepted_checkpoint(step, result, validation, semantic, memory_refs):
    provenance = result.get("provenance") if isinstance(result.get("provenance"), dict) else {}
    artifacts = copy.deepcopy(validation.get("artifacts") or [])
    checkpoint = {
        "schema": "upc-checkpoint/1.0",
        "step_id": str(step.get("id") or ""),
        "step_version": int(step.get("version") or 1),
        "step_contract_sha256": step_contract_fingerprint(step),
        "input_sha256": str(provenance.get("input_sha256") or ""),
        "dependency_checkpoint_sha256": [str(x) for x in
                                         (provenance.get("dependency_checkpoint_sha256") or []) if str(x)],
        "expert_ref": str(provenance.get("expert_ref") or
                          (step.get("implementation") or {}).get("expert_ref") or ""),
        "expert_code_sha256": str(provenance.get("expert_code_sha256") or ""),
        "package_sha256": str(provenance.get("package_sha256") or
                              (step.get("implementation") or {}).get("package_sha256") or ""),
        "result_sha256": str(result.get("raw_sha256") or _stable_hash(result)),
        "artifact_facts": artifacts,
        "semantic_verdict": copy.deepcopy(semantic),
        "memory_refs": [str(x) for x in (memory_refs or []) if str(x)],
        "accepted_at": now_iso(),
    }
    checkpoint["checkpoint_sha256"] = _stable_hash({k: v for k, v in checkpoint.items()
                                                     if k not in ("accepted_at", "checkpoint_sha256")})
    return checkpoint


def reconcile_checkpoints(graph, root_input_sha256="", expert_code_hashes=None):
    """Invalidate only accepted steps whose proof no longer matches current inputs/contracts/code.

    Legacy UPC graphs without ``upc-checkpoint/1.0`` remain readable and are not invalidated merely
    because they predate provenance.  New checkpoints are fail-closed and invalidate descendants.
    """
    expert_code_hashes = expert_code_hashes if isinstance(expert_code_hashes, dict) else {}
    by_id = step_map(graph)
    invalidated, reasons = [], {}
    changed = True
    while changed:
        changed = False
        for step in graph.get("steps") or []:
            if step.get("status") != "succeeded" or step.get("id") in invalidated:
                continue
            checkpoint = step.get("checkpoint") if isinstance(step.get("checkpoint"), dict) else {}
            if checkpoint.get("schema") != "upc-checkpoint/1.0":
                continue
            why = []
            if checkpoint.get("step_contract_sha256") != step_contract_fingerprint(step):
                why.append("step contract changed")
            deps = [by_id.get(str(x)) for x in (step.get("dependencies") or [])]
            if any(dep and dep.get("status") == "stale" for dep in deps):
                why.append("dependency checkpoint invalidated")
            expected_dep_hashes = [str((dep.get("checkpoint") or {}).get("checkpoint_sha256") or "")
                                   for dep in deps if dep]
            stored_dep_hashes = [str(x) for x in (checkpoint.get("dependency_checkpoint_sha256") or [])]
            if stored_dep_hashes and stored_dep_hashes != expected_dep_hashes:
                why.append("dependency checkpoint changed")
            if not deps and root_input_sha256 and checkpoint.get("input_sha256") and \
                    str(checkpoint.get("input_sha256")) != str(root_input_sha256):
                why.append("root input changed")
            expert_ref = str(checkpoint.get("expert_ref") or "")
            current_code = str(expert_code_hashes.get(expert_ref) or "")
            if current_code and checkpoint.get("expert_code_sha256") and \
                    current_code != checkpoint.get("expert_code_sha256"):
                why.append("expert code changed")
            for fact in checkpoint.get("artifact_facts") or []:
                path = Path(str((fact or {}).get("path") or "")) if isinstance(fact, dict) else Path("")
                if not path.is_file():
                    why.append("accepted artifact is missing")
                    break
                if fact.get("sha256") and file_sha256(path) != fact.get("sha256"):
                    why.append("accepted artifact changed")
                    break
            if not why:
                continue
            step.setdefault("checkpoint_history", []).append(copy.deepcopy(checkpoint))
            step["status"] = "stale"
            step["status_reason"] = "; ".join(why)
            step["output"] = None
            step["artifact_refs"] = []
            step["evidence"] = []
            step["checkpoint"] = None
            step["updated_at"] = now_iso()
            invalidated.append(step.get("id"))
            reasons[str(step.get("id"))] = why
            changed = True
    if invalidated:
        graph["version"] = int(graph.get("version") or 1) + 1
        graph["parent_version"] = graph["version"] - 1
        graph["updated_at"] = now_iso()
        refresh_ready(graph)
    return {"invalidated": invalidated, "reasons": reasons, "graph_version": graph.get("version")}


def accept_step(graph, step_id, result, semantic_verdict=None, memory=None):
    step = step_map(graph).get(str(step_id))
    if not step:
        raise KeyError("unknown step: " + str(step_id))
    if step.get("status") != "running":
        raise ValueError("accept requires running step")
    validation = verify_step_result(step, result)
    semantic = semantic_verdict if isinstance(semantic_verdict, dict) else {}
    if validation.get("needs_semantic_judge"):
        threshold = float((step.get("acceptance") or {}).get("minimum_confidence") or 0.7)
        if semantic.get("verdict") != "pass" or float(semantic.get("confidence") or 0) < threshold:
            validation["ok"] = False
            validation["issues"].append(
                "semantic acceptance failed: verdict=%s, confidence=%.2f, required=%.2f" %
                (semantic.get("verdict") or "missing", float(semantic.get("confidence") or 0), threshold))
    attempt = {"attempt": len(step.get("attempts") or []) + 1, "step_version": step.get("version"),
               "result": copy.deepcopy(result), "validation": validation,
               "semantic_verdict": copy.deepcopy(semantic), "at": now_iso()}
    step.setdefault("attempts", []).append(attempt)
    if not validation["ok"]:
        step["error"] = {"code": "acceptance_failed", "message": "; ".join(validation["issues"])}
        transition_step(graph, step_id, "repairing", step["error"]["message"])
        lesson = memory_entry("lesson", "Не считать решением: " + step["error"]["message"],
                              status="rejected", scope="step",
                              source={"type": "deterministic_gate", "ref": result.get("raw_sha256", "")},
                              evidence_refs=[result.get("raw_sha256", "")], confidence=1.0,
                              step_id=step_id, step_version=step.get("version"), attempt=attempt["attempt"])
        step["memory_refs"] = add_memory(graph, list(memory or []) + [lesson], accepted=False)
        return {"ok": False, "validation": validation, "event": "repairing"}
    step["output"] = copy.deepcopy(result.get("output"))
    step["artifact_refs"] = copy.deepcopy(validation.get("artifacts") or [])
    step["evidence"] = copy.deepcopy(result.get("evidence") or [])
    step["error"] = None
    step["memory_refs"] = add_memory(graph, memory or [], accepted=True)
    step["checkpoint"] = _accepted_checkpoint(
        step, result, validation, semantic, step.get("memory_refs") or [])
    event = transition_step(graph, step_id, "succeeded", "accepted")
    refresh_ready(graph)
    return {"ok": True, "validation": validation, "event": event}


def descendants(graph, step_id):
    children = {}
    for step in graph.get("steps") or []:
        for dep in step.get("dependencies") or []:
            children.setdefault(dep, []).append(step.get("id"))
    seen, stack = set(), list(children.get(step_id, []))
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, []))
    return seen


def expand_subgraph(graph, parent_step_id, proposed_steps, reason="", delegation_depth=1):
    """Apply a worker proposal only through the orchestrator's bounded graph mutation gate.

    The delegate remains an auditable planning step. New entry steps depend on it, and old
    downstream consumers additionally depend on every terminal child so they cannot race ahead.
    """
    parent = step_map(graph).get(str(parent_step_id))
    if not parent:
        raise KeyError("unknown parent step: " + str(parent_step_id))
    if (parent.get("implementation") or {}).get("mode") != "delegate":
        raise ValueError("only delegate steps may propose a subgraph")
    if parent.get("status") not in ("running", "succeeded"):
        raise ValueError("delegate must be running or succeeded")
    budgets = graph.get("budgets") or {}
    if int(delegation_depth or 0) > int(budgets.get("max_depth") or 5):
        raise ValueError("delegation depth exceeded")
    raw = [x for x in (proposed_steps or []) if isinstance(x, dict)]
    if not raw:
        raise ValueError("empty subgraph proposal")
    dynamic_used = int((graph.get("run") or {}).get("dynamic_steps_used") or 0)
    if dynamic_used + len(raw) > int(budgets.get("max_dynamic_steps") or 20):
        raise ValueError("dynamic step budget exceeded")
    if len(graph.get("steps") or []) + len(raw) > int(budgets.get("max_steps") or 40):
        raise ValueError("total step budget exceeded")

    prefix = _safe_id(parent_step_id) + "_d%d_" % int(delegation_depth or 1)
    local_ids = [str(x.get("id") or "s%03d" % (i + 1)) for i, x in enumerate(raw)]
    id_map = {local_ids[i]: _safe_id(prefix + local_ids[i]) for i in range(len(raw))}
    children = []
    local_dependents = {sid: [] for sid in id_map.values()}
    for index, spec in enumerate(raw, 1):
        local = local_ids[index - 1]
        stage = dict(spec)
        stage["id"] = id_map[local]
        deps = [id_map[str(dep)] for dep in (spec.get("depends_on") or []) if str(dep) in id_map]
        if not deps:
            deps = [str(parent_step_id)]
        stage["depends_on"] = deps
        child = make_step(stage, stage, len(graph.get("steps") or []) + index)
        child["delegation"] = {"parent_step_id": str(parent_step_id),
                               "depth": int(delegation_depth or 1), "reason": _clip(reason, 1000)}
        children.append(child)
        for dep in deps:
            if dep in local_dependents:
                local_dependents[dep].append(child["id"])
    child_ids = {x["id"] for x in children}
    terminals = [sid for sid in child_ids if not local_dependents.get(sid)]
    # Existing consumers of the delegate now also wait for all terminal children.
    for step in graph.get("steps") or []:
        if step.get("id") == parent_step_id or step.get("id") in child_ids:
            continue
        deps = list(step.get("dependencies") or [])
        if str(parent_step_id) in deps:
            step["dependencies"] = list(dict.fromkeys(deps + terminals))
    graph.setdefault("steps", []).extend(children)
    graph["run"]["dynamic_steps_used"] = dynamic_used + len(children)
    parent.setdefault("implementation", {})["subgraph_ref"] = {
        "graph_version": int(graph.get("version") or 1) + 1,
        "step_ids": [x["id"] for x in children], "terminal_step_ids": terminals,
    }
    graph["version"] = int(graph.get("version") or 1) + 1
    graph["parent_version"] = graph["version"] - 1
    graph["edges"] = [{"from": dep, "to": step["id"], "condition": "succeeded"}
                      for step in graph.get("steps") or [] for dep in step.get("dependencies") or []]
    ids = {x["id"] for x in graph.get("steps") or []}
    parents = {edge["from"] for edge in graph["edges"]}
    graph["entry_step_ids"] = [x["id"] for x in graph.get("steps") or [] if not x.get("dependencies")]
    graph["terminal_step_ids"] = sorted(ids - parents)
    graph["updated_at"] = now_iso()
    valid = validate_process(graph)
    if not valid["ok"]:
        raise ValueError("invalid proposed subgraph: " + "; ".join(valid["errors"]))
    refresh_ready(graph)
    return {"parent_step_id": str(parent_step_id), "added": [x["id"] for x in children],
            "terminal_step_ids": terminals, "graph_version": graph["version"]}


def propose_failure_subgraph(parent_step, sources=None, failure=None, max_batch=4):
    """Produce a bounded, domain-neutral decomposition after measured no-progress/timeout.

    The original business contract is moved to the terminal integration/validation child.  Batch
    children receive explicit source refs, preventing the repaired graph from feeding all mixed
    inputs back into the same oversized expert.
    """
    parent_step = parent_step if isinstance(parent_step, dict) else {}
    failure = failure if isinstance(failure, dict) else {}
    if not step_can_partition(parent_step):
        raise ValueError("non-data/runtime step cannot be partitioned by source files")
    normalized = []
    for index, raw in enumerate(sources or [], 1):
        item = dict(raw) if isinstance(raw, dict) else {"path": str(raw)}
        path = Path(str(item.get("path") or item.get("name") or ""))
        item.setdefault("name", path.name or ("input_%03d" % index))
        item.setdefault("format", path.suffix.casefold().lstrip(".") or "unknown")
        item.setdefault("path", str(path))
        normalized.append(item)
    max_batch = max(1, min(int(max_batch or 4), 8))
    original_acceptance = copy.deepcopy(parent_step.get("acceptance") or {})
    original_output = copy.deepcopy(parent_step.get("output_contract") or {})
    permissions = copy.deepcopy(parent_step.get("permissions") or empty_permissions())
    purpose = str(parent_step.get("purpose") or parent_step.get("title") or "Выполнить исходный шаг")

    groups = []
    if len(normalized) > 1:
        by_format = {}
        for item in normalized:
            by_format.setdefault(item.get("format") or "unknown", []).append(item)
        for fmt in sorted(by_format):
            rows = by_format[fmt]
            for offset in range(0, len(rows), max_batch):
                groups.append((fmt, rows[offset:offset + max_batch]))

    if groups:
        proposed = []
        map_ids = []
        for index, (fmt, rows) in enumerate(groups, 1):
            sid = "batch_%03d" % index
            map_ids.append(sid)
            proposed.append({
                "id": sid,
                "title": "Обработать ограниченную партию %d (%s)" % (index, fmt),
                "purpose": ("Прочитать только объявленную партию, доказанно извлечь её факты и "
                            "нормализовать их в канонический промежуточный результат. Не выполнять "
                            "глобальное сопоставление, не делать итоговый бизнес-вывод и не требовать "
                            "файлы соседних партий: их обрабатывает runtime, а объединение выполняет merge-шаг."),
                "execution_role": "data",
                "input_contract": {"required": True, "scope": "map_partition",
                                   "allow_partition": False,
                                   "parent_step_id": str(parent_step.get("id") or ""),
                                   "parent_business_goal": purpose,
                                   "source_refs": [x.get("path") for x in rows],
                                   "source_sha256": [x.get("sha256") for x in rows if x.get("sha256")],
                                   "format": fmt,
                                   "process_source_manifest": [
                                       {k: item.get(k) for k in ("name", "path", "format", "sha256")}
                                       for item in normalized]},
                "output_contract": {"artifacts": ["source_facts.json"],
                                    "data_schema": {"type": "object"},
                                    "postconditions": [
                                        "каждый объявленный вход представлен каноническими фактами",
                                        "результат содержит provenance входа и не содержит глобальных выводов"]},
                "acceptance": {
                    "required_artifacts": ["source_facts.json"],
                    "deterministic_checks": [
                        "обработаны все и только source_refs этой партии",
                        "source_facts.json содержит provenance и структурированные факты"],
                    "semantic_criteria": []},
                "required_artifacts": ["source_facts.json"],
                "semantic_criteria": [],
                "implementation_mode": "generate",
                "permissions": {**empty_permissions(),
                                "read": list((permissions or {}).get("read") or ["declared_inputs"]),
                                "create": ["run_output_dir"]},
            })
        proposed.append({
            "id": "integrate",
            "depends_on": map_ids,
            "title": "Объединить и проверить результат исходного шага",
            "purpose": purpose + " Объединить только принятые результаты партий и проверить полный бизнес-контракт.",
            "execution_role": "integration",
            "input_contract": {"required": True, "scope": "map_merge", "allow_partition": False,
                               "artifacts": ["accepted dependency outputs"],
                               "process_source_manifest": [
                                   {k: item.get(k) for k in ("name", "path", "format", "sha256")}
                                   for item in normalized]},
            "output_contract": original_output,
            "acceptance": original_acceptance,
            "implementation_mode": "generate", "permissions": permissions,
        })
        return proposed

    # A single logical input cannot be split by files.  Split by epistemic role instead: inspect
    # facts, execute from the proved model, then independently validate the original contract.
    source_refs = [x.get("path") for x in normalized]
    return [
        {"id": "inspect", "title": "Доказанно понять вход и ограничения",
         "purpose": "Инспектировать фактическую структуру входа для шага: " + purpose,
         "input_contract": {"required": bool(source_refs), "source_refs": source_refs},
         "output_contract": {"artifacts": ["source_facts.json"], "data_schema": {"type": "object"}},
         "required_artifacts": ["source_facts.json"], "implementation_mode": "generate",
         "permissions": {**empty_permissions(), "read": list((permissions or {}).get("read") or ["declared_inputs"]),
                         "create": ["run_output_dir"]}},
        {"id": "execute", "depends_on": ["inspect"], "title": "Выполнить исходную операцию",
         "purpose": purpose + " Использовать только доказанные факты предыдущего шага.",
         "input_contract": {"required": True, "artifacts": ["source_facts.json"]},
         "output_contract": original_output, "implementation_mode": "generate", "permissions": permissions},
        {"id": "validate", "depends_on": ["execute"], "title": "Независимо принять бизнес-результат",
         "purpose": "Проверить выход исходной операции против её Task Contract, не подменяя исполнение.",
         "input_contract": {"required": True, "artifacts": ["accepted dependency outputs"]},
         "output_contract": original_output, "acceptance": original_acceptance,
         "implementation_mode": "generate", "permissions": empty_permissions()},
    ]


def expand_failure_subgraph(graph, parent_step_id, sources=None, failure=None, proposed_steps=None):
    """Turn a failing atomic step into a delegate and splice its bounded recovery subgraph."""
    working = copy.deepcopy(graph)
    parent = step_map(working).get(str(parent_step_id))
    if not parent:
        raise KeyError("unknown parent step: " + str(parent_step_id))
    failure = failure if isinstance(failure, dict) else {}
    action = str(failure.get("action") or "")
    if action != "split_step":
        raise ValueError("failure controller did not authorize split_step")
    if parent.get("status") not in ("running", "repairing", "failed"):
        raise ValueError("step cannot be decomposed from " + str(parent.get("status")))
    depth = int(((parent.get("delegation") or {}).get("depth") or 0)) + 1
    signature = _stable_hash({"step_id": parent_step_id, "version": parent.get("version"),
                              "failure_class": failure.get("failure_class"),
                              "sources": sources})
    history = parent.setdefault("controller_history", [])
    if any(str(x.get("signature")) == signature for x in history if isinstance(x, dict)):
        raise ValueError("identical decomposition already applied")
    raw = proposed_steps or propose_failure_subgraph(parent, sources, failure)
    parent.setdefault("implementation", {})["mode"] = "delegate"
    parent["implementation"]["why"] = "Adaptive Failure Controller: " + str(failure.get("failure_class") or "failure")
    parent["controller_decision"] = copy.deepcopy(failure)
    parent["output"] = {"status": "decomposed", "failure_class": failure.get("failure_class")}
    parent["evidence"] = [{"passed": True, "check": "bounded recovery subgraph authorized",
                           "signature": signature}]
    if parent.get("status") == "failed":
        transition_step(working, parent_step_id, "repairing", "controller selected split_step")
    if parent.get("status") == "repairing":
        transition_step(working, parent_step_id, "running", "materializing recovery subgraph")
    if parent.get("status") == "running":
        transition_step(working, parent_step_id, "succeeded", "decomposed; business acceptance moved to children")
    row = {"at": now_iso(), "signature": signature, "failure": copy.deepcopy(failure),
           "action": "split_step", "depth": depth}
    history.append(row)
    result = expand_subgraph(working, parent_step_id, raw,
                             reason="failure-driven decomposition: " + str(failure.get("failure_class") or ""),
                             delegation_depth=depth)
    graph.clear()
    graph.update(working)
    result["controller_signature"] = signature
    return result


def repair_step(graph, step_id, reason):
    step = step_map(graph).get(str(step_id))
    if not step:
        raise KeyError("unknown step: " + str(step_id))
    if step.get("status") not in ("repairing", "failed", "stale", "blocked_human"):
        raise ValueError("step is not repairable from " + str(step.get("status")))
    old_version = int(step.get("version") or 1)
    step.setdefault("version_history", []).append({
        "version": old_version, "output": copy.deepcopy(step.get("output")),
        "evidence": copy.deepcopy(step.get("evidence") or []), "error": copy.deepcopy(step.get("error")),
        "attempts": copy.deepcopy(step.get("attempts") or []), "archived_at": now_iso(),
    })
    step["version"] = old_version + 1
    step["status"] = "ready"
    step["output"] = None
    step["artifact_refs"] = []
    step["evidence"] = []
    step["error"] = None
    step["attempts"] = []
    step["started_at"] = None
    step["finished_at"] = None
    step["updated_at"] = now_iso()
    invalidated = []
    for sid in descendants(graph, step_id):
        child = step_map(graph).get(sid)
        if child and child.get("status") == "succeeded":
            child["status"] = "stale"
            child["status_reason"] = "dependency %s changed from v%d to v%d" % (
                step_id, old_version, step["version"])
            child["updated_at"] = now_iso()
            invalidated.append(sid)
    graph["version"] = int(graph.get("version") or 1) + 1
    graph["parent_version"] = graph["version"] - 1
    graph["updated_at"] = now_iso()
    return {"step_id": step_id, "version": step["version"], "invalidated": invalidated,
            "reason": _clip(reason, 1000)}


def block_for_human(graph, step_id, question, permission_request=None):
    step = step_map(graph).get(str(step_id))
    if not step:
        raise KeyError("unknown step: " + str(step_id))
    if step.get("status") not in ("ready", "running", "repairing"):
        raise ValueError("cannot block human from " + str(step.get("status")))
    event = transition_step(graph, step_id, "blocked_human", question, {
        "human_gate": {"question": _clip(question, 1200), "permission_request": permission_request,
                       "created_at": now_iso(), "answer": None}})
    return event


def answer_human(graph, step_id, answer, approved=None, by="owner"):
    step = step_map(graph).get(str(step_id))
    if not step or step.get("status") != "blocked_human":
        raise ValueError("step is not waiting for a human")
    gate = step.get("human_gate") if isinstance(step.get("human_gate"), dict) else {}
    gate.update({"answer": _clip(answer, 6000), "approved": approved, "answered_by": by,
                 "answered_at": now_iso()})
    step["human_gate"] = gate
    return transition_step(graph, step_id, "ready", "human answered")


def is_budget_gate(step):
    """Recognize current and legacy runtime-budget pauses without mistaking business budgets."""
    step = step if isinstance(step, dict) else {}
    gate = step.get("human_gate") if isinstance(step.get("human_gate"), dict) else {}
    request = gate.get("permission_request") if isinstance(gate.get("permission_request"), dict) else {}
    if request.get("kind") == "runtime_budget":
        return True
    question = str(gate.get("question") or "").casefold()
    return any(marker in question for marker in (
        "лимит процесса исчерпан", "process budget exhausted",
        "недостаточно бюджета хотя бы для build/run/verify"))


def grant_step_budget(graph, step_id):
    """Grant one bounded build/repair cycle after an explicit owner answer.

    The grant is derived from the checkpointed reserve, never from arbitrary user text. This keeps
    long processes resumable without turning resource limits into an unbounded automatic retry.
    """
    step = step_map(graph).get(str(step_id))
    if not step or step.get("status") != "blocked_human" or not is_budget_gate(step):
        raise ValueError("step is not waiting for runtime budget")
    gate = step.get("human_gate") if isinstance(step.get("human_gate"), dict) else {}
    request = gate.get("permission_request") if isinstance(gate.get("permission_request"), dict) else {}
    raw = request.get("reserve") if isinstance(request.get("reserve"), dict) else {}
    defaults = {"attempts": 4, "llm_calls": 9, "tokens": 96000,
                "cost_usd": 1.0, "generated_experts": 1}
    caps = {"attempts": 10, "llm_calls": 25, "tokens": 300000,
            "cost_usd": 5.0, "generated_experts": 2}
    reserve = {}
    for key, default in defaults.items():
        try:
            amount = float(raw.get(key, default)) if key == "cost_usd" else int(raw.get(key, default))
        except (TypeError, ValueError):
            amount = default
        reserve[key] = max(0, min(amount, caps[key]))
    budgets = graph.setdefault("budgets", default_budgets())
    run = graph.setdefault("run", {})
    mapping = {
        "attempts": ("attempts_used", "max_total_attempts"),
        "llm_calls": ("llm_calls_used", "max_llm_calls"),
        "tokens": ("tokens_used", "max_total_tokens"),
        "cost_usd": ("estimated_cost_usd", "max_cost_usd"),
        "generated_experts": ("generated_experts_used", "max_generated_experts"),
    }
    changed = {}
    for public, (used_key, limit_key) in mapping.items():
        cast = float if public == "cost_usd" else int
        used = cast(run.get(used_key) or 0)
        old = cast(budgets.get(limit_key) or 0)
        new = max(old, used) + cast(reserve[public])
        budgets[limit_key] = new
        changed[limit_key] = {"before": old, "after": new}
    gate["budget_grant"] = {"reserve": reserve, "granted_at": now_iso(), "limits": changed}
    step["human_gate"] = gate
    graph["updated_at"] = now_iso()
    return {"reserve": reserve, "limits": changed}


def recover_after_restart(graph):
    """Never guess that an interrupted external effect succeeded."""
    events = []
    for step in graph.get("steps") or []:
        if step.get("status") != "running":
            continue
        has_danger = any((step.get("permissions") or {}).get(kind) for kind in DANGEROUS_PERMISSIONS)
        if has_danger:
            step["status"] = "blocked_human"
            step["human_gate"] = {
                "question": "Мост перезапустился во время внешнего действия. Подтвердите фактический результат перед продолжением.",
                "permission_request": None, "created_at": now_iso(), "answer": None,
            }
            step["status_reason"] = "restart requires external-effect reconciliation"
        else:
            step["status"] = "ready"
            step["status_reason"] = "safe retry after restart"
        step["updated_at"] = now_iso()
        events.append({"step_id": step.get("id"), "to": step.get("status"), "at": now_iso()})
    graph["updated_at"] = now_iso()
    return events


def process_status(graph):
    statuses = [x.get("status") for x in (graph.get("steps") or [])]
    if statuses and all(x in ("succeeded", "skipped") for x in statuses):
        return "succeeded"
    if any(x == "blocked_human" for x in statuses):
        return "blocked_human"
    if any(x in ("running", "repairing") for x in statuses):
        return "running"
    if any(x == "failed" for x in statuses):
        return "failed"
    if any(x == "cancelled" for x in statuses) and not any(x in ("pending", "ready", "stale") for x in statuses):
        return "cancelled"
    return "pending"


def checkpoint(graph, path, events_path=None, event=None):
    valid = validate_process(graph)
    if not valid["ok"]:
        raise ValueError("cannot checkpoint invalid graph: " + "; ".join(valid["errors"]))
    graph["run"]["status"] = process_status(graph)
    graph["run"]["updated_at"] = now_iso()
    graph["updated_at"] = now_iso()
    atomic_write_json(path, graph)
    if events_path and event:
        append_event(events_path, event)
    return graph
