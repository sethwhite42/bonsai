---
name: prune
description: Prune stale context. Write a detailed handoff file (plus one per warm subagent) so a compacted session can resume cheaply, then give the exact /compact line. Use when the context is large and much of it is finished work, before stepping away, or when Bonsai asks for it.
argument-hint: "[short task name, e.g. moon-film]"
---

I'm stepping away. Save everything needed to resume cheaply, while the cache is still warm. Keep the number of turns small (write, don't explore), but do NOT keep the handoff short.

## Level of detail: err toward MORE

The expensive failure after a compact is the next model re-reading files and re-running searches to re-learn what you know right now. Reading a long handoff once costs far less than that. So the test for every section: **could a fresh model start the next step without opening a file just to re-orient?** If not, add what it would have gone looking for.

That means writing down what you learned from the files themselves, not only what you did: the relevant functions/classes and where they live (path:line), how the pieces connect, data shapes and formats, gotchas and quirks you hit, which files matter and which turned out irrelevant. Quote short exact snippets, values and error messages rather than paraphrasing. No length cap: several thousand words is fine if it's all load-bearing. Cut only narration (the order things happened in) and anything already in memory files or CLAUDE.md.

Task name: $ARGUMENTS (if empty, pick a short kebab-case name for the current workstream and tell me).

## 1. Write the main handoff

Write `~/.claude/handoffs/<task>.md` (overwrite if it exists; it's the current state, not a log). Wherever this skill writes `~`, write out the absolute path of your home folder instead, so the path survives the compact. Write it for a fresh model with no memory of this session. Include:

- **Goal**: what we're ultimately doing, in two or three lines.
- **Current state**: what's done, what's working, what's half-finished. Exact file paths.
- **Decisions and why**: including approaches tried and rejected, and the reason, so they aren't retried.
- **Exact values**: settings, parameters, prompt text, IDs, commands, numbers that took effort to find. Verbatim.
- **My preferences and don'ts**: anything I said about how I want this done, or not done.
- **Open questions waiting on me**.
- **Mid-conversation**: what we were actively discussing when I left.
- **Next steps**: ordered, concrete.
- **Subagents**: one line per active subagent: what it was doing, and the path to its handoff.

Leave out anything already recorded in memory files or CLAUDE.md; link to them instead.

## 2. Subagent handoffs

Messaging a subagent makes it re-read its entire context. If its cache has gone cold, that one message rebuilds the whole context at the cache-write price, which can cost more than everything else in this routine. So decide per subagent; the default is NOT to wake it.

First check warmth, in ONE Bash call: `python "${CLAUDE_PLUGIN_ROOT}/scripts/cachecheck.py" --all-agents -n 3; echo ----; python "${CLAUDE_PLUGIN_ROOT}/scripts/cachecheck.py" --warm` (use `python3` where `python` isn't found) (the first lists each subagent's last activity; the second is the all-sessions warmth view for step 3; run it even if this session has no subagents). Then sort every subagent from this session into one bucket:

- **Done or superseded** (finished its job, its results already reported back to you, or replaced by a newer agent): discard. Don't message it. Just note in the main handoff what it produced, if that matters.
- **Cold** (last active more than ~45 min ago), even if its work is unfinished: don't wake it. Write its section in the main handoff yourself, from its last report and what you know. Note that a fresh agent will need to pick up from there.
- **Warm and holding unfinished work that isn't already in your context**: only these get the message below.

List your bucket decisions in step 3 so I can overrule them. For each subagent in the third bucket, use SendMessage to tell it:

> Write your handoff to `~/.claude/handoffs/<task>--<short-agent-role>.md`: goal, current state, exact values and paths, what you learned from the files (functions/classes at path:line, how pieces connect, data formats, gotchas), what you tried that failed, next steps. Write it for a fresh agent with none of your context, detailed enough that it can start the next step without re-reading files to re-orient. Err toward more detail; no length cap. Then stop. Do no further work.

Don't resume a subagent for anything else. If no subagent qualifies, say so and skip this step.

## 3. Show me

First paste the `--warm` output from step 2 as-is (it's markdown; NOT in a code block), so I can see which other sessions are still warm and which are about to go cold.

Give me a compact summary of the main handoff (headings and key points, not the full text) and list the files written. Ask if anything is missing. Add what I say.

## 4. Tell me the compact line

End with the exact line for me to run, filled in:

```
/compact Read ~/.claude/handoffs/<task>.md first; it holds the full state. We were in the middle of: <one line>.
```

Remind me: when resuming, continue subagent work by starting a NEW subagent briefed with its handoff file, not by reviving the old one.
