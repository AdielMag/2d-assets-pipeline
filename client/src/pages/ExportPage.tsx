import { useEffect, useState } from 'react'
import { api, storageUrl, resolveAtlasParent, resolveDomainFolder, TYPE_LABEL } from '../api'
import type { Asset, Atlas } from '../api'
import { useApp } from '../AppContext'
import { selectedVersionImage } from './AssetsPage'

const isPot = (n: number) => n > 0 && (n & (n - 1)) === 0

// Mirrors the server's unity_export.pascal_name(): split on non-alphanumerics, capitalize
// each part. Export paths (and the domain folder an asset lands in) must match exactly.
function pascalName(name: string): string {
  const parts = name.split(/[^a-zA-Z0-9]+/).filter(Boolean)
  return parts.map((p) => p[0].toUpperCase() + p.slice(1)).join('') || 'Asset'
}

interface AtlasNode extends Atlas {
  children: AtlasNode[]
}

function buildAtlasTree(atlases: Atlas[]): AtlasNode[] {
  const nodes = new Map<number, AtlasNode>(atlases.map((a) => [a.id, { ...a, children: [] }]))
  const roots: AtlasNode[] = []
  for (const a of nodes.values()) {
    if (a.parent_id != null && nodes.has(a.parent_id)) nodes.get(a.parent_id)!.children.push(a)
    else roots.push(a)
  }
  return roots
}

function flattenAtlasTree(nodes: AtlasNode[], depth = 0): { atlas: AtlasNode; depth: number }[] {
  const out: { atlas: AtlasNode; depth: number }[] = []
  for (const n of nodes) {
    out.push({ atlas: n, depth })
    out.push(...flattenAtlasTree(n.children, depth + 1))
  }
  return out
}

function DomainExportPaths({ atlases, unityPath, onChanged }: {
  atlases: Atlas[]
  unityPath: string
  onChanged: () => void
}) {
  const [browsing, setBrowsing] = useState<number | null>(null)
  const rows = flattenAtlasTree(buildAtlasTree(atlases))

  const browse = async (id: number) => {
    setBrowsing(id)
    try {
      const res = await api.browseAtlasExportPath(id)
      if (res.path != null) { await api.updateAtlas(id, { export_path: res.path }); onChanged() }
    } catch (e) {
      console.error('Failed to open folder picker', e)
    } finally {
      setBrowsing(null)
    }
  }

  const reset = async (id: number) => {
    await api.updateAtlas(id, { export_path: null })
    onChanged()
  }

  if (atlases.length === 0) return null

  return (
    <div className="card" style={{ padding: 20 }}>
      <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 4 }}>Domain Export Paths</div>
      <div style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 14 }}>
        Where each domain's sprite atlas lands inside the Unity project. Always nested under{' '}
        <code className="mono" style={{ color: 'var(--text-3)' }}>Assets/Sprites/</code>.
      </div>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        {rows.map(({ atlas, depth }) => {
          const folder = `${resolveAtlasParent(atlas)}/${pascalName(atlas.name)}`
          const overridden = atlas.export_path != null
          return (
            <div
              key={atlas.id}
              style={{
                display: 'flex', alignItems: 'center', gap: 10, padding: '9px 10px',
                paddingLeft: 10 + depth * 18, borderRadius: 8,
                background: overridden ? 'rgba(108,140,255,0.07)' : 'var(--input)',
                border: `1px solid ${overridden ? 'var(--accent-border)' : 'var(--border-2)'}`,
              }}
            >
              <span style={{ fontSize: 12.5, fontWeight: 600, flexShrink: 0, minWidth: 0, maxWidth: 140, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {atlas.name}
              </span>
              <span
                className="mono"
                title={`Assets/${folder}`}
                style={{
                  flex: 1, minWidth: 0, fontSize: 11, color: overridden ? 'var(--text-2)' : 'var(--muted)',
                  overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                }}
              >
                {folder}
              </span>
              {overridden && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  title="Reset to default location"
                  onClick={() => reset(atlas.id)}
                  style={{ padding: '4px 9px', fontSize: 11, flexShrink: 0 }}
                >
                  Reset
                </button>
              )}
              <button
                type="button"
                className="btn btn-secondary"
                disabled={browsing === atlas.id || !unityPath}
                title={unityPath ? 'Pick a folder under Assets/Sprites/' : 'Set the Unity project path in Project Settings first'}
                onClick={() => browse(atlas.id)}
                style={{ padding: '4px 10px', fontSize: 11, whiteSpace: 'nowrap', flexShrink: 0 }}
              >
                {browsing === atlas.id ? 'Waiting…' : 'Browse…'}
              </button>
            </div>
          )
        })}
      </div>
    </div>
  )
}

function JsonPreview({ settings }: { settings: Record<string, unknown> }) {
  return (
    <div
      className="mono"
      style={{
        border: '1px solid var(--border-2)', background: 'var(--input)', borderRadius: 8,
        padding: '12px 13px', fontSize: 11, lineHeight: 1.8, overflowX: 'auto',
      }}
    >
      <div>{'{'}</div>
      {Object.entries(settings).map(([k, v], i, arr) => (
        <div key={k} style={{ paddingLeft: 14 }}>
          <span style={{ color: 'var(--accent)' }}>"{k}"</span>
          {': '}
          <span style={{ color: typeof v === 'number' ? 'var(--warm)' : v === null ? 'var(--muted-2)' : 'var(--green)' }}>
            {JSON.stringify(v)}
          </span>
          {i < arr.length - 1 ? ',' : ''}
        </div>
      ))}
      <div>{'}'}</div>
    </div>
  )
}

export default function ExportPage() {
  const { project } = useApp()
  const [assets, setAssets] = useState<Asset[]>([])
  const [atlases, setAtlases] = useState<Atlas[]>([])
  const [selected, setSelected] = useState<Record<number, boolean>>({})
  const [status, setStatus] = useState<{ unity_path: string; importer_installed: boolean } | null>(null)
  const [preview, setPreview] = useState<{ filename: string; settings: Record<string, unknown>; size: [number, number] | null } | null>(null)
  const [previewAssetId, setPreviewAssetId] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [message, setMessage] = useState('')
  const [errors, setErrors] = useState<string[]>([])

  const reloadAtlases = () => {
    if (!project) return
    api.listAtlases(project.id).then(setAtlases).catch(() => setAtlases([]))
  }

  useEffect(() => {
    if (!project) return
    api.listAssets(project.id).then((list) => {
      setAssets(list)
      setSelected(Object.fromEntries(list.filter((a) => a.versions.length > 0).map((a) => [a.id, true])))
      const first = list.find((a) => a.versions.length > 0) ?? list[0]
      setPreviewAssetId(first?.id ?? null)
    })
    api.exportStatus(project.id).then(setStatus).catch(() => setStatus(null))
    reloadAtlases()
  }, [project?.id])

  useEffect(() => {
    if (previewAssetId == null) { setPreview(null); return }
    api.importSettingsPreview(previewAssetId).then(setPreview).catch(() => setPreview(null))
  }, [previewAssetId, assets])

  if (!project) {
    return <div style={{ color: 'var(--muted)' }}>No project selected. Create one on the Projects page.</div>
  }

  const selectedIds = Object.entries(selected).filter(([, v]) => v).map(([k]) => Number(k))

  const doExport = async () => {
    setBusy(true)
    setMessage('')
    setErrors([])
    try {
      const res = await api.exportAssets(project.id, selectedIds)
      const builtAtlases = res.atlases?.built_atlases ?? []
      const atlasNote = builtAtlases.length
        ? ` — built ${builtAtlases.length} sprite atlas${builtAtlases.length === 1 ? '' : 'es'} (${builtAtlases.join(', ')})`
        : ''
      setMessage(`Exported ${res.exported.length} asset${res.exported.length === 1 ? '' : 's'} to ${project.unity_path}${atlasNote}`)
      setErrors(res.errors.map((e) => {
        const a = assets.find((x) => x.id === e.asset_id)
        return `${a?.name ?? e.asset_id}: ${e.error}`
      }))
    } catch (e) {
      setErrors([e instanceof Error ? e.message : 'Export failed'])
    } finally {
      setBusy(false)
    }
  }

  const install = async () => {
    setErrors([])
    try {
      await api.installImporter(project.id)
      setStatus(await api.exportStatus(project.id))
    } catch (e) {
      setErrors([e instanceof Error ? e.message : 'Install failed'])
    }
  }

  const findAtlas = (a: Asset) => atlases.find((at) => at.id === a.atlas_id) ?? null
  const atlasName = (a: Asset) => findAtlas(a)?.name ?? null
  const exportFolder = (a: Asset) => {
    const atlas = findAtlas(a)
    return atlas ? `${resolveAtlasParent(atlas)}/${pascalName(atlas.name)}` : resolveDomainFolder(a.type)
  }

  const selectedAssets = assets.filter((a) => selected[a.id])
  const layoutPreview = selectedAssets
    .slice(0, 4)
    .map((a) => `${exportFolder(a)}/${pascalName(a.name)}.png`)

  const atlasCounts = new Map<string, number>()
  let unassignedCount = 0
  for (const a of selectedAssets) {
    const name = atlasName(a)
    if (name) atlasCounts.set(name, (atlasCounts.get(name) ?? 0) + 1)
    else unassignedCount++
  }

  return (
    <>
      <div style={{ marginBottom: 20 }}>
        <h1 style={{ marginBottom: 3 }}>Export to Unity</h1>
        <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>{project.name}</div>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1.2fr 1fr', gap: 22, alignItems: 'start' }}>
        {/* Asset checklist */}
        <div className="card" style={{ padding: 20 }}>
          <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 14 }}>
            Select Assets · {selectedIds.length} of {assets.length}
          </div>
          {assets.length === 0 && (
            <div style={{ fontSize: 12, color: 'var(--muted)' }}>No assets in this project yet.</div>
          )}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
            {assets.map((a) => {
              const checked = !!selected[a.id]
              const img = selectedVersionImage(a)
              const exportable = a.versions.length > 0
              return (
                <div
                  key={a.id}
                  onClick={() => {
                    if (!exportable) return
                    setSelected((s) => ({ ...s, [a.id]: !s[a.id] }))
                    setPreviewAssetId(a.id)
                  }}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 11, padding: '8px 9px', borderRadius: 8,
                    cursor: exportable ? 'pointer' : 'default',
                    background: checked ? '#20252c' : 'transparent',
                    opacity: exportable ? 1 : 0.45,
                  }}
                >
                  <div
                    style={{
                      width: 17, height: 17, borderRadius: 5, flexShrink: 0,
                      display: 'flex', alignItems: 'center', justifyContent: 'center',
                      fontSize: 11, fontWeight: 700, color: '#0e1116',
                      border: `1.5px solid ${checked ? 'var(--accent)' : 'var(--border-3)'}`,
                      background: checked ? 'var(--accent)' : 'transparent',
                    }}
                  >
                    {checked ? '✓' : ''}
                  </div>
                  <div className="checkerboard checkerboard-sm" style={{ width: 38, height: 38, borderRadius: 6, flexShrink: 0, overflow: 'hidden' }}>
                    {img && <img src={storageUrl(img)} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }} />}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12.5, fontWeight: 600 }}>{a.name}</div>
                    <div style={{ fontSize: 10.5, color: 'var(--muted)' }}>
                      {TYPE_LABEL[a.type]} · v{a.versions.length}{exportable ? '' : ' — nothing generated yet'}
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Export panel */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card" style={{ padding: 20 }}>
            <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 12 }}>Unity Export</div>
            <div className="field-label" style={{ fontSize: 11, marginBottom: 5 }}>Unity project path</div>
            <div
              className="mono"
              style={{
                border: '1px solid var(--border-2)', background: 'var(--input)', borderRadius: 7,
                padding: '8px 11px', fontSize: 11.5, color: 'var(--text-3)', marginBottom: 14,
                overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
              }}
            >
              {project.unity_path || 'Not set — configure it in Project Settings'}
            </div>

            <div className="field-label" style={{ fontSize: 11, marginBottom: 6 }}>Output layout</div>
            <div
              className="mono"
              style={{
                border: '1px solid var(--border-2)', background: 'var(--input)', borderRadius: 7,
                padding: '11px 12px', fontSize: 11, lineHeight: 1.75, color: '#8b93a0', marginBottom: 16,
              }}
            >
              Assets/<br />
              {layoutPreview.map((p, i) => (
                <span key={p}>
                  {i === layoutPreview.length - 1 && selectedIds.length <= 4 ? '└─ ' : '├─ '}{p}<br />
                </span>
              ))}
              {selectedIds.length > 4 && <span>└─ … {selectedIds.length - 4} more<br /></span>}
              {selectedIds.length === 0 && <span style={{ color: 'var(--muted-2)' }}>nothing selected</span>}
            </div>

            <div className="field-label" style={{ fontSize: 11, marginBottom: 6 }}>
              Sprite atlases (one Unity SpriteAtlas per domain, built automatically — no manual step)
            </div>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 5, marginBottom: 16 }}>
              {atlasCounts.size === 0 && unassignedCount === 0 && (
                <div style={{ fontSize: 11.5, color: 'var(--muted-2)' }}>nothing selected</div>
              )}
              {[...atlasCounts.entries()].map(([name, count]) => (
                <div key={name} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11.5 }}>
                  <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                    <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--accent)' }} />
                    {name}<span className="mono" style={{ color: 'var(--muted-2)' }}>.spriteatlas</span>
                  </span>
                  <span style={{ color: 'var(--muted)' }}>{count} sprite{count === 1 ? '' : 's'}</span>
                </div>
              ))}
              {unassignedCount > 0 && (
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11.5, color: 'var(--muted-2)' }}>
                  <span>No domain — legacy type folder, not atlas-packed</span>
                  <span>{unassignedCount}</span>
                </div>
              )}
            </div>

            <div
              style={{
                display: 'flex', alignItems: 'center', justifyContent: 'space-between',
                border: '1px solid var(--border-2)', borderRadius: 8, padding: '10px 12px', marginBottom: 16,
              }}
            >
              <span style={{ fontSize: 12 }}>AssetPipelineImporter.cs</span>
              {status?.importer_installed ? (
                <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, fontWeight: 600, color: 'var(--green)' }}>
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: 'var(--green)' }} />
                  Installed
                </div>
              ) : (
                <button className="btn btn-secondary" style={{ padding: '5px 10px', fontSize: 11 }} onClick={install}>
                  Install into Unity project
                </button>
              )}
            </div>

            {message && <div style={{ color: 'var(--green)', fontSize: 12, marginBottom: 10 }}>{message}</div>}
            {errors.map((e) => (
              <div key={e} style={{ color: '#ff7b7b', fontSize: 12, marginBottom: 6 }}>{e}</div>
            ))}
            <button
              className="btn btn-accent"
              disabled={busy || selectedIds.length === 0 || !project.unity_path}
              onClick={doExport}
              title="Export selected assets and build sprite atlases into Unity project"
              style={{ width: '100%', fontWeight: 700, padding: 11, display: 'inline-flex', alignItems: 'center', justifyContent: 'center', gap: 8 }}
            >
              <svg width="15" height="15" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                <path d="M8 10.5V1.8M8 1.8L5 4.8M8 1.8l3 3" />
                <path d="M1.8 9.5v3.7a1 1 0 0 0 1 1h10.4a1 1 0 0 0 1-1V9.5" />
              </svg>
              <span>{busy ? 'Exporting…' : `Export ${selectedIds.length} Selected Asset${selectedIds.length === 1 ? '' : 's'} →`}</span>
            </button>
          </div>

          <DomainExportPaths atlases={atlases} unityPath={project.unity_path} onChanged={reloadAtlases} />

          {preview && (
            <div className="card" style={{ padding: 18 }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 10 }}>
                <div style={{ fontSize: 12, fontWeight: 600 }}>{preview.filename}</div>
                {preview.size && (
                  <div className="mono" style={{ fontSize: 10.5, color: 'var(--muted)' }}>
                    {preview.size[0]}×{preview.size[1]}px
                    {isPot(preview.size[0]) && isPot(preview.size[1]) ? ' · POT' : ''}
                  </div>
                )}
              </div>
              <JsonPreview settings={preview.settings} />
            </div>
          )}
        </div>
      </div>
    </>
  )
}
