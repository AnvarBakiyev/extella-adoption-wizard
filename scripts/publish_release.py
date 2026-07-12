#!/usr/bin/env python3
"""Публикация ПОДПИСАННОГО релиза моста Визарда в канал обновлений (KV rel:bridge:*).

DEV-инструмент (только у нас): берёт живые app/server.py + app/wizard.html, режет на чанки
base64 в KV, собирает manifest {version, files:{name:{sha256,chunks,bytes}}} и ПОДПИСЫВАЕТ его
приватным ключом Ed25519 (~/.extella_release_key). Мост применяет обновление ТОЛЬКО с валидной
подписью (публичный ключ вшит в server.py) — общий KV не даёт подсунуть чужой код (RCE-защита).
Версия берётся из BRIDGE_VERSION в server.py. Использование: python3 publish_release.py
"""
import json, re, urllib.request, base64, hashlib, time
from datetime import datetime, timezone
from pathlib import Path
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization

BASE = "https://api.extella.ai"
REL = "rel:bridge"
CHUNK = 8000
APP = Path.home() / "extella_wizard" / "app"
KEYFILE = Path.home() / ".extella_release_key"

token = re.search(r'AUTH_TOKEN\s*=\s*"([^"]+)"', (Path.home() / ".claude/extella_mcp_server.py").read_text()).group(1)
H = {"X-Auth-Token": token, "Content-Type": "application/json", "X-Profile-Id": "default", "X-Agent-Id": "agent_extella_default"}


def api(ep, p, t=30):
    for _ in range(4):
        try:
            req = urllib.request.Request(BASE + ep, data=json.dumps(p).encode(), headers=H, method="POST")
            with urllib.request.urlopen(req, timeout=t) as r:
                return json.loads(r.read().decode())
        except Exception as e:
            last = str(e)[:80]; time.sleep(1)
    return {"_err": last}


def publish():
    priv = serialization.load_pem_private_key(KEYFILE.read_bytes(), password=None)
    srv = (APP / "server.py").read_bytes()
    html = (APP / "wizard.html").read_bytes()
    version = re.search(rb'BRIDGE_VERSION = "([^"]+)"', srv).group(1).decode()
    files = {}
    # многофайл (Фаза 1): все .py моста (server.py + wz_platform.py + будущие модули) + wizard.html.
    # config.json НЕ шлём (секрет; не .py). Мост применяет и откатывает весь набор из манифеста.
    bundle = [(p.name, p.read_bytes()) for p in sorted(APP.glob("*.py"))] + [("wizard.html", html)]
    for name, raw in bundle:
        b64 = base64.b64encode(raw).decode()
        parts = [b64[i:i + CHUNK] for i in range(0, len(b64), CHUNK)]
        try:
            old_n = int(json.loads(api("/api/kv/get", {"key": f"{REL}:{name}:meta"}).get("value") or "{}").get("chunks", 0))
        except Exception:
            old_n = 0
        for i, pt in enumerate(parts):
            for _ in range(3):
                if api("/api/kv/set", {"key": f"{REL}:{name}:{i}", "value": pt, "description": "rel"}).get("status") == "success":
                    break
        sha = hashlib.sha256(raw).hexdigest()
        api("/api/kv/set", {"key": f"{REL}:{name}:meta", "value": json.dumps({"chunks": len(parts), "bytes": len(raw), "sha256": sha}), "description": "rel"})
        for i in range(len(parts), old_n):
            api("/api/kv/remove", {"key": f"{REL}:{name}:{i}"})
        files[name] = {"sha256": sha, "chunks": len(parts), "bytes": len(raw)}
        print(f"  {name}: {len(parts)} чанков, sha256 {sha[:12]}")
    # подписанный manifest — единственный источник доверия версии и sha256
    manifest = json.dumps({"version": version, "files": files}, sort_keys=True)
    sig = base64.b64encode(priv.sign(manifest.encode("utf-8"))).decode()
    api("/api/kv/set", {"key": f"{REL}:meta",
                        "value": json.dumps({"version": version, "published_at": datetime.now(timezone.utc).isoformat(),
                                             "manifest": manifest, "sig": sig}),
                        "description": "release signed"})
    print(f"опубликован ПОДПИСАННЫЙ релиз моста v{version}")


def halt():
    """Стоп-кран: подписанный halt — клиенты перестают применять релиз (отзыв плохой публикации).
    Снять halt = обычная publish() новой хорошей версии."""
    priv = serialization.load_pem_private_key(KEYFILE.read_bytes(), password=None)
    cur = json.loads(api("/api/kv/get", {"key": f"{REL}:meta"}).get("value") or "{}")
    ver = cur.get("version", "0")
    manifest = json.dumps({"version": ver, "disabled": True}, sort_keys=True)
    sig = base64.b64encode(priv.sign(manifest.encode("utf-8"))).decode()
    api("/api/kv/set", {"key": f"{REL}:meta",
                        "value": json.dumps({"version": ver, "disabled": True, "published_at": datetime.now(timezone.utc).isoformat(),
                                             "manifest": manifest, "sig": sig}),
                        "description": "release HALTED"})
    print(f"СТОП-КРАН: канал релизов остановлен (v{ver}). Снять — publish() новой версии.")


if __name__ == "__main__":
    import sys
    if "--halt" in sys.argv:
        halt()
    else:
        publish()
