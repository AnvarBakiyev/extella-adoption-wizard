def mcp_connect(server_id="", pkg_type="", pkg="", title="") -> str:
    import json, os, re, subprocess, threading, queue, time
    server_id = "" if (not server_id or str(server_id).startswith("{{")) else str(server_id).strip()
    pkg_type = "" if (not pkg_type or str(pkg_type).startswith("{{")) else str(pkg_type).strip().lower()
    pkg = "" if (not pkg or str(pkg).startswith("{{")) else str(pkg).strip()
    title = "" if (not title or str(title).startswith("{{")) else str(title).strip()
    def err(m): return json.dumps({"status":"error","message":m}, ensure_ascii=False)
    if pkg_type not in ("npm","pypi"): return err("поддерживаются только npm/pypi пакеты (получено: "+pkg_type+")")
    if not pkg: return err("нужен идентификатор пакета (pkg)")

    def _abs(cmd0):
        ABS = {"uvx": ["/opt/homebrew/bin/uvx", "/usr/local/bin/uvx"],
               "npx": ["/opt/homebrew/bin/npx", "/usr/local/bin/npx", "/opt/homebrew/opt/node@24/bin/npx"]}
        for p_ in ABS.get(cmd0, []):
            if os.path.exists(p_): return p_
        return cmd0
    def _session(cmd, timeout):
        env = dict(os.environ); env["PATH"] = "/opt/homebrew/bin:/usr/local/bin:" + env.get("PATH", "")
        p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env, text=True, bufsize=1)
        q = queue.Queue()
        def reader():
            for line in p.stdout:
                line = line.strip()
                if line:
                    try: q.put(json.loads(line))
                    except Exception: pass
        threading.Thread(target=reader, daemon=True).start()
        rid = [0]
        def rpc(method, params=None, tmo=timeout):
            rid[0] += 1
            msg = {"jsonrpc":"2.0","id":rid[0],"method":method}
            if params is not None: msg["params"] = params
            p.stdin.write(json.dumps(msg)+"\n"); p.stdin.flush()
            deadline = time.time()+tmo
            while time.time() < deadline:
                try:
                    m = q.get(timeout=1.0)
                    if m.get("id") == rid[0]: return m
                except Exception:
                    if p.poll() is not None: return {"error":{"message":"server died"}}
            return {"error":{"message":"timeout"}}
        def notify(method):
            p.stdin.write(json.dumps({"jsonrpc":"2.0","method":method})+"\n"); p.stdin.flush()
        return p, rpc, notify

    parts = [x for x in server_id.split("/") if x]
    tail = (parts[-1] if parts else "") or pkg
    if tail.lower() in ("mcp","mcp-server","server","mcp_server") and len(parts) > 1:
        tail = parts[-2].split(".")[-1] + "_" + tail
    key = re.sub(r"[^a-z0-9_]+","_", tail.lower()).strip("_")[:40]
    if not key: return err("не удалось построить ключ сервера")
    cmd = ["uvx", pkg] if pkg_type == "pypi" else ["npx", "-y", pkg]
    run_cmd = [_abs(cmd[0])] + cmd[1:]
    try:
        p, rpc, notify = _session(run_cmd, 150)
    except Exception as e:
        return err("не запустился: "+str(e)[:110])
    try:
        init = rpc("initialize", {"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"extella-connect","version":"0.2"}}, 150)
        if "error" in init: return err("сервер не ответил на рукопожатие: "+str(init["error"])[:120])
        notify("notifications/initialized")
        r = rpc("tools/list", {}, 60)
        tools = [{"name":t["name"],"desc":(t.get("description") or "")[:100]} for t in r.get("result",{}).get("tools",[])]
        if not tools: return err("сервер поднялся, но не отдал ни одного инструмента")
    finally:
        try: p.terminate()
        except Exception: pass
    d = os.path.expanduser("~/.extella_mcp"); os.makedirs(d, exist_ok=True)
    fp = os.path.join(d, "allowlist.json")
    try: allow = json.load(open(fp))
    except Exception: allow = {}
    allow[key] = {"cmd": cmd, "title": title or key, "pkg": pkg, "tools": [t["name"] for t in tools]}
    json.dump(allow, open(fp, "w"), ensure_ascii=False, indent=1)
    return json.dumps({"status":"success","server":key,"title":title or key,"count":len(tools),"tools":tools[:20]}, ensure_ascii=False)