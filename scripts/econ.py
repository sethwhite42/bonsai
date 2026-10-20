"""econ - does pruning a session pay off now? The floating threshold.

A prune pays when what it saves over the calls still to come beats what it costs. Costs are in
input-token equivalents of the session's own model (prices.py): cache read W_READ (0.025 on
Fable 5.1, 0.05 on Opus 5.5, 0.1 on Sonnet), cache write W_WRITE 2, output W_OUT.

  removable = ctx - floor         floor = what's left after a compact (system prompt, tools,
                                  memory, summary, restored files): this session's own last
                                  post-compact context, else the ledger's median, else FLOOR
  cost      = W_READ x ctx        the compact call reads the context from the cache
            + W_WRITE x (COMPACT_WRITE + rebuild)   its own cache write, then the first call
                                  after it caching the new context again (the ledger's median
                                  `rebuild`, else REBUILD)
            + W_IN x AFTER_IN     the prompt-suggestion call right after, sent uncached
            + W_OUT x SUMMARY_OUT the summary, which in a lean prune is the whole handoff
  saving    = W_READ x removable per later call while the cache stays warm
  break-even calls = cost / saving

These are the parts of one measured prune (2026-10, Opus 5.5, 242k -> 83k, by telemetry):
compact call read 242k, wrote 6.4k, 2.2k input, 3.5k output; the prompt-suggestion call after
it sent 29k uncached; the first real call wrote 29k. A lean prune drops the separate handoff
turn (3 calls, 44% of that prune) and writes a longer summary instead. A full prune (warm
subagents) costs more; the panel prices the lean one, the usual case.

Because cache reads are cheap on Fable 5.1, a prune there saves little per call and takes
longer to pay off than the same prune on Opus or Sonnet.

Expected remaining calls are unknown; the estimate is the session's own length so far
(a long session tends to keep going), clamped to [R_MIN, R_MAX].

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
import prices

FLOOR = 80_000
COMPACT_WRITE = 6_000
REBUILD = 30_000
AFTER_IN = 29_000
SUMMARY_OUT = 8_000
MIN_REMOVE = 100_000
MIN_GAP = 60
R_MIN, R_MAX = 15, 200


def ledger_fit():
    """(floor, rebuild) from prunestats.json medians, or the defaults with too few prunes."""
    try:
        with open(os.path.join(STATE, "prunestats.json"), encoding="utf-8") as f:
            rows = list(json.load(f).values())
    except Exception:
        rows = []
    posts = [r["post"] for r in rows if r.get("post")]
    rebuilds = [r["rebuild"] for r in rows if r.get("rebuild")]
    floor = statistics.median(posts) if len(posts) >= 5 else FLOOR
    rebuild = statistics.median(rebuilds) if len(rebuilds) >= 5 else REBUILD
    return floor, rebuild


def expected_calls(session_calls):
    return max(R_MIN, min(R_MAX, session_calls or 0))


def case(ctx, g=None, fit=None, model=None):
    """Prune economics for a session at context `ctx`. g = its growth.py state, if any;
    model = its model (else g's, else prices.DEFAULT).

    Returns {removable, cost, per_call, breakeven, expected, warm, cold, at, why} where
    warm/cold say whether a prune pays in each case and `at` is the context at which
    the warm case starts to pay (for the panel's meter)."""
    floor, rebuild = fit or ledger_fit()
    g = g or {}
    w_in, w_read, w_write, w_out = prices.weights(model or g.get("model"))
    seg = g.get("seg") or {}
    if g.get("compacts") and seg.get("floor"):
        floor = seg["floor"]   # this session's own floor
    calls = g.get("calls") or 0
    since = seg.get("calls", calls) if g.get("compacts") else None
    r = expected_calls(calls)
    ctx = ctx or 0
    removable = max(0, ctx - floor)
    fixed = w_write * (COMPACT_WRITE + rebuild) + w_in * AFTER_IN + w_out * SUMMARY_OUT
    cost = w_read * ctx + fixed
    per_call = w_read * removable
    breakeven = cost / per_call if per_call else None
    gap_ok = since is None or since >= MIN_GAP
    warm = bool(breakeven and removable >= MIN_REMOVE and gap_ok and r >= breakeven)
    cold = bool(removable and w_write * removable + r * per_call >= cost)
    # warm break-even context x: w_read*(x-floor)*r >= w_read*x + fixed
    at = None
    if r > 1:
        at = max(floor + MIN_REMOVE, (fixed + w_read * floor * r) / (w_read * (r - 1)))
    why = ("" if warm else
           "too little to remove" if removable < MIN_REMOVE else
           f"compacted {since} calls ago" if not gap_ok else
           "won't earn back its cost")
    return {"removable": removable, "cost": cost, "per_call": per_call, "breakeven": breakeven,
            "expected": r, "warm": warm, "cold": cold, "at": at, "since": since, "why": why}


if __name__ == "__main__":
    fit = ledger_fit()
    print("fit (floor, rebuild):", [round(v) for v in fit])
    for model in ("claude-fable-5-1", "claude-opus-5-5", "claude-sonnet-5-5"):
        for c in (150_000, 250_000, 400_000):
            for n in (40, 150):
                x = case(c, {"calls": n, "compacts": 0, "seg": {}}, fit, model)
                print(f"{prices.label(model):10} ctx {c // 1000}k, {n} calls so far: break-even "
                      f"{x['breakeven'] and round(x['breakeven'])} calls, warm {x['warm']}, cold {x['cold']}, "
                      f"pays from {round(x['at'] / 1000) if x['at'] else '-'}k  {x['why']}")
