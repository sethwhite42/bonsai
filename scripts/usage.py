"""usage - API calls, tokens and API-price dollars per model, today and over the last 7 days.

Read from every session transcript on this machine (subagents included), incrementally: each
file is read only from where the last run stopped, and the running totals per day and model are
kept in hooks-state/bonsai-usage.json. A run stops after BUDGET_S and carries on next time, so the
first scan of a big history spreads over a few panel refreshes.

Claude Code writes one line per content block of a reply, each with the same message id and
usage, so repeats of an id are skipped. Dollars are at API prices (prices.py), on a subscription
a way to compare models, not a bill.

  python usage.py   prints today's and the 7-day totals per model
"""
import glob, json, os, sys, time
from datetime import datetime

from common import PROJECTS, STATE
import prices

CACHE = os.path.join(STATE, "bonsai-usage.json")
DAYS = 7
BUDGET_S = 6.0
V = 1
# per day and model: [calls, input, output, cache read, cache write, dollars]


def files(since):
    out = []
    for pat in ("*/*.jsonl", "*/*/subagents/*.jsonl"):
        for p in glob.glob(os.path.join(PROJECTS, pat)):
            try:
                st = os.stat(p)
            except OSError:
                continue
            if st.st_mtime >= since:
                out.append((p, st.st_size))
    return out


def scan(entry, path, size):
    """Read `path` from entry['off'] to its last complete line, adding into entry['days']."""
    with open(path, "rb") as f:
        f.seek(entry["off"])
        chunk = f.read(size - entry["off"])
    end = chunk.rfind(b"\n")
    if end < 0:
        return
    seen = entry.setdefault("ids", [])
    days = entry["days"]
    for raw in chunk[:end].split(b"\n"):
        if b'"usage"' not in raw or b'"assistant"' not in raw:
            continue
        try:
            d = json.loads(raw)
        except Exception:
            continue
        m = d.get("message") or {}
        u = m.get("usage")
        model = m.get("model")
        if d.get("type") != "assistant" or not u or not model or model.startswith("<"):
            continue
        mid = m.get("id") or d.get("requestId")
        if mid and mid in seen:
            continue
        if mid:
            seen.append(mid)
            del seen[:-20]
        try:
            day = datetime.fromisoformat(d["timestamp"].replace("Z", "+00:00")).astimezone().strftime("%Y-%m-%d")
        except Exception:
            continue
        row = days.setdefault(day, {}).setdefault(model, [0, 0, 0, 0, 0, 0.0])
        row[0] += 1
        row[1] += u.get("input_tokens") or 0
        row[2] += u.get("output_tokens") or 0
        row[3] += u.get("cache_read_input_tokens") or 0
        row[4] += u.get("cache_creation_input_tokens") or 0
        row[5] += prices.dollars(model, u)
    entry["off"] += end + 1


def update():
    """Bring the per-file totals up to date (within BUDGET_S). Returns (cache, finished)."""
    try:
        with open(CACHE, encoding="utf-8") as f:
            c = json.load(f)
        if c.get("v") != V:
            raise ValueError
    except Exception:
        c = {"v": V, "files": {}}
    t0 = time.time()
    since = t0 - (DAYS + 1) * 86400
    cutoff = datetime.fromtimestamp(since).strftime("%Y-%m-%d")
    fs = c["files"]
    live = files(since)
    done = True
    # newest first, so a first scan shows today soonest
    for path, size in sorted(live, key=lambda x: -os.path.getmtime(x[0]) if os.path.exists(x[0]) else 0):
        e = fs.setdefault(path, {"off": 0, "days": {}})
        if size < e["off"]:   # rewritten: start over
            e.update(off=0, days={}, ids=[])
        if size == e["off"]:
            continue
        if time.time() - t0 > BUDGET_S:
            done = False
            break
        try:
            scan(e, path, size)
        except OSError:
            pass
    keep = {p for p, _ in live}
    for p in list(fs):
        if p not in keep:
            del fs[p]
        else:
            fs[p]["days"] = {d: v for d, v in fs[p]["days"].items() if d >= cutoff}
    c["done"] = done
    tmp = CACHE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(c, f)
    os.replace(tmp, CACHE)
    return c, done


def summary():
    """{"today": {model: row}, "week": {model: row}, "complete": bool} with rows as above."""
    c, done = update()
    today = time.strftime("%Y-%m-%d")
    week0 = datetime.fromtimestamp(time.time() - (DAYS - 1) * 86400).strftime("%Y-%m-%d")
    out = {"today": {}, "week": {}, "complete": done}
    for e in c["files"].values():
        for day, models in e["days"].items():
            for model, row in models.items():
                for key, ok in (("today", day == today), ("week", day >= week0)):
                    if ok:
                        acc = out[key].setdefault(model, [0, 0, 0, 0, 0, 0.0])
                        for i, v in enumerate(row):
                            acc[i] += v
    return out


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    t = time.time()
    s = summary()
    for key in ("today", "week"):
        print(key)
        for model, r in sorted(s[key].items(), key=lambda x: -x[1][5]):
            print(f"  {prices.label(model):12} ${r[5]:9.2f}  {r[0]:6} calls  out {r[2]:>11,}  "
                  f"read {r[3]:>14,}  write {r[4]:>12,}")
    print(f"complete: {s['complete']}  ({time.time() - t:.1f}s)")
