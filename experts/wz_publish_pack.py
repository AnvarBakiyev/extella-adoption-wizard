# expert: wz_publish_pack
# description: Publish: генерит Process Pack из зарегистрированной композиции — тянет код экспертов (/api/expert/get), пишет карточку/README/install.py, git init+commit, опц. gh push, регистрирует карточку в витрине. Ответ на «как создан git»: платформа генерит его при публикации. Исполнять на устройстве (нужны git/gh/файлы).

def wz_publish_pack(pack_id="", name="", description="", experts="", orchestrator="",
                    agent_id="", agent_role_md="", concept_md="", readme="",
                    config_kv="ci:config", github_owner="AnvarBakiyev", push=False, private=False,
                    api_token="", api_base="https://api.extella.ai",
                    registry_dir="", build_root="") -> dict:
    import json, os, subprocess, urllib.request
    from pathlib import Path

    def _b(v):
        return (not v) or str(v).startswith("{{")

    if _b(pack_id):
        return {"status": "error", "message": "pack_id обязателен"}
    if _b(name):
        name = pack_id
    if _b(api_base):
        api_base = "https://api.extella.ai"
    if _b(api_token):
        cfg = Path.home() / "extella_wizard" / "app" / "config.json"
        try:
            api_token = json.loads(cfg.read_text(encoding="utf-8")).get("auth_token", "") if cfg.exists() else ""
        except Exception:
            api_token = ""
    if not api_token:
        return {"status": "error", "message": "нет api_token"}

    hdr = {"X-Auth-Token": api_token, "Content-Type": "application/json",
           "X-Profile-Id": "default", "X-Agent-Id": agent_id or "agent_extella_default"}

    def post(path, body, t=60):
        req = urllib.request.Request(api_base.rstrip("/") + path,
                                     data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                     headers=hdr, method="POST")
        with urllib.request.urlopen(req, timeout=t) as r:
            return json.loads(r.read().decode("utf-8"))

    import os as _os
    _env = dict(_os.environ)
    _env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:" + _env.get("PATH", "")  # brew скрыт от PATH листенера

    def sh(args, cwd):
        p = subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, timeout=180, env=_env)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    build = Path(build_root) if not _b(build_root) else (Path.home() / "extella_wizard" / "published")
    root = build / pack_id
    (root / "experts").mkdir(parents=True, exist_ok=True)
    (root / "_registry").mkdir(parents=True, exist_ok=True)

    # 1. Материализуем эксперты из платформы (реальная зарегистрированная композиция)
    exp_names = [e.strip() for e in (experts if isinstance(experts, list) else str(experts).split(",")) if str(e).strip()]
    written, missing = [], []
    for en in exp_names:
        try:
            r = post("/api/expert/get", {"name": en, "global": True})
            code = r.get("expert_code") or ""
            if not code:
                missing.append(en); continue
            # гарантируем header-комменты для install.py
            head = ""
            if "# expert:" not in code[:200]:
                head = "# expert: %s\n# description: %s\n\n" % (en, (r.get("expert_description") or en))
            (root / "experts" / (en + ".py")).write_text(head + code, encoding="utf-8")
            written.append(en)
        except Exception as e:
            missing.append(en + ":" + str(e)[:40])

    # 2. Роль агента, концепт
    if not _b(agent_role_md):
        (root / "agents").mkdir(exist_ok=True)
        (root / "agents" / "role.md").write_text(agent_role_md, encoding="utf-8")
    if not _b(concept_md):
        (root / "concepts").mkdir(exist_ok=True)
        (root / "concepts" / (pack_id + ".md")).write_text(concept_md, encoding="utf-8")

    # 3. Карточка витрины (type:process)
    card = {"id": pack_id, "name": name, "type": "process", "version": "1.0.0",
            "description": description or name, "experts": written, "orchestrator": orchestrator or "",
            "synthAgentId": agent_id or "", "runtimeConfigKey": config_kv,
            "source": "https://github.com/%s/%s" % (github_owner, pack_id) if push else "local",
            "installed": True, "publishedBy": github_owner}
    (root / "_registry" / (pack_id + ".json")).write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")

    # 4. README + install.py (генерённые)
    (root / "README.md").write_text(readme or ("# " + name + "\n\n" + (description or "")), encoding="utf-8")
    installer = ('#!/usr/bin/env python3\n"""Auto-generated installer — регистрирует эксперты пака в аккаунт Extella."""\n'
                 'import json,os,glob,urllib.request\nHERE=os.path.dirname(os.path.abspath(__file__))\n'
                 'CFG=os.path.expanduser("~/extella_wizard/app/config.json")\n'
                 'cfg=json.load(open(CFG)); TOK=cfg["auth_token"]; BASE=cfg.get("api_base","https://api.extella.ai")\n'
                 'HDR={"X-Auth-Token":TOK,"Content-Type":"application/json","X-Profile-Id":"default","X-Agent-Id":cfg.get("agent_id","agent_extella_default")}\n'
                 'def api(p,b):\n req=urllib.request.Request(BASE+p,data=json.dumps(b).encode(),headers=HDR,method="POST")\n'
                 ' return json.loads(urllib.request.urlopen(req,timeout=90).read().decode())\n'
                 'def desc(s):\n return next((l.split(":",1)[1].strip() for l in s.splitlines()[:6] if l.startswith("# description:")),"")\n'
                 'for f in sorted(glob.glob(os.path.join(HERE,"experts","*.py"))):\n'
                 ' n=os.path.basename(f)[:-3]; src=open(f).read()\n'
                 ' r=api("/api/expert/save",{"name":n,"description":desc(src) or n,"code":src,"kwargs":{},"cspl":"fython","global":True})\n'
                 ' print(("OK " if r.get("status")=="success" else "FAIL ")+n)\n')
    (root / "install.py").write_text(installer, encoding="utf-8")
    (root / ".gitignore").write_text("__pycache__/\n*.pyc\n", encoding="utf-8")

    # 5. git init + commit
    log = {}
    rc, o = sh(["git", "init", "-q"], root); log["init"] = rc
    sh(["git", "add", "-A"], root)
    rc, o = sh(["git", "-c", "user.email=pack@extella.ai", "-c", "user.name=Extella",
                "commit", "-qm", "Publish %s — generated by Extella" % pack_id], root)
    log["commit"] = rc

    # 6. push (device gh auth)
    repo_url = "local"
    if push and not _b(github_owner):
        vis = "--private" if private else "--public"
        rc, o = sh(["gh", "repo", "create", "%s/%s" % (github_owner, pack_id), vis,
                    "--source=.", "--remote=origin", "--push",
                    "--description", (description or name)[:200]], root)
        log["gh_create"] = rc; log["gh_out"] = o[-200:]
        if rc == 0:
            repo_url = "https://github.com/%s/%s" % (github_owner, pack_id)
        else:  # репо мог уже существовать → просто push
            sh(["git", "remote", "add", "origin", "https://github.com/%s/%s.git" % (github_owner, pack_id)], root)
            rc2, o2 = sh(["git", "push", "-u", "origin", "HEAD:main", "-f"], root)
            log["push"] = rc2; log["push_out"] = o2[-200:]
            if rc2 == 0:
                repo_url = "https://github.com/%s/%s" % (github_owner, pack_id)

    if repo_url != "local":
        card["source"] = repo_url

    # 7. регистрируем карточку в витрине (реестр плагинов на диске)
    reg = Path(registry_dir) if not _b(registry_dir) else (Path.home() / "extella-plugins" / "_registry")
    card_installed = None
    try:
        reg.mkdir(parents=True, exist_ok=True)
        (reg / (pack_id + ".json")).write_text(json.dumps(card, ensure_ascii=False, indent=2), encoding="utf-8")
        card_installed = str(reg / (pack_id + ".json"))
    except Exception as e:
        log["registry_err"] = str(e)[:100]

    # 8. KV-каталог витрины _mkt_automations — его читает вкладка «AI Автоматизации» (карточка появляется в магазине)
    try:
        _cur = post("/api/kv/get", {"key": "_mkt_automations"}).get("value")
        _cat = json.loads(_cur) if _cur else {"items": []}
        _items = [it for it in _cat.get("items", []) if it.get("id") != pack_id]
        _items.insert(0, card)
        _cat["items"] = _items
        post("/api/kv/set", {"key": "_mkt_automations", "value": json.dumps(_cat, ensure_ascii=False),
                             "description": "automations catalog (витрина)"})
        log["catalog_items"] = len(_items)
    except Exception as e:
        log["catalog_err"] = str(e)[:100]

    return {"status": "success", "pack_id": pack_id, "repo_url": repo_url,
            "experts_written": written, "experts_missing": missing,
            "card_registered": card_installed, "build_dir": str(root), "git": log}
