import { useCallback, useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ASSET_TYPES, TYPE_DOT, TYPE_LABEL } from '../api'
import type { AssetType, Atlas } from '../api'
import { useApp } from '../AppContext'

const ACCEPT = '.png,.jpg,.jpeg,.webp,.bmp'
const ALLOWED = ['png', 'jpg', 'jpeg', 'webp', 'bmp']

type ItemStatus = 'pending' | 'previewing' | 'importing' | 'done' | 'error'

interface QueueItem {
  id: string
  file: File
  name: string
  /** Object URL of the file as dropped, kept so the card can show before/after. */
  srcUrl: string
  /** Object URL of the server's keyed-out PNG, or null while it's still being made. */
  cutUrl: string | null
  previewError: string | null
  status: ItemStatus
  error: string | null
  assetId: number | null
}

const stem = (filename: string) => filename.replace(/\.[^.]+$/, '')
const extOf = (filename: string) => filename.split('.').pop()?.toLowerCase() ?? ''
const message = (e: unknown) => (e instanceof Error ? e.message : 'Something went wrong')

function StatusChip({ item }: { item: QueueItem }) {
  const map: Record<ItemStatus, { label: string; color: string }> = {
    pending: { label: 'Ready', color: 'var(--muted)' },
    previewing: { label: 'Keying…', color: 'var(--accent)' },
    importing: { label: 'Saving…', color: 'var(--accent)' },
    done: { label: '✓ Saved', color: 'var(--green)' },
    error: { label: 'Failed', color: '#ff7b7b' },
  }
  const { label, color } = map[item.status]
  return <span style={{ fontSize: 10.5, fontWeight: 600, color }}>{label}</span>
}

export default function ImportPage() {
  const { project } = useApp()
  const navigate = useNavigate()

  const [atlases, setAtlases] = useState<Atlas[]>([])
  const [atlasId, setAtlasId] = useState<number | null>(null)
  const [type, setType] = useState<AssetType>('ui_element')
  const [sliced, setSliced] = useState(true)
  const [resolution, setResolution] = useState('')
  const [trim, setTrim] = useState(true)
  const [items, setItems] = useState<QueueItem[]>([])
  const [busy, setBusy] = useState(false)
  const [dragOver, setDragOver] = useState(false)
  const fileInput = useRef<HTMLInputElement>(null)

  // Read inside callbacks that must not re-subscribe on every queue change.
  const itemsRef = useRef<QueueItem[]>(items)
  itemsRef.current = items

  useEffect(() => {
    if (!project) return
    api.listAtlases(project.id).then(setAtlases).catch(() => setAtlases([]))
  }, [project?.id])

  // Object URLs are only freed by revoking them; the queue holds two per item.
  useEffect(() => () => {
    for (const it of itemsRef.current) {
      URL.revokeObjectURL(it.srcUrl)
      if (it.cutUrl) URL.revokeObjectURL(it.cutUrl)
    }
  }, [])

  const patch = (id: string, next: Partial<QueueItem>) =>
    setItems((prev) => prev.map((it) => (it.id === id ? { ...it, ...next } : it)))

  const preview = useCallback(async (item: QueueItem, opts: { trim: boolean; sliced: boolean }) => {
    patch(item.id, { status: 'previewing', previewError: null })
    try {
      const url = await api.cutoutPreview(item.file, opts)
      setItems((prev) => prev.map((it) => {
        if (it.id !== item.id) return it
        if (it.cutUrl) URL.revokeObjectURL(it.cutUrl)
        return { ...it, cutUrl: url, status: 'pending' }
      }))
    } catch (e) {
      patch(item.id, { status: 'pending', previewError: message(e) })
    }
  }, [])

  const addFiles = (files: FileList | File[]) => {
    const accepted: QueueItem[] = []
    for (const file of Array.from(files)) {
      if (!ALLOWED.includes(extOf(file.name))) continue
      accepted.push({
        id: `${file.name}-${file.size}-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`,
        file,
        name: stem(file.name),
        srcUrl: URL.createObjectURL(file),
        cutUrl: null,
        previewError: null,
        status: 'pending',
        error: null,
        assetId: null,
      })
    }
    if (!accepted.length) return
    setItems((prev) => [...prev, ...accepted])
    for (const it of accepted) preview(it, { trim, sliced })
  }

  // Trim/slicing change what the cutout looks like, so anything not yet saved is re-keyed
  // rather than left showing a preview of settings that no longer apply.
  const firstRender = useRef(true)
  useEffect(() => {
    if (firstRender.current) { firstRender.current = false; return }
    for (const it of itemsRef.current) {
      if (it.status === 'pending' || it.status === 'error') preview(it, { trim, sliced })
    }
  }, [trim, sliced, preview])

  const removeItem = (id: string) => {
    setItems((prev) => prev.filter((it) => {
      if (it.id !== id) return true
      URL.revokeObjectURL(it.srcUrl)
      if (it.cutUrl) URL.revokeObjectURL(it.cutUrl)
      return false
    }))
  }

  const clearDone = () => {
    setItems((prev) => prev.filter((it) => {
      if (it.status !== 'done') return true
      URL.revokeObjectURL(it.srcUrl)
      if (it.cutUrl) URL.revokeObjectURL(it.cutUrl)
      return false
    }))
  }

  const addDomain = async () => {
    if (!project) return
    const name = window.prompt('New domain name')
    if (!name) return
    const created = await api.createAtlas(project.id, { name, parent_id: null })
    setAtlases(await api.listAtlases(project.id))
    setAtlasId(created.id)
  }

  const importAll = async () => {
    if (!project) return
    setBusy(true)
    for (const it of itemsRef.current) {
      if (it.status === 'done') continue
      patch(it.id, { status: 'importing', error: null })
      try {
        const asset = await api.importCutout(project.id, it.file, {
          name: it.name.trim() || stem(it.file.name),
          type,
          atlas_id: atlasId,
          resolution: resolution.trim() || undefined,
          is_sliced: sliced,
          trim,
        })
        patch(it.id, { status: 'done', assetId: asset.id })
      } catch (e) {
        patch(it.id, { status: 'error', error: message(e) })
      }
    }
    setBusy(false)
    api.listAtlases(project.id).then(setAtlases).catch(() => {})
  }

  if (!project) {
    return <div style={{ color: 'var(--muted)' }}>No project selected. Create one on the Projects page.</div>
  }

  const queued = items.filter((it) => it.status !== 'done').length
  const savedCount = items.filter((it) => it.status === 'done').length
  const domainName = atlasId == null ? 'Unassigned' : atlases.find((a) => a.id === atlasId)?.name ?? ''

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'baseline', justifyContent: 'space-between', marginBottom: 6, gap: 16 }}>
        <h1 style={{ whiteSpace: 'nowrap' }}>Import Cutouts</h1>
        {savedCount > 0 && (
          <button className="btn btn-secondary" onClick={() => navigate('/assets')}>
            View {savedCount} imported asset{savedCount === 1 ? '' : 's'} →
          </button>
        )}
      </div>
      <div style={{ fontSize: 12.5, color: 'var(--muted)', marginBottom: 20, lineHeight: 1.5 }}>
        Drop images drawn on a solid magenta (<span className="mono">#FF00FF</span>) background. The key is
        removed, edges are de-fringed, and each image is saved as an asset in the domain you pick.
        Files that already have real transparency are passed through untouched.
      </div>

      <div className="card" style={{ padding: 16, borderRadius: 11, marginBottom: 18 }}>
        <div style={{ display: 'flex', gap: 16, flexWrap: 'wrap', alignItems: 'flex-start' }}>
          <div style={{ minWidth: 220, flex: '1 1 220px' }}>
            <div className="field-label">Domain</div>
            <div style={{ display: 'flex', gap: 6 }}>
              <select
                className="input"
                value={atlasId ?? ''}
                onChange={(e) => setAtlasId(e.target.value ? Number(e.target.value) : null)}
              >
                <option value="">Unassigned</option>
                {atlases.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
              </select>
              <button className="btn btn-secondary" title="Create a domain" onClick={addDomain}>+</button>
            </div>
          </div>
          <div style={{ minWidth: 150, flex: '0 1 170px' }}>
            <div className="field-label">Resolution</div>
            <input
              className="input"
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
              placeholder="Original size"
              title="Leave blank to keep each image at the size it came in at"
            />
          </div>
          <div style={{ minWidth: 190, flex: '0 1 210px' }}>
            <div className="field-label">Framing</div>
            <div className="seg-row">
              <div className={`seg seg-sm${trim ? ' active' : ''}`} onClick={() => setTrim(true)}>Trim</div>
              <div className={`seg seg-sm${!trim ? ' active' : ''}`} onClick={() => setTrim(false)}>Keep framing</div>
            </div>
          </div>
        </div>

        <div className="field-label" style={{ marginTop: 14, marginBottom: 8 }}>Type</div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
          {ASSET_TYPES.map((t) => (
            <div
              key={t}
              className={`pill${type === t ? ' active' : ''}`}
              onClick={() => { setType(t); setSliced(t === 'ui_element') }}
            >
              <span style={{ width: 7, height: 7, borderRadius: '50%', background: TYPE_DOT[t], display: 'inline-block', marginRight: 6 }} />
              {TYPE_LABEL[t]}
            </div>
          ))}
        </div>

        {type !== 'tile' && type !== 'sprite_sheet' && (
          <>
            <div className="field-label" style={{ marginTop: 14, marginBottom: 8 }}>Slicing Mode</div>
            <div className="seg-row" style={{ maxWidth: 420 }}>
              <div className={`seg${sliced ? ' active' : ''}`} onClick={() => setSliced(true)}>✂ Sliced (9-Slice)</div>
              <div className={`seg${!sliced ? ' active' : ''}`} onClick={() => setSliced(false)}>🖼 Non-Sliced</div>
            </div>
          </>
        )}
      </div>

      <div
        onClick={() => fileInput.current?.click()}
        onDragOver={(e) => { e.preventDefault(); setDragOver(true) }}
        onDragLeave={() => setDragOver(false)}
        onDrop={(e) => { e.preventDefault(); setDragOver(false); addFiles(e.dataTransfer.files) }}
        style={{
          border: `1.5px dashed ${dragOver ? 'var(--accent)' : 'var(--border-3)'}`,
          background: dragOver ? 'rgba(108,140,255,0.06)' : 'var(--card)',
          borderRadius: 11, padding: '30px 20px', textAlign: 'center', cursor: 'pointer',
          marginBottom: 18,
        }}
      >
        <div style={{ fontSize: 13.5, fontWeight: 600, marginBottom: 4 }}>
          Drop images here, or click to browse
        </div>
        <div style={{ fontSize: 11.5, color: 'var(--muted)' }}>
          PNG, JPG, WEBP or BMP · multiple files at once
        </div>
        <input
          ref={fileInput}
          type="file"
          accept={ACCEPT}
          multiple
          style={{ display: 'none' }}
          onChange={(e) => { if (e.target.files) addFiles(e.target.files); e.target.value = '' }}
        />
      </div>

      {items.length > 0 && (
        <>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 12, gap: 12 }}>
            <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>
              {queued} to import into <strong style={{ color: 'var(--text-2)' }}>{domainName}</strong>
              {resolution.trim() ? ` · ${resolution.trim()}` : ' · original size'}
            </div>
            <div style={{ display: 'flex', gap: 8 }}>
              {savedCount > 0 && <button className="btn btn-secondary" onClick={clearDone}>Clear saved</button>}
              <button className="btn btn-accent" disabled={busy || queued === 0} onClick={importAll}>
                {busy ? 'Importing…' : `Import ${queued} asset${queued === 1 ? '' : 's'}`}
              </button>
            </div>
          </div>

          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(230px, 1fr))', gap: 14 }}>
            {items.map((it) => (
              <div key={it.id} className="card hover-border" style={{ borderRadius: 11, overflow: 'hidden' }}>
                <div style={{ display: 'flex', height: 118 }}>
                  <div style={{ flex: 1, background: '#14161a', display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
                    <img src={it.srcUrl} alt="" style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }} />
                  </div>
                  <div className="checkerboard" style={{ flex: 1, display: 'flex', alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
                    {it.cutUrl
                      ? <img src={it.cutUrl} alt="" style={{ maxWidth: '100%', maxHeight: '100%', objectFit: 'contain' }} />
                      : <span style={{ fontSize: 10.5, color: 'var(--muted)' }}>
                          {it.status === 'previewing' ? 'keying…' : it.previewError ? 'no preview' : ''}
                        </span>}
                  </div>
                </div>
                <div style={{ padding: '10px 11px 11px' }}>
                  <input
                    className="input"
                    value={it.name}
                    disabled={it.status === 'done'}
                    onChange={(e) => patch(it.id, { name: e.target.value })}
                    style={{ fontSize: 12, padding: '6px 8px', marginBottom: 7 }}
                  />
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                    <StatusChip item={it} />
                    {it.status === 'done' && it.assetId != null ? (
                      <span
                        onClick={() => navigate(`/assets/${it.assetId}`)}
                        style={{ fontSize: 10.5, color: 'var(--accent)', cursor: 'pointer' }}
                      >
                        Open →
                      </span>
                    ) : (
                      <span
                        onClick={() => removeItem(it.id)}
                        title="Remove from queue"
                        style={{ fontSize: 12, color: 'var(--muted)', cursor: 'pointer', padding: '0 3px' }}
                      >
                        ×
                      </span>
                    )}
                  </div>
                  {(it.error || it.previewError) && (
                    <div style={{ fontSize: 10.5, color: '#ff7b7b', marginTop: 6, lineHeight: 1.4 }}>
                      {it.error || it.previewError}
                    </div>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}
    </>
  )
}
