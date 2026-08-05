import { useEffect, useRef, useState } from 'react'
import { api, storageUrl } from '../api'
import type { Project } from '../api'
import { useApp } from '../AppContext'

function PaletteEditor({ palette, onChange }: { palette: string[]; onChange: (p: string[]) => void }) {
  return (
    <div style={{ display: 'flex', gap: 14, marginBottom: 20, flexWrap: 'wrap' }}>
      {palette.map((hex, i) => (
        <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
          <label
            style={{
              width: 38, height: 38, borderRadius: 9, border: '1px solid rgba(255,255,255,0.12)',
              background: hex, cursor: 'pointer', position: 'relative', display: 'block',
            }}
            title="Click to change, right-click to remove"
            onContextMenu={(e) => { e.preventDefault(); onChange(palette.filter((_, j) => j !== i)) }}
          >
            <input
              type="color"
              value={hex}
              onChange={(e) => onChange(palette.map((c, j) => (j === i ? e.target.value : c)))}
              style={{ opacity: 0, width: '100%', height: '100%', cursor: 'pointer' }}
            />
          </label>
          <div className="mono" style={{ fontSize: 9.5, color: 'var(--muted)' }}>{hex}</div>
        </div>
      ))}
      <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 6 }}>
        <div
          onClick={() => onChange([...palette, '#6c8cff'])}
          style={{
            width: 38, height: 38, borderRadius: 9, border: '1.5px dashed var(--border-3)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            color: 'var(--muted-2)', fontSize: 18, cursor: 'pointer',
          }}
        >
          +
        </div>
        <div style={{ fontSize: 9.5, color: 'transparent' }}>·</div>
      </div>
    </div>
  )
}

function Stepper({ value, onChange }: { value: number; onChange: (v: number) => void }) {
  const btn: React.CSSProperties = {
    width: 30, height: 30, border: '1px solid var(--border-2)', background: '#22262d',
    borderRadius: 7, display: 'flex', alignItems: 'center', justifyContent: 'center',
    cursor: 'pointer', fontSize: 16, userSelect: 'none',
  }
  return (
    <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
      <div style={btn} onClick={() => onChange(Math.max(8, value - 8))}>–</div>
      <div className="mono" style={{ width: 56, textAlign: 'center', fontSize: 14, fontWeight: 600 }}>{value}</div>
      <div style={btn} onClick={() => onChange(Math.min(512, value + 8))}>+</div>
    </div>
  )
}

export default function ProjectSettingsPage() {
  const { project, refreshProjects } = useApp()
  const [draft, setDraft] = useState<Project | null>(null)
  const [saving, setSaving] = useState(false)
  const [savedFlash, setSavedFlash] = useState(false)
  const [browsing, setBrowsing] = useState(false)
  const fileRef = useRef<HTMLInputElement>(null)

  useEffect(() => setDraft(project ? { ...project } : null), [project?.id])

  if (!project || !draft) {
    return <div style={{ color: 'var(--muted)' }}>No project selected. Create one on the Projects page.</div>
  }

  const set = <K extends keyof Project>(key: K, value: Project[K]) =>
    setDraft((d) => (d ? { ...d, [key]: value } : d))

  const save = async () => {
    setSaving(true)
    try {
      await api.updateProject(project.id, {
        name: draft.name,
        style_description: draft.style_description,
        palette: draft.palette,
        unity_path: draft.unity_path,
        ppu: draft.ppu,
        filter_mode: draft.filter_mode,
        wrap_mode: draft.wrap_mode,
        power_of_two: draft.power_of_two,
      })
      await refreshProjects()
      setSavedFlash(true)
      setTimeout(() => setSavedFlash(false), 1500)
    } finally {
      setSaving(false)
    }
  }

  const browseUnityPath = async () => {
    setBrowsing(true)
    try {
      const res = await api.browseUnityPath(project.id)
      if (res.unity_path) set('unity_path', res.unity_path)
    } catch (e) {
      console.error('Failed to open folder picker', e)
    } finally {
      setBrowsing(false)
    }
  }

  const uploadRef = async (file: File) => {
    await api.uploadReference(project.id, file)
    await refreshProjects()
  }

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 20 }}>
        <div>
          <h1 style={{ marginBottom: 3 }}>Project Settings</h1>
          <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>{project.name}</div>
        </div>
        <button className="btn btn-accent" disabled={saving} onClick={save} style={{ padding: '9px 18px' }}>
          {savedFlash ? '✓ Saved' : 'Save Changes'}
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1.25fr 1fr', gap: 22, alignItems: 'start', minWidth: 0 }}>
        {/* Art Style */}
        <div className="card" style={{ padding: 22, minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 16 }}>Art Style</div>

          <div className="field-label">Project name</div>
          <input
            className="input"
            value={draft.name}
            onChange={(e) => set('name', e.target.value)}
            style={{ marginBottom: 18 }}
          />

          <div className="field-label">Style description</div>
          <textarea
            className="textarea"
            rows={4}
            value={draft.style_description}
            onChange={(e) => set('style_description', e.target.value)}
            placeholder="Dark fantasy pixel art, 32px grid, desaturated stone & earth palette with warm torch-lit highlights…"
            style={{ marginBottom: 18 }}
          />

          <div className="field-label" style={{ marginBottom: 8 }}>Palette</div>
          <PaletteEditor palette={draft.palette} onChange={(p) => set('palette', p)} />

          <div className="field-label" style={{ marginBottom: 8 }}>Reference images</div>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 10 }}>
            {draft.reference_images.map((rel) => (
              <div key={rel} style={{ position: 'relative' }}>
                <img
                  src={storageUrl(rel)}
                  alt=""
                  style={{ width: '100%', aspectRatio: '1', borderRadius: 8, objectFit: 'cover', display: 'block' }}
                />
                <div
                  onClick={async () => { await api.removeReference(project.id, rel); await refreshProjects() }}
                  title="Remove"
                  style={{
                    position: 'absolute', top: 4, right: 4, width: 20, height: 20, borderRadius: 5,
                    background: 'rgba(20,22,26,0.85)', color: 'var(--text-3)', display: 'flex',
                    alignItems: 'center', justifyContent: 'center', cursor: 'pointer', fontSize: 12,
                  }}
                >
                  ✕
                </div>
              </div>
            ))}
            <div
              onClick={() => fileRef.current?.click()}
              style={{
                width: '100%', aspectRatio: '1', borderRadius: 8, border: '1.5px dashed var(--border-3)',
                display: 'flex', alignItems: 'center', justifyContent: 'center',
                color: 'var(--muted-2)', fontSize: 22, cursor: 'pointer',
              }}
            >
              +
            </div>
            <input
              ref={fileRef}
              type="file"
              accept="image/png,image/jpeg,image/webp"
              style={{ display: 'none' }}
              onChange={(e) => {
                const f = e.target.files?.[0]
                if (f) uploadRef(f)
                e.target.value = ''
              }}
            />
          </div>
        </div>

        {/* Unity Integration */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16, minWidth: 0 }}>
          <div className="card" style={{ padding: 22, minWidth: 0 }}>
            <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 16 }}>Unity Integration</div>

            <div className="field-label">Unity project path</div>
            <div style={{ display: 'flex', gap: 8, marginBottom: 18 }}>
              <input
                className="input mono"
                value={draft.unity_path}
                readOnly
                placeholder="Not set — browse to your Unity project folder"
                style={{ fontSize: 12, flex: 1, cursor: 'default' }}
              />
              <button
                type="button"
                className="btn btn-secondary"
                disabled={browsing}
                onClick={browseUnityPath}
                style={{ padding: '0 14px', whiteSpace: 'nowrap' }}
              >
                {browsing ? 'Waiting…' : 'Browse…'}
              </button>
            </div>

            <div className="field-label" style={{ marginBottom: 8 }}>Default pixels per unit</div>
            <div style={{ marginBottom: 18 }}>
              <Stepper value={draft.ppu} onChange={(v) => set('ppu', v)} />
            </div>

            <div className="field-label" style={{ marginBottom: 8 }}>Default filter mode</div>
            <div className="seg-row" style={{ marginBottom: 18 }}>
              <div className={`seg${draft.filter_mode === 'point' ? ' active' : ''}`} onClick={() => set('filter_mode', 'point')}>Point</div>
              <div className={`seg${draft.filter_mode === 'bilinear' ? ' active' : ''}`} onClick={() => set('filter_mode', 'bilinear')}>Bilinear</div>
            </div>

            <div className="field-label" style={{ marginBottom: 8 }}>Default wrap mode</div>
            <div className="seg-row" style={{ marginBottom: 18 }}>
              <div className={`seg${draft.wrap_mode === 'clamp' ? ' active' : ''}`} onClick={() => set('wrap_mode', 'clamp')}>Clamp</div>
              <div className={`seg${draft.wrap_mode === 'repeat' ? ' active' : ''}`} onClick={() => set('wrap_mode', 'repeat')}>Repeat</div>
            </div>

            <div
              onClick={() => set('power_of_two', !draft.power_of_two)}
              style={{ display: 'flex', alignItems: 'center', gap: 10, cursor: 'pointer', userSelect: 'none' }}
            >
              <div
                style={{
                  width: 34, height: 20, borderRadius: 10, flexShrink: 0, position: 'relative',
                  background: draft.power_of_two ? 'var(--accent)' : '#3a4048', transition: 'background 0.15s',
                }}
              >
                <div
                  style={{
                    position: 'absolute', top: 2, left: draft.power_of_two ? 16 : 2,
                    width: 16, height: 16, borderRadius: '50%', background: '#fff', transition: 'left 0.15s',
                  }}
                />
              </div>
              <div>
                <div style={{ fontSize: 12.5, fontWeight: 600 }}>Power-of-two textures</div>
                <div style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 1.4 }}>
                  Pads UI / icon / sprite exports to POT with transparent margin. Tiles &amp; sheets untouched.
                </div>
              </div>
            </div>
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--muted-2)', lineHeight: 1.5, padding: '0 4px' }}>
            These defaults apply to new assets. Each asset can override PPU, filter mode and 9-slice borders
            individually in Asset Detail. Each domain's own export path can be overridden on the Assets page
            (📁 next to its name).
          </div>
        </div>
      </div>
    </>
  )
}
