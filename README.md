# Bonsai

**Bonsai saves tokens, saves time and keeps Claude sharp — all while you're away — so you never have to think about compacting.**

Every turn in Claude Code re-reads the whole conversation, so a session that has grown to 250k tokens costs about 250k tokens a turn, even when most of that is finished work, and all that stale context makes answers less focused. A prompt cache makes those re-reads cheap, but it expires an hour after the last reply. Once it's gone, the next message re-writes the whole context at full price.

Bonsai watches every session on your machine and prunes the big ones at the right moment. When you step away from the PC, or a session's cache is about to expire, it writes a detailed handoff file and then compacts the session. When you come back, the session resumes from the handoff instead of from a 250k-token transcript.

![The Bonsai panel](docs/panel.png)

## What you get

- **A Bonsai button** in the prompt footer, which opens the panel (or use `/bonsai`).
- **Warm sessions:** every session whose prompt cache is still alive, with minutes left, context size, and a **Prune** button. Sessions about to go cold are flagged.
- **Auto-prune** (off until you switch it on in the panel): sessions where a prune pays off (see [When a prune pays](#when-a-prune-pays)), or over a size you pick, hand off and compact on their own when:
  - you've been away from the keyboard for 10 minutes and the session hasn't replied for 10 minutes, so the break gets used for housekeeping (both clocks run together, so in practice it fires 10–20 minutes after the last reply), or
  - their cache is down to its last 10 minutes and about to expire.

  All three settings are dropdowns in the panel. Auto-prune only acts on a session while no turn is running in it.
- **Exclude:** the ✕ on a row keeps that session out of auto-prune, for a disposable giant task you'd rather not have compacted. the **↺** on an excluded row puts it back. Manual Prune still works on excluded sessions.
- **This session:** context size against the point where a prune starts to pay, how many of your messages a prune would take to pay off (counted at that session's own API calls per message), what grew the context since the last compact (thinking, shell output, file reads, edits, skills and so on), the biggest single jump, a trend line of the context over time, what the last API call cost (tokens read from the cache, written to it, sent uncached and output, in API-price dollars), the total for all calls since your last message, and the cache time left.
- **Usage:** your plan's 5-hour and weekly limits as meters with their reset times (the same figures as Claude Code's status line), and per model (Fable, Opus, Sonnet, Haiku) what today and the last 7 days would cost at API prices, with call counts. The per-model figures come from this PC's transcripts, so they cover every session here and nothing from other machines. On a subscription the dollars are a way to compare models, not a bill.
- **Savings:** an estimate of tokens saved by pruning over the last 30 days. It's measured in input-token equivalents, against never compacting at all. It subtracts files read again after a compact and the extra calls of the first prompt afterwards, but still reads best as an upper bound.
- **A context nudge:** once a prune would pay off, a bar appears above the prompt with the session's context size, how many of your messages the prune takes to pay off, the cache time, and two buttons. **Prune** writes the handoff and compacts, the same as auto-prune. **Dismiss** hides the bar until the context grows another 75k. While any prune of the session runs, the bar reads **Bonsai: pruning…** until the compact is done. The bar is drawn by the plugin, so it costs no tokens.

- **A prune skill:** `/bonsai-context:prune [task-name]` writes the handoff and gives you the `/compact` line to run.

### What "Prune" does

Usually it's one step, a **lean prune**: the session is compacted with the handoff standard as the compact's instructions, so the summary *is* the handoff. It holds the goal, the current state, decisions and why, exact values, and next steps, in enough detail that a fresh model can continue without re-reading files. Bonsai then copies the summary to `~/.claude/handoffs/<task>.md`, with no model call. The compact already reads the whole context from the cache, so writing the handoff there saves the separate handoff turn: about 40% of a prune's cost in a measured prune.

When a subagent of the session was active in the last 45 minutes, Bonsai does a **full prune** instead, because only a model turn can ask a subagent for its handoff:

1. Claude writes `~/.claude/handoffs/<task>.md`.
2. For each subagent: if its cache is still warm and it holds unfinished work, Claude asks it to write its own handoff. Cold subagents are never woken, because waking one re-sends its whole context at full price.
3. The session is compacted, with instructions to read the handoff first.

## When a prune pays

A prune isn't free. The compact re-reads the whole context, the context left afterwards has to be cached again, and a full prune's handoff turn re-reads the context a few more times, so a full prune costs about one context's worth of tokens. What it saves is the part it removes, on every later call. Bonsai works this out for each session, so the threshold floats instead of being a fixed size:

- **Removable** = context − what's left after a compact (the system prompt, tools, memory and summary: about 80k, or this session's own figure once it has been compacted).
- **Cost** = the compact reading the context from the cache, its cache write, the first call afterwards caching the new context again (the median from your savings ledger, about 30k), and the summary itself. These parts were measured on a real prune.
- **Break-even** = cost ÷ (cache-read price × removable), in calls. A prune pays when the session is likely to run longer than that. The estimate of calls still to come is the session's own length so far.
- **Guards:** at least 100k removable, and at least 60 calls since the last compact. In the author's history, prunes soon after the previous one lost tokens a quarter of the time, and spaced-out ones never did.
- **Prices are per model.** A cache read costs 0.025× the input price on Fable 5.1, 0.05× on Opus 5.5 and 0.1× on Sonnet, so carrying a big context is cheapest on Fable, and a prune there takes about twice as many calls to pay off as on Opus.
- **Before the cache expires**, the next message would rebuild the whole context at full price, so a prune pays at much smaller sizes. The cache-expiry trigger uses that case.

To pick a fixed size instead, set the panel's **Over** dropdown to one.

## Results

Over two months of the author's own heavy use (early compactions, by hand at first and then with this plugin, on sessions of 90k–930k tokens), compacting early saved an estimated **37 million input-token equivalents**: 35% of what those stretches of conversation would have cost without compacting, after subtracting the cost of the compaction and the handoff. That's with each model's own cache prices (1.5.0); the earlier figure of 58.8 million assumed cache reads cost 0.1× input on every model, which overstated savings on Fable and Opus 5.5. The biggest wins were long sessions pruned at 400k+. Small prunes of sessions under about 170k, or prunes just before a session ended, sometimes cost slightly more than they saved.

That's one person's workload, not a benchmark. The panel's **Savings** line runs the same estimate on yours.

## Install

In a Claude Code **terminal** session:

```
/plugin install bonsai-context --marketplace sethwhite42/bonsai
```

Answer `y` to add the marketplace, then pick a scope (user scope puts it in every session). The plugin also loads in the Claude desktop app's Code tab once it's installed. The install command itself only works from a terminal.

**Requirements:**
- **Python 3** on the PATH as `python3`, `python` or `py`. Only the standard library is used.
- **Claude Code** with plugin hooks modules (2.1.29x or newer).

**Idle detection** (for "when I'm away"):

| Platform | How it's read | Notes |
| --- | --- | --- |
| Windows | `GetLastInputInfo` | built in |
| macOS | `ioreg` | built in |
| Linux/X11 | `xprintidle` | must be installed |

Where idle time can't be read, only the cache-expiry trigger prunes.

## What it runs and touches

**Sends:** nothing. Bonsai makes no network calls; everything stays on your machine.

**Reads:** session transcripts under `~/.claude/projects/`, for token usage, timestamps and session titles only, to size contexts and time caches.

**Programs it runs, and why:**
- **Python 3** (`python3`, `python` or `py`, whichever exists) to run its own scripts in the plugin's `scripts/` folder:
  - `common.py`: finds the state folder and reads idle time.
  - `panelview.py`: builds the panel and decides whether auto-prune should act. It runs every 30 seconds in each session where Bonsai is loaded.
  - `cachecheck.py`: lists warm sessions. It's run by `panelview.py`, and by the prune skill to see which subagents are still warm.
  - `growth.py`: keeps a running record of what grew this session's context, reading only what the transcript gained since the last refresh. It's run by `panelview.py`.
  - `leanprune.py`: checks for recently active subagents before a prune, and after a lean prune copies the compact's summary from the transcript to the handoff file.
  - `usage.py` and `prices.py`: the per-model usage in the Usage box, reading only what each transcript gained since the last refresh, and the price table the savings math uses.
  - `econ.py`: works out whether a prune pays off now (see [When a prune pays](#when-a-prune-pays)). It's run by `panelview.py`.
  - `prunestats.py`: estimates savings for the panel's Savings line. It's run in the background about once an hour.
  - `ctxstudy.py`: an optional report you can run yourself (`python ctxstudy.py`) on what grows context across all your sessions, and what happens after each compact. Bonsai never runs it.
- **`ioreg`** (macOS) or **`xprintidle`** (Linux), to read how long you've been away from the keyboard. On Windows the same reading comes from the `GetLastInputInfo` system call.

**Hooks:** none on your prompts or tool calls. Bonsai never adds text to what you send Claude. The nudge bar, the panel and the footer button are drawn by its hooks module.

**Commands it runs in your session:** when a prune happens (you click Prune, or auto-prune fires once you've switched it on), Bonsai runs `/compact` with the handoff standard as its instructions (a lean prune). For a full prune it instead:
1. submits a prompt telling Claude to run the `bonsai-context:prune` skill unattended. It asks Claude to write the handoff file without asking questions, to end with the handoff path, and says the session will be compacted next.
2. runs `/compact` once that turn ends, with instructions to keep the handoff path in the summary and read it first.

It never runs anything else in your sessions.

**Writes** only to these places:
- `~/.claude/hooks-state/`: `bonsai-prefs.json` (your panel settings), `bonsai-live-<session>.json` (a heartbeat showing Bonsai is running in that session), `bonsai-log-<session>.log` (a step log), `prune-request-<session>.json` (prune requests between sessions), `bonsai-growth-<session>.json` (what grew that session's context), `ctxnudge-<session>.json` (where you last dismissed the nudge bar), `bonsai-usage.json` (running usage totals per day and model), `bonsai-ratelimits.json` (your plan's limit figures), `lean-prunes.jsonl` (a line per lean prune, for the savings ledger), `prunestats.json` (the savings ledger) and `prunestats-refresh.stamp` (when savings were last updated).
- `~/.claude/handoffs/`: the handoff files written during a prune.

It never edits Claude Code's settings, permissions, instructions or any project files.

## Notes

- **Where state is kept:** in `~/.claude/hooks-state/`, which holds prefs, per-session logs, prune requests and the savings ledger. Handoffs go to `~/.claude/handoffs/`.
- **What it assumes:**
  - **Cache lifetime:** 60 minutes, which is Claude Code's normal cache lifetime.
  - **Context size:** read from each session's transcript under `~/.claude/projects/`.
- **Sessions it never prunes:**
  - **Sessions driven by another program** (`claude -p` from an app or script) are listed but not pruned. No plugin runs between that program's calls to pick up the request.
  - **Sessions where no panel is open** can't be pruned from another session's panel. Prune works on a session only while its own Claude Code is running with the plugin loaded.
- It's built for the desktop app and terminal, and tested mostly on Windows with the desktop app.

## License

MIT
