# expert: wz_ping
# description: Эксперт wz_ping (Adoption Wizard).
# params: 

$extens("include.py")

def wz_ping() -> dict:
    import platform
    import socket
    import getpass
    from datetime import datetime
    return {"status": "success",
            "host": socket.gethostname(),
            "platform": platform.platform(),
            "user": getpass.getuser(),
            "local_time": datetime.now().isoformat()}
