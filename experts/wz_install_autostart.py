# expert: wz_install_autostart
# description: Шаг 4 онбординга: ставит мост визарда как АВТОЗАПУСКАемый сервис на ЭТОМ устройстве (автостарт при входе + автоперезапуск при падении). macOS → Launch
# params: app_dir, port, label

$extens("include.py")

def wz_install_autostart(app_dir: str = "", port: int = 8765,
                         label: str = "ai.extella.wizard-bridge") -> dict:
    """Шаг 4 онбординга: ставит мост визарда как АВТОЗАПУСКАемый сервис на ЭТОМ устройстве
    (автостарт при входе + автоперезапуск при падении). macOS → LaunchAgent (launchd);
    Linux → systemd --user (+enable-linger, чтобы жил после логаута). Идемпотентно (перезагружает).
    Мост single-instance, параллельные запуски безопасны. Возвращает {ok, method, detail}.
    label/app_dir/port настраиваемы (для изолированного теста без клоббера живого моста)."""
    import sys
    import os
    import socket
    import subprocess
    from pathlib import Path

    app = Path(app_dir) if app_dir else (Path.home() / "extella_wizard" / "app")
    server = app / "server.py"
    if not server.exists():
        return {"ok": False, "err": "server.py не найден в " + str(app) + " — сначала wz_wizard_serve", "host": socket.gethostname()}
    py = sys.executable or "python3"
    plat = sys.platform

    if plat == "darwin":
        la = Path.home() / "Library" / "LaunchAgents"
        la.mkdir(parents=True, exist_ok=True)
        plist = la / (label + ".plist")
        content = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
            '<plist version="1.0"><dict>\n'
            '  <key>Label</key><string>' + label + '</string>\n'
            '  <key>ProgramArguments</key><array><string>' + py + '</string><string>' + str(server) + '</string></array>\n'
            '  <key>WorkingDirectory</key><string>' + str(app) + '</string>\n'
            '  <key>EnvironmentVariables</key><dict><key>EXTELLA_BRIDGE_OWNER</key><string>launchd</string></dict>\n'
            '  <key>RunAtLoad</key><true/>\n'
            '  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>\n'
            '  <key>ThrottleInterval</key><integer>3</integer>\n'
            '  <key>StandardOutPath</key><string>' + str(app / "bridge.log") + '</string>\n'
            '  <key>StandardErrorPath</key><string>' + str(app / "bridge.err") + '</string>\n'
            '</dict></plist>\n'
        )
        plist.write_text(content, encoding="utf-8")
        uid = os.getuid()
        subprocess.run(["launchctl", "bootout", "gui/%d/%s" % (uid, label)], capture_output=True)
        r = subprocess.run(["launchctl", "bootstrap", "gui/%d" % uid, str(plist)], capture_output=True, text=True)
        if r.returncode != 0:
            subprocess.run(["launchctl", "load", "-w", str(plist)], capture_output=True)
        return {"ok": True, "method": "launchd", "plist": str(plist), "host": socket.gethostname()}

    if plat.startswith("linux"):
        sd = Path.home() / ".config" / "systemd" / "user"
        sd.mkdir(parents=True, exist_ok=True)
        svc = label.replace("ai.extella.", "extella-").replace(".", "-") + ".service"
        unit = sd / svc
        unit.write_text(
            "[Unit]\nDescription=Extella Wizard Bridge\n"
            "[Service]\nType=simple\n"
            "ExecStart=" + py + " " + str(server) + "\n"
            "WorkingDirectory=" + str(app) + "\n"
            "Environment=EXTELLA_BRIDGE_OWNER=systemd\n"
            "Restart=on-failure\nRestartSec=3\n"
            "[Install]\nWantedBy=default.target\n", encoding="utf-8")
        try:
            subprocess.run(["loginctl", "enable-linger", os.environ.get("USER", "")], capture_output=True)
        except Exception:
            pass
        subprocess.run(["systemctl", "--user", "daemon-reload"], capture_output=True)
        r = subprocess.run(["systemctl", "--user", "enable", "--now", svc], capture_output=True, text=True)
        ok = r.returncode == 0
        return {"ok": ok, "method": "systemd-user", "unit": str(unit),
                "detail": (r.stderr or r.stdout or "")[:150], "host": socket.gethostname()}

    return {"ok": False, "method": "none",
            "err": "автозапуск не поддержан для платформы " + plat + " — настройте вручную", "host": socket.gethostname()}
