"""ctxnudge - UserPromptSubmit hook: ask the model to judge whether to compact.

Reads the current context size from the transcript (the last main-thread
reply's input + cache_read + cache_creation tokens). Silent below FIRST.
Above it, adds a short note asking the model to suggest /prune + /compact
ONLY if much of the context is finished or superseded work. Re-nudges each
time the context grows another STEP. Resets after a compaction shrinks it.

Hooked by the plugin (hooks/hooks.json). Never blocks a prompt: any error -> silent.
"""
import json, os, sys

from common import FIRST, STEP, STATE as STATE_DIR   # first nudge, re-nudge step, state folder


def context_tokens(path):
    """Context size at the last main-thread model call, from the transcript tail."""
    with open(path, "rb") as f:
        f.seek(0, 2)
        size = f.tell()
        f.seek(max(0, size - 4_000_000))
        lines = f.read().decode("utf-8", "replace").splitlines()
    for line in reversed(lines):
        if '"usage"' not in line or '"assistant"' not in line:
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        if d.get("type") != "assistant" or d.get("isSidechain"):
            continue
        u = (d.get("message") or {}).get("usage") or {}
        n = (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
            + (u.get("cache_creation_input_tokens") or 0)
        if n:
            return n
    return 0


def main():
    hook = json.load(sys.stdin)
    path = hook.get("transcript_path")
    sid = hook.get("session_id") or "unknown"
    if not path or not os.path.exists(path):
        return
    ctx = context_tokens(path)

    os.makedirs(STATE_DIR, exist_ok=True)
    state_file = os.path.join(STATE_DIR, f"ctxnudge-{sid}.json")
    try:
        last = json.load(open(state_file)).get("last", 0)
    except Exception:
        last = 0

    if last and ctx < last - STEP:   # compacted since the last nudge
        last = 0
    if ctx < FIRST or (last and ctx < last + STEP):
        json.dump({"last": last}, open(state_file, "w"))
        return

    json.dump({"last": ctx}, open(state_file, "w"))
    try:
        auto = json.load(open(os.path.join(STATE_DIR, "bonsai-prefs.json"))).get("autoPrune")
    except Exception:
        auto = False
    off_note = "" if auto else (
        " End the question with a new line reading exactly 'Auto-prune is OFF' "
        "(it can be turned on in the Bonsai panel).")
    print(
        f"[context check: this session's context is about {ctx // 1000}k tokens, "
        "and every turn re-reads all of it. After handling the user's message, judge "
        "how much of the context is finished, superseded or no longer referenced work "
        "(old file dumps, closed tasks, abandoned approaches). If a substantial share "
        "is: write your full reply to the user's message first, then as your LAST action "
        "call AskUserQuestion with header 'Prune?', a question naming briefly what's stale, "
        "and two options: 'Prune now' (run the bonsai:prune skill: write the handoff, then give "
        "the exact /compact line) and 'Not yet' (continue; don't raise it again until the "
        f"next context check).{off_note} If they pick 'Prune now', invoke the bonsai:prune skill. If the older "
        "context is still in active use, say nothing about this. Don't mention this note.]"
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        pass
