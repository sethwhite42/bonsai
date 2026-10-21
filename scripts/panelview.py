"""panelview - what the bonsai plugin draws, as JSON.

The plugin's hooks module (hooks/register.tsx) is a generic drawer: it runs
`python panelview.py --self <session-id> --blocks 2` every 30 s and after each turn,
and draws the blocks this prints. Edit THIS file to change the panel's content or
layout in every session at once, with no plugin reload.

Output: {"blocks": [Block, ...], "layout": {}, "footer": {}, "autoPruneSelf": bool,
         "nudge": {"ctx": int, "parts": [Part]} | null}   (the bar above the prompt, once a prune pays)
  Block = {"kind": "line", "parts": [Part]}
        | {"kind": "gap"}
        | {"kind": "box", "color": <theme color>, "children": [Block],
           "title"?: [Part], "titleRight"?: [Part], "style"?: "round"|"bold"|"single"|...,
           "grow"?: bool, "width"?: int}
        | {"kind": "row", "children": [Block], "gap"?: cells}   (side by side; wraps when narrow)
        | {"kind": "kv", "label", "parts": [Part]}   (label in a fixed column; --blocks 2 only)
        | {"kind": "meter", "frac": 0..1, "color": hex, "track"?, "width"?: px, "height"?: px, "label"?: [Part]}
          (an SVG bar on the desktop, a text bar in the terminal; sessions may carry one as "meter")
        | {"kind": "session", "sid", "parts": [Part], "right": [Part],
           "action": "prune" | "pruning" | "not open" | "",
           "sub": [[Part], ...], "toggle"?: {"label", "pref", "value"}}
          (toggle = a small dim button before the action, e.g. ✕ exclude / ↺ include)
        | {"kind": "button", "label", "pref": <key>, "value": <json>, "parts"?: [Part]}
          (pressing it merges {pref: value} into hooks-state/bonsai-prefs.json)
  "autoPruneSelf": true when THIS session should prune itself now.
  Part  = {"text", "color"?, "bold"?, "dim"?}
  Colors are theme names: warning, success, error, suggestion, subtle, claude ...
The plugin adds the Prune -> Confirm/Cancel buttons for action "prune".
"""
import argparse, json, os, subprocess, sys, time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
from common import STATE, STEP, pc_idle_s
import econ, growth, prices, usage
LIVE_S = 20                           # heartbeat age that still counts as open
SOON = 0.25                           # "going cold" = under this share of the TTL
AUTO_MIN = 10                         # auto-prune when this many cache minutes or fewer are left
AUTO_CTX = 0                          # ...and the context is at least this big (0 = when a prune pays, econ.py)
CTX_CHOICES = [0, 100_000, 150_000, 200_000, 250_000, 300_000, 400_000, 500_000]   # panel dropdowns
MIN_CHOICES = [5, 10, 15, 20]
IDLE_MIN = 10                         # idle prune: no keyboard/mouse for this long (0 = off)
IDLE_CHOICES = [0, 5, 10, 15, 20, 30]
SAVINGS_DAYS = 30                     # window for the savings tile (from prunestats.json)
TITLE_W = 34                          # session title column width
BAR_W = 10                            # cache-left bar width in cells
# The Cache button in the prompt footer (keys: label, color, dim, box, hitBox).
# The terminal honors them; the desktop app draws it as its own gray chip regardless (tested 0.5.7).
# The chip has no inner padding; padding the label with en spaces didn't help (tried 2026-10-15).
FOOTER = {"label": "Bonsai"}


def P(text, **kw):
    return {"text": str(text), **kw}


def line(*parts):
    return {"kind": "line", "parts": list(parts)}


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def k(n):
    if abs(n) >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    return f"{round(n / 1000)}k"


def kn(n):
    """token count for small numbers too: 850, 2.1k, 245k"""
    return str(round(n)) if n < 1000 else f"{n / 1000:.1f}k" if n < 10_000 else k(n)


def bar(frac, width, color):
    """A filled/empty cell bar, e.g. ██████░░░░."""
    frac = max(0.0, min(1.0, frac))
    filled = round(frac * width)
    if 0 < frac and filled == 0:
        filled = 1
    return [P("█" * filled, color=color), P("░" * (width - filled), dim=True)]


def dismissed_at(sid, ctx):
    """Context at the last Dismiss of the nudge bar (0 = none, or a compact since)."""
    last = (read_json(os.path.join(STATE, f"ctxnudge-{sid}.json")) or {}).get("last", 0)
    if last and ctx is not None and ctx >= 0 and ctx < last - STEP:
        last = 0
    return last


def last_log_line(sid):
    try:
        with open(os.path.join(STATE, f"bonsai-log-{sid[:8]}.log"), encoding="utf-8") as f:
            lines = [l.rstrip() for l in f if f" {sid[:8]} " in l and "footer drawn" not in l]
        if not lines:
            return None
        stamp, _, msg = lines[-1].split(" ", 2)
        age = time.time() - datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        return msg if age < 3600 else None   # hide stale prune status after an hour
    except Exception:
        return None


def savings():
    """Totals from prunestats.json (written by prunestats.py) for the last SAVINGS_DAYS."""
    path = os.path.join(STATE, "prunestats.json")
    # refresh in the background about hourly (a run takes ~6 s); the marker stops every
    # open session's panel from launching its own copy
    mark = os.path.join(STATE, "prunestats-refresh.stamp")
    try:
        stale = time.time() - os.path.getmtime(mark) > 3600
    except OSError:
        stale = True
    if stale:
        open(mark, "w").close()
        subprocess.Popen([sys.executable, os.path.join(HERE, "prunestats.py")],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         creationflags=getattr(subprocess, "DETACHED_PROCESS", 0)
                         | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    data = read_json(path) or {}
    cutoff = time.time() - SAVINGS_DAYS * 86400
    rows = []
    for r in data.values():
        try:
            if datetime.strptime(r["when"], "%Y-%m-%d %H:%M").timestamp() >= cutoff:
                rows.append(r)
        except Exception:
            pass
    if not rows:
        return None
    net = sum(r.get("net", 0) for r in rows)
    best = max(rows, key=lambda r: r.get("net", 0))
    pending = sum(1 for r in rows if r.get("open"))
    age_h = (time.time() - os.path.getmtime(path)) / 3600
    return {"n": len(rows), "net": net, "best": best, "pending": pending, "age_h": age_h}


# ── palette: neutral text + one accent + one warning ────────────────────────
# Two hues only (four were tried): the blue-only look reads like part of Claude Code itself.
ACCENT = "#5B8DEF"      # the blue of Claude Code's own usage bars
WARN = "#E8A33D"        # going cold / near the nudge / failures
BORDER = "#3A3A3A"      # every group box, so groups read as one family
TRACK = "#3A3A3A"       # meter track
METER_W = 150           # px, tile meters
ROW_METER_W = 110       # px, session-row meters
TITLE_CH = 26           # session title, characters, before the ellipsis
# Spacing, in character cells (the desktop turns a cell into ~8 px across, one line down).
LAYOUT = {"gap": 1, "padX": 1, "padY": 1, "labelW": 10}   # whole panel: gap between groups, outer margin
BOX_PAD = {"padX": 2, "padTop": 0.5, "padBottom": 1}   # inside every group box (0.5 = desktop test)


RL_NAMES = {"five_hour": "5-hour", "seven_day": "Weekly", "spend_limit": "Spend"}


def rl_label(kind):
    """'seven_day_fable' -> 'Weekly Fable'; unknown kinds keep their own words."""
    for key, name in RL_NAMES.items():
        if kind == key:
            return name
        if kind.startswith(key + "_"):
            return f"{name} {kind[len(key) + 1:].replace('_', ' ').title()}"
    return kind.replace("_", " ").title()


def resets(iso):
    """'14:00' today, 'Tue 14:00' within a week, else the date."""
    try:
        t = datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone()
    except Exception:
        return ""
    now = datetime.now().astimezone()
    if t.date() == now.date():
        return t.strftime("%H:%M")
    if (t - now).days < 6:
        return t.strftime("%a %H:%M")
    return t.strftime("%b %d")


def money(x):
    return f"${x:,.0f}" if x >= 100 else f"${x:,.2f}"


def usage_box(kv):
    """Plan limits (written by the plugin from Claude Code's own figures) + per-model usage."""
    kids = []
    rl = read_json(os.path.join(STATE, "bonsai-ratelimits.json")) or {}
    fresh = time.time() - (rl.get("at") or 0) / 1000 < 6 * 3600
    for w in (rl.get("rateLimits") or []) if fresh else []:
        pct = float(w.get("percentUsed") or 0)
        hue = WARN if pct >= 80 else ACCENT
        when = resets(w.get("resetsAt") or "")
        kids.append(kv(rl_label(str(w.get("kind") or "?")), P(f"{pct:g}%", bold=True, color=WARN if pct >= 80 else None),
                       P(f"  resets {when}" if when else "", dim=True)))
        kids.append(meter(pct / 100, hue))
    try:
        u = usage.summary()
    except Exception as e:
        u = None
        kids.append(line(P(f"usage scan failed: {e}", color=WARN)))
    if u:
        week = sorted(u["week"].items(), key=lambda x: -x[1][5])
        for model, r in week[:5]:
            t = u["today"].get(model)
            kids.append(kv(prices.label(model)[:10],
                           P(money(t[5]) if t else "$0", bold=True), P(" today", dim=True),
                           P("  ·  ", dim=True), P(money(r[5])), P(" 7 days", dim=True),
                           P(f"  ·  {r[0]:,} calls", dim=True)))
        if week:
            kids.append(line(P("At API prices, from this PC's transcripts"
                               + ("" if u["complete"] else " (still counting)"), dim=True)))
    if not kids:
        kids.append(line(P("No usage yet.", dim=True)))
    return {"kind": "box", "color": BORDER, "title": [P("Usage", bold=True)],
            "titleRight": [P("plan" if fresh and rl.get("rateLimits") else "", dim=True)], "children": kids}


def cache_hue(left, ttl):
    """blue while warm, amber once going cold"""
    return WARN if left < ttl * SOON else ACCENT


def meter(frac, color, label=None, width=METER_W):
    return {"kind": "meter", "frac": max(0.0, min(1.0, frac)), "color": color, "track": TRACK,
            "width": width, "height": 6, **({"label": label} if label else {})}


def short(t, n):
    return t if len(t) <= n else t[:n - 1].rstrip() + "…"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--self", dest="me", default="")
    ap.add_argument("--blocks", type=int, default=1)   # the drawer's block format (2 = kv, row gaps)
    a = ap.parse_args()
    now = time.time()

    r = subprocess.run([sys.executable, os.path.join(HERE, "cachecheck.py"), "--json"],
                       capture_output=True, text=True, timeout=25)
    data = json.loads(r.stdout)
    ttl, sessions = data["ttl"], data["sessions"]
    prefs = read_json(os.path.join(STATE, "bonsai-prefs.json")) or {}
    auto = bool(prefs.get("autoPrune"))
    # thresholds the person picks in the panel (defaults are the constants above)
    global AUTO_MIN, AUTO_CTX
    AUTO_CTX = int(prefs.get("autoCtx", AUTO_CTX))
    AUTO_MIN = int(prefs.get("autoMin", AUTO_MIN))
    idle_min = int(prefs.get("idleMin", IDLE_MIN))
    # sessions the person took out of auto-prune and Prune all (a disposable monster task, say);
    # a manual Prune still works on them. Kept by id, so one that goes cold and comes back stays out.
    excluded = [x for x in prefs.get("excluded", []) if isinstance(x, str)][-50:]
    idle_s = pc_idle_s() if idle_min else None
    pc_idle = idle_s is not None and idle_s >= idle_min * 60

    me = next((s for s in sessions if s["sid"] == a.me), None)
    ctx = me["ctx"] if me else None
    # the floating threshold: does a prune pay off for this session now? (econ.py)
    fit = econ.ledger_fit()
    try:
        g = growth.update(a.me) if a.me else None
    except Exception:
        g = None
    ec = econ.case(ctx if ctx and ctx > 0 else 0, g, fit)
    pay_at = ec["at"] or 0
    near = bool(ctx and ctx > 0 and pay_at and ctx >= pay_at - 15_000)

    def pays(s, cold=False):
        """Whether auto-prune (and Prune all) should take session s: over the chosen size or,
        set to "when it pays", by the economics of its own growth record."""
        c = s["ctx"]
        if not c or c <= 0:
            return False
        if AUTO_CTX:
            return c >= AUTO_CTX
        x = ec if s["sid"] == a.me else econ.case(c, growth.peek(s["sid"]), fit)
        return x["cold"] if cold else x["warm"]
    over = f"over {k(AUTO_CTX)}" if AUTO_CTX else "where a prune pays"
    cut = ttl * SOON
    soon = [s for s in sessions if s["left"] < cut]
    st = last_log_line(a.me)

    # should this session prune itself? (the plugin files the request and checks no turn is running)
    # Two triggers, both only for sessions over the size threshold with the cache still warm:
    #  - expiring: under AUTO_MIN cache minutes left (the session sat untouched ~50 min)
    #  - idle: the PC has had no input for idle_min AND this session has been quiet as long,
    #    so a break gets used for housekeeping instead of interrupting work. The quiet check
    #    keeps a session driven from the phone (Remote Control) from qualifying.
    auto_self, auto_reason = False, ""
    if auto and me and a.me not in excluded and me["left"] > 0 and ctx:
        quiet_min = ttl - me["left"]
        if me["left"] <= AUTO_MIN and pays(me, cold=True):
            auto_reason = "cache nearly expired"
        elif pc_idle and quiet_min >= idle_min and pays(me):
            auto_reason = f"PC idle {int(idle_s // 60)} min"
        if auto_reason:
            req = read_json(os.path.join(STATE, f"prune-request-{a.me}.json")) or {}
            recent = max(req.get("at", 0), req.get("handled", 0)) / 1000
            auto_self = now - recent > 3600

    def msgs(calls):
        """break-even calls as messages you'd type, at this session's own calls per message"""
        n = max(1, round(calls / growth.calls_per_msg(g)))
        return f"{n} message" + ("" if n == 1 else "s")

    def kv(label, *parts):
        if a.blocks >= 2:
            return {"kind": "kv", "label": label, "parts": list(parts)}
        return line(P(label.ljust(11), dim=True), *parts)

    blocks = []

    # ── 1. auto-prune: the headline feature ─────────────────────────────────
    # sessions an outside program drives (claude -p) are shown but never pruned: no plugin runs
    # between that program's calls to take the request, so their context is the program's job
    big = sorted((s for s in sessions if pays(s, cold=s["left"] <= AUTO_MIN) and not s.get("external")
                  and s["sid"] not in excluded),
                 key=lambda s: s["left"])
    if not auto:
        nxt = line(P("Off: sessions only prune when you press Prune.", dim=True))
    elif big:
        s = big[0]
        wait = max(0, int(s["left"]) - AUTO_MIN)
        when = (f"  next {idle_min}-min break, or in {wait} min" if idle_min else f"  in {wait} min")             if wait else "  now, once idle"
        nxt = kv("Next", P(short(s["title"], 22), bold=True),
                 P(when, color=ACCENT if wait else WARN))
    else:
        nxt = line(P(f"Nothing to do: no warm session {over}.", dim=True))
    blocks.append({"kind": "box", "color": ACCENT if auto else BORDER, "gap": 1,
                   "title": [P("⟳ Auto-prune", bold=True, color=ACCENT if auto else None)],
                   # the ON/OFF chip in the title row is the toggle itself
                   "titleButton": {"label": "ON" if auto else "OFF", "pref": "autoPrune", "value": not auto},
                   "children": [
                       line(P("Big sessions hand off and compact on their own when they sit idle.", dim=True)),
                       {"kind": "row", "children": [
                           {"kind": "select", "pref": "autoCtx", "label": "Over", "value": AUTO_CTX,
                            "numeric": True, "options": [{"value": v, "label": k(v) if v else "when it pays"}
                                                         for v in CTX_CHOICES]},
                           {"kind": "select", "pref": "autoMin", "label": "at", "value": AUTO_MIN,
                            "numeric": True, "options": [{"value": v, "label": f"{v} min left"} for v in MIN_CHOICES]},
                       ]},
                       {"kind": "row", "children": [
                           {"kind": "select", "pref": "idleMin", "label": "or I'm away", "value": idle_min,
                            "numeric": True, "options": [{"value": v, "label": f"{v} min" if v else "never"}
                                                         for v in IDLE_CHOICES]},
                       ]},
                       nxt,
                   ]})

    # ── 2. this session ─────────────────────────────────────────────────────
    me_children = []
    if ctx and ctx > 0:
        me_children.append(kv("Context", P(k(ctx), bold=True, color=WARN if near else None),
                              P(f"  prune pays from ~{k(pay_at)}" if pay_at else "", dim=True)))
        if pay_at:
            me_children.append(meter(ctx / pay_at, WARN if near else ACCENT,
                                     [P(f"{round(100 * ctx / pay_at)}%", dim=True)]))
        if ec["warm"]:
            me_children.append(kv("Prune", P(f"pays off after ~{msgs(ec['breakeven'])}", bold=True),
                                  P(f"  (~{round(ec['breakeven'])} calls, frees {k(ec['removable'])})", dim=True)))
        elif ec["why"] and ec["removable"]:
            me_children.append(kv("Prune", P(f"not yet: {ec['why']}", dim=True)))
        if g and g["seg"]["calls"] > 1:
            cats, grown = growth.top_cats(g)
            if cats:
                parts = [P(f"+{k(grown)}", bold=True), P("  ")]
                for i, (name, _, frac) in enumerate(cats):
                    parts += [P(" · " if i else "", dim=True), P(name), P(f" {round(100 * frac)}%", dim=True)]
                me_children.append(kv("Grown by", *parts))
            big_j = g["seg"].get("big")
            if big_j and big_j["tok"] >= 10_000:
                me_children.append(kv("Biggest", P(f"+{k(big_j['tok'])}", bold=True),
                                      P(f"  {growth.LABEL.get(big_j['cat'], big_j['cat'])}, "
                                        f"{time.strftime('%H:%M', time.localtime(big_j['t']))}", dim=True)))
            sp = growth.sparkline(g, 24)
            if sp:
                me_children.append(kv("Trend", P(sp, color=ACCENT),
                                      P("  since the last compact" if g["compacts"] else "  this session", dim=True)))
    else:
        me_children.append(kv("Context", P("compacted" if ctx == -1 else "unknown", bold=True)))
    # what the calls since your last message cost, at API prices
    if (g or {}).get("lc", {}).get("usd") is not None:
        t = growth.turn_total(g)
        n = t.get("calls", 0)
        # just after you type, the turn is reset and has no calls (or totals) yet
        t = {k: t.get(k, 0) for k in ("calls", "in", "out", "cr", "cw", "usd")}
        me_children.append(kv("This turn", P(money(t["usd"]), bold=True),
                              P(f"  {n} call{'' if n == 1 else 's'} since your last message", dim=True)))
        me_children.append(kv("Tokens", P(f"read {kn(t['cr'])} · write {kn(t['cw'])} · "
                                          f"in {kn(t['in'])} · out {kn(t['out'])}", dim=True)))
    if me:
        left = max(0, int(me["left"]))
        # after a compact the clock runs from the compact, and only the system prompt and tools
        # are cached: the summary is written fresh on the next message whenever it comes
        me_children += [kv("Cache", P(f"{left} min", bold=True),
                           P(" left · since the compact, system prompt and tools only" if ctx == -1
                             else " left", dim=True)),
                        meter(left / ttl, cache_hue(left, ttl))]
    else:
        me_children.append(kv("Cache", P("cold", bold=True)))
    if st:
        bad = "fail" in st or "skip" in st
        me_children.append(kv("Last step", P(st, color=WARN if bad else None, dim=not bad, wrap="truncate-end")))
    blocks.append({"kind": "box", "color": BORDER, "title": [P("This session", bold=True)],
                   "children": me_children})

    # ── 3. warm sessions, most urgent first ─────────────────────────────────
    def row(s):
        left = max(0, int(s["left"]))
        cold = left < cut
        mine = s["sid"] == a.me
        req = read_json(os.path.join(STATE, f"prune-request-{s['sid']}.json"))
        beat = read_json(os.path.join(STATE, f"bonsai-live-{s['sid']}.json"))
        live = mine or (beat and now - beat.get("at", 0) / 1000 < LIVE_S)
        action = ("app-managed" if s.get("external") else
                  "pruning" if req and not req.get("handled") else ("prune" if live else "not open"))
        c = s["ctx"]
        hot = pays(s)
        ctxp = (P("compacted", dim=True) if c == -1 else P("?", dim=True) if not c
                else P(k(c), bold=hot, color=WARN if hot else None))
        hue = cache_hue(left, ttl)
        label = [P(f"{left} min", bold=cold, color=hue if cold else None, dim=not cold)]
        if mine:
            label.append(P("  this session", color=ACCENT))
        elif s.get("external"):
            label.append(P("  external app", dim=True))
        out = s["sid"] in excluded
        toggle = None if s.get("external") else (
            {"label": "↺", "pref": "excluded", "value": [x for x in excluded if x != s["sid"]]} if out
            else {"label": "✕", "pref": "excluded", "value": excluded + [s["sid"]]})
        return {"kind": "session", "sid": s["sid"], "toggle": toggle,
                "parts": [P(short(s["title"], TITLE_CH), bold=mine, wrap="truncate-end")],
                "right": [ctxp], "action": action,
                "meter": meter(left / ttl, hue, label, ROW_METER_W),
                "sub": [[P(f"↳ {g['kind']} {g['id'][:8]} · {max(0, int(g['left']))} min", dim=True)]
                        for g in s["agents"]]}

    ordered = sorted(sessions, key=lambda s: s["left"])
    rows = [row(s) for s in ordered if s["sid"] not in excluded]
    out_rows = [row(s) for s in ordered if s["sid"] in excluded]
    # Prune all: every open session over the auto-prune threshold that isn't already pruning
    big_sids = {s["sid"] for s in big}
    pending = [r["sid"] for r in rows if r["action"] == "prune" and r["sid"] in big_sids]
    head = []
    if pending:
        head = [{"kind": "pruneAll", "label": f"Prune all ({len(pending)})", "sids": pending,
                 "parts": [P(f"every open session {over}", dim=True)]}]
    blocks.append({"kind": "box", "color": BORDER, "gap": 1,
                   "title": [P("Warm sessions", bold=True)],
                   "titleRight": ([P(f"⚠ {len(soon)} going cold", bold=True, color=WARN)] if soon
                                  else [P(f"{len(sessions)}", dim=True)]),
                   "children": head + (rows or [line(P("None.", dim=True))])})
    if out_rows:
        blocks.append({"kind": "box", "color": BORDER, "gap": 1,
                       "title": [P("Excluded", bold=True)],
                       "titleRight": [P("no auto-prune · Prune still works", dim=True)],
                       "children": out_rows})

    # ── 3b. usage: plan limits and spend per model ──────────────────────────
    blocks.append(usage_box(kv))

    # ── 4. savings ──────────────────────────────────────────────────────────
    sv = savings()
    if sv:
        net = sv["net"]
        sv_children = [
            line(P(("+" if net >= 0 else "") + k(net), bold=True, color=ACCENT if net >= 0 else WARN),
                 P(f"  saved over {sv['n']} prunes", dim=True)),
            line(P("best  ", dim=True), P("+" + k(sv["best"].get("net", 0))),
                 P("  " + short(sv["best"].get("title", ""), 24), dim=True, wrap="truncate-end")),
            line(P("input-token equivalents"
                   + (f" · {sv['pending']} still counting" if sv["pending"] else ""), dim=True)),
        ]
    else:
        sv_children = [line(P("No prunes measured yet.", dim=True))]
    blocks.append({"kind": "box", "color": BORDER, "title": [P("Savings", bold=True)],
                   "titleRight": [P(f"{SAVINGS_DAYS} days", dim=True)], "children": sv_children})

    # ── 5. legend ───────────────────────────────────────────────────────────
    blocks.append(line(P(f"Prune = compact, with the handoff in the summary · TTL {int(ttl)} min · {time.strftime('%H:%M')}",
                         dim=True)))

    for b in blocks:
        if b["kind"] == "box":
            for key, v in BOX_PAD.items():
                b.setdefault(key, v)
    # ── the nudge bar above the prompt: this session crossed its next checkpoint ──
    # (once a prune pays off, econ.py; after a Dismiss, again STEP further on). The plugin draws
    # Prune / Dismiss beside these parts; Dismiss writes {"last": ctx} to ctxnudge-<sid>.json.
    nudge_bar = None
    last = dismissed_at(a.me, ctx)
    if ctx and ec["warm"] and (not last or ctx >= last + STEP) and not auto_self:
        bar_parts = [P("Bonsai", bold=True, color=ACCENT), P("  "), P(k(ctx), bold=True, color=WARN),
                     P(" context", dim=True), P("  ·  prune pays off after ", dim=True),
                     P(f"~{msgs(ec['breakeven'])}", bold=True)]
        if me:
            bar_parts += [P("  ·  cache ", dim=True), P(f"{max(0, int(me['left']))} min", bold=True)]
        if not auto:
            bar_parts.append(P("  ·  auto-prune off", dim=True))
        nudge_bar = {"ctx": ctx, "parts": bar_parts}

    print(json.dumps({"blocks": blocks, "layout": LAYOUT, "band": [],   # band: older drawers still read it
                      "nudge": nudge_bar,
                      "footer": FOOTER, "autoPruneSelf": auto_self,
                      "autoReason": auto_reason}))


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        print(json.dumps({"blocks": [{"kind": "line", "parts": [P(f"panelview error: {e}", color="error")]}]}))
