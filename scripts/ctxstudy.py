"""ctxstudy - read-only study of context growth and post-compact re-familiarization.

Reads Claude Code transcripts (main sessions only; subagent traffic is skipped
except that Agent tool results count as growth) and reports:

  A. what grows the context: every main-thread API call's context delta vs the
     previous call, attributed in proportion to the estimated size of what came
     in between (tool results by tool, Read by extension, mcp__ by server,
     images ~1600 tokens, prompts, attachments, the previous reply's output)
  B. per compaction: removed, prune-with-handoff vs plain/auto, re-reads in the
     next --window API calls (files read, Grep/Glob patterns, URLs, Bash commands
     seen before the compact), handoff read?, tool calls per prompt-turn, first
     prompt-turn wall time, context regrowth at +1/+3/+5/+10 calls
  C. prunestats-style saving, corrected for re-read tokens (cache write once +
     carried on every later call) and extra API calls; break-even calls
  D. comparisons: handoff prune vs plain manual vs auto; frequent vs spaced

Token estimates: chars/4, images 1600. Weights as prunestats (in 1, read 0.1,
write 2, out 5; cold = gap > ttl). Never writes to transcripts.

Usage: python ctxstudy.py [--days 0 (=all)] [--window 15] [--ttl 60] [--json out.json]
"""
import argparse, glob, json, os, re, statistics as st, time
from collections import defaultdict, Counter

from common import PROJECTS as ROOT
from prunestats import weighted, ctx_of, ts, W_READ, W_WRITE, W_OUT, W_IN

IMG = 1600
HANDOFF_RE = re.compile(r"[\\/]\.claude[\\/]handoffs[\\/]", re.I)


def tok_of_content(c):
    """(tokens, images) of a tool_result / message content."""
    if c is None:
        return 0, 0
    if isinstance(c, str):
        return len(c) / 4, 0
    n = imgs = 0
    for b in c if isinstance(c, list) else [c]:
        if not isinstance(b, dict):
            n += len(str(b)) / 4
        elif b.get("type") == "image":
            imgs += 1
        elif b.get("type") == "text":
            n += len(b.get("text") or "") / 4
        elif b.get("type") == "tool_reference":
            n += 20
        else:
            n += len(json.dumps(b)) / 4
    return n + imgs * IMG, imgs


def norm_path(p):
    return (p or "").replace("/", "\\").lower().rstrip("\\")


def category(name, inp, imgs):
    if name == "Read":
        if imgs:
            return "Read:image"
        ext = os.path.splitext(inp.get("file_path") or "")[1].lower() or "(none)"
        return "Read:" + ext
    if name in ("Grep", "Glob", "Bash", "PowerShell", "WebFetch", "WebSearch"):
        return name
    if name in ("Agent", "Task"):
        return "Agent"
    if name in ("Edit", "Write", "MultiEdit", "NotebookEdit"):
        return "Edit/Write"
    if name.startswith("mcp__"):
        srv = name.split("__")[1]
        if len(srv) > 30:
            srv = srv[:8]
        base = "mcp__" + srv
        return base + (":image" if imgs else ":text")
    if name == "Skill":
        return "Skill"
    return "other:" + name


def out_cat(name):
    if name in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
        return "Write/Edit"
    if name in ("Bash", "PowerShell"):
        return "Bash/PS"
    if name in ("Agent", "Task"):
        return "Agent prompt"
    if name.startswith("mcp__"):
        return "mcp"
    return "other"


def text_of(content):
    if isinstance(content, str):
        return content
    out = []
    for b in content or []:
        if isinstance(b, dict) and b.get("type") == "text":
            out.append(b.get("text") or "")
    return "\n".join(out)


def parse(path):
    """Ordered main-thread events."""
    ev = []
    calls = {}
    title = None
    with open(path, "rb") as f:
        for raw in f:
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            ty = d.get("type")
            if ty == "custom-title":
                title = d.get("customTitle") or title
                continue
            if ty == "ai-title" and not title:
                title = d.get("aiTitle")
                continue
            if d.get("isSidechain"):
                continue
            t = d.get("timestamp")
            if not t:
                continue
            t = ts(t)
            if ty == "system" and d.get("subtype") == "compact_boundary":
                ev.append({"k": "compact", "t": t, "rec": d})
            elif ty == "assistant":
                m = d.get("message") or {}
                mid = m.get("id")
                u = m.get("usage")
                if not mid or not u:
                    continue
                c = calls.get(mid)
                if c is None:
                    if not ctx_of(u):
                        continue
                    c = {"k": "call", "t": t, "id": mid, "u": dict(u), "tools": [], "tend": t}
                    calls[mid] = c
                    ev.append(c)
                else:
                    c["tend"] = t
                    if (u.get("output_tokens") or 0) > (c["u"].get("output_tokens") or 0):
                        c["u"]["output_tokens"] = u.get("output_tokens")
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        c["tools"].append((b.get("id"), b.get("name") or "?", b.get("input") or {}))
                        c.setdefault("vis", Counter())["out:tool_use " + out_cat(b.get("name") or "?")] += len(json.dumps(b.get("input") or {})) / 4
                    elif isinstance(b, dict) and b.get("type") == "text":
                        c.setdefault("vis", Counter())["out:text"] += len(b.get("text") or "") / 4
            elif ty == "user":
                m = d.get("message") or {}
                cont = m.get("content")
                if isinstance(cont, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in cont):
                    for b in cont:
                        if isinstance(b, dict) and b.get("type") == "tool_result":
                            n, im = tok_of_content(b.get("content"))
                            ev.append({"k": "result", "t": t, "tid": b.get("tool_use_id"), "tok": n, "imgs": im})
                        elif isinstance(b, dict):
                            n, im = tok_of_content([b])
                            ev.append({"k": "text", "t": t, "cat": "user:paste/image" if im else "user:meta", "tok": n})
                    continue
                txt = text_of(cont)
                n, im = tok_of_content(cont)
                if d.get("isCompactSummary") or txt.startswith("This session is being continued"):
                    ev.append({"k": "text", "t": t, "cat": "compact summary", "tok": n})
                    continue
                low = txt[:400]
                is_prune = ("bonsai-context:prune" in low or "<command-name>/prune" in low
                            or re.search(r"skills[\\/]prune", low) is not None)
                if d.get("isMeta") or low.startswith("<local-command") or low.startswith("<task-notification") \
                        or low.startswith("[Request interrupted"):
                    cat = "user:meta"
                    if "Base directory for this skill" in low:
                        mm = re.search(r"Base directory for this skill: \S*?[\\/]([^\\/\s]+)\s", low)
                        cat = "skill:" + (mm.group(1) if mm else "?")
                    ev.append({"k": "text", "t": t, "cat": cat, "tok": n, "prune": is_prune})
                    continue
                cmd = low.startswith("<command-name>")
                ev.append({"k": "prompt", "t": t, "cat": "user prompt" if not im else "user:paste/image",
                           "tok": n, "cmd": cmd, "prune": is_prune, "compactcmd": "<command-name>/compact" in low,
                           "bonsai_args": "pruned by Bonsai" in txt})
            elif ty == "attachment":
                a = d.get("attachment") or {}
                at = a.get("type") or "?"
                if at in ("total_tokens_reminder",):
                    n = 15
                elif at in ("prompt_snapshot",):
                    n = 0     # SDK system-prompt snapshot, not new context
                else:
                    n = len(json.dumps(a)) / 4
                ev.append({"k": "text", "t": t, "cat": "attach:" + at, "tok": n})
    return ev, title


def segments(ev):
    """Split event indices at compact boundaries."""
    segs, cur = [], []
    for i, e in enumerate(ev):
        if e["k"] == "compact":
            segs.append(cur)
            cur = [i]
        else:
            cur.append(i)
    segs.append(cur)
    return segs


def prompt_turns(ev, idxs):
    """[(prompt_event, [call events])] for human (non-command) prompts within idxs."""
    turns, cur = [], None
    for i in idxs:
        e = ev[i]
        if e["k"] == "prompt":
            if cur:
                turns.append(cur)
            cur = (e, [])
        elif e["k"] == "call" and cur:
            cur[1].append(e)
    if cur:
        turns.append(cur)
    return turns


def repeat_key(name, inp):
    if name == "Read":
        return ("Read", norm_path(inp.get("file_path")), inp.get("offset"), inp.get("limit"))
    if name in ("Grep", "Glob"):
        return (name, inp.get("pattern"), norm_path(inp.get("path")), inp.get("glob"))
    if name == "WebFetch":
        return ("WebFetch", inp.get("url"))
    if name in ("Bash", "PowerShell"):
        return (name, " ".join((inp.get("command") or "").split()))
    return None


def file_key(name, inp):
    if name == "Read":
        return norm_path(inp.get("file_path"))
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=float, default=0, help="only transcripts modified in the last N days (0 = all)")
    ap.add_argument("--window", type=int, default=15, help="API calls after a compact to scan for re-reads")
    ap.add_argument("--ttl", type=float, default=60)
    ap.add_argument("--limit", type=int, default=960_000)
    ap.add_argument("--json", help="write per-compaction rows here")
    a = ap.parse_args()
    ttl = a.ttl * 60

    files = glob.glob(os.path.join(ROOT, "*", "*.jsonl"))
    if a.days:
        cut = time.time() - a.days * 86400
        files = [p for p in files if os.path.getmtime(p) >= cut]
    files.sort(key=os.path.getsize, reverse=True)   # largest copy of a resumed session first

    seen_calls = set()
    cat_tok = Counter()          # attributed delta tokens
    cat_est = Counter()          # raw estimates
    sess_cat = defaultdict(Counter)
    jumps = []
    neg_total = unexplained = pos_total = 0
    ncalls = 0
    comps = {}

    for path in files:
        try:
            ev, title = parse(path)
        except Exception as ex:
            print("skip", path, ex)
            continue
        proj = os.path.basename(os.path.dirname(path))
        sname = (title or os.path.basename(path)[:8])[:44]
        tool_by_id = {}
        for e in ev:
            if e["k"] == "call":
                for tid, nm, inp in e["tools"]:
                    tool_by_id[tid] = (nm, inp, e)

        # ---------------- A: growth attribution ----------------
        prev = None
        pend = Counter()
        for e in ev:
            k = e["k"]
            if k == "compact":
                prev = None
                pend = Counter()
            elif k == "result":
                nm, inp, _ = tool_by_id.get(e["tid"], ("?", {}, None))
                pend[category(nm, inp, e["imgs"])] += e["tok"]
            elif k in ("text", "prompt"):
                pend[e["cat"]] += e["tok"]
            elif k == "call":
                if prev is not None and e["id"] not in seen_calls:
                    delta = ctx_of(e["u"]) - ctx_of(prev["u"])
                    comp = Counter(pend)
                    vis = prev.get("vis") or Counter()
                    outt = prev["u"].get("output_tokens") or 0
                    comp.update(vis)
                    comp["out:thinking (output - visible)"] += max(0, outt - sum(vis.values()))
                    ncalls += 1
                    for c, v in comp.items():
                        cat_est[c] += v
                    if delta > 0:
                        pos_total += delta
                        s = sum(comp.values())
                        # proportional when the estimate roughly covers the delta; otherwise
                        # credit estimates in full and book the rest as residual
                        scale = delta / s if s and delta <= 1.5 * s + 2000 else (1.0 if s else 0)
                        for c, v in comp.items():
                            share = v * scale
                            cat_tok[c] += share
                            sess_cat[(proj, sname)][c] += share
                        resid = delta - s * scale
                        if resid > 1:
                            rc = "residual: after >ttl gap (resume/cold)" if e["t"] - prev["tend"] > ttl else "residual: warm"
                            unexplained += resid
                            cat_tok[rc] += resid
                            sess_cat[(proj, sname)][rc] += resid
                            comp[rc] = resid
                        cause = comp.most_common(1)[0] if comp else ("?", 0)
                        jumps.append((delta, sname, proj, cause[0], round(cause[1]),
                                      time.strftime("%Y-%m-%d %H:%M", time.localtime(e["t"]))))
                    else:
                        neg_total += -delta
                if e["id"] in seen_calls and prev is not None:
                    pass
                seen_calls.add(e["id"])
                prev = e
                pend = Counter()

        # ---------------- B/C: compactions ----------------
        segs = segments(ev)
        all_turns = prompt_turns(ev, range(len(ev)))
        durs = [c[-1]["tend"] - p["t"] for p, c in all_turns if c and not p["cmd"]]
        med_dur = st.median(durs) if durs else None
        prev_comp_t = None
        prev_comp_calls = None
        for si in range(1, len(segs)):
            seg = segs[si]
            ci = seg[0]
            rec = ev[ci]["rec"]
            uuid = rec.get("uuid")
            md = rec.get("compactMetadata") or {}
            pre = md.get("preTokens") or 0
            trig = md.get("trigger")
            t0 = ev[ci]["t"]
            pre_idx = segs[si - 1] if si - 1 >= 0 else []
            pre_idx = [i for i in pre_idx if ev[i]["k"] != "compact"]
            pre_calls = [ev[i] for i in pre_idx if ev[i]["k"] == "call"]
            post_calls = [ev[i] for i in seg if ev[i]["k"] == "call"]
            if not post_calls or not pre:
                continue
            # prune / handoff detection in the last 40 calls / since the last human prompt before the compact
            tail = pre_idx[-400:]
            handoff_written = any(HANDOFF_RE.search(str(inp.get("file_path") or ""))
                                  for i in tail if ev[i]["k"] == "call"
                                  for _, nm, inp in ev[i]["tools"] if nm in ("Write", "Edit"))
            prune_cmd = any(ev[i].get("prune") or ev[i].get("bonsai_args") for i in tail
                            if ev[i]["k"] in ("prompt", "text")) or \
                any(ev[i].get("bonsai_args") for i in seg[:10] if ev[i]["k"] == "prompt")
            kind = "auto" if trig == "auto" else ("handoff" if (handoff_written or prune_cmd) else "plain")

            # pre-segment repeat keys and baseline
            pre_keys, pre_files = set(), set()
            base_rr = 0
            seen_tmp = set()
            for e in pre_calls:
                for tid, nm, inp in e["tools"]:
                    rk = repeat_key(nm, inp)
                    fk = file_key(nm, inp)
                    if rk:
                        pre_keys.add(rk)
                    if fk:
                        if fk in seen_tmp:
                            base_rr += 1
                        seen_tmp.add(fk)
                        pre_files.add(fk)
            pre_reads = sum(1 for e in pre_calls for _, nm, _ in e["tools"] if nm == "Read")
            # result tokens by tool id
            res_tok = {ev[i]["tid"]: ev[i]["tok"] for i in seg if ev[i]["k"] == "result"}

            win = post_calls[:a.window]
            rr_n = rr_tok = 0
            rr_by = Counter()
            rr_at = []            # (call index, tokens)
            handoff_read = False
            ho_tok = 0
            post_tools = 0
            for j, e in enumerate(win):
                for tid, nm, inp in e["tools"]:
                    post_tools += 1
                    if nm == "Read" and HANDOFF_RE.search(str(inp.get("file_path") or "")):
                        handoff_read = True
                        ho_tok += res_tok.get(tid, 0)
                        continue
                    if nm in ("Bash", "PowerShell") and "handoffs" in (inp.get("command") or ""):
                        handoff_read = True
                    rk = repeat_key(nm, inp)
                    fk = file_key(nm, inp)
                    if (rk and rk in pre_keys) or (fk and fk in pre_files):
                        tk = res_tok.get(tid, 0)
                        rr_n += 1
                        rr_tok += tk
                        rr_by[nm] += tk
                        rr_at.append((j, tk))
            # also: handoff named in summary but read via attachment? count Reads anywhere in first 30
            if not handoff_read:
                for e in post_calls[a.window:30]:
                    for tid, nm, inp in e["tools"]:
                        if nm == "Read" and HANDOFF_RE.search(str(inp.get("file_path") or "")):
                            handoff_read = True
                            ho_tok += res_tok.get(tid, 0)

            # prompt-turn stats
            pre_turns = [x for x in prompt_turns(ev, pre_idx) if not x[0]["cmd"] and x[1]]
            post_turns = [x for x in prompt_turns(ev, seg) if not x[0]["cmd"] and x[1]]
            tc = lambda calls: sum(len(c["tools"]) for c in calls)
            pre_tpt = st.mean([tc(c) for _, c in pre_turns]) if pre_turns else None
            pre_cpt = st.mean([len(c) for _, c in pre_turns]) if pre_turns else None
            p5 = post_turns[:5]
            post_tpt = st.mean([tc(c) for _, c in p5]) if p5 else None
            post_cpt = st.mean([len(c) for _, c in p5]) if p5 else None
            first_dur = (p5[0][1][-1]["tend"] - p5[0][0]["t"]) if p5 else None
            extra_signed = (post_cpt - pre_cpt) * len(p5) if (p5 and pre_cpt is not None) else 0.0
            extra_calls = max(0.0, extra_signed)

            post = ctx_of(post_calls[0]["u"])
            fl = Counter()
            for i in seg:
                if ev[i]["k"] == "call":
                    break
                if ev[i]["k"] in ("text", "prompt"):
                    c = ev[i]["cat"]
                    fl["summary" if c == "compact summary" else
                       "restored files" if c in ("attach:file", "attach:compact_file_reference") else "other msgs"] += ev[i]["tok"]
            removed = max(pre - post, 0)
            regrow = {n: (ctx_of(post_calls[n - 1]["u"]) if len(post_calls) >= n else None) for n in (1, 3, 5, 10, 15)}

            # ---- C: prunestats-style saved + corrections ----
            prev_turn_t = pre_calls[-1]["t"] if pre_calls else t0
            saved = 0.0
            rates = []
            last_t = prev_turn_t
            for k2, e in enumerate(post_calls):
                u = e["u"]
                gap = e["t"] - last_t
                last_t = e["t"]
                cold = gap > ttl or (u.get("cache_creation_input_tokens") or 0) > 0.5 * ctx_of(u)
                if ctx_of(u) + removed > a.limit:
                    break
                if k2 == 0:
                    if gap > ttl:
                        saved += W_WRITE * removed
                    else:
                        saved += W_READ * pre - weighted(u) + W_IN * (u.get("input_tokens") or 0) + W_OUT * (u.get("output_tokens") or 0)
                    rates.append(W_WRITE if gap > ttl else W_READ)
                else:
                    r = W_WRITE if cold else W_READ
                    saved += r * removed
                    rates.append(r)
            n_after = len(rates)
            # re-read: written once (W_WRITE) then carried at each later call's rate
            rr_cost = 0.0
            for j, tk in rr_at:
                if j < n_after:
                    rr_cost += W_WRITE * tk + sum(rates[j + 1:]) * tk
            avg_call = st.mean([weighted(c["u"]) for c in post_calls[:n_after]]) if n_after else 0
            extra_cost = extra_calls * avg_call
            extra_cost_signed = extra_signed * avg_call
            # compaction run cost (estimate, as prunestats when no acompact file)
            warm = (t0 - prev_turn_t) < ttl
            run_cost = pre * (W_READ if warm else W_WRITE) + W_OUT * max(post // 4, 2000)
            sdir = path[:-6]
            for p in glob.glob(os.path.join(sdir, "subagents", "agent-acompact-*.jsonl")):
                try:
                    pe, _ = parse_any(p)
                except Exception:
                    continue
                if pe and abs(pe[-1]["t"] - t0) < 300:
                    run_cost = sum(weighted(x["u"]) for x in pe)
                    break
            handoff_cost = 0.0
            if kind == "handoff":
                # turns from the last prune command/skill text to the boundary
                pk = [i for i in pre_idx if ev[i]["k"] in ("prompt", "text") and ev[i].get("prune")]
                if pk:
                    handoff_cost = sum(weighted(ev[i]["u"]) for i in pre_idx if i > pk[-1] and ev[i]["k"] == "call")
            cost0 = run_cost + handoff_cost
            cost1 = cost0 + rr_cost + extra_cost
            per_turn = W_READ * removed
            gap_calls = len(pre_calls) if si >= 2 else None
            row = {
                "session": sname, "proj": proj, "uuid": uuid, "when": time.strftime("%Y-%m-%d %H:%M", time.localtime(t0)),
                "kind": kind, "trigger": trig, "pre": pre, "post": post, "removed": removed,
                "calls_since_prev_compact": gap_calls, "hours_since_prev_compact": (t0 - prev_comp_t) / 3600 if prev_comp_t else None,
                "calls_after": len(post_calls), "rr_n": rr_n, "rr_tok": round(rr_tok), "rr_by": dict(rr_by),
                "pre_reads": pre_reads, "pre_rereads": base_rr, "pre_calls": len(pre_calls),
                "handoff_written": handoff_written, "handoff_read": handoff_read,
                "pre_tools_per_turn": pre_tpt, "post_tools_per_turn": post_tpt,
                "pre_calls_per_turn": pre_cpt, "post_calls_per_turn": post_cpt, "extra_calls": extra_calls,
                "first_turn_s": first_dur, "median_turn_s": med_dur,
                "regrow": regrow, "n_counted": n_after,
                "saved": saved, "rr_cost": rr_cost, "extra_cost": extra_cost, "extra_cost_signed": extra_cost_signed,
                "ho_tok": round(ho_tok), "floor_summary": fl["summary"], "floor_files": fl["restored files"],
                "floor_other": fl["other msgs"], "net1s": saved - cost0 - rr_cost - extra_cost_signed,
                "cost0": cost0, "cost1": cost1, "net0": saved - cost0, "net1": saved - cost1,
                "be0": cost0 / per_turn if per_turn else None, "be1": cost1 / per_turn if per_turn else None,
            }
            old = comps.get(uuid)
            if old is None or old["calls_after"] < row["calls_after"]:
                comps[uuid] = row
            prev_comp_t = t0

    # ======================= REPORT =======================
    K = lambda n: f"{n / 1000:,.1f}k" if n is not None else "-"
    print(f"\n=== A. Context growth attribution ({len(files)} transcripts, {ncalls:,} main-thread calls) ===")
    print(f"positive growth {K(pos_total)} tok; drops {K(neg_total)}; residual (delta >> estimate) {K(unexplained)}")
    est_sum = sum(cat_est.values())
    print(f"raw chars/4 estimate total {K(est_sum)} (ratio to positive growth {est_sum / pos_total:.2f})")
    agg = Counter()
    for c, v in cat_tok.items():
        agg[c] = v
    print(f"{'category':34} {'attributed':>11} {'%':>5} {'raw est':>10}")
    for c, v in agg.most_common(30):
        print(f"{c:34} {K(v):>11} {100 * v / pos_total:>4.1f}% {K(cat_est.get(c, 0)):>10}")
    # family roll-up
    fam = Counter()
    for c, v in agg.items():
        f = c.split(":")[0] if c.startswith(("Read", "mcp__", "attach", "user", "other", "skill")) else c
        if c.startswith("out:tool_use"):
            f = "out:tool_use inputs"
        if c.startswith("mcp__") and c.endswith(":image"):
            f = "mcp images (screenshots)"
        fam[f] += v
    print("\nroll-up:", ", ".join(f"{c} {100 * v / pos_total:.1f}%" for c, v in fam.most_common(14)))

    print("\nTop growth sources per session (top 15 sessions by growth):")
    tot_s = sorted(sess_cat.items(), key=lambda kv: -sum(kv[1].values()))[:15]
    for (proj, sn), cc in tot_s:
        tot = sum(cc.values())
        top = ", ".join(f"{c} {100 * v / tot:.0f}%" for c, v in cc.most_common(3))
        print(f"  {sn[:40]:40} {K(tot):>9}  {top}")

    print("\n20 biggest single-call jumps:")
    jumps.sort(reverse=True)
    for d, sn, proj, cause, cv, when in jumps[:20]:
        print(f"  +{K(d):>8} {when} {sn[:36]:36} {cause} (~{K(cv)})")
    big = [j for j in jumps if j[0] >= 10_000]
    bc = Counter()
    for j in big:
        c = j[3]
        bc[c.split(":")[0] if not c.startswith("mcp__") else ("mcp image" if c.endswith("image") else "mcp text")] += j[0]
    print(f"\njumps >= 10k: {len(big)} calls, {K(sum(j[0] for j in big))} tok = {100 * sum(j[0] for j in big) / pos_total:.0f}% of all growth; by cause:",
          ", ".join(f"{c} {K(v)}" for c, v in bc.most_common(10)))

    rows = sorted(comps.values(), key=lambda r: r["when"])
    print(f"\n=== B. Compactions (main thread, n={len(rows)}) ===")
    print(f"{'when':16} {'kind':7} {'pre':>7} {'removed':>8} {'rr#':>4} {'rr tok':>7} {'HOr':>3} {'tpt pre/post':>12} {'1st/med s':>11} {'+1':>6} {'+5':>6} {'+10':>6} session")
    for r in rows:
        rg = r["regrow"]
        tp = f"{r['pre_tools_per_turn'] or 0:.1f}/{r['post_tools_per_turn'] or 0:.1f}"
        ds = f"{(r['first_turn_s'] or 0):.0f}/{(r['median_turn_s'] or 0):.0f}"
        print(f"{r['when']:16} {r['kind']:7} {K(r['pre']):>7} {K(r['removed']):>8} {r['rr_n']:>4} {K(r['rr_tok']):>7} "
              f"{'Y' if r['handoff_read'] else '-':>3} {tp:>12} {ds:>11} {K(rg[1]):>6} {K(rg[5]):>6} {K(rg[10]):>6} {r['session'][:28]}")

    def summ(sub, label):
        if not sub:
            return
        md = lambda xs: st.median(xs) if xs else float("nan")
        mn = lambda xs: st.mean(xs) if xs else float("nan")
        rr = [r["rr_tok"] for r in sub]
        rrf = [r["rr_tok"] / r["removed"] for r in sub if r["removed"]]
        g10 = [r["regrow"][10] - r["post"] for r in sub if r["regrow"][10] is not None]
        g10f = [(r["regrow"][10] - r["post"]) / r["removed"] for r in sub if r["regrow"][10] is not None and r["removed"]]
        tr = [r["post_tools_per_turn"] / r["pre_tools_per_turn"] for r in sub if r["post_tools_per_turn"] is not None and r["pre_tools_per_turn"]]
        dr = [r["first_turn_s"] / r["median_turn_s"] for r in sub if r["first_turn_s"] and r["median_turn_s"]]
        hr = sum(r["handoff_read"] for r in sub)
        posts = [r["post"] for r in sub]
        hot = [r["ho_tok"] for r in sub if r["handoff_read"]]
        print(f"{'':22}   post-compact floor (ctx of 1st call) median {K(md(posts))} = summary {K(md([r['floor_summary'] for r in sub]))}"
              f" + restored files {K(md([r['floor_files'] for r in sub]))} + other msgs {K(md([r['floor_other'] for r in sub]))} + system/tools rest;"
              f" handoff Read tokens median {K(md(hot))}")
        print(f"{label:22} n={len(sub):>3} rr tok med {K(md(rr)):>7} mean {K(mn(rr)):>7} | rr/removed med {100 * md(rrf):4.1f}% "
              f"| regrow@10 med {K(md(g10)):>7} ({100 * md(g10f):4.1f}% of removed) | tools/turn ratio med {md(tr):.2f} "
              f"| 1st-turn/median time {md(dr):.2f} | handoff read {hr}/{len(sub)}")

    print("\n--- B summary by kind ---")
    for kd in ("handoff", "plain", "auto"):
        summ([r for r in rows if r["kind"] == kd], kd)
    summ(rows, "all")
    base = [r for r in rows if r["pre_reads"]]
    if base:
        print(f"pre-compact baseline: {sum(r['pre_rereads'] for r in base)} re-reads of {sum(r['pre_reads'] for r in base)} Reads "
              f"({100 * sum(r['pre_rereads'] for r in base) / max(1, sum(r['pre_reads'] for r in base)):.0f}%); "
              f"post window: {sum(r['rr_n'] for r in rows)} repeat calls in {sum(min(a.window, r['calls_after']) for r in rows)} calls")
    rb = Counter()
    for r in rows:
        rb.update(r["rr_by"])
    print("re-read tokens by tool:", ", ".join(f"{c} {K(v)}" for c, v in rb.most_common()))

    man = [r for r in rows if r["kind"] != "auto" and r["n_counted"]]
    print(f"\n=== C. Corrected net (manual compactions, n={len(man)}) ===")
    print(f"{'when':16} {'kind':7} {'removed':>8} {'calls':>5} {'saved':>8} {'cost':>7} {'+reread':>8} {'+extra':>7} {'net0':>8} {'net1':>8} {'BE0':>5} {'BE1':>5}")
    for r in man:
        print(f"{r['when']:16} {r['kind']:7} {K(r['removed']):>8} {r['n_counted']:>5} {K(r['saved']):>8} {K(r['cost0']):>7} "
              f"{K(r['rr_cost']):>8} {K(r['extra_cost']):>7} {K(r['net0']):>8} {K(r['net1']):>8} {r['be0'] or 0:>5.0f} {r['be1'] or 0:>5.0f}")
    if man:
        s0 = sum(r["net0"] for r in man)
        s1 = sum(r["net1"] for r in man)
        sv = sum(r["saved"] for r in man)
        s1s = sum(r["net1s"] for r in man)
        print(f"signed extra-call correction (post-pre calls/turn may be negative): net {K(s1s)}; "
              f"extra-call signed total {K(sum(r['extra_cost_signed'] for r in man))}")
        print(f"total saved {K(sv)}; net uncorrected {K(s0)}; corrected {K(s1)}; overestimate {K(s0 - s1)} "
              f"({100 * (s0 - s1) / s0 if s0 else 0:.0f}% of uncorrected net); re-read part {K(sum(r['rr_cost'] for r in man))}, "
              f"extra-call part {K(sum(r['extra_cost'] for r in man))}")
        print(f"median break-even calls: uncorrected {st.median([r['be0'] for r in man if r['be0']]):.0f}, "
              f"corrected {st.median([r['be1'] for r in man if r['be1']]):.0f}; "
              f"negative-net share: uncorrected {sum(r['net0'] < 0 for r in man)}/{len(man)}, corrected {sum(r['net1'] < 0 for r in man)}/{len(man)}")

    print("\n=== D. Comparisons ===")
    for kd in ("handoff", "plain"):
        sub = [r for r in man if r["kind"] == kd]
        if sub:
            print(f"{kd:8} n={len(sub):>3} median removed {K(st.median([r['removed'] for r in sub]))}, "
                  f"median net0 {K(st.median([r['net0'] for r in sub]))}, net1 {K(st.median([r['net1'] for r in sub]))}, "
                  f"median BE1 {st.median([r['be1'] for r in sub if r['be1']] or [0]):.0f} calls")
    freq = [r for r in rows if r["calls_since_prev_compact"] is not None]
    for lab, sub in (("frequent (<=60 calls since prev)", [r for r in freq if r["calls_since_prev_compact"] <= 60]),
                     ("spaced (>60 calls)", [r for r in freq if r["calls_since_prev_compact"] > 60]),
                     ("first compact of session", [r for r in rows if r["calls_since_prev_compact"] is None])):
        summ(sub, lab[:22])
        m2 = [r for r in sub if r["kind"] != "auto" and r["n_counted"]]
        if m2:
            print(f"{'':22}   manual n={len(m2)}: median removed {K(st.median([r['removed'] for r in m2]))}, "
                  f"net1 median {K(st.median([r['net1'] for r in m2]))}, negative {sum(r['net1'] < 0 for r in m2)}/{len(m2)}")
    # size bands
    print("\nby preTokens band (manual):")
    for lo, hi in ((0, 150e3), (150e3, 250e3), (250e3, 400e3), (400e3, 2e6)):
        sub = [r for r in man if lo <= r["pre"] < hi]
        if sub:
            print(f"  {K(lo):>7}-{K(hi):>7} n={len(sub):>3} median rr {K(st.median([r['rr_tok'] for r in sub]))}, "
                  f"net1 median {K(st.median([r['net1'] for r in sub]))}, neg {sum(r['net1'] < 0 for r in sub)}/{len(sub)}, "
                  f"median calls after {st.median([r['n_counted'] for r in sub]):.0f}")
    if a.json:
        json.dump(rows, open(a.json, "w"), indent=1, default=str)
        print("\nrows ->", a.json)


def parse_any(path):
    """Calls of a subagent file (sidechain lines included)."""
    out, seen = [], set()
    with open(path, "rb") as f:
        for raw in f:
            if b'"usage"' not in raw:
                continue
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except Exception:
                continue
            m = d.get("message") or {}
            if d.get("type") == "assistant" and m.get("usage") and m.get("id") not in seen and d.get("timestamp"):
                seen.add(m.get("id"))
                out.append({"t": ts(d["timestamp"]), "u": m["usage"]})
    return out, None


if __name__ == "__main__":
    main()
