"""Shared paths, thresholds and the idle check for the bonsai scripts."""
import os
import subprocess
import sys

# Claude Code's config folder (CLAUDE_CONFIG_DIR when set, as Claude Code itself does)
CLAUDE_DIR = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.join(os.path.expanduser("~"), ".claude")
PROJECTS = os.path.join(CLAUDE_DIR, "projects")        # session transcripts
STATE = os.path.join(CLAUDE_DIR, "hooks-state")        # prefs, logs, prune requests, savings ledger

# context-size nudge: first at FIRST tokens, again every STEP more (ctxnudge.py and the panel)
FIRST, STEP = 150_000, 75_000


def pc_idle_s():
    """Seconds since the last keyboard/mouse input, or None where it can't be read
    (then the idle trigger stays off and only the cache-expiry trigger prunes)."""
    try:
        if sys.platform == "win32":
            import ctypes

            class LII(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
            lii = LII(cbSize=ctypes.sizeof(LII))
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return None
            return ((ctypes.windll.kernel32.GetTickCount() - lii.dwTime) & 0xFFFFFFFF) / 1000
        if sys.platform == "darwin":
            out = subprocess.run(["ioreg", "-c", "IOHIDSystem", "-d", "4"],
                                 capture_output=True, text=True, timeout=5).stdout
            for ln in out.splitlines():
                if "HIDIdleTime" in ln:
                    return int(ln.rsplit("=", 1)[1].strip()) / 1e9
            return None
        # Linux/X11: xprintidle, if installed (Wayland has no common equivalent)
        out = subprocess.run(["xprintidle"], capture_output=True, text=True, timeout=5)
        return int(out.stdout.strip()) / 1000 if out.returncode == 0 else None
    except Exception:
        return None


if __name__ == "__main__":   # the hooks module asks where the state folder is
    import json
    os.makedirs(STATE, exist_ok=True)
    print(json.dumps({"state": STATE.replace(os.sep, "/")}))
