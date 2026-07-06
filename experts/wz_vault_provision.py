# expert: wz_vault_provision
# description: provision vault.key on hosting device from client PIN (PBKDF2); returns key sha256 for cross-device match check, never the key/pin
# params: 

$extens("include.py")
def wz_vault_provision(pin: str = "", client: str = "default") -> dict:
    """Провижининг vault-ключа на устройстве-хостинге из PIN (тот же вывод, что на маке): PBKDF2-HMAC-SHA256,
    600k, per-client соль. Пишет vault.key локально, возвращает sha256(key)[:16] для сверки (НЕ сам ключ/PIN)."""
    import hashlib, base64, socket, os
    from pathlib import Path
    if not pin or len(pin) < 6:
        return {"host": socket.gethostname(), "ok": False, "err": "pin too short"}
    salt = hashlib.sha256(("extella-vault:" + str(client)).encode("utf-8")).digest()
    dk = hashlib.pbkdf2_hmac("sha256", str(pin).encode("utf-8"), salt, 600000, dklen=32)
    key = base64.urlsafe_b64encode(dk)
    cands = ["/opt/extella-listener/extella_wizard/vault.key", str(Path.home()/"extella_wizard/app/vault.key"), str(Path.cwd()/"extella_wizard/vault.key")]
    written = None
    for c in cands:
        try:
            Path(c).parent.mkdir(parents=True, exist_ok=True)
            Path(c).write_bytes(key)
            try: os.chmod(c, 0o600)
            except Exception: pass
            written = c; break
        except Exception:
            continue
    return {"host": socket.gethostname(), "ok": bool(written), "vault_path": written, "key_sha256": hashlib.sha256(key).hexdigest()[:16]}