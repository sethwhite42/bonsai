"""leanprune - the one-step prune: the compact writes the handoff itself.

A full prune spends a whole model turn writing the handoff (3 calls in a 2026-10 measurement,
44% of the prune's cost) before the compact re-reads the same context to summarise it. The
lean prune asks the compact to write the handoff as its summary, then this script copies
that summary out of the transcript into ~/.claude/handoffs/<task>.md. No model call.

  python leanprune.py prep --session SID   -> {"lean": bool, "warm": [...], "handoffs": dir}
      lean is false when a subagent of this session was active in the last WARM_S seconds:
      it may hold unfinished work, and only a model turn can ask it for a handoff.
  python leanprune.py save --session SID --since MS   -> {"path": ...} or {"error": ...}
      writes the newest compact summary after MS (epoch ms) to the handoff file, and logs
      the prune in lean-prunes.jsonl for prunestats.py.
"""
import argparse, glob, json, os, re, sys, time
from datetime import datetime

from common import CLAUDE_DIR, PROJECTS, STATE

HANDOFFS = os.path.join(CLAUDE_DIR, "handoffs")
WARM_S = 45 * 60
LEDGER = os.path.join(STATE, "lean-prunes.jsonl")
HEAD = "This session is being continued"


def transcript(sid):
    hits = glob.glob(os.path.join(PROJECTS, "*", sid + ".jsonl"))
    return max(hits, key=os.path.getmtime) if hits else None


def prep(sid):
    t = transcript(sid)
    now = time.time()
    warm = []
    if t:
        for p in glob.glob(os.path.join(t[:-6], "subagents", "*.jsonl")):
            name = os.path.basename(p)[:-6]
            if name.startswith("agent-acompact"):
                continue   # Claude Code's own compaction runs
            age = now - os.path.getmtime(p)
            if age < WARM_S:
                warm.append({"agent": name, "min_ago": round(age / 60)})
    return {"lean": not warm, "warm": warm, "handoffs": HANDOFFS.replace(os.sep, "/")}


def ts_ms(s):
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp() * 1000
    except Exception:
        return 0


def summary_after(path, since):
    best = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for ln in f:
            if '"isCompactSummary"' not in ln:
                continue
            try:
                d = json.loads(ln)
            except Exception:
                continue
            if not d.get("isCompactSummary") or ts_ms(d.get("timestamp") or "") < since:
                continue
            c = (d.get("message") or {}).get("content")
            if isinstance(c, list):
                c = "".join(b.get("text", "") for b in c if isinstance(b, dict))
            best = (d.get("timestamp"), str(c or ""))
    return best


def body_of(text):
    """The summary itself, without Claude Code's wrapper lines before and after it."""
    if text.startswith(HEAD):
        i = text.find("Summary:")
        text = text[i + len("Summary:"):] if i >= 0 else text
    for tail in ("\nIf you need specific details from before compaction",
                 "\nContinue the conversation from where it left off"):
        j = text.find(tail)
        if j >= 0:
            text = text[:j]
    return text.strip()


def handoff_path(body, sid):
    """The path the summary names on its `Handoff:` line, if it's in the handoffs folder."""
    m = re.search(r"Handoff:\s*`?([^`\n]+?\.md)`?\s*$", body, re.M)
    if m:
        p = os.path.normpath(m.group(1).strip())
        if os.path.dirname(p).lower() == os.path.normpath(HANDOFFS).lower():
            return p
        name = re.sub(r"[^A-Za-z0-9_-]", "", os.path.basename(p)[:-3])
        if name:
            return os.path.join(HANDOFFS, name + ".md")
    return os.path.join(HANDOFFS, f"session-{sid[:8]}.md")


def save(sid, since):
    t = transcript(sid)
    if not t:
        return {"error": "transcript not found"}
    s = summary_after(t, since)
    if not s:
        return {"error": "no compact summary after the prune started"}
    at, text = s
    body = body_of(text)
    path = handoff_path(body, sid)
    os.makedirs(HANDOFFS, exist_ok=True)
    note = f"<!-- written by Bonsai's lean prune from the compact summary, session {sid}, {at} -->\n\n"
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(note + body + "\n")
    with open(LEDGER, "a", encoding="utf-8") as f:
        f.write(json.dumps({"session": sid, "at": at, "chars": len(body), "path": path}) + "\n")
    return {"path": path.replace(os.sep, "/"), "chars": len(body)}


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["prep", "save"])
    ap.add_argument("--session", required=True)
    ap.add_argument("--since", type=float, default=0)
    a = ap.parse_args()
    print(json.dumps(prep(a.session) if a.mode == "prep" else save(a.session, a.since)))
