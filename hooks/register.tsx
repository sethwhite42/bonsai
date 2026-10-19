import { atom, read, update } from 'claude-code'
import type { Register, Api } from 'claude-code'

import type { Block, Meter, Nudge, Part } from '../types'

// A generic drawer + prune listener. WHAT the pane shows comes from
// scripts/panelview.py (edit that: every session picks it up on its next
// refresh, no plugin reload). Change this file only for new block kinds or the
// prune mechanics.

const PANE = 'bonsai'
const SCAN_MS = 30_000
const POLL_MS = 5_000
// Set up by setup(): the plugin's folder, a Python that runs, and the state folder
// (scripts/common.py decides it, so the scripts and this module agree).
let ROOT = ''
let PY = 'python'
let STATE_DIR = ''
let ready: Promise<boolean> | null = null
const script = (name: string) => `${ROOT}/scripts/${name}`

async function setup($: Api): Promise<boolean> {
  ROOT = $.plugin.root.replace(/\\/g, '/').replace(/\/\.claude-plugin\/?$/, '')
  // python3 first (macOS/Linux); on Windows that name is often a Store stub that fails, so fall through
  for (const py of ['python3', 'python', 'py']) {
    try {
      const r = await $.process.run([py, script('common.py')], { timeoutMs: 15_000 })
      const d = JSON.parse(r.stdout)
      if (d.state) { PY = py; STATE_DIR = d.state; return true }
    } catch {}
  }
  return false
}
const isReady = ($: Api) => (ready ??= setup($))

// one log per session: a shared file lost lines when many sessions wrote at once (e.g. on restart)
const logPath = (sid8: string) => `${STATE_DIR}/bonsai-log-${sid8}.log`

const PRUNE_PROMPT =
  'Run the bonsai-context:prune skill now, unattended: Bonsai is pruning this session (I clicked ' +
  'Prune, or auto-prune fired because I stepped away from the PC or its cache was about to ' +
  'expire) and I am ' +
  'not watching it. Pick the task name yourself. Write the main handoff and ' +
  'any subagent handoffs as the skill says, but skip the step 3 question (nobody will ' +
  'answer). Still give the step 4 /compact line in case the automatic compact fails. End ' +
  'your reply with the handoff file path on its own line. The session will be compacted ' +
  'automatically right after this turn.'
const COMPACT_INSTRUCTIONS =
  'This session was pruned by Bonsai. The handoff file named at the end of ' +
  'the last reply holds the full state: keep its exact path in the summary and say to ' +
  'read it first. Resume subagent work with new subagents briefed from their handoff files.'

const view = atom({ plugin: 'bonsai-context', key: 'view' } as const, [])
const layout = atom({ plugin: 'bonsai-context', key: 'layout' } as const, {})
const footer = atom({ plugin: 'bonsai-context', key: 'footer' } as const, {})
const armed = atom({ plugin: 'bonsai-context', key: 'armed' } as const, null)
const nudge = atom({ plugin: 'bonsai-context', key: 'nudge' } as const, null as Nudge | null)

let phase: 'idle' | 'running' = 'idle'
let footerLogged = false
let nudgePruned = false // Prune pressed on the nudge bar
let busy = false // a turn is running in this session
let phaseAt = 0 // a prune that never reached turn.complete must not block later ones
const STUCK_MS = 15 * 60_000

const reqPath = (sid: string) => `${STATE_DIR}/prune-request-${sid}.json`
const livePath = (sid: string) => `${STATE_DIR}/bonsai-live-${sid}.json`
const prefsPath = () => `${STATE_DIR}/bonsai-prefs.json` // shared toggles, e.g. { autoPrune: true }
const nudgePath = (sid: string) => `${STATE_DIR}/ctxnudge-${sid}.json` // { last: ctx at the last Dismiss }

// The full panel opens on demand as a pop-up (the footer Bonsai button or /bonsai); Esc closes it.
// columns: the width it asks for when docked (a width the person dragged wins)
const PANE_COLS = 72
const openPanel = ($: Api) => $.ui.open({ id: PANE, title: 'Bonsai', focus: true, closeOnEscape: true, columns: PANE_COLS })
async function togglePanel($: Api) {
  if ((await $.ui.panes()).some(p => p.id === PANE)) await $.ui.close({ id: PANE })
  else await openPanel($)
}


async function readJson($: Api, path: string): Promise<any> {
  try {
    return JSON.parse(String(await $.fs.read(path)))
  } catch {
    return null
  }
}

// Steps go to a log file (panelview.py shows this session's last line in the pane).
async function note($: Api, msg: string) {
  if (!(await isReady($))) return
  const t = new Date(await $.clock.now()).toISOString()
  const sid = (await $.session.id()).slice(0, 8)
  let old = ''
  try { old = String(await $.fs.read(logPath(sid))) } catch {}
  await $.fs.write(logPath(sid), (old + `${t} ${sid} ${msg}\n`).slice(-50_000))
  void refresh($)
}

async function refresh($: Api) {
  let blocks: Block[]
  if (!(await isReady($))) {
    await update($, view, () => [{ kind: 'line', parts: [
      { text: 'Bonsai needs Python 3 on the PATH (python3, python or py).', color: 'error' }] }])
    return
  }
  try {
    const selfId = await $.session.id()
    // --blocks 2: this drawer knows kv blocks and row gaps (panelview keeps older drawers working)
    const r = await $.process.run([PY, script('panelview.py'), '--self', selfId, '--blocks', '2'], { timeoutMs: 25_000 })
    const d = JSON.parse(r.stdout)
    blocks = d.blocks
    await update($, layout, () => d.layout ?? {})
    await update($, footer, () => d.footer ?? {})
    // the bar stays down from a Prune press until the compact brings the context under the checkpoint
    if (!d.nudge) nudgePruned = false
    await update($, nudge, () => (nudgePruned || phase === 'running' ? null : d.nudge ?? null))
    // panelview.py decides WHEN (big context, and the PC idle or the cache nearly out); we add "no turn running"
    if (d.autoPruneSelf && !busy && phase === 'idle') {
      await $.fs.write(reqPath(selfId), JSON.stringify({ at: await $.clock.now(), auto: true }))
      await note($, `auto-prune (${d.autoReason || 'due'}); requesting prune`)
      void poll($)
    }
  } catch (err) {
    blocks = [{ kind: 'line', parts: [{ text: `panelview failed: ${String(err)}`, color: 'error' }] }]
  }
  await update($, view, () => blocks)
}

async function compact($: Api) {
  await note($, 'compacting')
  try {
    const r = await $.session.compact({ instructions: COMPACT_INSTRUCTIONS })
    await note($, r?.skip ? 'compact skipped by a hook; run /compact yourself' : 'compacted')
  } catch (err) {
    // headless sessions (the desktop app) have no $.session.compact: run /compact as if typed
    await note($, 'session.compact unavailable here; running /compact')
    try {
      await $.command.run({ command: 'compact', args: COMPACT_INSTRUCTIONS })
      await note($, 'compacted (via /compact)')
    } catch (err2) {
      await note($, `compact failed: ${String(err2)}; run /compact yourself`)
    }
  }
}

// Heartbeat (so other panels can offer Prune for this session) + own request check.
async function poll($: Api) {
  if (!(await isReady($))) return
  const selfId = await $.session.id()
  const now = await $.clock.now()
  await $.fs.write(livePath(selfId), JSON.stringify({ at: now }))
  const req = await readJson($, reqPath(selfId))
  if (phase === 'running' && now - phaseAt > STUCK_MS) phase = 'idle'
  if (req && !req.handled && phase === 'idle' && !busy) {
    await $.fs.write(reqPath(selfId), JSON.stringify({ ...req, handled: now }))
    await note($, 'prune request picked up; submitting prune prompt')
    // resolves as the prune turn starts, so the next turn.complete is that turn's
    await $.prompt.submit({ text: PRUNE_PROMPT, asUser: true })
    phase = 'running'
    phaseAt = await $.clock.now()
    await note($, 'prune turn started; will compact when it ends')
  }
}

export const register: Register = on => {
  on('session.start', async ($, e, next) => {
    await $.command.register({ name: 'bonsai', description: 'Open the Bonsai panel (Esc closes it)' })
    void note($, `loaded ${$.plugin.name}`)
    void refresh($)
    void poll($)
    $.clock.every(SCAN_MS, () => void refresh($))
    $.clock.every(POLL_MS, () => void poll($))
    return next(e)
  })

  on('command.run', { command: 'bonsai' }, async $ => {
    await refresh($)
    await openPanel($)
    return { text: 'Bonsai panel opened.' }
  })

  on('prompt.submit', async ($, e, next) => {
    busy = true
    return next(e)
  })

  on('turn.complete', async ($, e, next) => {
    const r = await next(e)
    busy = false
    if (phase === 'running') {
      phase = 'idle'
      $.clock.after(1500, () => void compact($))
    }
    void refresh($)
    return r
  })

  // The nudge bar above the prompt: stats on the left, Prune / Dismiss on the right.
  // panelview.py decides when it shows (a checkpoint crossed and not dismissed). Prune runs the
  // same handoff + compact as auto-prune; Dismiss hides it until the next checkpoint.
  on('ui.render', { component: 'AbovePrompt' }, async ($, e, next) => {
    const n = await read($, nudge)
    if (!n || e.props.hasSurvey) return next(e)
    const { Box, Text, Button } = $.ui.resolve(e)
    const prune = async () => {
      nudgePruned = true
      await update($, nudge, () => null)
      await $.fs.write(reqPath(await $.session.id()), JSON.stringify({ at: await $.clock.now(), nudge: true }))
      await note($, `nudge bar: Prune at ${Math.round(n.ctx / 1000)}k`)
      void poll($)
    }
    const dismiss = async () => {
      await update($, nudge, () => null)
      await $.fs.write(nudgePath(await $.session.id()), JSON.stringify({ last: n.ctx }))
      await note($, `nudge bar: dismissed at ${Math.round(n.ctx / 1000)}k`)
    }
    return (
      <Box justifyContent="space-between" alignItems="center" columnGap={2} paddingX={1}>
        <Box flexShrink={1}>
          <Text wrap="truncate-end">
            {n.parts.map(p => (
              <Text color={p.color ?? undefined} bold={!!p.bold} dimColor={!!p.dim}>{p.text}</Text>
            ))}
          </Text>
        </Box>
        <Box gap={1} flexShrink={0}>
          <Button key="nudge-prune" label="Prune" variant="primary" onPress={prune} />
          <Button key="nudge-dismiss" label="Dismiss" onPress={dismiss} />
        </Box>
      </Box>
    )
  })

  // discreet Cache button at the right of the prompt footer, beside the engine's mode labels
  on('ui.render', { component: 'SessionMode' }, async ($, e) => {
    const { Box, Text, Button } = $.ui.resolve(e)
    // label and box margins come from panelview.py's "footer": no restart to change them
    const f: any = await read($, footer)
    if (!footerLogged && f.label) { footerLogged = true; void note($, `footer drawn on ${e.surface}: ${JSON.stringify(f)}`) }
    return (
      <Box gap={1} {...(f.box ?? {})}>
        {e.props.modes.length > 0 && <Text dimColor>{e.props.modes.join(' & ')}</Text>}
        {/* a Button takes its label as a string (a styled child is refused); the desktop
            draws it as its own chip either way */}
        <Box key="cache-hit">
          <Button key="cache-toggle" plain label={f.label ?? 'Bonsai'} onPress={() => togglePanel($)} />
        </Box>
      </Box>
    )
  })

  on('ui.render', { component: 'Pane', requestId: PANE }, async ($, e) => {
    const { Box, Text, Button, Svg, Select } = $.ui.resolve(e)
    const blocks = await read($, view)
    const armedSid = await read($, armed)
    const lay: any = await read($, layout)
    if (!blocks.length) return <Text dimColor>Checking…</Text>

    const parts = (ps: Part[]) => (
      <Text>
        {ps.map(p => (
          <Text color={p.color ?? undefined} bold={!!p.bold} dimColor={!!p.dim} wrap={(p.wrap as any) ?? undefined}>{p.text}</Text>
        ))}
      </Text>
    )

    // a progress meter: a rounded track with a colored fill, drawn as SVG where the surface
    // has it (desktop), else as a thin text bar (terminal)
    const meter = (m: Meter) => {
      const frac = Math.max(0, Math.min(1, m.frac))
      const W = m.width ?? 200, H = m.height ?? 6, r = H / 2
      const fill = frac > 0 ? Math.max(H, Math.round(W * frac)) : 0
      const bar = e.surface === 'terminal' ? (
        <Text>
          <Text color={m.color}>{'━'.repeat(Math.round(frac * 20))}</Text>
          <Text dimColor>{'━'.repeat(20 - Math.round(frac * 20))}</Text>
        </Text>
      ) : (
        <Svg alt={`${Math.round(frac * 100)}%`} width={W} height={H}
          source={`<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="0 0 ${W} ${H}">` +
            `<rect width="${W}" height="${H}" rx="${r}" fill="${m.track ?? '#3a3a3a'}"/>` +
            (fill ? `<rect width="${fill}" height="${H}" rx="${r}" fill="${m.color}"/>` : '') + `</svg>`} />
      )
      return (
        <Box gap={1} alignItems="center">
          {bar}
          {m.label && parts(m.label)}
        </Box>
      )
    }

    const prune = async (sid: string) => {
      await update($, armed, () => null)
      await $.fs.write(reqPath(sid), JSON.stringify({ at: await $.clock.now() }))
      void poll($) // pruning this very session: pick it up now
      void refresh($)
    }

    // Prune arms a Confirm in its place; no Cancel button, it just disarms after 8 s.
    const arm = async (v: string) => {
      await update($, armed, () => v)
      $.clock.after(8000, () => void update($, armed, cur => (cur === v ? null : cur)))
    }

    const action = (sid: string, a: string) => {
      if (a === 'pruning') return <Text color="warning">pruning…</Text>
      if (a !== 'prune') return a ? <Text dimColor>{a}</Text> : null
      if (armedSid === sid)
        return <Button key={`yes-${sid}`} label="Confirm" variant="primary" onPress={() => prune(sid)} />
      return <Button key={`prune-${sid}`} label="Prune" onPress={() => arm(sid)} />
    }

    const setPref = async (key: string, value: unknown) => {
      const prefs = (await readJson($, prefsPath())) ?? {}
      await $.fs.write(prefsPath(), JSON.stringify({ ...prefs, [key]: value }))
      void refresh($)
    }

    const draw = (b: Block): any => {
      if (b.kind === 'gap') return <Text> </Text>
      if (b.kind === 'button')
        return (
          <Box gap={1}>
            <Button key={`pref-${b.pref}`} label={b.label} onPress={() => setPref(b.pref, b.value)} />
            {b.parts && parts(b.parts)}
          </Box>
        )
      if (b.kind === 'line') return parts(b.parts)
      if (b.kind === 'box')
        return (
          <Box flexDirection="column" borderStyle={b.style ?? 'round'} borderColor={b.color}
            paddingX={b.padX ?? 1} paddingTop={b.padTop ?? b.pad ?? 0} paddingBottom={b.padBottom ?? b.pad ?? 0} rowGap={b.gap ?? 0} flexGrow={b.grow ? 1 : 0} width={b.width}>
            {b.title && (
              <Box justifyContent="space-between">
                {parts(b.title)}
                <Box gap={1} alignItems="center">
                  {b.titleRight && parts(b.titleRight)}
                  {b.titleButton && (
                    <Button key={`pref-${b.titleButton.pref}`} label={b.titleButton.label}
                      onPress={() => setPref(b.titleButton!.pref, b.titleButton!.value)} />
                  )}
                </Box>
              </Box>
            )}
            {b.children.map(draw)}
          </Box>
        )
      // wraps when the pane is too narrow, instead of running off the side
      if (b.kind === 'row')
        return <Box columnGap={b.gap ?? 3} rowGap={1} flexWrap="wrap" alignItems="center">{b.children.map(draw)}</Box>
      // a label in a fixed column, so values line up whatever the font
      if (b.kind === 'kv')
        return (
          <Box>
            <Box width={b.labelW ?? lay.labelW ?? 10} flexShrink={0}><Text dimColor>{b.label}</Text></Box>
            {parts(b.parts)}
          </Box>
        )
      if (b.kind === 'meter') return meter(b)
      if (b.kind === 'pruneAll') {
        const all = async () => {
          await update($, armed, () => null)
          const now = await $.clock.now()
          for (const sid of b.sids) await $.fs.write(reqPath(sid), JSON.stringify({ at: now, all: true }))
          void poll($)
          void refresh($)
        }
        return (
          <Box gap={1} alignItems="center">
            {armedSid === 'ALL' ? (
              <Button key="all-yes" label={`Confirm: prune ${b.sids.length}`} variant="primary" onPress={all} />
            ) : (
              <Button key="all-arm" label={b.label} onPress={() => arm('ALL')} />
            )}
            {b.parts && parts(b.parts)}
          </Box>
        )
      }
      if (b.kind === 'select')
        return (
          <Box gap={1} alignItems="center">
            <Select key={`sel-${b.pref}`} label={b.label} value={String(b.value)}
              options={b.options.map(o => ({ key: `${b.pref}-${o.value}`, value: String(o.value), label: o.label }))}
              onSelect={v => setPref(b.pref, b.numeric ? Number(v) : v)} />
            {b.parts && parts(b.parts)}
          </Box>
        )
      return (
        <Box flexDirection="column">
          <Box justifyContent="space-between" flexWrap="wrap" columnGap={2}>
            <Box flexShrink={1}>{parts(b.parts)}</Box>
            <Box gap={2} alignItems="center" flexShrink={0}>
              {parts(b.right)}
              {b.toggle && (
                <Button key={`tog-${b.sid}`} label={b.toggle.label} dimColor
                  onPress={() => setPref(b.toggle!.pref, b.toggle!.value)} />
              )}
              {action(b.sid, b.action)}
            </Box>
          </Box>
          {b.meter && meter(b.meter)}
          {b.sub.map(parts)}
        </Box>
      )
    }

    return (
      <Box flexDirection="column" gap={lay.gap ?? 1} paddingX={lay.padX ?? 0} paddingY={lay.padY ?? 0}>
        {blocks.map(draw)}
      </Box>
    )
  })
}
