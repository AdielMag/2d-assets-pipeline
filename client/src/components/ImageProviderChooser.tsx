import { useEffect, useState } from 'react'
import { api } from '../api'
import type { ImageProviderInfo, ModelParamSpec } from '../api'
import { GoogleGLogo, HiggsfieldLogo } from './ProviderLogos'

/** Why a knob is worth touching, shown as the dropdown's tooltip. Only the ones where the
 *  right answer isn't obvious from the value — measured, not guessed (see
 *  server/tools/sweep_polish_text.py). */
const PARAM_HINT: Record<string, string> = {
  quality:
    'Model-side effort. Raising it usually costs more credits — GPT Image is 0.75 at "low" and 7 at "high" — while Seedream\'s "high" is free. The credits figure above updates with your choice.',
  resolution:
    'Output resolution. Costs more on some models (Nano Banana 2: 1.5 at 1k, 3 at 4k) and nothing on others (Kling gives 2k at the same price as 1k).',
  aspect_ratio:
    '"auto" derives the shape from the reference image and snaps it to the closest ratio this model supports. Every model defaults to a square canvas, which makes a wide element come back drawn small on a square and cropped back — measured at SSIM 0.89 → 0.31. Costs nothing.',
  variant: 'Model variant. Changes both quality and price.',
  thinking: 'Reasoning effort. Free on Nano Banana 2 Lite.',
  mode: 'Generation mode. "quality" costs more on Grok.',
}

let cachedImageProviders: ImageProviderInfo[] | null = null

export function useImageProviders() {
  const [providers, setProviders] = useState<ImageProviderInfo[]>(cachedImageProviders ?? [])
  useEffect(() => {
    api.providers().then((res) => {
      cachedImageProviders = res.image
      setProviders(res.image)
    }).catch(() => {})
  }, [])
  return providers
}

/** Trims a credit figure to at most 2 decimals without trailing zeros (2.25, 2.5, 3, not
 *  2.250000000000004 from float multiplication or 3.00). */
export const fmtCredits = (n: number) => n.toFixed(2).replace(/\.?0+$/, '')

/** Credit cost of a single provider call at the selected model's currently saved
 *  params. Unsupported (Antigravity, or no model yet) reports `supported: false`
 *  rather than a made-up number. `paramsRev` should bump whenever a per-model param
 *  (quality, resolution, ...) is saved — the estimate is computed server-side from the
 *  saved params, so without a refetch it keeps showing the price for the old ones.
 *  Debounced since it's a live CLI round-trip, not a local calculation. */
export function usePerCallCost(provider: string, model: string | undefined, paramsRev = 0) {
  const [state, setState] = useState<{ supported: boolean; credits: number | null; loading: boolean }>(
    { supported: false, credits: null, loading: false },
  )
  useEffect(() => {
    if (provider !== 'higgsfield' || !model) {
      setState({ supported: false, credits: null, loading: false })
      return
    }
    let cancelled = false
    setState((s) => ({ ...s, loading: true }))
    const t = setTimeout(() => {
      api.perCallCost(provider, model)
        .then((r) => { if (!cancelled) setState({ supported: r.supported, credits: r.credits, loading: false }) })
        .catch(() => { if (!cancelled) setState({ supported: false, credits: null, loading: false }) })
    }, 400)
    return () => { cancelled = true; clearTimeout(t) }
  }, [provider, model, paramsRev])
  return state
}

const IMAGE_MODEL_INFO: Record<string, { perf: string; cost: string }> = {
  'gemini-3.6-flash-high': {
    perf: '🏆 Flagship 2D & UI Renderer (Antigravity, nano-banana class)',
    cost: 'Highest-effort flash tier · Unlimited CLI usage (Free via Google AI Pro/Ultra)',
  },
  'gemini-3.6-flash-medium': {
    perf: '⚡ Standard 2D Sprite Generator (Antigravity)',
    cost: 'Balanced quality/speed · Unlimited CLI usage',
  },
  'gemini-3.6-flash-low': {
    perf: '🚀 Fastest Draft Generator (Antigravity)',
    cost: 'Lowest-effort flash tier for quick iteration · Unlimited CLI usage',
  },
  'gemini-3.5-flash-high': {
    perf: '📦 Previous-gen Flagship Renderer (Antigravity)',
    cost: 'Highest-effort flash tier, prior model generation · Unlimited CLI usage',
  },
  'gemini-3.5-flash-medium': {
    perf: '📦 Previous-gen Balanced Renderer (Antigravity)',
    cost: 'Balanced quality/speed, prior model generation · Unlimited CLI usage',
  },
  'gemini-3.5-flash-low': {
    perf: '📦 Previous-gen Fast Draft Renderer (Antigravity)',
    cost: 'Lowest-effort flash tier, prior model generation · Unlimited CLI usage',
  },
  'gemini-3.1-pro-high': {
    perf: '🎨 Highest Fidelity Pro Renderer (Antigravity)',
    cost: 'Highest-effort pro tier for detailed assets · Unlimited CLI usage',
  },
  'gemini-3.1-pro-low': {
    perf: '✨ Fast Pro-tier Renderer (Antigravity)',
    cost: 'Lower-effort pro tier, faster than -high · Unlimited CLI usage',
  },
  'nano_banana_flash': {
    perf: '🍌 Higgsfield Nano Banana 2 — general-purpose renderer',
    cost: 'Via your Higgsfield plan · spends credits (Unlimited plans only apply on higgsfield.ai\'s website)',
  },
  'text2image_soul_v2': {
    perf: '🌟 Higgsfield Soul 2.0 — flagship text-to-image',
    cost: 'Via your Higgsfield plan · spends credits (Unlimited plans only apply on higgsfield.ai\'s website)',
  },
}

export default function ImageProviderChooser({
  provider,
  onChangeProvider,
  model,
  onChangeModel,
  visualModel,
  onChangeVisualModel,
  onChangeParams,
  compact,
}: {
  provider: string
  onChangeProvider: (p: string) => void
  model?: string
  onChangeModel?: (m: string) => void
  visualModel?: string
  onChangeVisualModel?: (vm: string) => void
  /** Fired after a param change has been persisted. Params alter the real price —
   *  gpt_image_2 is 0.75 credits at `quality: low` and 7 at `high` — so anything showing a
   *  cost estimate has to re-fetch it, or it displays a stale figure for a call that now
   *  costs nine times more. */
  onChangeParams?: (params: Record<string, string>) => void
  compact?: boolean
}) {
  const providers = useImageProviders()
  const current = providers.find((p) => p.name === provider) ?? providers[0]

  const selectProvider = (name: string) => {
    onChangeProvider(name)
    const p = providers.find((x) => x.name === name)
    if (p && p.models && p.models.length > 0 && onChangeModel) {
      onChangeModel(p.selected_model || p.default_model || p.models[0])
    }
  }

  const segClass = compact ? 'seg seg-sm' : 'seg'
  const selectStyle: React.CSSProperties = {
    background: 'var(--input)', border: '1px solid var(--border-2)', borderRadius: 6,
    padding: compact ? '4px 8px' : '5px 9px', fontSize: compact ? 11 : 12, outline: 'none',
    color: 'var(--text-1)', minWidth: 0, maxWidth: '100%',
  }
  // A select is as wide as its longest option unless it is allowed to shrink. In compact
  // mode this sits in a fixed-width sidebar, so it has to give way rather than push out.
  const compactSelectStyle: React.CSSProperties = { ...selectStyle, flex: '1 1 120px' }

  const availableModels = current?.models ?? (
    provider === 'antigravity'
      ? ['gemini-3.6-flash-high', 'gemini-3.6-flash-medium', 'gemini-3.6-flash-low', 'gemini-3.5-flash-high', 'gemini-3.5-flash-medium', 'gemini-3.5-flash-low', 'gemini-3.1-pro-high', 'gemini-3.1-pro-low']
      : ['nano_banana_flash', 'text2image_soul_v2']
  )

  const availableVisualModels = current?.visual_models ?? ['auto', 'gemini-3.1-flash-image', 'gemini-3-pro-image-preview']

  const selectedModel = model || current?.selected_model || current?.default_model || availableModels[0]
  const selectedVisualModel = visualModel || current?.selected_visual_model || current?.default_visual_model || availableVisualModels[0]
  const info = selectedModel ? IMAGE_MODEL_INFO[selectedModel] : undefined

  // Higgsfield only: which models are unlimited on the account's higgsfield.ai
  // *website* (with its toggle on). Per Higgsfield's own docs this never extends to
  // the CLI/API — every generation through this app spends real credits regardless
  // — so this is account context ("you could get this one free on the website
  // instead"), not a claim about what hitting Generate here will cost.
  // Which knobs the selected model accepts, and what they're currently set to. Fetched
  // per model (one CLI round trip on the server, cached there) rather than for all ~28
  // models up front — see registry.image_model_params.
  const [paramSpecs, setParamSpecs] = useState<Record<string, ModelParamSpec>>({})
  const [paramValues, setParamValues] = useState<Record<string, string>>({})

  useEffect(() => {
    if (provider !== 'higgsfield' || !selectedModel) {
      setParamSpecs({})
      setParamValues({})
      return
    }
    let cancelled = false
    api.modelParams(provider, selectedModel)
      .then((r) => {
        if (cancelled) return
        const specs = r.params || {}
        setParamSpecs(specs)
        // Saved value if the server has one for THIS model, otherwise the model's own
        // declared default. Values are never carried across a model switch: a flag one
        // model accepts is rejected outright by another that doesn't declare it.
        const saved = current?.selected_params || {}
        const next: Record<string, string> = {}
        for (const [name, spec] of Object.entries(specs)) {
          const v = saved[name]
          next[name] = v && spec.options.includes(v) ? v : (spec.default ?? spec.options[0])
        }
        setParamValues(next)
      })
      .catch(() => { if (!cancelled) { setParamSpecs({}); setParamValues({}) } })
    return () => { cancelled = true }
    // `current` is intentionally not a dep: it changes identity on every providers refresh,
    // which would refetch (and reset) the params mid-edit.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [provider, selectedModel])

  const setParam = (name: string, value: string) => {
    const next = { ...paramValues, [name]: value }
    setParamValues(next)
    if (!selectedModel) return
    api.saveProviderSettings({ provider_params: { [provider]: { [selectedModel]: next } } })
      // Notified only after the save lands: the cost estimate is computed server-side from
      // the saved params, so telling the parent earlier would re-fetch the old price.
      .then(() => onChangeParams?.(next))
      .catch(() => {})
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 6, width: compact ? 'auto' : '100%', minWidth: 0 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', minWidth: 0 }}>
        <div className="seg-row" style={compact ? { flexShrink: 0 } : undefined}>
          <div
            className={`${segClass}${provider === 'antigravity' ? ' active' : ''}`}
            onClick={() => selectProvider('antigravity')}
            title="Google Antigravity CLI Agent (Unlimited free sprite generation with Gemini 3.6 Flash / Pro)"
            style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
              flex: '0 0 auto', padding: compact ? '7px 12px' : '9px 16px',
            }}
          >
            <GoogleGLogo />
          </div>
          <div
            className={`${segClass}${provider === 'higgsfield' ? ' active' : ''}`}
            onClick={() => selectProvider('higgsfield')}
            title="Higgsfield API (Soul model — billed against your Higgsfield plan credits)"
            style={{
              display: 'inline-flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
              flex: '0 0 auto', padding: compact ? '7px 12px' : '9px 16px',
            }}
          >
            <HiggsfieldLogo />
          </div>
        </div>
        {availableModels.length > 0 && (
          <select
            style={compact ? compactSelectStyle : selectStyle}
            value={selectedModel}
            onChange={(e) => {
              const newModel = e.target.value
              if (onChangeModel) onChangeModel(newModel)
              api.saveProviderSettings({ provider_models: { [provider]: newModel } }).catch(() => {})
            }}
            title="Select 2D asset rendering agent model"
          >
            {availableModels.map((m) => (
              <option key={m} value={m} title={IMAGE_MODEL_INFO[m]?.perf ?? m}>
                {m}
              </option>
            ))}
          </select>
        )}
        {/* One dropdown per knob the selected model actually declares — GPT Image gets
            quality + resolution, FLUX Kontext gets neither. Driven by the model's own
            description so an option that would be rejected can never be offered. */}
        {provider === 'higgsfield' && Object.entries(paramSpecs).map(([name, spec]) => (
          <label
            key={name}
            title={PARAM_HINT[name] ?? `${name} — accepted by ${selectedModel}`}
            style={{ display: 'inline-flex', alignItems: 'center', gap: 4, flexShrink: 0 }}
          >
            <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>{name.replace(/_/g, ' ')}</span>
            <select
              style={compact ? compactSelectStyle : selectStyle}
              value={paramValues[name] ?? spec.default ?? spec.options[0]}
              onChange={(e) => setParam(name, e.target.value)}
            >
              {spec.options.map((o) => (
                <option key={o} value={o}>{o}</option>
              ))}
            </select>
          </label>
        ))}
        {provider === 'higgsfield' && current?.credits != null && (
          <span
            title="Remaining Higgsfield plan credits"
            style={{ fontSize: 10.5, color: 'var(--muted)', flexShrink: 0, whiteSpace: 'nowrap' }}
          >
            🪙 {current.credits} credits left
          </span>
        )}
        {provider === 'antigravity' && availableVisualModels.length > 0 && (
          <select
            style={compact ? compactSelectStyle : selectStyle}
            value={selectedVisualModel}
            onChange={(e) => {
              const newVisualModel = e.target.value
              if (onChangeVisualModel) onChangeVisualModel(newVisualModel)
              api.saveProviderSettings({ provider_visual_models: { [provider]: newVisualModel } }).catch(() => {})
            }}
            title="Select visual / image generation model (injected into prompt)"
          >
            {availableVisualModels.map((vm) => (
              <option key={vm} value={vm}>
                {vm === 'auto' ? 'Visual: Auto (Default)' : `Visual: ${vm}`}
              </option>
            ))}
          </select>
        )}
      </div>
      {info && (
        <div
          style={{
            fontSize: 10.5, background: 'rgba(255,255,255,0.03)', padding: '5px 8px', borderRadius: 5,
            border: '1px solid var(--border-2)', lineHeight: 1.45, width: 'fit-content', maxWidth: compact ? 340 : '100%',
            alignSelf: compact ? 'flex-end' : 'stretch',
          }}
        >
          <span style={{ fontWeight: 600, color: 'var(--text-1)', marginRight: 6 }}>{info.perf}</span>
          <span style={{ color: 'var(--muted)' }}>{info.cost}</span>
        </div>
      )}
    </div>
  )
}
