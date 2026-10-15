"""prunestats - estimate what each compaction (/prune or auto) saved.

Claude Code already records every compaction in the transcript as a
`compact_boundary` record with `preTokens` (the context size before), so no
logging is needed at prune time. For each compaction this script:

  removed   = preTokens - context of the first reply after the compaction
  cost      = the compaction call itself (from its agent-acompact-* file when
              present, else estimated) + the /prune handoff turns, if any
  saved     = for every main-thread turn until the next compaction (or now),
              what that turn would have cost with `removed` still in context:
                warm turn  -> +removed x 0.1 (cache read)
                cold turn  -> +removed x 2   (cache rebuild)
                first turn -> would have been a plain warm read of preTokens
  net       = saved - cost

Units are "input-token equivalents": input 1, cache read 0.1, cache write 2
(1h TTL), output 5. Model-agnostic, so compare as % of the window's spend.
Results are kept in ~/.claude/hooks-state/prunestats.json so they outlive
transcript cleanup (30 days by default). Open windows are re-estimated each run.

Caveat: assumes the session would otherwise have gone on unchanged; in
reality it might have auto-compacted later anyway, which this ignores.

Usage: python prunestats.py [--days 30] [--ttl 60] [--all]  (--all includes
subagent compactions)
"""
import argparse, glob, json, os, time
from datetime import datetime

from common import PROJECTS as ROOT, STATE

LEDGER = os.path.join(STATE, "prunestats.json")
W_IN, W_READ, W_WRITE, W_OUT = 1, 0.1, 2, 5


def ts(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def weighted(u):
    return (W_IN * (u.get("input_tokens") or 0) + W_READ * (u.get("cache_read_input_tokens") or 0)
            + W_WRITE * (u.get("cache_creation_input_tokens") or 0) + W_OUT * (u.get("output_tokens") or 0))


def ctx_of(u):
    return (u.get("input_tokens") or 0) + (u.get("cache_read_input_tokens") or 0) \
        + (u.get("cache_creation_input_tokens") or 0)


def load(path):
    """Ordered events: ('turn', t, usage) deduped by message.id, ('compact', t, rec), ('prune', t)."""
    ev, seen = [], set()
    with open(path, "rb") as f:
        for raw in f:
            line = raw.decode("utf-8", "replace")
            if '"compact_boundary"' in line or '"usage"' in line or "<command-name>/prune" in line:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                t = d.get("timestamp")
                if not t:
                    continue
                if d.get("subtype") == "compact_boundary":
                    ev.append(("compact", ts(t), d))
                elif d.get("type") == "assistant":
                    m = d.get("message") or {}
                    u = m.get("usage")
                    if u and ctx_of(u) and m.get("id") not in seen:
                        seen.add(m.get("id"))
                        ev.append(("turn", ts(t), u))
                elif d.get("type") == "user" and "<command-name>/prune" in line:
                    ev.append(("prune", ts(t), None))
    return ev


def compaction_run_cost(session_dir, t):
    """Actual cost of the compaction call, if Claude Code logged it as agent-acompact-*."""
    for p in glob.glob(os.path.join(session_dir, "subagents", "agent-acompact-*.jsonl")):
        turns = [e for e in load(p) if e[0] == "turn"]
        if turns and abs(turns[-1][1] - t) < 300:
            return sum(weighted(u) for _, _, u in turns), True
    return None, False


def analyse(path, ttl_s, limit):
    ev = load(path)
    session_dir = path[:-6]
    out = []
    idx = [i for i, e in enumerate(ev) if e[0] == "compact"]
    for n, i in enumerate(idx):
        rec = ev[i][2]
        t0 = ev[i][1]
        if (rec.get("compactMetadata") or {}).get("trigger") == "auto":
            continue   # forced by a full window: there is no "didn't compact" alternative
        pre = (rec.get("compactMetadata") or {}).get("preTokens") or 0
        end = idx[n + 1] if n + 1 < len(idx) else len(ev)
        window = [e for e in ev[i + 1:end] if e[0] == "turn"]
        if not window or not pre:
            continue

        # /prune overhead: turns from the last /prune command up to this boundary
        before = ev[:i]
        last_prev = idx[n - 1] if n else -1
        prune_at = max((k for k, e in enumerate(before) if e[0] == "prune" and k > last_prev), default=None)
        prune_cost = sum(weighted(e[2]) for e in before[prune_at:] if e[0] == "turn") if prune_at is not None else 0
        prev_turn_t = next((e[1] for e in reversed(before) if e[0] == "turn"), t0)

        run_cost, actual_run = compaction_run_cost(session_dir, t0)
        post = ctx_of(window[0][2])
        if run_cost is None:   # estimate: re-read pre (cached if warm) + a ~summary-sized output
            warm = (t0 - prev_turn_t) < ttl_s
            run_cost = pre * (W_READ if warm else W_WRITE) + W_OUT * max(post // 4, 2000)
        removed = max(pre - post, 0)

        actual = cf = 0.0
        last_t = prev_turn_t
        for k, (_, t, u) in enumerate(window):
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

        saved = cf - actual
        out.append({
            "session": os.path.basename(session_dir), "file": path, "uuid": rec.get("uuid"),
            "when": datetime.fromtimestamp(t0).strftime("%Y-%m-%d %H:%M"),
            "trigger": (rec.get("compactMetadata") or {}).get("trigger"),
            "via_prune": prune_at is not None, "pre": pre, "post": post, "removed": removed,
            "turns_after": k + 1 if window else 0, "open": end == len(ev),
            "actual_after": round(actual), "saved": round(saved),
            "cost": round(run_cost + prune_cost), "cost_measured": actual_run,
            "net": round(saved - run_cost - prune_cost),
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
    for p in files:
        if os.path.getmtime(p) < cutoff:
            continue
        try:
            with open(p, "rb") as f:
                if b"compact_boundary" not in f.read():
                    continue
            rows = analyse(p, a.ttl * 60, a.limit)
        except Exception:
            continue
        name = title_of(p)
        for r in rows:
            r["title"] = name
            key = r["uuid"] or f"{r['session']}@{r['when']}"
            if key not in ledger or ledger[key]["turns_after"] <= r["turns_after"]:
                ledger[key] = r   # same compaction can appear in a resumed copy of the transcript
    os.makedirs(os.path.dirname(LEDGER), exist_ok=True)
    json.dump(ledger, open(LEDGER, "w"), indent=1)

    rows = sorted(ledger.values(), key=lambda r: r["when"])
    if not rows:
        print("No compactions found.")
        return
    k = lambda n: f"{n / 1000:,.0f}k"
    print(f"{'when':16} {'how':6} {'pre':>6} {'post':>6} {'turns':>5} {'saved':>8} {'cost':>7} {'net':>8} {'net %':>6}  session")
    tot_net = tot_cf = 0
    for r in rows:
        how = ("prune" if r["via_prune"] else r["trigger"] or "?")[:6]
        cf = r["actual_after"] + r["saved"]
        pct = 100 * r["net"] / cf if cf > 0 else 0
        flag = "*" if r["open"] else " "
        print(f"{r['when']:16} {how:6} {k(r['pre']):>6} {k(r['post']):>6} {r['turns_after']:>4}{flag} "
              f"{k(r['saved']):>8} {k(r['cost']):>7} {k(r['net']):>8} {pct:>5.0f}%  {r['title'][:40]}")
        tot_net += r["net"]
        tot_cf += cf
    print(f"\nTotal net {k(tot_net)} input-token-equivalents; "
          f"{100 * tot_net / tot_cf:.0f}% of what those windows would have cost."
          if tot_cf > 0 else "")
    print("* = still the session's latest compaction; keeps growing as the session continues.\n"
          "net % = net / what the window would have cost without the compaction. Negative = the prune cost more than it saved.")


if __name__ == "__main__":
    main()
