import { test, expect } from 'claude-code/testing'

// The footer chip is the panel's only entry point: it must draw on both surfaces
// (a Button with a styled child instead of a label is refused, and the chip vanishes)
// and keep the engine's own mode labels beside it.
test('Cache chip draws beside the mode labels', async ($, on) => {
  on('ui.render', async () => null) // stands in for the engine's own drawing
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'bonsai-context', surface, component: 'SessionMode',
      props: { modes: ['bypass permissions'] } })
    expect(await ui.find({ type: 'Text', text: /bypass permissions/ })).toBeDefined()
    expect(await ui.find({ key: 'cache-toggle' })).toBeDefined()
    await ui.unmount()
  }
})

// The nudge bar stays out of the way until panelview reports a crossed checkpoint (the kit has no
// Python and can't seed plugin state, so the drawn bar is checked by hand in a live session).
test('Nudge bar is hidden with no checkpoint crossed', async ($, on) => {
  on('ui.render', async ($, e) => { const { Text } = $.ui.resolve(e); return <Text>engine band</Text> })
  for (const surface of ['terminal', 'desktop'] as const) {
    const ui = await $.ui.mount({ plugin: 'bonsai-context', surface, component: 'AbovePrompt', props: { hasSurvey: false } as any })
    expect(await ui.find({ key: 'nudge-prune' })).toBeUndefined()
    await ui.unmount()
  }
})
