# expert: wz_cli_capability_factory
# description: Эксперт wz_cli_capability_factory (Adoption Wizard).
# params: 

#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Extella CLI Capability Factory (движок create_cli_capability_pack).
Берёт короткую спецификацию инструмента и выпускает целую Способность:
  - cap_<tool>_resolver            (найти/поставить бинарь; пишет локальный указатель ~/.extella_cli/<tool>)
  - cap_<tool>_<op>                (бизнес-кнопки; argv-массив, shell=False, allowlist параметров)
  - cap_<tool>_<op>_batch          (пакетный режим; per-file timeout)
  - KV cap_<tool>_manifest (паспорт), cap_<tool>_pack (карточка витрины)
  - concept [EXTELLA:CAP] cap:<tool>  (discovery для агента)
Безопасность: каждый argv-токен — ОДИН элемент (подстановка внутри токена, без .split()) → инъекция невозможна.
Управляемость: каждая Способность — свой паспорт/версия/эксперты (слой L2C).
Честно: принуждение прав — код обёртки; реальную песочницу (сеть/ФС) добавит платформа (L2C-P1).
"""
import os, re, json, urllib.request, sys

# ---------------------------------------------------------------- REST driver
def _tok():
    s = open(os.path.expanduser('~/.claude/extella_mcp_server.py')).read()
    return re.search(r'AUTH_TOKEN\s*=\s*["\']([^"\']+)["\']', s).group(1)

_H = {'Content-Type': 'application/json', 'X-Auth-Token': _tok(),
      'X-Profile-Id': 'default', 'X-Agent-Id': 'agent_extella_default'}

def _post(path, body, timeout=300):
    req = urllib.request.Request('https://api.extella.ai' + path,
                                 data=json.dumps(body).encode(), headers=_H)
    try:
        return json.loads(urllib.request.urlopen(req, timeout=timeout).read())
    except Exception as e:
        try:    return {'_err': e.read().decode()[:200]}
        except: return {'_err': str(e)[:160]}

def _res(run):
    o = run.get('result') or run.get('output') or run.get('_err') or run
    if isinstance(o, str):
        try: return json.loads(o)
        except: return o
    return o

def render(tmpl, **kw):
    for k, v in kw.items():
        tmpl = tmpl.replace('%%' + k + '%%', v)
    return tmpl

# ---------------------------------------------------------------- templates
RESOLVER_BREW = r'''
def cap_%%TOOL%%_resolver(confirm_install="no") -> str:
    import os, subprocess, json
    CANDS = %%CANDS%%
    def rec(p):
        d = os.path.expanduser("~/.extella_cli"); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "%%TOOL%%"), "w").write(p)
    def verify(p):
        try:
            r = subprocess.run([p] + %%VERIFY%%, capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return (r.stdout or r.stderr).strip().split("\n")[0][:24]
        except Exception: pass
        return None
    for p in CANDS:
        if os.path.exists(p):
            v = verify(p)
            if v: rec(p); return json.dumps({"status":"already","bin_path":p,"version":v,"source":"detected"}, ensure_ascii=False)
    if not confirm_install or confirm_install.startswith("{{") or confirm_install.lower() != "yes":
        return json.dumps({"status":"missing","message":"%%DISPLAY%% не установлен. confirm_install='yes' поставит через brew."}, ensure_ascii=False)
    brew = next((b for b in ["/opt/homebrew/bin/brew","/usr/local/bin/brew"] if os.path.exists(b)), None)
    if not brew:
        return json.dumps({"status":"failed","message":"Homebrew не найден — нужен brew или ручная установка."}, ensure_ascii=False)
    env = dict(os.environ); env["NONINTERACTIVE"] = "1"
    try:
        subprocess.run([brew] + %%BREWCMD%%, capture_output=True, text=True, timeout=%%BREWTIMEOUT%%, env=env)
    except Exception as e:
        return json.dumps({"status":"failed","message":"brew упал: " + str(e)[:100]}, ensure_ascii=False)
    for p in CANDS:
        if os.path.exists(p):
            v = verify(p)
            if v: rec(p); return json.dumps({"status":"installed","bin_path":p,"version":v,"source":"brew"}, ensure_ascii=False)
    return json.dumps({"status":"failed","message":"Поставили, но бинарь не находится."}, ensure_ascii=False)
'''

RESOLVER_PIP = r'''
def cap_%%TOOL%%_resolver(confirm_install="no") -> str:
    import os, subprocess, sys, json, shutil
    def rec(p):
        d = os.path.expanduser("~/.extella_cli"); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "%%TOOL%%"), "w").write(p)
    def verify(p):
        try:
            r = subprocess.run([p] + %%VERIFY%%, capture_output=True, text=True, timeout=20)
            if r.returncode == 0:
                return (r.stdout or r.stderr).strip().split("\n")[0][:24]
        except Exception: pass
        return None
    def locate():
        try:
            import importlib; importlib.invalidate_caches()
            import %%PYMOD%% as M
            return %%PATHEXPR%%
        except Exception:
            return None
    p = shutil.which("%%BIN%%")
    if p:
        v = verify(p)
        if v: rec(p); return json.dumps({"status":"already","bin_path":p,"version":v,"source":"which"}, ensure_ascii=False)
    p = locate()
    if p and os.path.exists(p):
        v = verify(p)
        if v: rec(p); return json.dumps({"status":"already","bin_path":p,"version":v,"source":"pip"}, ensure_ascii=False)
    if not confirm_install or confirm_install.startswith("{{") or confirm_install.lower() != "yes":
        return json.dumps({"status":"missing","message":"%%DISPLAY%% не установлен. confirm_install='yes' поставит через pip."}, ensure_ascii=False)
    try:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "%%PIPPKG%%"], capture_output=True, text=True, timeout=280)
    except Exception as e:
        return json.dumps({"status":"failed","message":"pip упал: " + str(e)[:100]}, ensure_ascii=False)
    p = locate()
    if p and os.path.exists(p):
        v = verify(p)
        if v: rec(p); return json.dumps({"status":"installed","bin_path":p,"version":v,"source":"pip"}, ensure_ascii=False)
    return json.dumps({"status":"failed","message":"Поставили pip-пакет, но бинарь не найден."}, ensure_ascii=False)
'''

RESOLVER_COMPOSITE = r'''
def cap_%%TOOL%%_resolver(confirm_install="no") -> str:
    # Составная установка: несколько частей (brew + pip) + языковые данные + правильный PATH.
    import os, subprocess, sys, json, shutil, urllib.request
    BREW = %%BREWLIST%%
    PIP = %%PIPLIST%%
    DRIVER = "%%DRIVER%%"
    PATH_ADD = %%PATHADD%%
    TESS = %%TESSDATA%%
    def rec(p):
        d = os.path.expanduser("~/.extella_cli"); os.makedirs(d, exist_ok=True)
        open(os.path.join(d, "%%TOOL%%"), "w").write(p)
    def aug():
        return os.pathsep.join(PATH_ADD + [os.environ.get("PATH", "")])
    def driver_ok():
        p = shutil.which(DRIVER, path=aug())
        if not p: return None
        try:
            e = dict(os.environ); e["PATH"] = aug()
            r = subprocess.run([p, "--version"], capture_output=True, text=True, timeout=40, env=e)
            if r.returncode == 0: return p
        except Exception: pass
        return None
    def ensure_tess():
        if not TESS: return
        dirs = TESS.get("dirs") or [TESS.get("dir", "~/.extella_cli/tessdata")]
        td = None
        for c in dirs:
            c = os.path.expanduser(c)
            if os.path.isdir(c): td = c; break
        if not td:
            td = os.path.expanduser(dirs[0])
            try: os.makedirs(td, exist_ok=True)
            except Exception: return
        for name, url in TESS["files"].items():
            f = os.path.join(td, name + ".traineddata")
            if not os.path.exists(f) or os.path.getsize(f) < 1000:
                try: urllib.request.urlretrieve(url, f)
                except Exception: pass
    p = driver_ok()
    if p:
        ensure_tess(); rec(p)
        return json.dumps({"status": "already", "bin_path": p, "driver": DRIVER, "source": "detected"}, ensure_ascii=False)
    if not confirm_install or confirm_install.startswith("{{") or confirm_install.lower() != "yes":
        return json.dumps({"status": "missing", "message": "%%DISPLAY%% не установлен. confirm_install='yes' поставит все части."}, ensure_ascii=False)
    brew = next((b for b in ["/opt/homebrew/bin/brew", "/usr/local/bin/brew"] if os.path.exists(b)), None)
    if BREW and not brew:
        return json.dumps({"status": "failed", "message": "Homebrew не найден для системных частей (" + ", ".join(BREW) + ")."}, ensure_ascii=False)
    log = []
    env = dict(os.environ); env["NONINTERACTIVE"] = "1"
    for f in BREW:
        try: subprocess.run([brew, "install", f], capture_output=True, text=True, timeout=600, env=env)
        except Exception as e: log.append("brew " + f + ": " + str(e)[:60])
    for pk in PIP:
        try: subprocess.run([sys.executable, "-m", "pip", "install", "-q", pk], capture_output=True, text=True, timeout=420)
        except Exception as e: log.append("pip " + pk + ": " + str(e)[:60])
    ensure_tess()
    p = driver_ok()
    if p:
        rec(p)
        return json.dumps({"status": "installed", "bin_path": p, "driver": DRIVER, "parts": BREW + PIP, "source": "composite"}, ensure_ascii=False)
    return json.dumps({"status": "failed", "message": "Поставили части, но " + DRIVER + " не запускается", "log": log[:4]}, ensure_ascii=False)
'''

WRAPPER = r'''
def cap_%%TOOL%%_%%OP%%(%%SIG%%) -> str:
    import os, subprocess, json, shutil, tempfile
%%ENUMS%%
    def binpath():
        f = os.path.expanduser("~/.extella_cli/%%TOOL%%")
        if os.path.exists(f):
            p = open(f).read().strip()
            if p and os.path.exists(p): return p
        p = shutil.which("%%BIN%%")
        if p: return p
        for c in %%CANDS%%:
            if os.path.exists(c): return c
        return None
    if not input_path or input_path.startswith("{{") or not os.path.exists(input_path):
        return json.dumps({"status":"error","message":"нужен существующий input_path"}, ensure_ascii=False)
%%VALIDATE%%
    b = binpath()
    if not b:
        return json.dumps({"status":"error","message":"%%DISPLAY%% не установлен — сначала cap_%%TOOL%%_resolver(confirm_install='yes')"}, ensure_ascii=False)
%%OUTPREP%%
    before = os.path.getsize(input_path)
    TMPL = %%ARGV%%
    SUB = %%SUBMAP%%
    argv = [b]
    for tok in TMPL:
        for k, v in SUB.items():
            tok = tok.replace("{" + k + "}", str(v))
        argv.append(tok)
    _env = dict(os.environ)
%%ENVBUILD%%
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=%%TIMEOUT%%, env=_env)
    except Exception as e:
        return json.dumps({"status":"error","message":"вызов упал: " + str(e)[:100]}, ensure_ascii=False)
    if r.returncode != 0 or not os.path.exists(output_path):
        return json.dumps({"status":"error","message":"инструмент не создал файл","err":(r.stderr or "")[:140]}, ensure_ascii=False)
    after = os.path.getsize(output_path)
%%POST%%
    return json.dumps({"status":"success","output_path":output_path,"in_kb":round(before/1024,1),"out_kb":round(after/1024,1)%%EXTRA%%}, ensure_ascii=False)
'''

BATCH = r'''
def cap_%%TOOL%%_%%OP%%_batch(in_dir="", out_dir="", %%BSIG%%) -> str:
    import os, subprocess, json, glob, shutil, tempfile
%%ENUMS%%
    def binpath():
        f = os.path.expanduser("~/.extella_cli/%%TOOL%%")
        if os.path.exists(f):
            p = open(f).read().strip()
            if p and os.path.exists(p): return p
        p = shutil.which("%%BIN%%")
        if p: return p
        for c in %%CANDS%%:
            if os.path.exists(c): return c
        return None
    if not in_dir or in_dir.startswith("{{") or not os.path.isdir(in_dir):
        return json.dumps({"status":"error","message":"нужен существующий in_dir"}, ensure_ascii=False)
%%VALIDATE%%
    b = binpath()
    if not b:
        return json.dumps({"status":"error","message":"%%DISPLAY%% не установлен — сначала cap_%%TOOL%%_resolver(confirm_install='yes')"}, ensure_ascii=False)
    if not out_dir or out_dir.startswith("{{"): out_dir = in_dir.rstrip("/") + "_out"
    os.makedirs(out_dir, exist_ok=True)
    _env = dict(os.environ)
%%ENVBUILD%%
    srcs = sorted(glob.glob(os.path.join(in_dir, "*%%INEXT%%")))
    tin = 0; tout = 0; ok = 0; fail = 0; items = []
    for src in srcs:
        stem = os.path.splitext(os.path.basename(src))[0]
        dst = os.path.join(out_dir, stem + "%%OUTSUFFIX%%")
        before = os.path.getsize(src)
%%BPREP%%
        TMPL = %%ARGV%%
        SUB = %%BSUBMAP%%
        argv = [b]
        for tok in TMPL:
            for k, v in SUB.items():
                tok = tok.replace("{" + k + "}", str(v))
            argv.append(tok)
        try:
            r = subprocess.run(argv, capture_output=True, text=True, timeout=%%TIMEOUT%%, env=_env)
            if r.returncode == 0 and os.path.exists(dst):
                after = os.path.getsize(dst)
%%BPOST%%
                tin += before; tout += after; ok += 1
                items.append({"file": os.path.basename(src), "out_kb": round(after/1024,1)})
            else:
                fail += 1; items.append({"file": os.path.basename(src), "error": (r.stderr or "")[:60]})
        except Exception as e:
            fail += 1; items.append({"file": os.path.basename(src), "error": str(e)[:60]})
    saved = round(100*(tin-tout)/tin, 1) if tin else 0
    return json.dumps({"status":"success","count":len(srcs),"ok":ok,"failed":fail,"total_saved_pct":saved,"in_mb":round(tin/1048576,2),"out_mb":round(tout/1048576,2),"out_dir":out_dir,"items":items[:20]}, ensure_ascii=False)
'''

# ---------------------------------------------------------------- generation
def _sig(op):
    parts = ['input_path=""', 'output_path=""']
    for p in op.get("params", []):
        parts.append('%s="%s"' % (p["name"], p["default"]))
    return ", ".join(parts)

def _bsig(op):
    parts = []
    for p in op.get("params", []):
        parts.append('%s="%s"' % (p["name"], p["default"]))
    return ", ".join(parts) if parts else "_pad=None"

def _enums(op):
    lines = []
    for p in op.get("params", []):
        if "enum" in p:
            lines.append('    ALLOWED_%s = %s' % (p["name"], tuple(p["enum"]).__repr__()))
    return "\n".join(lines)

def _validate(op):
    lines = []
    for p in op.get("params", []):
        if "enum" in p:
            lines.append('    if not %s or %s.startswith("{{") or %s not in ALLOWED_%s: %s = "%s"'
                         % (p["name"], p["name"], p["name"], p["name"], p["name"], p["default"]))
    return "\n".join(lines) if lines else "    pass"

def _submap(op, batch=False):
    inp = "src" if batch else "input_path"
    out = "dst" if batch else "output_path"
    m = ['"input": %s' % inp, '"output": %s' % out]
    if op.get("out_mode") == "dir":
        m.append('"outdir": %s' % ("out_dir" if batch else "_outdir"))
        m.append('"profile": _profile')
    for p in op.get("params", []):
        m.append('"%s": %s' % (p["name"], p["name"]))
    return "{" + ", ".join(m) + "}"

def _outprep(op):
    suf = op["out_suffix"]
    if op.get("out_mode") == "dir":
        return (
            '    _inbase = os.path.splitext(os.path.basename(input_path))[0]\n'
            '    _outdir = (os.path.dirname(os.path.abspath(output_path)) if output_path and not output_path.startswith("{{") else os.path.dirname(os.path.abspath(input_path))) or "."\n'
            '    os.makedirs(_outdir, exist_ok=True)\n'
            '    output_path = os.path.join(_outdir, _inbase + "' + suf + '")\n'
            '    _profile = tempfile.mkdtemp(prefix="_locap_")'
        )
    return (
        '    if not output_path or output_path.startswith("{{"):\n'
        '        base, _ = os.path.splitext(input_path); output_path = base + "' + suf + '"\n'
        '    os.makedirs(os.path.dirname(os.path.abspath(output_path)) or ".", exist_ok=True)'
    )

def _bprep(op):
    if op.get("out_mode") == "dir":
        return '        _profile = tempfile.mkdtemp(prefix="_locap_")'
    return '        pass'

def _postcode(op):
    if op.get("keep_smaller"):
        return ("    if after >= before:\n"
                "        shutil.copyfile(input_path, output_path); after = before")
    return "    pass"

def _bpost(op):
    if op.get("keep_smaller"):
        return ("                if after >= before:\n"
                "                    shutil.copyfile(src, dst); after = before")
    return "                pass"

def _extra(op):
    if op.get("keep_smaller"):
        return ',"saved_pct":round(100*(before-after)/before,1) if before else 0'
    return ""

def _envbuild(spec):
    renv = spec.get("run_env")
    if not renv:
        return "    pass"
    lines = []
    for pa in renv.get("path_add", []):
        lines.append('    _env["PATH"] = ' + repr(pa) + ' + os.pathsep + _env.get("PATH","")')
    for k, v in renv.get("set", {}).items():
        lines.append('    _env[' + repr(k) + '] = os.path.expanduser(' + repr(v) + ')')
    return "\n".join(lines) if lines else "    pass"

def gen_resolver(spec):
    inst = spec["install"]
    common = dict(TOOL=spec["tool"], DISPLAY=spec["display_name"], BIN=spec["bin"],
                  CANDS=json.dumps(spec["cands"]), VERIFY=json.dumps(spec["verify"]))
    if inst["mode"] == "brew_abs":
        cmd = ["install"] + (["--cask"] if inst.get("cask") else []) + [inst["brew"]]
        return render(RESOLVER_BREW, BREW=inst["brew"], BREWCMD=json.dumps(cmd),
                      BREWTIMEOUT=str(inst.get("timeout", 280)), **common)
    if inst["mode"] == "pip_binary":
        return render(RESOLVER_PIP, PIPPKG=inst["pip_pkg"], PYMOD=inst["pymod"],
                      PATHEXPR=inst["path_expr"], **common)
    if inst["mode"] == "composite":
        return render(RESOLVER_COMPOSITE,
                      BREWLIST=json.dumps(inst.get("brew", [])),
                      PIPLIST=json.dumps(inst.get("pip", [])),
                      DRIVER=inst["driver"],
                      PATHADD=json.dumps(inst.get("path_add", [])),
                      TESSDATA=(json.dumps(inst["tessdata"]) if inst.get("tessdata") else "None"),
                      **common)
    raise ValueError("unknown install mode " + inst["mode"])

def gen_wrapper(spec, op):
    return render(WRAPPER, TOOL=spec["tool"], OP=op["op"], DISPLAY=spec["display_name"],
                  BIN=spec["bin"], CANDS=json.dumps(spec["cands"]),
                  SIG=_sig(op), ENUMS=_enums(op), VALIDATE=_validate(op),
                  ARGV=json.dumps(op["argv"]), SUBMAP=_submap(op), OUTPREP=_outprep(op),
                  OUTSUFFIX=op["out_suffix"], TIMEOUT=str(op.get("timeout", 120)),
                  ENVBUILD=_envbuild(spec), POST=_postcode(op), EXTRA=_extra(op))

def gen_batch(spec, op):
    return render(BATCH, TOOL=spec["tool"], OP=op["op"], DISPLAY=spec["display_name"],
                  BIN=spec["bin"], CANDS=json.dumps(spec["cands"]),
                  BSIG=_bsig(op), ENUMS=_enums(op), VALIDATE=_validate(op),
                  ARGV=json.dumps(op["argv"]), BSUBMAP=_submap(op, batch=True), BPREP=_bprep(op),
                  OUTSUFFIX=op["out_suffix"], INEXT=op["input_ext"],
                  TIMEOUT=str(op.get("timeout", 120)), ENVBUILD=_envbuild(spec), BPOST=_bpost(op))

def gen_manifest(spec):
    ops = {}
    for op in spec["operations"]:
        ops[op["op"]] = {"argv_template": op["argv"],
                         "allowed_params": {p["name"]: {"enum": p["enum"]} for p in op.get("params", []) if "enum" in p},
                         "per_file_timeout_s": op.get("timeout", 120)}
    return {"schema": "extella.cli_capability/v1", "tool": spec["tool"],
            "display_name": spec["display_name"], "version": 1,
            "binary": {"cmd": spec["bin"], "platforms": ["darwin", "linux"]},
            "install": spec["install"], "operations": ops,
            "permissions": {"network": "none", "filesystem": {"read": ["{input}"], "write": ["{output_dir}/**"]},
                            "secrets": [], "destructive": False, "requires_human_confirm": False},
            "enforcement": "wrapper-code", "execution_modes": ["sync", "batch"],
            "experts": {"resolver": "cap_%s_resolver" % spec["tool"],
                        "wrappers": ["cap_%s_%s" % (spec["tool"], op["op"]) for op in spec["operations"]],
                        "batch": ["cap_%s_%s_batch" % (spec["tool"], op["op"]) for op in spec["operations"]]},
            "honest_limits": ["версия бинаря не пиннится между машинами",
                              "принуждение прав — код обёртки, не платформенная песочница (ждёт L2C-P1, Тимур)"],
            "created_by": "wz_cli_capability_factory"}

def gen_concept(spec):
    btns = "; ".join("cap_%s_%s (%s)" % (spec["tool"], op["op"], op["display"]) for op in spec["operations"])
    return ("[EXTELLA:CAP] cap:%s — Способность: %s (локально/офлайн). Бизнес-кнопки: %s + пакетные *_batch. "
            "Агент ВЫБИРАЕТ способность и зовёт эксперт с бизнес-параметрами, НЕ пишет команду. "
            "Установка/проверка бинаря: cap_%s_resolver (confirm_install='yes'). "
            "Честные границы: версия бинаря не пиннится; принуждение прав — код обёртки, не песочница (L2C-P1)."
            % (spec["tool"], spec["display_name"], btns, spec["tool"]))

def gen_pack(spec):
    experts = ["cap_%s_resolver" % spec["tool"]]
    for op in spec["operations"]:
        experts += ["cap_%s_%s" % (spec["tool"], op["op"]), "cap_%s_%s_batch" % (spec["tool"], op["op"])]
    return {"pack": "cli/%s" % spec["tool"], "type": "cli_capability",
            "manifest_key": "cap_%s_manifest" % spec["tool"], "experts": experts,
            "concept": "cap:%s" % spec["tool"], "install_expert": "cap_%s_resolver" % spec["tool"],
            "card": {**spec["card"], "aisle": spec.get("aisle", "docs")},
            "honest_limits": ["принуждение прав — код обёртки, не песочница (ждёт L2C-P1, Тимур)"]}

def build_capability(spec):
    experts = {"cap_%s_resolver" % spec["tool"]: gen_resolver(spec)}
    for op in spec["operations"]:
        experts["cap_%s_%s" % (spec["tool"], op["op"])] = gen_wrapper(spec, op)
        experts["cap_%s_%s_batch" % (spec["tool"], op["op"])] = gen_batch(spec, op)
    return {"experts": experts,
            "kv": {"cap_%s_manifest" % spec["tool"]: gen_manifest(spec),
                   "cap_%s_pack" % spec["tool"]: gen_pack(spec)},
            "concept": gen_concept(spec)}

def register(spec, verbose=True):
    art = build_capability(spec)
    log = []
    for name, code in art["experts"].items():
        st = _post('/api/expert/save', {"name": name, "description": "CLI Capability %s — %s" % (spec["tool"], name),
                                        "code": code, "kwargs": {}, "cspl": "fython"}).get("status", "?")
        log.append("expert %s -> %s" % (name, st))
    for k, v in art["kv"].items():
        st = _post('/api/kv/set', {"key": k, "value": json.dumps(v, ensure_ascii=False),
                                   "description": "CLI capability %s" % spec["tool"], "global": True}).get("status", "?")
        log.append("kv %s -> %s" % (k, st))
    st = _post('/api/concept/add', {"text": art["concept"]}).get("status", "?")
    log.append("concept cap:%s -> %s" % (spec["tool"], st))
    if verbose:
        for l in log: print("   " + l)
    return art

# ---------------------------------------------------------------- specs
SPEC_PANDOC = {
    "tool": "pandoc", "display_name": "Pandoc (конвертация документов)", "bin": "pandoc",
    "cands": ["/opt/homebrew/bin/pandoc", "/usr/local/bin/pandoc"],
    "verify": ["--version"],
    "install": {"mode": "pip_binary", "pip_pkg": "pypandoc_binary", "pymod": "pypandoc", "path_expr": "M.get_pandoc_path()"},
    "operations": [
        {"op": "md_to_docx", "display": "Markdown → Word", "argv": ["{input}", "-o", "{output}"],
         "params": [], "out_suffix": ".docx", "timeout": 120, "keep_smaller": False, "input_ext": ".md"},
        {"op": "md_to_html", "display": "Markdown → HTML", "argv": ["{input}", "-s", "-o", "{output}"],
         "params": [], "out_suffix": ".html", "timeout": 120, "keep_smaller": False, "input_ext": ".md"},
    ],
    "aisle": "docs",
    "card": {"title": "Документы из Markdown", "one_liner": "Собрать Word/HTML из шаблона и данных — локально, пачкой"},
}

SPEC_GHOSTSCRIPT = {
    "tool": "ghostscript", "display_name": "Ghostscript (сжатие PDF)", "bin": "gs",
    "cands": ["/opt/homebrew/bin/gs", "/usr/local/bin/gs", "/opt/local/bin/gs", "/usr/bin/gs"],
    "verify": ["--version"],
    "install": {"mode": "brew_abs", "brew": "ghostscript"},
    "operations": [
        {"op": "compress_pdf", "display": "Сжать PDF",
         "argv": ["-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.4", "-dPDFSETTINGS=/{quality}",
                  "-dNOPAUSE", "-dBATCH", "-dQUIET", "-dSAFER", "-sOutputFile={output}", "{input}"],
         "params": [{"name": "quality", "enum": ["screen", "ebook", "printer", "prepress"], "default": "ebook"}],
         "out_suffix": "_compressed.pdf", "timeout": 120, "keep_smaller": True, "input_ext": ".pdf"},
    ],
    "aisle": "docs",
    "card": {"title": "Сжать PDF", "one_liner": "Ужать PDF на 50–70% — локально, файлы не уходят"},
}

SPEC_IMAGEMAGICK = {
    "tool": "imagemagick", "display_name": "ImageMagick (пакет картинок)", "bin": "magick",
    "cands": ["/opt/homebrew/bin/magick", "/usr/local/bin/magick"],
    "verify": ["--version"],
    "install": {"mode": "brew_abs", "brew": "imagemagick"},
    "operations": [
        {"op": "to_jpg", "display": "В JPG", "argv": ["{input}", "{output}"],
         "params": [], "out_suffix": ".jpg", "timeout": 120, "keep_smaller": False, "input_ext": ".png"},
        {"op": "resize", "display": "Уменьшить", "argv": ["{input}", "-resize", "{size}", "{output}"],
         "params": [{"name": "size", "enum": ["25%", "50%", "75%"], "default": "50%"}],
         "out_suffix": "_small.jpg", "timeout": 120, "keep_smaller": False, "input_ext": ".jpg"},
    ],
    "aisle": "media",
    "card": {"title": "Пакет картинок", "one_liner": "Размер и формат тысяч изображений разом — локально"},
}

SPEC_QPDF = {
    "tool": "qpdf", "display_name": "qpdf (структура PDF)", "bin": "qpdf",
    "cands": ["/opt/homebrew/bin/qpdf", "/usr/local/bin/qpdf"],
    "verify": ["--version"],
    "install": {"mode": "brew_abs", "brew": "qpdf"},
    "operations": [
        {"op": "optimize", "display": "Оптимизировать для веба", "argv": ["--linearize", "{input}", "{output}"],
         "params": [], "out_suffix": "_web.pdf", "timeout": 120, "keep_smaller": False, "input_ext": ".pdf"},
        {"op": "rotate", "display": "Повернуть", "argv": ["--rotate={angle}", "{input}", "{output}"],
         "params": [{"name": "angle", "enum": ["+90", "+180", "+270", "-90"], "default": "+90"}],
         "out_suffix": "_rotated.pdf", "timeout": 120, "keep_smaller": False, "input_ext": ".pdf"},
    ],
    "aisle": "docs",
    "card": {"title": "Повернуть и оптимизировать PDF", "one_liner": "Поворот и веб-оптимизация PDF без потери качества"},
}

_TDATA = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/"
SPEC_OCR = {
    "tool": "ocr", "display_name": "OCR (поиск по сканам)", "bin": "ocrmypdf", "cands": [],
    "verify": ["--version"],
    "install": {"mode": "composite", "brew": ["tesseract"], "pip": ["ocrmypdf"], "driver": "ocrmypdf",
                "path_add": ["/opt/homebrew/bin"],
                "tessdata": {"dirs": ["/opt/homebrew/share/tessdata", "/usr/local/share/tessdata"],
                             "files": {"rus": _TDATA + "rus.traineddata"}}},
    "run_env": {"path_add": ["/opt/homebrew/bin"]},
    "operations": [
        {"op": "searchable", "display": "Скан → PDF с поиском",
         "argv": ["-l", "{lang}", "--skip-text", "--output-type", "pdf", "{input}", "{output}"],
         "params": [{"name": "lang", "enum": ["rus+eng", "rus", "eng"], "default": "rus+eng"}],
         "out_suffix": "_ocr.pdf", "timeout": 300, "keep_smaller": False, "input_ext": ".pdf"},
    ],
    "aisle": "docs",
    "card": {"title": "Поиск по сканам (OCR)", "one_liner": "Сканы и фото-PDF → документы с полнотекстовым поиском"},
}

SPEC_FFMPEG = {
    "tool": "ffmpeg", "display_name": "ffmpeg (видео и аудио)", "bin": "ffmpeg",
    "cands": ["/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"],
    "verify": ["-version"],
    "install": {"mode": "brew_abs", "brew": "ffmpeg"},
    "operations": [
        {"op": "extract_audio", "display": "Извлечь аудио (MP3)",
         "argv": ["-y", "-i", "{input}", "-vn", "-acodec", "libmp3lame", "{output}"],
         "params": [], "out_suffix": ".mp3", "timeout": 300, "keep_smaller": False, "input_ext": ".mp4"},
        {"op": "to_mp4", "display": "В MP4 (H.264)",
         "argv": ["-y", "-i", "{input}", "-c:v", "libx264", "-preset", "fast", "-c:a", "aac", "{output}"],
         "params": [], "out_suffix": "_h264.mp4", "timeout": 300, "keep_smaller": False, "input_ext": ".mov"},
    ],
    "aisle": "media",
    "card": {"title": "Видео и аудио", "one_liner": "Перекодировать, сжать и извлечь аудио из медиатеки"},
}

SPEC_LIBREOFFICE = {
    "tool": "libreoffice", "display_name": "LibreOffice (Office → PDF)", "bin": "soffice",
    "cands": ["/Applications/LibreOffice.app/Contents/MacOS/soffice", "/opt/homebrew/bin/soffice", "/usr/bin/soffice"],
    "verify": ["--version"],
    "install": {"mode": "brew_abs", "brew": "libreoffice", "cask": True, "timeout": 900},
    "operations": [
        {"op": "to_pdf", "display": "Office → PDF",
         "argv": ["--headless", "--convert-to", "pdf", "--outdir", "{outdir}",
                  "-env:UserInstallation=file://{profile}", "{input}"],
         "params": [], "out_suffix": ".pdf", "out_mode": "dir", "timeout": 180, "keep_smaller": False, "input_ext": ".docx"},
    ],
    "aisle": "docs",
    "card": {"title": "Office → PDF", "one_liner": "Word, Excel, PowerPoint → PDF целыми папками", "emoji": "🗂️",
             "howto": "Скажи: «переведи все документы в этой папке в PDF»."},
}

SPEC_IMG2PDF = {
    "tool": "img2pdf", "display_name": "img2pdf (картинки → PDF)", "bin": "img2pdf", "cands": [],
    "verify": ["--version"],
    "install": {"mode": "composite", "brew": [], "pip": ["img2pdf"], "driver": "img2pdf", "path_add": []},
    "operations": [
        {"op": "to_pdf", "display": "Картинка → PDF", "argv": ["{input}", "-o", "{output}"],
         "params": [], "out_suffix": ".pdf", "timeout": 120, "keep_smaller": False, "input_ext": ".png"},
    ],
    "aisle": "docs",
    "card": {"title": "Картинки → PDF", "one_liner": "Собрать PDF из картинок и сканов — локально", "emoji": "📎",
             "howto": "Скажи: «собери PDF из этих картинок» — вернёт один PDF."},
}

ALL_SPECS = [SPEC_GHOSTSCRIPT, SPEC_PANDOC, SPEC_QPDF, SPEC_IMAGEMAGICK, SPEC_OCR, SPEC_FFMPEG, SPEC_LIBREOFFICE, SPEC_IMG2PDF]

if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "dryrun"
    if mode == "dryrun":
        art = build_capability(SPEC_PANDOC)
        print("EXPERTS:", list(art["experts"].keys()))
        print("KV:", list(art["kv"].keys()))
        print("\n--- cap_pandoc_md_to_docx ---\n", art["experts"]["cap_pandoc_md_to_docx"])
        print("\n--- resolver ---\n", art["experts"]["cap_pandoc_resolver"][:600])
