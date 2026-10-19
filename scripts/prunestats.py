"""prunestats - estimate what each compaction (/prune or auto) saved.

Claude Code already records every compaction in the transcript as a
`compact_boundary` record with `preTokens` (the context size before), so no
logging is needed at prune time. For each manual compaction this script:

  removed   = preTokens - context of the first reply after the compaction
  cost      = the compaction call itself (from its agent-acompact-* file when
              present, else estimated) + the prune handoff turns, if any
              + reorient (below)
  saved     = for every main-thread turn until the next compaction (or now),
              what that turn would have cost with `removed` still in context:
                warm turn  -> +removed x 0.1 (cache read)
                cold turn  -> +removed x 2   (cache rebuild)
                first turn -> would have been a plain warm read of preTokens
              minus the re-read correction (below)
  net       = saved - cost

A prune is spotted by `<command-name>/prune` or `/bonsai-context:prune`, by
Bonsai's own unattended prompt ("Run the bonsai-context:prune skill ..."), by
the prune skill's "Base directory for this skill: ...skills/prune" text, or by
a Skill tool call naming it. The handoff turns run from the first of these
(after the last ordinary human prompt) to the boundary.

Corrections (v2; ctxstudy.py found v1 overstated savings):
  reread    Read calls in the window whose (path lowercased with \\ separators,
            offset, limit) was also Read in the segment before the compaction
            (first repeat of each key only). Their result tokens (chars/4,
            images 1600) would not exist without the compaction, so from the
            next call on the counterfactual is reduced by those tokens: x2 on
            the first call that carries them (the cache write), then x0.1 on
            warm calls / x2 on cold calls. Ledger field `reread` = tokens.
  reorient  the first human prompt-turn after the compaction (its calls, up to
            the next human prompt; prune/compact commands and Bonsai's prune
            prompt are skipped). If it used more API calls than the median
            calls-per-prompt-turn before the compaction, extra calls x the mean
            weighted cost of that turn's calls is added to cost. Only the first
            turn: later turns are usually new tasks.

Units are "input-token equivalents": input 1, cache read 0.1, cache write 2
(1h TTL), output 5. Model-agnostic, so compare as % of the window's spend.
Results are kept in ~/.claude/hooks-state/prunestats.json so they outlive
transcript cleanup (30 days by default). Open windows are re-estimated each run;
rows from an older method version (`v` < 2) are re-estimated whenever their
transcript still exists, otherwise kept as they are.

Caveat: assumes the session would otherwise have gone on unchanged; in
reality it might have auto-compacted later anyway, which this ignores.

Usage: python prunestats.py [--days 30] [--ttl 60] [--all]  (--all includes
subagent compactions)
"""
import argparse, glob, json, os, re, statistics, sys, time
from datetime import datetime

from common import PROJECTS as ROOT, STATE

LEDGER = os.path.join(STATE, "prunestats.json")
VERSION = 2
W_IN, W_READ, W_WRITE, W_OUT = 1, 0.1, 2, 5
IMG = 1600
SKILL_PRUNE_RE = re.compile(r"^Base directory for this skill: \S*?skills[\\/]prune\b")
CMD_PRUNE_RE = re.compile(r"<command-name>/(?:bonsai-context:)?prune\b")


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def weighted(u):
    return (W_IN * (u.get("input_tokens") or 0) + W_READ * (u.get("cache_read_input_tokens") or 0)
            + W_WRITE * (u.get("cache_creation_input_tokens") or 0) + W_OUT * (u.get("output_tokens") or 0))


def ctx_of(u):
    return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
        + (u.get("cache_creation_input_tokens") or 0)


def tok_of_content(c):
    """Estimated tokens of a tool_result / message content: chars/4, images 1600."""
    if c is None:
        return 0
    if isinstance(c, str):
        return len(c) / 4
    n = 0
    for b in c if isinstance(c, list) else [c]:
        if not isinstance(b, dict):
            n += len(str(b)) / 4
        elif b.get("type") == "image":
            n += IMG
        elif b.get("type") == "text":
            n += len(b.get("text") or "") / 4
        else:
            n += len(json.dumps(b)) / 4
    return n


def text_of(content):
    if isinstance(content, str):
        return content
    return "\n".join(b.get("text") or "" for b in content or [] if isinstance(b, dict) and b.get("type") == "text")


def read_key(inp):
    return ((inp.get("file_path") or "").replace("/", "\\").lower().rstrip("\\"), inp.get("offset"), inp.get("limit"))


def load(path):
    """Ordered events:
    ('turn', t, usage, tools)   API call deduped by message.id; tools = [(id, name, input)]
    ('compact', t, rec)         compact_boundary
    ('prune', t, None)          a prune marker (see docstring)
    ('prompt', t, flags)        human prompt; flags {'cmd': slash command, 'prune': prune prompt}
    ('result', t, tool_id, tokens)"""
    ev, turns = [], {}
    with open(path, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            t = d.get("timestamp")
            if not t:
                continue
            ty = d.get("type")
            if d.get("subtype") == "compact_boundary":
                ev.append(("compact", ts(t), d))
            elif ty == "assistant":
                m = d.get("message") or {}
                u, mid = m.get("usage"), m.get("id")
                if mid in turns:
                    e = turns[mid]
                elif u and ctx_of(u):
                    e = turns[mid] = ("turn", ts(t), u, [])
                    ev.append(e)
                else:
                    continue
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        inp = b.get("input") or {}
                        e[3].append((b.get("id"), b.get("name") or "?", inp))
                        if b.get("name") == "Skill" and re.fullmatch(r"(?:bonsai-context:)?prune", str(inp.get("skill") or "")):
                            ev.append(("prune", ts(t), None))
            elif ty == "user" and not d.get("isSidechain"):
                cont = (d.get("message") or {}).get("content")
                if isinstance(cont, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in cont):
                    for b in cont:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            ev.append(("result", ts(t), b.get("tool_use_id"), tok_of_content(b.get("content"))))
                    continue
                txt = text_of(cont).lstrip()
                if d.get("isCompactSummary") or txt.startswith("This session is being continued"):
                    continue
                head = txt[:400]
                cmd = head.startswith(("<command-name>", "<command-message>"))
                prune = (cmd and CMD_PRUNE_RE.search(head) is not None) \
                    or head.startswith("Run the bonsai-context:prune skill") or SKILL_PRUNE_RE.match(head) is not None
                if prune:
                    ev.append(("prune", ts(t), None))
                if d.get("isMeta") or head.startswith(("<local-command", "<task-notification", "[Request interrupted",
                                                       "Base directory for this skill")):
                    continue
                ev.append(("prompt", ts(t), {"cmd": cmd or head.startswith("<command"), "prune": prune}))
    return ev


def prompt_turns(seg):
    """[(flags, [turn events])] split at every human prompt in an event list."""
    out = []
    for e in seg:
        if e[0] == "prompt":
            out.append((e[2], []))
        elif e[0] == "turn" and out:
            out[-1][1].append(e)
    return [(f, c) for f, c in out if c and not f["cmd"] and not f["prune"]]


def compaction_run_cost(session_dir, t):
    """Actual cost of the compaction call, if Claude Code logged it as agent-acompact-*."""
    for p in glob.glob(os.path.join(session_dir, "subagents", "agent-acompact-*.jsonl")):
        turns = [e for e in load(p) if e[0] == "turn"]
        if turns and abs(turns[-1][1] - t) < 300:
            return sum(weighted(e[2]) for e in turns), True
    return None, False


def analyse(path, ttl_s, limit):
    ev = load(path)
    session_dir = path[:-6]
    res_tok = {e[2]: e[3] for e in ev if e[0] == "result"}
    out = []
    idx = [i for i, e in enumerate(ev) if e[0] == "compact"]
    for n, i in enumerate(idx):
        rec = ev[i][2]
        t0 = ev[i][1]
        if (rec.get("compactMetadata") or {}).get("trigger") == "auto":
            continue   # forced by a full window: there is no "didn't compact" alternative
        pre = (rec.get("compactMetadata") or {}).get("preTokens") or 0
        end = idx[n + 1] if n + 1 < len(idx) else len(ev)
        seg_after = ev[i + 1:end]
        window = [e for e in seg_after if e[0] == "turn"]
        if not window or not pre:
            continue

        # prune overhead: turns from the first prune marker of the final run (no ordinary prompt between) to the boundary
        last_prev = idx[n - 1] if n else -1
        seg_before = ev[last_prev + 1:i]
        prune_at = None
        for k in range(len(seg_before) - 1, -1, -1):
            e = seg_before[k]
            if e[0] == "prune":
                prune_at = k
            elif e[0] == "prompt" and not e[2]["prune"] and prune_at is not None:
                break
        prune_cost = sum(weighted(e[2]) for e in seg_before[prune_at:] if e[0] == "turn") if prune_at is not None else 0
        before = ev[:i]
        prev_turn_t = next((e[1] for e in reversed(before) if e[0] == "turn"), t0)

        run_cost, actual_run = compaction_run_cost(session_dir, t0)
        post = ctx_of(window[0][2])
        if run_cost is None:   # estimate: re-read pre (cached if warm) + a ~summary-sized output
            warm = (t0 - prev_turn_t) < ttl_s
            run_cost = pre * (W_READ if warm else W_WRITE) + W_OUT * max(post // 4, 2000)
        removed = max(pre - post, 0)

        # re-reads: Reads in the window of keys already Read before this compaction (first repeat only)
        pre_reads = {read_key(inp) for e in seg_before if e[0] == "turn" for _, nm, inp in e[3] if nm == "Read"}
        reread = pending = carried = 0.0

        actual = cf = 0.0
        last_t = prev_turn_t
        k = 0
        for k, (_, t, u, tools) in enumerate(window):
            a = weighted(u)
            actual += a
            gap = t - last_t
            last_t = t
            write = u.get("cache_creation_input_tokens") or 0
            cold = gap > ttl_s or write > 0.5 * ctx_of(u)
            if ctx_of(u) + removed > limit:
                break   # without the prune, auto-compact would have fired here; stop counting
            if k == 0:
                # without the compaction this turn reads the old context (warm unless the gap says cold)
                if gap > ttl_s:
                    cf += a + W_WRITE * removed
                else:
                    cf += W_IN * (u.get("input_tokens") or 0) + W_READ * pre + W_OUT * (u.get("output_tokens") or 0)
            else:
                cf += a + (W_WRITE if cold else W_READ) * removed
            # re-read tokens are in this call's context but would not be without the compaction
            cf -= W_WRITE * pending + (W_WRITE if cold else W_READ) * carried
            carried += pending
            pending = 0.0
            for tid, nm, inp in tools:
                if nm == "Read" and read_key(inp) in pre_reads:
                    pre_reads.discard(read_key(inp))
                    tk = res_tok.get(tid, 0)
                    pending += tk
                    reread += tk

        # re-orientation: extra calls in the first human prompt-turn vs the pre-compaction median
        reorient = 0.0
        pre_turns = prompt_turns(seg_before[:prune_at] if prune_at is not None else seg_before)
        post_turns = prompt_turns(seg_after)
        if pre_turns and post_turns:
            med = statistics.median(len(c) for _, c in pre_turns)
            first = post_turns[0][1]
            extra = len(first) - med
            if extra > 0:
                reorient = extra * statistics.mean(weighted(e[2]) for e in first)

        saved = cf - actual
        cost = run_cost + prune_cost + reorient
        out.append({
            "v": VERSION,
            "session": os.path.basename(session_dir), "file": path, "uuid": rec.get("uuid"),
            "when": datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M"),
            "trigger": (rec.get("compactMetadata") or {}).get("trigger"),
            "via_prune": prune_at is not None, "pre": pre, "post": post, "removed": removed,
            "turns_after": k + 1, "open": end == len(ev),
            "actual_after": round(actual), "saved": round(saved),
            "reread": round(reread), "reorient": round(reorient),
            "cost": round(cost), "cost_measured": actual_run,
            "net": round(saved - cost),
        })
    return out


def title_of(path):
    t = None
    try:
        with open(path, "rb") as f:
            for raw in f:
                if b'"custom-title"' in raw:
                    t = json.loads(raw).get("customTitle") or t
    except Exception:
        pass
    return t or os.path.basename(path)[:8]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=30)
    ap.add_argument("--ttl", type=float, default=60, help="cache lifetime in minutes")
    ap.add_argument("--limit", type=int, default=960_000, help="auto-compact point (tokens)")
    ap.add_argument("--all", action="store_true", help="include subagent compactions")
    a = ap.parse_args()

    try:
        ledger = json.load(open(LEDGER))
    except Exception:
        ledger = {}
    cutoff = time.time() - a.days * 86400
    files = glob.glob(os.path.join(ROOT, "*", "*.jsonl"))
    if a.all:
        files += glob.glob(os.path.join(ROOT, "*", "*", "subagents", "agent-a[!c]*.jsonl"))
    files = [p for p in files if os.path.getmtime(p) >= cutoff]
    # rows from an older method: re-estimate from their transcript if it is still there
    stale = {r.get("file") for r in ledger.values() if r.get("v", 1) < VERSION}
    files += [p for p in stale if p and p not in files and os.path.exists(p)]
    errors = []
    for p in files:
        try:
            with open(p, "rb") as f:
                if b"compact_boundary" not in f.read():
                    continue
            rows = analyse(p, a.ttl * 60, a.limit)
        except Exception as ex:
            errors.append(f"{os.path.basename(p)}: {ex!r}")
            continue
        name = title_of(p)
        for r in rows:
            r["title"] = name
            key = r["uuid"] or f"{r['session']}@{r['when']}"
            old = ledger.get(key)
            if old is None or old.get("v", 1) < VERSION or old["turns_after"] <= r["turns_after"]:
                ledger[key] = r   # same compaction can appear in a resumed copy of the transcript
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    json.dump(ledger, open(LEDGER, "w"), indent=1)

    rows = sorted(ledger.values(), key=lambda r: r["when"])
    if not rows:
        print("No compactions found.")
        return
    k = lambda n: f"{n / 1000:,.0f}k"
    kk = lambda r, f: k(r[f]) if f in r else "-"
    print(f"{'when':16} {'how':6} {'pre':>6} {'post':>6} {'turns':>5} {'saved':>8} {'reread':>6} {'cost':>7} "
          f"{'reor':>5} {'net':>8} {'net %':>6}  session")
    tot_net = tot_cf = 0
    for r in rows:
        how = ("prune" if r["via_prune"] else r["trigger"] or "?")[:6]
        cf = r["actual_after"] + r["saved"]
        pct = 100 * r["net"] / cf if cf > 0 else 0
        flag = "*" if r["open"] else " "
        print(f"{r['when']:16} {how:6} {k(r['pre']):>6} {k(r['post']):>6} {r['turns_after']:>4}{flag} "
              f"{k(r['saved']):>8} {kk(r, 'reread'):>6} {k(r['cost']):>7} {kk(r, 'reorient'):>5} {k(r['net']):>8} "
              f"{pct:>5.0f}%  {r['title'][:40]}")
        tot_net += r["net"]
        tot_cf += cf
    print(f"\nTotal net {k(tot_net)} input-token-equivalents; "
          f"{100 * tot_net / tot_cf:.0f}% of what those windows would have cost."
          if tot_cf > 0 else "")
    print("* = still the session's latest compaction; keeps growing as the session continues.\n"
          "saved is after the re-read correction (reread = re-read tokens); cost includes reor (re-orientation).\n"
          "net % = net / what the window would have cost without the compaction. Negative = the prune cost more than it saved.")
    if errors:
        print(f"\n{len(errors)} transcript(s) skipped on error:", *errors[:10], sep="\n  ", file=sys.stderr)


if __name__ == "__main__":
    main()
