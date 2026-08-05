import { useCallback, useEffect, useState } from 'react'
import { api } from '../api'
import type { ImageProviderInfo, LlmProviderInfo, ProvidersInfo, ProviderSettingsPatch } from '../api'
import { PROVIDER_LOGOS } from '../components/ProviderLogos'

function Toggle({ on, onClick, disabled }: { on: boolean; onClick: () => void; disabled?: boolean }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      title={on ? 'Enabled' : 'Disabled'}
      style={{
        width: 38, height: 22, borderRadius: 999, border: 'none', flexShrink: 0,
        background: on ? 'var(--green)' : '#3a4048', position: 'relative',
        cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.5 : 1,
        transition: 'background .15s', padding: 0,
      }}
    >
      <span
        style={{
          position: 'absolute', top: 3, left: on ? 19 : 3, width: 16, height: 16,
          borderRadius: '50%', background: '#fff', transition: 'left .15s',
        }}
      />
    </button>
  )
}

function CostBadge({ cost }: { cost: string }) {
  const isPaid = cost === 'paid'
  const label = isPaid ? 'Paid' : 'Free'
  return (
    <span
      style={{
        fontSize: 10.5, fontWeight: 700, letterSpacing: '0.03em', padding: '2px 8px',
        borderRadius: 999, textTransform: 'uppercase',
        color: isPaid ? '#ffcf8f' : '#8ff0c4',
        background: isPaid ? 'rgba(245,166,35,0.14)' : 'rgba(62,207,142,0.14)',
        border: `1px solid ${isPaid ? 'rgba(245,166,35,0.35)' : 'rgba(62,207,142,0.35)'}`,
      }}
    >
      {label}
    </span>
  )
}

function StatusDot({ ok }: { ok: boolean }) {
  return <span style={{ width: 7, height: 7, borderRadius: '50%', flexShrink: 0, background: ok ? 'var(--green)' : '#5a616b' }} />
}

function ImageProviderCard({
  p, onToggleEnabled, onSetDefault, onChangeModel, saving,
}: {
  p: ImageProviderInfo
  onToggleEnabled: () => void
  onSetDefault: () => void
  onChangeModel: (m: string) => void
  saving: boolean
}) {
  const dim = !p.enabled
  const Logo = PROVIDER_LOGOS[p.name]
  return (
    <div className="card" style={{ padding: 16, opacity: dim ? 0.72 : 1, borderColor: p.is_default ? 'var(--accent-border)' : undefined }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 9, minWidth: 0 }}>
          <span title={p.label} style={{ display: 'inline-flex' }}>
            {Logo ? <Logo size={20} /> : <span style={{ fontSize: 14.5, fontWeight: 600 }}>{p.label}</span>}
          </span>
          <CostBadge cost={p.cost} />
          {p.is_default && (
            <span style={{ fontSize: 10.5, fontWeight: 600, color: 'var(--accent)', border: '1px solid var(--accent-border)', borderRadius: 999, padding: '1px 7px' }}>
              Default
            </span>
          )}
        </div>
        <div style={{ flex: 1 }} />
        <Toggle on={p.enabled} onClick={onToggleEnabled} disabled={saving} />
      </div>

      {p.models && p.models.length > 0 && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 10 }}>
          <span style={{ fontSize: 11.5, color: 'var(--muted)', fontWeight: 500 }}>Model:</span>
          <select
            value={p.selected_model || p.default_model}
            onChange={(e) => onChangeModel(e.target.value)}
            disabled={saving || !p.enabled}
            style={{
              background: 'var(--input)', border: '1px solid var(--border-2)', borderRadius: 6,
              padding: '4px 8px', fontSize: 11.5, outline: 'none', color: 'var(--text-1)',
            }}
          >
            {p.models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
        </div>
      )}

      <div style={{ display: 'flex', alignItems: 'center', gap: 7, marginTop: 11, fontSize: 12 }}>
        <StatusDot ok={p.configured} />
        <span style={{ color: p.configured ? 'var(--text-2)' : 'var(--warm)' }}>{p.detail}</span>
      </div>

      <div style={{ fontSize: 12, color: 'var(--text-3)', lineHeight: 1.55, marginTop: 9 }}>{p.how_used}</div>
      <div style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 1.5, marginTop: 6 }}>
        <span style={{ color: 'var(--muted-2)' }}>Needs: </span>{p.requires}
      </div>

      <div style={{ marginTop: 13, display: 'flex', gap: 8 }}>
        {!p.is_default && (
          <button className="btn btn-secondary" style={{ fontSize: 11.5, padding: '5px 12px' }} onClick={onSetDefault} disabled={saving || !p.enabled} title={!p.enabled ? 'Enable it first' : ''}>
            Set as default
          </button>
        )}
      </div>
    </div>
  )
}

function LlmRow({ p }: { p: LlmProviderInfo }) {
  const Logo = PROVIDER_LOGOS[p.name]
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '10px 0', borderBottom: '1px solid var(--border)' }}>
      <StatusDot ok={p.available} />
      <span title={p.label} style={{ display: 'inline-flex', alignItems: 'center', width: 28 }}>
        {Logo ? <Logo size={17} /> : <span style={{ fontSize: 13, fontWeight: 700 }}>{p.label[0]}</span>}
      </span>
      <span style={{ fontSize: 11.5, color: p.available ? 'var(--text-3)' : 'var(--warm)' }}>
        {p.available ? `${p.name} CLI found · ${p.models.join(', ')}` : `${p.name} CLI not on PATH`}
      </span>
      {p.supports_vision && <span style={{ fontSize: 10, color: 'var(--muted)', marginLeft: 'auto' }}>vision</span>}
    </div>
  )
}

export default function ProvidersPage() {
  const [data, setData] = useState<ProvidersInfo | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  const load = useCallback(async () => {
    try {
      setData(await api.providers())
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }, [])

  useEffect(() => { load() }, [load])

  const patch = async (body: ProviderSettingsPatch) => {
    setSaving(true)
    try {
      setData(await api.saveProviderSettings(body))
      await api.refreshStatus().catch(() => {})
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  if (error) return <div style={{ color: '#ff7b7b', fontSize: 13 }}>Failed to load providers: {error}</div>
  if (!data) return <div style={{ color: 'var(--muted)', fontSize: 13 }}>Loading…</div>

  return (
    <div style={{ maxWidth: 760 }}>
      <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, marginBottom: 4 }}>
        <div style={{ flex: 1 }}>
          <h1 style={{ fontSize: 20, fontWeight: 650, margin: 0 }}>Providers & Settings</h1>
          <p style={{ fontSize: 12.5, color: 'var(--muted)', margin: '6px 0 0', lineHeight: 1.55 }}>
            Choose how images and text are generated. Disable a provider to make sure it can never
            spend money — the server refuses to generate with anything switched off here.
          </p>
        </div>
        <button
          className="btn btn-secondary"
          style={{ fontSize: 11.5, padding: '6px 12px', display: 'inline-flex', alignItems: 'center', gap: 6 }}
          onClick={load}
          disabled={saving}
          title="Re-check local CLI tools, API keys, and connection status"
        >
          <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M14 8A6 6 0 1 1 8 2c1.7 0 3.2.7 4.2 1.8L14 2v4h-4" />
          </svg>
          <span>Re-check</span>
        </button>
      </div>

      <h2 style={{ fontSize: 12, fontWeight: 600, color: 'var(--muted)', letterSpacing: '0.04em', textTransform: 'uppercase', margin: '26px 0 12px' }}>
        Image generation
      </h2>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
        {data.image.map((p) => (
          <ImageProviderCard
            key={p.name}
            p={p}
            saving={saving}
            onToggleEnabled={() => patch({ enabled: { [p.name]: !p.enabled } })}
            onSetDefault={() => patch({ default_image_provider: p.name })}
            onChangeModel={(model) => patch({ provider_models: { [p.name]: model } })}
          />
        ))}
      </div>

      <h2 style={{ fontSize: 12, fontWeight: 600, color: 'var(--muted)', letterSpacing: '0.04em', textTransform: 'uppercase', margin: '30px 0 6px' }}>
        Text intelligence (LLM CLIs)
      </h2>
      <p style={{ fontSize: 11.5, color: 'var(--muted)', margin: '0 0 8px', lineHeight: 1.5 }}>
        Used for prompt refinement and vision region-detection. These run through your local CLI
        logins (Claude Code / Antigravity) — no API keys, no per-call billing.
      </p>
      <div className="card" style={{ padding: '4px 16px' }}>
        {data.text_llm.map((p) => <LlmRow key={p.name} p={p} />)}
      </div>
    </div>
  )
}
