import { useEffect, useRef, useState } from 'react'
import { api, storageUrl, timeAgo } from '../../api'
import ImageProviderChooser from '../../components/ImageProviderChooser'
import type { Mockup } from '../../api'
import type { StepProps } from './types'

function GenerateScreenModal({ projectId, onClose, onCreated }: {
  projectId: number
  onClose: () => void
  onCreated: (m: Mockup) => void
}) {
  const [prompt, setPrompt] = useState('')
  const [provider, setProvider] = useState('higgsfield')
  const [imageModel, setImageModel] = useState('')
  const [visualModel, setVisualModel] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  useEffect(() => {
    api.providers().then((p) => setProvider(p.settings.default_image_provider || 'higgsfield')).catch(() => {})
  }, [])

  const generate = async () => {
    if (!prompt.trim()) return
    setBusy(true)
    setError('')
    try {
      onCreated(await api.generateMockup(projectId, {
        prompt: prompt.trim(),
        provider,
        model: imageModel || undefined,
        visual_model: visualModel || undefined,
      }))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Generation failed')
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 6 }}>Generate a screen</div>
        <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 14 }}>
          Describe the whole screen — your project's art style is applied automatically.
        </div>
        <textarea
          className="textarea"
          autoFocus
          rows={3}
          value={prompt}
          onChange={(e) => setPrompt(e.target.value)}
          placeholder="Main menu: title banner at top, three stone buttons stacked in the centre, torches on the sides…"
          style={{ marginBottom: 14 }}
        />
        <div style={{ marginBottom: 16 }}>
          <ImageProviderChooser
            provider={provider}
            onChangeProvider={setProvider}
            model={imageModel}
            onChangeModel={setImageModel}
            visualModel={visualModel}
            onChangeVisualModel={setVisualModel}
          />
        </div>
        {error && <div style={{ color: '#ff7b7b', fontSize: 12, marginBottom: 10 }}>{error}</div>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn btn-accent" disabled={busy || !prompt.trim()} onClick={generate}>
            {busy ? 'Generating…' : 'Generate'}
          </button>
        </div>
      </div>
    </div>
  )
}

export function StepSource({ project, mockups, mockup, setMockupId, reload, next, setError }: StepProps) {
  const fileRef = useRef<HTMLInputElement>(null)
  const [showGen, setShowGen] = useState(false)

  const pick = (id: number) => { setMockupId(id); next() }

  return (
    <>
      <div style={{ display: 'flex', gap: 8, marginBottom: 18 }}>
        <button
          className="btn btn-secondary"
          onClick={() => fileRef.current?.click()}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
            <path d="M8 10.5V1.8M8 1.8L5 4.8M8 1.8l3 3" />
            <path d="M1.8 9.5v3.7a1 1 0 0 0 1 1h10.4a1 1 0 0 0 1-1V9.5" />
          </svg>
          Upload a screenshot
        </button>
        <button
          className="btn btn-accent"
          onClick={() => setShowGen(true)}
          style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontWeight: 700 }}
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
            <path d="M8 1a1 1 0 0 1 1 1v4.2a1 1 0 0 0 .8.98L14 8a1 1 0 0 1 0 2l-4.2.82a1 1 0 0 0-.8.98V15a1 1 0 0 1-2 0v-4.2a1 1 0 0 0-.8-.98L2 9a1 1 0 0 1 0-2l4.2-.82a1 1 0 0 0 .8-.98V2a1 1 0 0 1 1-1z" />
          </svg>
          Generate one
        </button>
        <input
          ref={fileRef}
          type="file"
          accept="image/png,image/jpeg,image/webp"
          style={{ display: 'none' }}
          onChange={async (e) => {
            const f = e.target.files?.[0]
            e.target.value = ''
            if (!f) return
            try {
              const m = await api.uploadMockup(project.id, f)
              await reload()
              pick(m.id)
            } catch (err) {
              setError(err instanceof Error ? err.message : 'Upload failed')
            }
          }}
        />
      </div>

      {mockups.length === 0 ? (
        <div className="card" style={{ padding: 48, textAlign: 'center', color: 'var(--muted)' }}>
          No screens yet. Upload a screenshot of the UI you want to rebuild, or generate one.
        </div>
      ) : (
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(220px,1fr))', gap: 16 }}>
          {mockups.map((m, i) => {
            const isCurrent = m.id === mockup?.id
            const built = m.regions.filter((r) => r.asset_id).length
            return (
              <div
                key={m.id}
                className="card hover-border"
                onClick={() => pick(m.id)}
                style={{
                  overflow: 'hidden', cursor: 'pointer',
                  borderColor: isCurrent ? 'var(--accent-border)' : undefined,
                }}
              >
                <div style={{ height: 150, background: '#181b21' }}>
                  <img
                    src={storageUrl(m.image_path)}
                    alt=""
                    style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
                  />
                </div>
                <div style={{ padding: '10px 12px' }}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6 }}>
                    <span
                      style={{ fontSize: 13, fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
                    >
                      {m.name || `Screen ${mockups.length - i}`}
                    </span>
                    <div style={{ display: 'flex', gap: 2, flexShrink: 0 }}>
                      <button
                        title="Rename this screen"
                        onClick={async (e) => {
                          e.stopPropagation()
                          const n = window.prompt('Screen name', m.name || `Screen ${mockups.length - i}`)
                          if (n == null) return
                          await api.updateMockup(m.id, { name: n.trim() })
                          await reload()
                        }}
                        style={{
                          background: 'none', border: 'none', color: 'var(--muted)', cursor: 'pointer',
                          fontSize: 12, padding: '2px 4px', display: 'inline-flex',
                        }}
                      >
                        ✎
                      </button>
                      <button
                        title="Delete this screen and its elements"
                        onClick={async (e) => {
                          e.stopPropagation()
                          if (!window.confirm('Delete this screen and all its elements?')) return
                          await api.deleteMockup(m.id)
                          await reload()
                        }}
                        style={{
                          background: 'none', border: 'none', color: '#ff7373', cursor: 'pointer',
                          fontSize: 12, padding: '2px 4px', display: 'inline-flex',
                        }}
                      >
                        <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                          <path d="M3 4h10M6 4V2.5a.5.5 0 01.5-.5h3a.5.5 0 01.5.5V4M5 4v9.5a.5.5 0 00.5.5h5a.5.5 0 00.5-.5V4" />
                        </svg>
                      </button>
                    </div>
                  </div>
                  <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 3 }}>
                    {m.regions.length === 0
                      ? 'Not broken down yet'
                      : `${m.regions.length} element${m.regions.length === 1 ? '' : 's'}${built ? ` · ${built} built` : ''}`}
                    {' · '}{timeAgo(m.created_at)}
                  </div>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {showGen && (
        <GenerateScreenModal
          projectId={project.id}
          onClose={() => setShowGen(false)}
          onCreated={async (m) => {
            setShowGen(false)
            await reload()
            pick(m.id)
          }}
        />
      )}
    </>
  )
}
