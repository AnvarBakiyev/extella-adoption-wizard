# expert: wz_wizard_stop
# description: wz_wizard_stop
# params:

def wz_wizard_stop(app_dir: str = "") -> dict:
    import os, time
    from pathlib import Path
    app = Path(app_dir) if app_dir else Path.home() / "extella_wizard" / "app"
    pidfile = app / "server.pid"
    if not pidfile.exists():
        return {"status": "success", "message": "no pidfile - bridge is not running (or was started manually)"}
    try:
        pid = int(pidfile.read_text().strip())
        os.kill(pid, 15)
        time.sleep(1)
        try:
            os.kill(pid, 0)
            os.kill(pid, 9)
        except ProcessLookupError:
            pass
        pidfile.unlink()
        return {"status": "success", "stopped_pid": pid}
    except ProcessLookupError:
        pidfile.unlink()
        return {"status": "success", "message": "process already gone, pidfile removed"}
    except Exception as e:
        return {"status": "error", "message": str(e)[:200]}
