"""growth - what grew a session's context, kept up to date incrementally.

panelview.py calls update(sid) every refresh for its own session. Each call reads
only the transcript bytes added since the last one, then saves the running totals
to ~/.claude/hooks-state/bonsai-growth-<sid>.json, where other sessions' panels
can read them too.

For every main-thread API call, the context growth since the previous call is
split across what came in between, in proportion to its estimated size
(chars/4, images ~1600 tokens): tool results by kind, the previous reply's
thinking, text and tool inputs, prompts, skill text, notices. Growth nothing
explains goes to "other". Totals restart at each compaction.

Saved fields: calls (since session start), seg = {t0, calls, floor, cats, big}
for the stretch since the last compaction (floor = context right after it),
series = [[epoch_s, ctx], ...] downsampled to at most SERIES_MAX points.

Usage: python growth.py <session-id>   (prints the saved summary)
"""
import glob, json, os, sys, time
from datetime import datetime

from common import PROJECTS, STATE

IMG = 1600
SERIES_MAX = 400
SEEN_MAX = 3000   # record ids remembered for de-duplication
LABEL = {   # category -> what the panel calls it
    "thinking": "thinking", "replies": "replies", "edits": "edits", "shell": "shell",
    "reads": "file reads", "images": "images", "search": "search", "web": "web",
    "subagents": "subagents", "skills": "skills", "prompts": "prompts",
    "notices": "notices", "tools": "other tools", "other": "unexplained",
}


def state_path(sid):
    return os.path.join(STATE, f"bonsai-growth-{sid}.json")


def transcript(sid):
    hits = glob.glob(os.path.join(PROJECTS, "*", f"{sid}.jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def ctx_of(u):
    return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
        + (u.get("cache_creation_input_tokens") or 0)


def tok(c):
    """(tokens, images) of message or tool_result content."""
    if c is None:
        return 0, 0
    if isinstance(c, str):
        return len(c) / 4, 0
    n = imgs = 0
    for b in c if isinstance(c, list) else [c]:
        if isinstance(b, dict) and b.get("type") == "image":
            imgs += 1
        elif isinstance(b, dict) and b.get("type") == "text":
            n += len(b.get("text") or "") / 4
        else:
            n += len(json.dumps(b) if isinstance(b, dict) else str(b)) / 4
    return n + imgs * IMG, imgs


def result_cat(name, imgs):
    if imgs:
        return "images"
    if name == "Read":
        return "reads"
    if name in ("Bash", "PowerShell"):
        return "shell"
    if name in ("Grep", "Glob"):
        return "search"
    if name in ("WebFetch", "WebSearch"):
        return "web"
    if name in ("Agent", "Task"):
        return "subagents"
    if name == "Skill":
        return "skills"
    return "tools"


def input_cat(name):
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "edits"
    if name in ("Bash", "PowerShell"):
        return "shell"
    if name in ("Agent", "Task"):
        return "subagents"
    return "tools"


def fresh():
    return {"v": 1, "seen": [], "offset": 0, "calls": 0, "prev_ctx": None, "last": None, "pending": {},
            "tools": {}, "series": [], "compacts": 0,
            "seg": {"t0": None, "calls": 0, "floor": None, "cats": {}, "big": None}}


def load_state(sid):
    try:
        with open(state_path(sid), encoding="utf-8") as f:
            s = json.load(f)
        return s if s.get("v") == 1 else fresh()
    except Exception:
        return fresh()


def add(d, k, n):
    if n > 0:
        d[k] = d.get(k, 0) + n


def close_call(s):
    """The previous API call's own output becomes pending growth for the next call."""
    c = s["last"]
    if not c:
        return
    vis = sum(c["vis"].values())
    add(s["pending"], "thinking", (c["out"] or 0) - vis)
    for k, n in c["vis"].items():
        add(s["pending"], k, n)
    s["last"] = None


def on_call(s, t, ctx):
    seg = s["seg"]
    s["calls"] += 1
    seg["calls"] += 1
    if seg["t0"] is None:
        seg["t0"] = t
    prev = s["prev_ctx"]
    if prev is None or seg["floor"] is None:
        seg["floor"] = ctx   # first call of the session or after a compaction: the base, not growth
    elif ctx > prev:
        delta = ctx - prev
        pend = {k: v for k, v in s["pending"].items() if v > 0}
        est = sum(pend.values())
        # chars/4 runs low (code and JSON tokenize nearer 3 chars a token), so when the
        # estimates cover a fair share of the jump they are scaled up to it; only a jump
        # they mostly fail to explain counts as unexplained
        scale = delta / est if est >= 0.25 * delta else 1.0 if est else 0
        top = None
        for k, v in pend.items():
            add(seg["cats"], k, round(v * scale))
            if not top or v > top[1]:
                top = (k, v)
        rest = delta - est * scale
        add(seg["cats"], "other", round(rest))
        cause = top[0] if top and top[1] * scale >= rest else "other"
        if not seg["big"] or delta > seg["big"]["tok"]:
            seg["big"] = {"tok": delta, "cat": cause, "t": t}
    s["pending"] = {}
    s["prev_ctx"] = ctx
    ser = s["series"]
    ser.append([int(t), ctx])
    if len(ser) > SERIES_MAX:
        s["series"] = ser[:-1][::2] + [ser[-1]]


def on_line(s, d):
    ty = d.get("type")
    if d.get("isSidechain") or not d.get("timestamp"):
        return
    # a resumed session can repeat earlier records in the same file: count each once
    uid = d.get("uuid")
    if uid:
        if uid in s["seen_set"]:
            return
        s["seen_set"].add(uid)
        s["seen"].append(uid)
    t = ts(d["timestamp"])
    if ty == "system" and d.get("subtype") == "compact_boundary":
        close_call(s)
        s["compacts"] += 1
        s["prev_ctx"] = None
        s["pending"] = {}
        s["seg"] = {"t0": t, "calls": 0, "floor": None, "cats": {}, "big": None}
        s["series"].append([int(t), -1])   # marks the compaction in the series
        return
    if ty == "assistant":
        m = d.get("message") or {}
        u, mid = m.get("usage"), m.get("id")
        if not u or not mid:
            return
        c = s["last"]
        if not c or c["id"] != mid:
            if not ctx_of(u):
                return
            close_call(s)
            on_call(s, t, ctx_of(u))
            c = s["last"] = {"id": mid, "out": 0, "vis": {}}
        c["out"] = max(c["out"], u.get("output_tokens") or 0)
        for b in m.get("content") or []:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "text":
                add(c["vis"], "replies", len(b.get("text") or "") / 4)
            elif b.get("type") == "tool_use":
                name = b.get("name") or "?"
                add(c["vis"], input_cat(name), len(json.dumps(b.get("input") or {})) / 4)
                s["tools"][b.get("id")] = name
        return
    if ty == "user":
        cont = (d.get("message") or {}).get("content")
        if isinstance(cont, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in cont):
            for b in cont:
                if not isinstance(b, dict):
                    continue
                if b.get("type") == "tool_result":
                    n, im = tok(b.get("content"))
                    add(s["pending"], result_cat(s["tools"].pop(b.get("tool_use_id"), "?"), im), n)
                else:
                    n, im = tok([b])
                    add(s["pending"], "images" if im else "notices", n)
            return
        n, im = tok(cont)
        head = (cont if isinstance(cont, str) else
                "".join(b.get("text") or "" for b in cont or [] if isinstance(b, dict)))[:400]
        if "Base directory for this skill" in head:
            add(s["pending"], "skills", n)
        elif d.get("isMeta") or d.get("isCompactSummary") or head.startswith("<"):
            add(s["pending"], "notices", n)
        else:
            add(s["pending"], "images" if im else "prompts", n)
        return
    if ty == "attachment":
        a = d.get("attachment") or {}
        if a.get("type") not in ("prompt_snapshot",):
            add(s["pending"], "notices", len(json.dumps(a)) / 4)


def update(sid):
    """Read what the transcript gained since last time; return the state (None if no transcript)."""
    path = transcript(sid)
    if not path:
        return None
    s = load_state(sid)
    s.setdefault("seen", [])
    size = os.path.getsize(path)
    if s.get("path") != path or s["offset"] > size:
        s = fresh()
    s["path"] = path
    s["seen_set"] = set(s.get("seen", []))
    if size > s["offset"]:
        with open(path, "rb") as f:
            f.seek(s["offset"])
            data = f.read()
        end = data.rfind(b"\n") + 1   # only whole lines; a half-written one waits for next time
        for raw in data[:end].splitlines():
            try:
                on_line(s, json.loads(raw.decode("utf-8", "replace")))
            except Exception:
                continue
        s["offset"] += end
        if len(s["tools"]) > 500:   # results that never came back
            s["tools"] = dict(list(s["tools"].items())[-200:])
        s["seen"] = s["seen"][-SEEN_MAX:]
        s["at"] = time.time()
        tmp = state_path(sid) + ".tmp"
        os.makedirs(STATE, exist_ok=True)
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({k: v for k, v in s.items() if k != "seen_set"}, f)
        os.replace(tmp, state_path(sid))
    return s


def peek(sid):
    """Another session's saved state, without reading its transcript."""
    try:
        with open(state_path(sid), encoding="utf-8") as f:
            s = json.load(f)
        return s if s.get("v") == 1 else None
    except Exception:
        return None


def top_cats(s, n=3):
    cats = s["seg"]["cats"]
    tot = sum(cats.values())
    if not tot:
        return [], 0
    rows = sorted(cats.items(), key=lambda kv: -kv[1])[:n]
    return [(LABEL.get(k, k), v, v / tot) for k, v in rows], tot


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(s, width=24, hours=None):
    """Context over time since the last compaction (or the last `hours`), as block characters."""
    pts = [p for p in s["series"] if p[1] > 0]
    cut = max((p[0] for p in s["series"] if p[1] == -1), default=0)
    if hours:
        cut = max(cut, time.time() - hours * 3600)
    pts = [p for p in pts if p[0] >= cut]
    if len(pts) < 2:
        return ""
    step = max(1, len(pts) // width)
    vals = [max(p[1] for p in pts[i:i + step]) for i in range(0, len(pts), step)][-width:]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    return "".join(SPARK[min(7, int((v - lo) / span * 7.999))] for v in vals)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sid = sys.argv[1] if len(sys.argv) > 1 else ""
    s = update(sid)
    if not s:
        print("no transcript for", sid)
        sys.exit(1)
    cats, tot = top_cats(s, 20)
    print(f"calls {s['calls']}  compacts {s['compacts']}  since last: {s['seg']['calls']} calls, "
          f"floor {s['seg']['floor']}, grown {round(tot / 1000)}k")
    for name, v, frac in cats:
        print(f"  {name:12} {round(v / 1000):>5}k  {100 * frac:4.0f}%")
    print("  biggest jump:", s["seg"]["big"])
    print("  ", sparkline(s, 40))
