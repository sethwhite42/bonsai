"""cachecheck - how warm is each Claude Code session's prompt cache?

A session's cache expires TTL minutes after its last model call (1h normally,
5 min once you're in extra usage). The clock is the timestamp of the last
model reply in the transcript, not the file's mtime: running /cachecheck inside
a session writes the command to the transcript first, which would otherwise
look like fresh activity. Subagents have their own clocks.

  python cachecheck.py            sessions active in the last 24h
  python cachecheck.py --ttl 5    if you've gone into extra usage
  python cachecheck.py --hours 72 --all-agents
"""
import argparse, glob, json, os, sys, time

from common import PROJECTS as ROOT

ap = argparse.ArgumentParser()
ap.add_argument("--ttl", type=float, default=60, help="cache TTL in minutes (default 60)")
ap.add_argument("--hours", type=float, default=24, help="look-back window (default 24)")
ap.add_argument("-n", type=int, default=10, help="max sessions to show")
ap.add_argument("--all-agents", action="store_true", help="list cold subagents too")
ap.add_argument("--json", action="store_true", help="JSON of warm sessions (for the panel)")
ap.add_argument("--warm", action="store_true",
                help="markdown: only warm sessions, the ones about to go cold in their own group")
a = ap.parse_args()
sys.stdout.reconfigure(encoding="utf-8")

now = time.time()


def title(path):
    return info(path)[0]


def info(path):
    """Last custom-title in the transcript, else the app's own placeholder for an
    untitled session (the desktop sidebar shows "General coding session"). Sessions an
    outside program drives through `claude -p` (entrypoint "sdk-cli") are named after their
    working folder instead. Returns (title, external)."""
    t, sdk, cwd = None, False, ""
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            if '"custom-title"' in line:
                try:
                    t = json.loads(line).get("customTitle") or t
                except Exception:
                    pass
            elif not cwd and '"cwd":"' in line:
                try:
                    d = json.loads(line)
                    cwd, sdk = d.get("cwd", ""), d.get("entrypoint") == "sdk-cli"
                except Exception:
                    pass
    if not t and sdk:
        t = os.path.basename(cwd.rstrip(chr(92) + "/")) or cwd
    return (t or "General coding session").replace("\n", " ")[:60], sdk


def last_call(path):
    """Time of the last model reply (assistant message with usage); mtime if none found."""
    from datetime import datetime
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        for chunk in (4_000_000, size):
            f.seek(max(0, size - chunk))
            for raw in reversed(f.read().splitlines()):
                if b'"usage"' not in raw or b'"assistant"' not in raw:
                    continue
                try:
                    d = json.loads(raw)
                    if d.get("type") == "assistant" and d.get("timestamp"):
                        return datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).timestamp()
                except Exception:
                    pass
            if chunk >= size:
                break
    return os.path.getmtime(path)


def context_size(path):
    """Context tokens at the last main-thread reply (input + cache read + cache write)."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        f.seek(max(0, f.tell() - 4_000_000))
        lines = f.read().splitlines()
    for raw in reversed(lines):
        if b'"compact_boundary"' in raw:
            return -1   # compacted since its last reply: the old size no longer applies
        if b'"usage"' not in raw or b'"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        u = (d.get("message") or {}).get("usage") or {}
        n = sum(u.get(k) or 0 for k in ("input_tokens", "cache_read_input_tokens", "cache_creation_input_tokens"))
        if n:
            return n
    return None


def status(t):
    age = (now - t) / 60
    left = a.ttl - age
    if left <= 0:
        s = "COLD"
    elif left < a.ttl * 0.25:
        s = f"warm, {left:.0f}m left  <- step away / compact now"
    else:
        s = f"warm, {left:.0f}m left"
    return age, s


def ago(m):
    return f"{m:.0f}m ago" if m < 90 else f"{m / 60:.1f}h ago"


sessions = [p for p in glob.glob(os.path.join(ROOT, "*", "*.jsonl"))
            if now - os.path.getmtime(p) < a.hours * 3600]
last = {p: last_call(p) for p in sessions}
sessions.sort(key=last.get, reverse=True)

if a.json:   # for the bonsai mod: warm sessions only
    out = []
    for p in sessions:
        left = a.ttl - (now - last[p]) / 60
        if left <= 0:
            continue
        agents = []
        for ag in glob.glob(os.path.join(p[:-6], "subagents", "*.jsonl")):
            aleft = a.ttl - (now - last_call(ag)) / 60
            if aleft > 0:
                agents.append({"id": os.path.basename(ag)[6:20], "left": round(aleft, 1),
                               "kind": "compaction" if "acompact" in ag else "subagent"})
        t, ext = info(p)
        out.append({"sid": os.path.basename(p)[:-6], "title": t, "left": round(left, 1),
                    "ctx": context_size(p), "agents": agents, "external": ext})
    print(json.dumps({"ttl": a.ttl, "sessions": out}))
    sys.exit()

if a.warm:
    soon, warm = [], []
    for p in sessions:
        left = a.ttl - (now - last[p]) / 60
        if left <= 0:
            continue
        line = f"- **{title(p)}**: {left:.0f} min left"
        for ag in glob.glob(os.path.join(p[:-6], "subagents", "*.jsonl")):
            aleft = a.ttl - (now - last_call(ag)) / 60
            if aleft > 0:
                kind = "compaction" if "acompact" in ag else "subagent"
                line += f"\n  - {kind} `{os.path.basename(ag)[6:20]}`: {aleft:.0f} min left"
        (soon if left < a.ttl * 0.25 else warm).append((left, line))
    if soon:
        print(f"> ### 🟧🟧 Going cold in under {a.ttl * 0.25:.0f} min 🟧🟧\n>")
        for _, line in sorted(soon):
            print("> " + line.replace("\n", "\n> "))
        print()
    if warm:
        print("### 🟩 Warm")
        for _, line in sorted(warm):
            print(line)
    if not soon and not warm:
        print("No other sessions are warm.")
    print(f"\n_TTL assumed {a.ttl:g} min; cold sessions hidden._")
    sys.exit()

if not sessions:
    print(f"No sessions active in the last {a.hours:g}h.")
for p in sessions[: a.n]:
    age, s = status(last[p])
    print(f"{title(p)}\n  last active {ago(age)} | {s}")
    agents = glob.glob(os.path.join(p[:-6], "subagents", "*.jsonl"))
    agents = [(last_call(ag), ag) for ag in agents if now - os.path.getmtime(ag) < a.hours * 3600
              or a.all_agents]
    agents.sort(reverse=True)
    for t, ag in agents:
        aage, astat = status(t)
        if aage > a.ttl and not a.all_agents:
            continue
        kind = "compaction" if "acompact" in ag else "subagent"
        print(f"    {kind} {os.path.basename(ag)[6:20]}  {ago(aage)} | {astat}")
print(f"\n(TTL assumed {a.ttl:g} min; use --ttl 5 once you're into extra usage.)")
