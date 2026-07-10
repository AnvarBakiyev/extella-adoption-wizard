# expert: wz_capability_install
# description: Ставит выбранную способность ПОСЛЕ клик-подтверждения пользователя и регистрирует её во вкладке «Мои» (KV _mkt_installed). Модель -> cap_localmodel_install (Ollama), MCP -> mcp_connect (npx/uvx, вет-путь). CLI/навык — регистрируем ССЫЛКУ без запуска чужого кода (канон: внешний код только через вет-установщик). Возвращает {status, installed, message}.

def wz_capability_install(kind="", install_ref="", method="", pkg_type="", title="", desc="",
                          url="", source="", api_token="", api_base="https://api.extella.ai",
                          target="", client="") -> str:
    import json, os, re, urllib.request
    from pathlib import Path
    from datetime import datetime, timezone

    def _b(v):
        return (not v) or str(v).startswith("{{")

    if _b(kind) or _b(install_ref):
        return json.dumps({"status": "error", "message": "нужны kind и install_ref"}, ensure_ascii=False)
    if _b(api_base):
        api_base = "https://api.extella.ai"
    if _b(api_token):
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return json.dumps({"status": "error", "message": "нет api_token"}, ensure_ascii=False)

    # КАНОН: KV/expert-run — служебный скоуп agent_extella_default (иначе KV не виден серверу/тулбару);
    # платный Claude тут НЕ вызывается (это только заголовок скоупа, не agent/run)
    headers = {"X-Auth-Token": api_token, "Content-Type": "application/json",
               "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}

    def _post(path, body, t=300):
        req = urllib.request.Request(api_base.rstrip("/") + path,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode("utf-8"))

    def _parse(raw):
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except Exception:
                try:
                    import ast
                    return ast.literal_eval(raw)
                except Exception:
                    return {"raw": raw[:400]}
        return raw

    if _b(method):
        method = {"model": "ollama", "mcp": "mcp_connect", "service": "mcp_connect",
                  "cli": "manual", "api": "manual", "skill": "rule"}.get(kind, "manual")

    status = "linked"     # по умолчанию — зарегистрирована ссылка (без запуска чужого кода)
    detail = {}
    how = ""

    try:
        if method == "ollama":
            # приватная локальная модель через Ollama (вет-путь)
            r = _parse(_post("/api/expert/run", {"expert_name": "cap_localmodel_install",
                             "params": {"model": install_ref}, "global": True}).get("result"))
            st = str((r or {}).get("status", "")).lower()
            status = "installed" if st in ("success", "already", "pulling", "ok", "done", "present") else "error"
            detail = {"model": install_ref, "ollama_status": st or "?"}
            how = "Локальная модель — приватно на устройстве. Композитор берёт её как шаг; можно спросить через ассистента."
            if status == "error":
                detail["message"] = str((r or {}).get("message", ""))[:200]

        elif method == "mcp_connect":
            # MCP-сервер как инструмент агента (вет-путь npx/uvx)
            slug = re.sub(r"[^a-z0-9]+", "_", str(install_ref).split("/")[-1].lower()).strip("_")[:32] or "mcp_server"
            r = _parse(_post("/api/expert/run", {"expert_name": "mcp_connect",
                             "params": {"server_id": slug, "pkg_type": pkg_type or "npm",
                                        "pkg": install_ref, "title": title or install_ref}, "global": True}).get("result"))
            if isinstance(r, dict) and str(r.get("status", "")).lower() in ("success", "ok", "connected", "done"):
                status = "installed"; detail = {"server_id": r.get("server_id", slug), "tools": r.get("count") or r.get("tools")}
                how = "MCP-сервер подключён — доступен агенту как инструмент (mcp_call), можно добавить в конструктор."
            else:
                # mcp_connect может отсутствовать/не подняться на чистой среде — честно как ссылка
                status = "linked"; detail = {"pkg": install_ref, "note": str((r or {}).get("message", "не удалось поднять — сохранено ссылкой"))[:160]}
                how = "MCP-пакет (%s). Подключить: npx/uvx %s — через ассистента/конструктор." % (pkg_type or "npm", install_ref)

        elif method == "brew":
            # РЕАЛЬНАЯ установка CLI через Homebrew на устройстве (вет-путь: доверенный пакетный менеджер)
            import subprocess
            brew = "/opt/homebrew/bin/brew"
            if not os.path.exists(brew):
                brew = "/usr/local/bin/brew"
            if not os.path.exists(brew):
                status = "error"; detail = {"message": "Homebrew не найден на устройстве"}
            else:
                env = dict(os.environ); env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
                try:
                    p = subprocess.run([brew, "install", install_ref], capture_output=True, text=True, timeout=280, env=env)
                    blob = (p.stderr or "") + (p.stdout or "")
                    ok = (p.returncode == 0) or ("already installed" in blob)
                    status = "installed" if ok else "error"
                    detail = {"formula": install_ref, "rc": p.returncode}
                    if not ok:
                        detail["message"] = blob[-200:]
                    how = "CLI установлен через Homebrew — вызывай `%s` через ассистента/терминал." % install_ref
                except subprocess.TimeoutExpired:
                    status = "installing"; detail = {"formula": install_ref, "note": "brew install идёт дольше — проверь позже"}
                    how = "Устанавливается через Homebrew (крупный пакет) — появится в «Мои» по завершении."

        elif method == "rule":
            # РЕАЛЬНОЕ прошивание навыка: тянем SKILL.md из gitUrl (tree->raw) и добавляем как правило поведения
            import ssl as _ssl
            src = url or install_ref
            body = ""
            m = re.match(r"https://github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.+)", src or "")
            if m:
                o, rp, br, path = m.groups()
                _ctx = _ssl.create_default_context(); _ctx.check_hostname = False; _ctx.verify_mode = _ssl.CERT_NONE
                for fn in ("SKILL.md", "skill.md", "README.md"):
                    try:
                        raw = "https://raw.githubusercontent.com/%s/%s/%s/%s/%s" % (o, rp, br, path, fn)
                        rq = urllib.request.Request(raw, headers={"User-Agent": "Extella/1.0"})
                        body = urllib.request.urlopen(rq, timeout=10, context=_ctx).read().decode("utf-8", "replace")
                        if body.strip():
                            break
                    except Exception:
                        continue
            if body.strip():
                b = re.sub(r"^---\s*\n.*?\n---\s*\n", "", body, flags=re.S).strip()   # снять YAML-фронтматтер
                rule_text = ("Навык «%s» (источник %s):\n%s" % (title or install_ref, src, b))[:4000]
                try:
                    _post("/api/rules/add", {"rule": rule_text}, t=60)
                    status = "installed"; detail = {"src": src, "rule_len": len(rule_text)}
                    how = "Навык прошит как правило поведения — агент будет ему следовать. Источник: %s" % src
                except Exception as e:
                    status = "linked"; detail = {"src": src, "message": "правило не записалось: " + str(e)[:120]}
                    how = "Навык (источник %s) — не удалось прошить, сохранён ссылкой." % src
            else:
                status = "linked"; detail = {"src": src}
                how = "Навык. Источник: %s (SKILL.md не нашёлся автоматически)." % src

        else:  # manual (произвольный GitHub-репо / api) — ссылка без запуска чужого кода
            status = "linked"; detail = {"ref": install_ref}
            how = "Внешний инструмент. Установка: clone/сборка %s; запуск через ассистента (произвольный код не запускаем без вета)." % install_ref
    except Exception as e:
        status = "error"; detail = {"message": str(e)[:200]}
        how = ""

    # --- регистрация во вкладке «Мои» (общий KV _mkt_installed) ---
    rec = {"kind": kind, "id": install_ref, "title": title or install_ref, "desc": (desc or "")[:220],
           "source": source or "", "url": url or "", "method": method, "ref": install_ref,
           "status": status, "how": how, "detail": detail,
           "installed_at": datetime.now(timezone.utc).isoformat()}
    registered = False
    if status in ("installed", "linked", "installing"):
        try:
            cur = _post("/api/kv/get", {"key": "_mkt_installed"}, t=60)
            val = cur.get("value") if isinstance(cur, dict) else None
            cat = json.loads(val) if val else {"items": []}
        except Exception:
            cat = {"items": []}
        cat["items"] = [it for it in cat.get("items", []) if not (it.get("kind") == kind and it.get("id") == install_ref)]
        cat["items"].insert(0, rec)
        cat["items"] = cat["items"][:60]   # потолок KV ~28КБ: 200 структурных записей его пробивают (урок шардинга _mkt_*)
        try:
            _post("/api/kv/set", {"key": "_mkt_installed", "value": json.dumps(cat, ensure_ascii=False),
                                  "description": "composer-installed capabilities (Мои)"}, t=60)
            registered = True
        except Exception:
            registered = False

    return json.dumps({"status": "success" if status != "error" else "error",
                       "install_status": status, "registered_in_my": registered,
                       "installed": rec, "message": detail.get("message", "")}, ensure_ascii=False)
