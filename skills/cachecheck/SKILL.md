---
name: cachecheck
description: Show how warm each session's prompt cache is (last active, minutes left)
argument-hint: [--ttl 5] [--hours 72] [--all-agents] [-n 20]
allowed-tools: Bash(python:*), Bash(python3:*)
---

!`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/cachecheck.py" $ARGUMENTS 2>/dev/null || python "${CLAUDE_PLUGIN_ROOT}/scripts/cachecheck.py" $ARGUMENTS`

Show me the output above exactly as-is in a code block. Add nothing else unless a session is flagged "step away / compact now"; then add one line naming it. Do not run any tools.
