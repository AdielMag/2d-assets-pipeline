import { useEffect, useState } from 'react'
import { api } from '../api'
import type { LlmProviderInfo, LlmSelection } from '../api'
import { PROVIDER_LOGOS } from './ProviderLogos'

const FALLBACK_PROVIDERS: LlmProviderInfo[] = [
  {
    name: 'claude',
    label: 'Claude',
    models: [
      'claude-4-sonnet',
      'claude-4.8-opus',
      'claude-4.7-opus',
      'claude-4.6-opus',
      'claude-3-7-sonnet',
      'claude-3-5-sonnet',
      'claude-3-5-haiku',
      'claude-3-opus',
      'sonnet',
      'opus',
      'haiku',
    ],
    default_model: 'claude-4-sonnet',
    supports_effort: true,
    efforts: ['low', 'medium', 'high'],
    supports_context_window: false,
    supports_vision: true,
    available: true,
  },
  {
    name: 'antigravity',
    label: 'Antigravity',
    models: [
      'gemini-3.6-flash',
      'gemini-3.6-pro',
      'gemini-3.5-flash',
      'gemini-3.5-pro',
      'gemini-3.1-pro',
      'claude-4.6-sonnet',
      'claude-4.6-opus',
      'claude-3-7-sonnet',
      'claude-3-5-sonnet',
      'claude-3-5-haiku',
      'gpt-4o',
      'gpt-oss-120b',
    ],
    default_model: 'gemini-3.6-flash',
    supports_effort: true,
    efforts: ['low', 'medium', 'high'],
    supports_context_window: false,
    supports_vision: true,
    available: true,
  },
]

const LLM_MODEL_INFO: Record<string, { perf: string; tokens: string }> = {
  'claude-4-sonnet': {
    perf: '🏆 Next-Gen Flagship Intelligence (Claude 4)',
    tokens: 'High speed & reasoning balance · Optimized token budget',
  },
  'claude-4.8-opus': {
    perf: '👑 Ultimate Frontier Reasoning (Claude 4.8 Opus)',
    tokens: 'Deepest multi-step reasoning capacity · Extended token window',
  },
  'claude-4.7-opus': {
    perf: '🔬 Advanced Frontier Reasoning (Claude 4.7 Opus)',
    tokens: 'High capacity token reasoning · Complex logic & code synthesis',
  },
  'claude-4.6-opus': {
    perf: '🧠 High Capacity Reasoning (Claude 4.6 Opus)',
    tokens: 'High capacity token budget · Superior multi-modal analysis',
  },
  'gemini-3.6-flash': {
    perf: '⚡ Gemini 3.6 Flash',
    tokens: 'Fastest intelligence & vision · Low token consumption',
  },
  'gemini-3.6-pro': {
    perf: '🧠 Gemini 3.6 Pro',
    tokens: 'High reasoning capacity · Complex multi-step instructions',
  },
  'gemini-3.5-flash': {
    perf: '📦 Gemini 3.5 Flash',
    tokens: 'Fast previous-gen model · Lightweight prompt drafting',
  },
  'gemini-3.5-pro': {
    perf: '📦 Gemini 3.5 Pro',
    tokens: 'Previous-gen Pro model · Solid reasoning capability',
  },
  'gemini-3.1-pro': {
    perf: '🎨 Gemini 3.1 Pro',
    tokens: 'Standard Pro-tier model · Reliable visual & spatial analysis',
  },
  'claude-4.6-sonnet': {
    perf: '🏆 Claude 4.6 Sonnet (Antigravity)',
    tokens: 'Flagship reasoning & prompt synthesis via Antigravity CLI',
  },
  'claude-sonnet-4-6': {
    perf: '🏆 Claude 4.6 Sonnet (Antigravity)',
    tokens: 'Flagship reasoning & prompt synthesis via Antigravity CLI',
  },
  'claude-opus-4-6-thinking': {
    perf: '👑 Claude 4.6 Opus (Antigravity)',
    tokens: 'Deepest reasoning capacity via Antigravity CLI',
  },
  'claude-3-7-sonnet': {
    perf: '🌟 Claude 3.7 Sonnet (Antigravity)',
    tokens: 'Adaptive token budget · Multimodal intelligence',
  },
  'claude-3-5-sonnet': {
    perf: '🎯 Claude 3.5 Sonnet (Antigravity)',
    tokens: 'High speed & precise reasoning · Reliable layout extraction',
  },
  'claude-3-5-haiku': {
    perf: '🚀 Claude 3.5 Haiku (Antigravity)',
    tokens: 'Ultra-fast latency · Minimal token consumption',
  },
  'gpt-4o': {
    perf: '⚡ GPT-4o (Antigravity)',
    tokens: 'Versatile multimodal intelligence via Antigravity CLI',
  },
  'gpt-oss-120b': {
    perf: '🤖 GPT-OSS 120B (Antigravity)',
    tokens: 'Open-weights high parameter model · Deep text reasoning',
  },
  'gpt-oss-120b-medium': {
    perf: '🤖 GPT-OSS 120B (Antigravity)',
    tokens: 'Open-weights high parameter model · Deep text reasoning',
  },
  'sonnet': {
    perf: '⚖ Balanced Performance',
    tokens: 'Standard token consumption · Strong visual & reasoning balance',
  },
  'opus': {
    perf: '🔬 Deep Analysis & Maximum Capacity',
    tokens: 'High token budget · Highest quality for complex multi-turn prompts',
  },
  'haiku': {
    perf: '🚀 Ultra-Fast Latency',
    tokens: 'Minimal token usage · Lightest footprint for quick tasks',
  },
}

const PROVIDER_HINTS: Record<string, string> = {
  antigravity: 'Google Antigravity CLI Agent (Gemini 3.6 Flash / Pro & Claude models)',
  claude: 'Claude Code / Anthropic CLI (Claude 4 / 3.7 / 3.5 models)',
}

export function useLlmProviders() {
  const [providers, setProviders] = useState<LlmProviderInfo[]>(FALLBACK_PROVIDERS)
  useEffect(() => {
    api.llmProviders().then((p) => { if (p && p.length > 0) setProviders(p) }).catch(() => {})
  }, [])
  return providers
}

/** Claude/Antigravity chooser with model + effort (+ context window) knobs shown
 * only when the selected provider supports them. */
export default function LlmChooser({ value, onChange, compact }: {
  value: LlmSelection
  onChange: (v: LlmSelection) => void
  compact?: boolean
}) {
  const providers = useLlmProviders()
  const current = providers.find((p) => p.name === (value.provider ?? 'claude')) ?? FALLBACK_PROVIDERS.find((p) => p.name === (value.provider ?? 'claude'))

  const setProvider = (name: string) => {
    const p = providers.find((x) => x.name === name)
    onChange({ ...value, provider: name, model: p?.default_model ?? '', effort: p?.supports_effort ? (value.effort || 'medium') : undefined })
  }

  const selectedModelName = value.model || current?.default_model || ''
  const info = selectedModelName ? LLM_MODEL_INFO[selectedModelName] : undefined

  const segClass = compact ? 'seg seg-sm' : 'seg'
  const selectStyle: React.CSSProperties = {
    background: 'var(--input)', border: '1px solid var(--border-2)', borderRadius: 6,
    padding: compact ? '5px 8px' : '7px 10px', fontSize: compact ? 11 : 12, outline: 'none',
    flex: 1, minWidth: 0,
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
      <div className="seg-row">
        {providers.map((p) => {
          const hint = PROVIDER_HINTS[p.name] || `${p.label} LLM Provider`
          const titleText = p.available ? hint : `${p.label} CLI not found on PATH`
          const Logo = PROVIDER_LOGOS[p.name]
          return (
            <div
              key={p.name}
              className={`${segClass}${(value.provider ?? 'claude') === p.name ? ' active' : ''}`}
              onClick={() => setProvider(p.name)}
              title={titleText}
              style={{
                display: 'inline-flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
                flex: '0 0 auto', padding: compact ? '7px 12px' : '9px 16px',
                opacity: p.available ? 1 : 0.45,
              }}
            >
              {Logo ? <Logo size={compact ? 14 : 16} /> : <span style={{ fontWeight: 700 }}>{p.label[0]}</span>}
            </div>
          )
        })}
      </div>
      {current && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <div style={{ display: 'flex', gap: 6 }}>
            {current.models.length > 0 && (
              <select
                style={selectStyle}
                value={value.model || current.default_model}
                onChange={(e) => onChange({ ...value, model: e.target.value })}
                title="Select reasoning LLM model"
              >
                {current.models.map((m) => (
                  <option key={m} value={m} title={LLM_MODEL_INFO[m]?.perf ?? m}>
                    {m}
                  </option>
                ))}
              </select>
            )}
            {current.supports_effort && (
              <select
                style={{ ...selectStyle, flex: '0 0 auto' }}
                value={value.effort || 'medium'}
                onChange={(e) => onChange({ ...value, effort: e.target.value })}
                title="Thinking reasoning effort budget (Low / Medium / High)"
              >
                {current.efforts.map((ef) => (
                  <option key={ef} value={ef}>
                    {ef === 'low' ? '⚡ low' : ef === 'high' ? '🚀 high' : '🔥 medium'}
                  </option>
                ))}
              </select>
            )}
            {current.supports_context_window && (
              <input
                style={{ ...selectStyle, width: 80, flex: '0 0 auto' }}
                type="number"
                placeholder="ctx"
                value={value.context_window ?? ''}
                onChange={(e) => onChange({ ...value, context_window: e.target.value ? Number(e.target.value) : null })}
                title="Context window limit in tokens"
              />
            )}
          </div>
          {info && (
            <div style={{ fontSize: 10.5, background: 'rgba(255,255,255,0.03)', padding: '5px 8px', borderRadius: 5, border: '1px solid var(--border-2)', lineHeight: 1.45 }}>
              <span style={{ fontWeight: 600, color: 'var(--text-1)', marginRight: 6 }}>{info.perf}</span>
              <span style={{ color: 'var(--muted)' }}>{info.tokens}</span>
            </div>
          )}
        </div>
      )}
    </div>
  )
}
