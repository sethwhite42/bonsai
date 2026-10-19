// Blocks printed by scripts/panelview.py
export type Part = { text: string; color?: string | null; bold?: boolean; dim?: boolean; wrap?: string }
export type Meter = { frac: number; color: string; track?: string; width?: number; height?: number; label?: Part[] }
export type Block =
  | { kind: 'line'; parts: Part[] }
  | { kind: 'gap' }
  | { kind: 'button'; label: string; pref: string; value: unknown; parts?: Part[] }
  | { kind: 'box'; color: string; children: Block[]; title?: Part[]; titleRight?: Part[]
      style?: string; grow?: boolean; width?: number; pad?: number; padX?: number; padTop?: number; padBottom?: number; gap?: number
      titleButton?: { label: string; pref: string; value: unknown } }
  | { kind: 'row'; children: Block[]; gap?: number }
  | { kind: 'kv'; label: string; parts: Part[]; labelW?: number }
  | ({ kind: 'meter' } & Meter)
  | { kind: 'pruneAll'; label: string; sids: string[]; parts?: Part[] }
  | { kind: 'select'; pref: string; label?: string; value: string | number; numeric?: boolean
      options: { value: string | number; label?: string }[]; parts?: Part[] }
  | { kind: 'session'; sid: string; parts: Part[]; right: Part[]; action: string; sub: Part[][]; meter?: Meter
      toggle?: { label: string; pref: string; value: unknown } }

// The bar above the prompt once the context crosses a nudge checkpoint
export type Nudge = { ctx: number; parts: Part[] }

declare module 'claude-code' {
  interface PluginState {
    'bonsai-context': { view: Block[]; layout: Record<string, number>; footer: Record<string, any>; armed: string | null
      nudge: Nudge | null }
  }
}
