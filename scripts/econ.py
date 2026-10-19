"""econ - does pruning a session pay off now? The floating threshold.

A prune pays when what it saves over the calls still to come beats what it costs:

  removable = ctx - floor        floor = what's left after a compact (system prompt,
                                 tools, memory, summary, restored files): this
                                 session's own last post-compact context, else the
                                 median from the savings ledger, else FLOOR
  cost      = SLOPE x ctx + BASE  the handoff turn (several calls, each re-reading the
                                 context), its output, and the compaction call; fitted
                                 by least squares to the ledger's measured prune costs
                                 when it has enough prunes (18 prunes, 2026-10: 0.68 x
                                 ctx + 80k, about one whole context per prune)
  saving    = W_READ x removable per later call while the cache stays warm
  break-even calls = cost / saving

Expected remaining calls are unknown; the estimate is the session's own length so
far (a long session tends to keep going), clamped to [R_MIN, R_MAX].

  warm case (the idle trigger, the nudge bar): pays when expected calls >= break-even,
    at least MIN_REMOVE is removable, and MIN_GAP calls have passed since the last compact
    (a 2026-10 study of 82 compactions: prunes within 60 calls of the previous one lost
    tokens 4 times in 16, spaced-out ones never; 80k is the typical floor)
  cold case (the cache is about to expire): the next call would rebuild the whole
    context at W_WRITE, so one rebuild of `removable` is saved on top: pays when
    W_WRITE x removable + expected calls x saving >= cost
"""
import json, os, statistics

from common import STATE

W_READ, W_WRITE = 0.1, 2
FLOOR = 80_000
SLOPE, BASE = 0.7, 80_000
FIT_MIN = 8   # prunes in the ledger before its own fit replaces the defaults
MIN_REMOVE = 100_000
MIN_GAP = 60
R_MIN, R_MAX = 15, 200


def ledger_fit():
    """(floor, slope, base) from prunestats.json, or the defaults when it has too few prunes."""
    try:
        with open(os.path.join(STATE, "prunestats.json"), encoding="utf-8") as f:
            rows = list(json.load(f).values())
    except Exception:
        rows = []
    posts = [r["post"] for r in rows if r.get("post")]
    floor = statistics.median(posts) if len(posts) >= 5 else FLOOR
    # the prune's own cost; re-orientation afterwards is left out (mostly a change of task)
    pts = [(r["pre"], r["cost"] - (r.get("reorient") or 0)) for r in rows
           if r.get("via_prune") and r.get("cost") and r.get("pre")]
    slope, base = SLOPE, BASE
    if len(pts) >= FIT_MIN:
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        var = sum((x - mx) ** 2 for x, _ in pts)
        if var:
            slope = sum((x - mx) * (y - my) for x, y in pts) / var
            base = my - slope * mx
    return floor, min(1.5, max(0.1, slope)), min(300_000, max(20_000, base))


def expected_calls(session_calls):
    return max(R_MIN, min(R_MAX, session_calls or 0))


def case(ctx, g=None, fit=None):
    """Prune economics for a session at context `ctx`. g = its growth.py state, if any.

    Returns {removable, cost, per_call, breakeven, expected, warm, cold, at, why} where
    warm/cold say whether a prune pays in each case and `at` is the context at which
    the warm case starts to pay (for the panel's meter)."""
    floor, slope, base = fit or ledger_fit()
    seg = (g or {}).get("seg") or {}
    if (g or {}).get("compacts") and seg.get("floor"):
        floor = seg["floor"]   # this session's own floor
    calls = (g or {}).get("calls") or 0
    since = seg.get("calls", calls) if (g or {}).get("compacts") else None
    r = expected_calls(calls)
    removable = max(0, (ctx or 0) - floor)
    cost = slope * (ctx or 0) + base
    per_call = W_READ * removable
    breakeven = cost / per_call if per_call else None
    gap_ok = since is None or since >= MIN_GAP
    warm = bool(breakeven and removable >= MIN_REMOVE and gap_ok and r >= breakeven)
    cold = bool(removable and W_WRITE * removable + r * per_call >= cost)
    # warm break-even context x: W_READ*(x-floor)*r >= slope*x + base
    at = None
    if W_READ * r > slope:
        at = max(floor + MIN_REMOVE, (base + W_READ * floor * r) / (W_READ * r - slope))
    why = ("" if warm else
           "too little to remove" if removable < MIN_REMOVE else
           f"compacted {since} calls ago" if not gap_ok else
           "won't earn back its cost")
    return {"removable": removable, "cost": cost, "per_call": per_call, "breakeven": breakeven,
            "expected": r, "warm": warm, "cold": cold, "at": at, "since": since, "why": why}


if __name__ == "__main__":
    import sys
    fit = ledger_fit()
    print("fit (floor, slope, base):", [round(v, 2) for v in fit])
    for c in (120_000, 150_000, 183_000, 250_000, 400_000):
        for n in (10, 40, 150):
            x = case(c, {"calls": n, "compacts": 0, "seg": {}}, fit)
            print(f"ctx {c // 1000}k, {n} calls so far: break-even {x['breakeven'] and round(x['breakeven'])}"
                  f" calls, warm {x['warm']}, cold {x['cold']}, pays from "
                  f"{round(x['at'] / 1000) if x['at'] else '-'}k  {x['why']}")
