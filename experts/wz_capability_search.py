# expert: wz_capability_search
# description: Поиск способностей ВНЕ каталога композитора по живым источникам интернета — HuggingFace (локальные GGUF-модели), npm (MCP-серверы), GitHub (CLI/инструменты), Smithery (навыки). Возвращает нормализованных кандидатов {kind,id,title,desc,source,url,install,trust} для показа в композиторе. Ничего НЕ ставит — только находит; установка отдельным клик-подтверждением (wz_capability_install).

def wz_capability_search(query="", kinds="", limit=3, api_token="", api_base="https://api.extella.ai", target="", client="") -> str:
    import json, os, urllib.request, urllib.parse, ssl

    def _b(v):
        return (not v) or str(v).startswith("{{")
    if _b(query):
        return json.dumps({"status": "error", "message": "нужен query (что искать словами)"}, ensure_ascii=False)
    try:
        limit = max(1, min(5, int(limit)))
    except Exception:
        limit = 3

    ctx = ssl.create_default_context(); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE

    def _get(url, t=8):
        req = urllib.request.Request(url, headers={"User-Agent": "ExtellaComposer/1.0"})
        with urllib.request.urlopen(req, timeout=t, context=ctx) as r:
            return json.loads(r.read().decode("utf-8"))

    def _num(n):
        try:
            n = float(n)
            for u, d in (("M", 1e6), ("k", 1e3)):
                if n >= d:
                    return ("%.1f%s" % (n / d, u)).replace(".0", "")
            return str(int(n))
        except Exception:
            return "0"

    def _models(q):
        out = []
        try:
            url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
                {"search": q, "limit": 14, "sort": "downloads", "direction": "-1", "full": "false"})
            for m in _get(url):
                mid = m.get("id", "")
                if "gguf" not in mid.lower():
                    continue   # Ollama ставит только GGUF-репозитории
                out.append({"kind": "model", "id": mid, "title": mid.split("/")[-1],
                            "desc": "Локальная GGUF-модель — работает приватно на вашем устройстве через Ollama.",
                            "source": "huggingface", "url": "https://huggingface.co/" + mid,
                            "install": {"method": "ollama", "ref": "hf.co/" + mid + ":Q4_K_M"},
                            "trust": "↓ " + _num(m.get("downloads")) + " · ♥ " + _num(m.get("likes")), "external": True})
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out

    def _mcp(q):
        out = []
        try:
            url = "https://registry.npmjs.org/-/v1/search?" + urllib.parse.urlencode({"text": q + " mcp server", "size": 14})
            for o in _get(url).get("objects", []):
                p = o.get("package", {}); nm = p.get("name", "")
                if "mcp" not in nm.lower():
                    continue
                out.append({"kind": "mcp", "id": nm, "title": nm, "desc": (p.get("description") or "")[:170],
                            "source": "npm", "url": (p.get("links") or {}).get("npm", "https://npmjs.com/package/" + nm),
                            "install": {"method": "mcp_connect", "ref": nm, "pkg_type": "npm"},
                            "trust": "npm ▲ " + ("%.0f" % (o.get("score", {}).get("final", 0) * 100)), "external": True})
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out

    def _cli(q):
        out = []
        try:
            gq = q + " cli in:name,description stars:>15"
            url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
                {"q": gq, "sort": "stars", "order": "desc", "per_page": 8})
            terms = [w.lower() for w in q.split() if len(w) > 2]
            for r in _get(url).get("items", []):
                if r.get("archived") or r.get("fork"):
                    continue
                blob = ((r.get("name") or "") + " " + (r.get("description") or "")).lower()
                if "skip to content" in blob:   # мусорное скрейп-описание
                    continue
                if terms and not any(t in blob for t in terms):
                    continue
                out.append({"kind": "cli", "id": r["full_name"], "title": r["name"],
                            "desc": (r.get("description") or "")[:170], "source": "github", "url": r.get("html_url"),
                            "install": {"method": "manual", "ref": r["full_name"]},
                            "trust": "⭐ " + _num(r.get("stargazers_count")), "external": True})
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out

    def _brew(q):
        # Homebrew-формулы — РЕАЛЬНО ставятся на устройстве (brew есть на Маке Анвара)
        out = []
        try:
            import subprocess
            brew = "/opt/homebrew/bin/brew"
            if not os.path.exists(brew):
                brew = "/usr/local/bin/brew"
            if not os.path.exists(brew):
                return out
            p = subprocess.run([brew, "search", "--formula", q], capture_output=True, text=True, timeout=25)
            names = [ln.strip() for ln in (p.stdout or "").splitlines()
                     if ln.strip() and not ln.startswith("==>") and "No formula" not in ln and " " not in ln.strip()]
            for nm in names[:limit]:
                desc = ""
                try:
                    desc = (_get("https://formulae.brew.sh/api/formula/%s.json" % nm, t=5) or {}).get("desc", "")
                except Exception:
                    desc = ""
                out.append({"kind": "cli", "id": "brew:" + nm, "title": nm,
                            "desc": (desc or "Homebrew-формула — CLI-инструмент.")[:170], "source": "homebrew",
                            "url": "https://formulae.brew.sh/formula/" + nm,
                            "install": {"method": "brew", "ref": nm}, "trust": "brew · ставится на устройство", "external": True})
        except Exception:
            pass
        return out

    def _cli_all(q):
        return _brew(q) + _cli(q)   # сначала реально-устанавливаемые brew-формулы, затем GitHub-репо (ссылкой)

    def _skill(q):
        out = []
        try:
            data = _get("https://registry.smithery.ai/skills?" + urllib.parse.urlencode({"q": q, "pageSize": 10, "page": 1}))
            for s in (data.get("skills") or []):
                name = s.get("displayName") or s.get("qualifiedName") or ""
                if not name:
                    continue
                out.append({"kind": "skill", "id": s.get("qualifiedName") or name, "title": name,
                            "desc": (s.get("description") or "")[:170], "source": "smithery",
                            "url": s.get("gitUrl") or "", "install": {"method": "rule", "ref": s.get("gitUrl") or ""},
                            "trust": "▶ " + _num(s.get("totalActivations")), "external": True})
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out

    def _api(q):
        # Готовые API-клиенты/SDK (npm) — отдельный тип, не смешиваем с MCP и CLI
        out = []
        try:
            url = "https://registry.npmjs.org/-/v1/search?" + urllib.parse.urlencode({"text": q + " api client", "size": 14})
            for o in _get(url).get("objects", []):
                p = o.get("package", {}); nm = p.get("name", "")
                blob = (nm + " " + (p.get("description") or "")).lower()
                if "mcp" in nm.lower() or "api" not in blob:
                    continue
                out.append({"kind": "api", "id": nm, "title": nm, "desc": (p.get("description") or "")[:170],
                            "source": "npm", "url": (p.get("links") or {}).get("npm", "https://npmjs.com/package/" + nm),
                            "install": {"method": "manual", "ref": nm, "pkg_type": "npm"},
                            "trust": "npm ▲ " + ("%.0f" % (o.get("score", {}).get("final", 0) * 100)), "external": True})
                if len(out) >= limit:
                    break
        except Exception:
            pass
        return out

    fns = {"model": _models, "mcp": _mcp, "cli": _cli_all, "skill": _skill, "service": _mcp, "api": _api}
    ks = [k.strip().lower() for k in str(kinds or "").replace(";", ",").split(",") if k.strip()]
    if not ks:
        ks = ["model", "mcp", "cli", "skill", "api"]
    seen = set(); results = []
    for k in ks:
        fn = fns.get(k)
        if not fn:
            continue
        for c in fn(query):
            key = (c["kind"], c["id"])
            if key in seen:
                continue
            seen.add(key); results.append(c)

    return json.dumps({"status": "success", "query": query, "kinds": ks,
                       "count": len(results), "candidates": results}, ensure_ascii=False)
