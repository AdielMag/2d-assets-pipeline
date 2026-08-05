import { useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, ASSET_TYPES, referenceLabel, resolveAtlasParent, storageUrl, timeAgo, TYPE_DOT, TYPE_LABEL } from '../api'
import type { Asset, AssetType, Atlas, Mockup } from '../api'
import { useApp } from '../AppContext'
import ImageEditorModal from '../components/ImageEditorModal'

export function selectedVersionImage(asset: Asset): string {
  const v =
    asset.versions.find((v) => v.id === asset.selected_version_id) ??
    asset.versions[asset.versions.length - 1]
  return v ? v.processed_path || v.raw_path : ''
}

/** What this asset was actually made from.
 *
 *  Two places hold that, and both matter. An asset made by hand carries its references on
 *  itself; one produced from a screen carries them on the version that was produced — for
 *  an extraction that is the screenshot crop the pixels were literally cut out of, which
 *  is the most useful thumbnail there is for judging whether the asset came out right. */
export function assetReferences(asset: Asset): { path: string; label: string }[] {
  const out: { path: string; label: string }[] = []
  for (const p of asset.reference_images ?? []) out.push({ path: p, label: referenceLabel(p, asset) })
  const v =
    asset.versions.find((x) => x.id === asset.selected_version_id) ??
    asset.versions[asset.versions.length - 1]
  for (const p of v?.reference_paths ?? []) {
    if (!out.some((r) => r.path === p)) out.push({ path: p, label: referenceLabel(p, asset) })
  }
  return out
}

interface AtlasNode extends Atlas {
  children: AtlasNode[]
}

function buildTree(atlases: Atlas[]): AtlasNode[] {
  const nodes = new Map<number, AtlasNode>(atlases.map((a) => [a.id, { ...a, children: [] }]))
  const roots: AtlasNode[] = []
  for (const a of nodes.values()) {
    if (a.parent_id != null && nodes.has(a.parent_id)) nodes.get(a.parent_id)!.children.push(a)
    else roots.push(a)
  }
  return roots
}

function DomainTree({ node, depth, selected, onSelect, onAddChild, onRename, onDelete, onSetExportPath, browsingExportPath }: {
  node: AtlasNode
  depth: number
  selected: number | null
  onSelect: (id: number) => void
  onAddChild: (parentId: number) => void
  onRename: (id: number, name: string) => void
  onDelete: (node: AtlasNode) => void
  onSetExportPath: (id: number) => void
  browsingExportPath: number | null
}) {
  const [hover, setHover] = useState(false)
  const exportFolder = resolveAtlasParent(node)
  return (
    <div>
      <div
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        onClick={() => onSelect(node.id)}
        title={`Exports to ${exportFolder}/${node.name.replace(/\s+/g, '')}`}
        style={{
          display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px',
          paddingLeft: 8 + depth * 14, borderRadius: 6, cursor: 'pointer',
          background: selected === node.id ? '#22262d' : 'transparent',
          fontSize: 12.5,
        }}
      >
        <svg width="11" height="11" viewBox="0 0 12 12" fill="none" stroke="#7d8590" strokeWidth="1.3" style={{ flexShrink: 0 }}>
          <path d="M1.5 3 L1.5 9.5 A1 1 0 0 0 2.5 10.5 L9.5 10.5 A1 1 0 0 0 10.5 9.5 L10.5 4 A1 1 0 0 0 9.5 3 L5.5 3 L4.5 1.5 L2.5 1.5 A1 1 0 0 0 1.5 2.5 Z" />
        </svg>
        <span style={{ flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{node.name}</span>
        <span style={{ fontSize: 10, color: 'var(--muted-2)', flexShrink: 0 }}>{node.asset_count}</span>
        {hover && (
          <div style={{ display: 'flex', gap: 4, flexShrink: 0 }}>
            <span
              title="Add child domain"
              onClick={(e) => { e.stopPropagation(); onAddChild(node.id) }}
              style={{ fontSize: 12, color: 'var(--accent)', padding: '0 3px' }}
            >+</span>
            <span
              title={`Export path (${exportFolder}) — click to pick a folder`}
              onClick={(e) => { e.stopPropagation(); onSetExportPath(node.id) }}
              style={{ fontSize: 11, color: browsingExportPath === node.id ? 'var(--accent)' : 'var(--muted)', padding: '0 3px' }}
            >📁</span>
            <span
              title="Rename"
              onClick={(e) => { e.stopPropagation(); const n = window.prompt('Rename domain', node.name); if (n) onRename(node.id, n) }}
              style={{ fontSize: 11, color: 'var(--muted)', padding: '0 3px' }}
            >✎</span>
            <span
              title="Delete domain"
              onClick={(e) => { e.stopPropagation(); onDelete(node) }}
              style={{ fontSize: 11, color: '#ff7b7b', padding: '0 3px' }}
            >×</span>
          </div>
        )}
      </div>
      {node.children.map((c) => (
        <DomainTree
          key={c.id} node={c} depth={depth + 1} selected={selected} onSelect={onSelect}
          onAddChild={onAddChild} onRename={onRename} onDelete={onDelete}
          onSetExportPath={onSetExportPath} browsingExportPath={browsingExportPath}
        />
      ))}
    </div>
  )
}

function DeleteDomainModal({
  node,
  onClose,
  onConfirm,
}: {
  node: AtlasNode
  onClose: () => void
  onConfirm: (mode: 'cascade' | 'only' | 'content_only') => void
}) {
  const countSubdomains = (n: AtlasNode): number => {
    let count = n.children.length
    for (const c of n.children) {
      count += countSubdomains(c)
    }
    return count
  }

  const countAssetsInSubtree = (n: AtlasNode): number => {
    let count = n.asset_count
    for (const c of n.children) {
      count += countAssetsInSubtree(c)
    }
    return count
  }

  const subdomainsCount = countSubdomains(node)
  const totalAssetsCount = countAssetsInSubtree(node)
  const hasContentUnder = subdomainsCount > 0 || totalAssetsCount > 0

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()} style={{ maxWidth: 460 }}>
        <div style={{ fontSize: 16, fontWeight: 600, marginBottom: 12 }}>
          Delete Domain "{node.name}"
        </div>
        <div style={{ fontSize: 13, color: 'var(--muted)', marginBottom: 16, lineHeight: 1.5 }}>
          {hasContentUnder ? (
            <>
              This domain branch contains{' '}
              {subdomainsCount > 0 && <strong>{subdomainsCount} sub-domain(s)</strong>}
              {subdomainsCount > 0 && totalAssetsCount > 0 && ' and '}
              {totalAssetsCount > 0 && <strong>{totalAssetsCount} asset(s)</strong>}.
              <br /><br />
              Do you want to delete <strong>everything under this domain as well</strong>?
            </>
          ) : (
            <>Are you sure you want to delete domain "{node.name}"?</>
          )}
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginBottom: 18 }}>
          <button
            className="btn btn-secondary"
            style={{ textAlign: 'left', padding: '10px 12px', borderColor: 'rgba(255, 123, 123, 0.4)', color: '#ff7b7b', whiteSpace: 'normal', height: 'auto', display: 'block' }}
            onClick={() => onConfirm('cascade')}
          >
            <div style={{ fontWeight: 600, marginBottom: 2 }}>Delete domain & EVERYTHING under it</div>
            <div style={{ fontSize: 11, opacity: 0.85 }}>
              Permanently delete "{node.name}"{subdomainsCount > 0 ? `, ${subdomainsCount} sub-domain(s),` : ''} and all contained assets.
            </div>
          </button>

          <button
            className="btn btn-secondary"
            style={{ textAlign: 'left', padding: '10px 12px', borderColor: 'rgba(255, 123, 123, 0.4)', color: '#ff7b7b', whiteSpace: 'normal', height: 'auto', display: 'block' }}
            onClick={() => onConfirm('content_only')}
          >
            <div style={{ fontWeight: 600, marginBottom: 2 }}>Delete domain CONTENT only</div>
            <div style={{ fontSize: 11, opacity: 0.85 }}>
              Keep "{node.name}" but delete{subdomainsCount > 0 ? ` ${subdomainsCount} sub-domain(s) and` : ''} all contained assets.
            </div>
          </button>

          <button
            className="btn btn-secondary"
            style={{ textAlign: 'left', padding: '10px 12px', whiteSpace: 'normal', height: 'auto', display: 'block' }}
            onClick={() => onConfirm('only')}
          >
            <div style={{ fontWeight: 600, marginBottom: 2 }}>Delete domain ONLY</div>
            <div style={{ fontSize: 11, color: 'var(--muted)' }}>
              Delete "{node.name}" only. Sub-domains move up and assets become unassigned.
            </div>
          </button>
        </div>

        <div style={{ display: 'flex', justifyContent: 'flex-end' }}>
          <button className="btn btn-secondary" onClick={onClose}>
            Cancel
          </button>
        </div>
      </div>
    </div>
  )
}

function NewAssetModal({ projectId, atlases, defaultAtlasId, onClose, onCreated }: {
  projectId: number
  atlases: Atlas[]
  defaultAtlasId: number | null
  onClose: () => void
  onCreated: (asset: Asset) => void
}) {
  const [name, setName] = useState('')
  const [type, setType] = useState<AssetType>('ui_element')
  const [atlasId, setAtlasId] = useState<number | null>(defaultAtlasId)
  const [aspectRatio, setAspectRatio] = useState('1:1')
  const [resolution, setResolution] = useState('256x256')
  const [isSliced, setIsSliced] = useState(true)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const handleTypeChange = (t: AssetType) => {
    setType(t)
    setIsSliced(t === 'ui_element')
  }

  const create = async () => {
    if (!name.trim()) return
    setBusy(true)
    setError('')
    try {
      const asset = await api.createAsset(projectId, {
        name: name.trim(), type, atlas_id: atlasId,
        aspect_ratio: aspectRatio.trim() || null,
        resolution: resolution.trim() || null,
        is_sliced: isSliced,
      })
      onCreated(asset)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create asset')
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 16 }}>New Asset</div>
        <div className="field-label">Asset name</div>
        <input
          className="input"
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && create()}
          placeholder="Play Button"
          style={{ marginBottom: 14 }}
        />
        <div className="field-label" style={{ marginBottom: 8 }}>Type</div>
        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginBottom: 14 }}>
          {ASSET_TYPES.map((t) => (
            <div
              key={t}
              className={`pill${type === t ? ' active' : ''}`}
              onClick={() => handleTypeChange(t)}
            >
              <span style={{ width: 7, height: 7, borderRadius: '50%', background: TYPE_DOT[t], display: 'inline-block', marginRight: 6 }} />
              {TYPE_LABEL[t]}
            </div>
          ))}
        </div>
        {type !== 'tile' && type !== 'sprite_sheet' && (
          <div style={{ marginBottom: 14 }}>
            <div className="field-label" style={{ marginBottom: 8 }}>Slicing Mode</div>
            <div className="seg-row">
              <div className={`seg${isSliced ? ' active' : ''}`} onClick={() => setIsSliced(true)}>✂ Sliced (9-Slice)</div>
              <div className={`seg${!isSliced ? ' active' : ''}`} onClick={() => setIsSliced(false)}>🖼 Non-Sliced (Single Sprite)</div>
            </div>
          </div>
        )}
        <div className="field-label" style={{ marginBottom: 8 }}>Domain</div>
        <select
          className="input"
          value={atlasId ?? ''}
          onChange={(e) => setAtlasId(e.target.value ? Number(e.target.value) : null)}
          style={{ marginBottom: 14 }}
        >
          <option value="">Unassigned</option>
          {atlases.map((a) => <option key={a.id} value={a.id}>{a.name}</option>)}
        </select>
        <div style={{ display: 'flex', gap: 10, marginBottom: 18 }}>
          <div style={{ flex: 1 }}>
            <div className="field-label">Aspect ratio (optional)</div>
            <input
              className="input"
              value={aspectRatio}
              onChange={(e) => setAspectRatio(e.target.value)}
              placeholder="1:1, 3.7:1, 16:9…"
            />
          </div>
          <div style={{ flex: 1 }}>
            <div className="field-label">Resolution</div>
            <input
              className="input"
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
              placeholder="256x256"
            />
          </div>
        </div>
        {error && <div style={{ color: '#ff7b7b', fontSize: 12, marginBottom: 10 }}>{error}</div>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn btn-accent" disabled={busy || !name.trim()} onClick={create}>Create Asset</button>
        </div>
      </div>
    </div>
  )
}

function AssetCard({ asset, atlases, usedOn, onOpen, onMove, onDelete, onSaved }: {
  asset: Asset
  atlases: Atlas[]
  usedOn?: string
  onOpen: () => void
  onMove: (atlasId: number | null) => void
  onDelete: () => void
  onSaved: (asset: Asset) => void
}) {
  const [menu, setMenu] = useState(false)
  const [editing, setEditing] = useState(false)
  const img = selectedVersionImage(asset)
  const refs = assetReferences(asset)
  const canEdit = asset.versions.length > 0

  return (
    <div
      className="card hover-border"
      style={{ borderRadius: 11, overflow: 'hidden', cursor: 'pointer', position: 'relative' }}
      onClick={onOpen}
      onMouseLeave={() => setMenu(false)}
    >
      <div className="checkerboard" style={{ position: 'relative', height: 140 }}>
        {img && (
          <img src={storageUrl(img)} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }} />
        )}
        <div
          style={{
            position: 'absolute', top: 8, left: 8, display: 'flex', alignItems: 'center', gap: 5,
            background: 'rgba(20,22,26,0.82)', borderRadius: 5, padding: '3px 8px',
            fontSize: 10, fontWeight: 600, pointerEvents: 'none',
          }}
        >
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: TYPE_DOT[asset.type] }} />
          {TYPE_LABEL[asset.type]}
        </div>
        {canEdit && (
          <button
            title="Edit image"
            onClick={(e) => { e.stopPropagation(); setEditing(true) }}
            style={{
              position: 'absolute', top: 6, right: 34, width: 24, height: 22, borderRadius: 5,
              background: 'rgba(20,22,26,0.82)', border: 'none', color: 'var(--text-3)',
              cursor: 'pointer', fontSize: 12, lineHeight: 1, display: 'flex',
              alignItems: 'center', justifyContent: 'center',
            }}
          >
            ✏️
          </button>
        )}
        <button
          title="More"
          onClick={(e) => { e.stopPropagation(); setMenu((m) => !m) }}
          style={{
            position: 'absolute', top: 6, right: 6, width: 24, height: 22, borderRadius: 5,
            background: 'rgba(20,22,26,0.82)', border: 'none', color: 'var(--text-3)',
            cursor: 'pointer', fontSize: 13, lineHeight: 1, display: 'flex',
            alignItems: 'center', justifyContent: 'center',
          }}
        >
          ⋯
        </button>
        {menu && (
          <div
            onClick={(e) => e.stopPropagation()}
            style={{
              position: 'absolute', top: 32, right: 6, zIndex: 5, width: 176, padding: 8,
              background: 'var(--card)', border: '1px solid var(--border-3)', borderRadius: 8,
              boxShadow: '0 6px 20px rgba(0,0,0,0.45)',
            }}
          >
            <div className="field-label" style={{ marginBottom: 4 }}>Domain</div>
            <select
              value={asset.atlas_id ?? ''}
              onChange={(e) => { onMove(e.target.value ? Number(e.target.value) : null); setMenu(false) }}
              style={{
                width: '100%', fontSize: 11, background: 'var(--input)', color: 'var(--text-2)',
                border: '1px solid var(--border-2)', borderRadius: 5, padding: '4px 5px', marginBottom: 8,
              }}
            >
              <option value="">Unassigned</option>
              {atlases.map((at) => <option key={at.id} value={at.id}>{at.name}</option>)}
            </select>
            <button
              className="btn btn-danger"
              style={{ width: '100%', padding: '5px 8px', fontSize: 11 }}
              onClick={() => { setMenu(false); onDelete() }}
            >
              Delete asset
            </button>
          </div>
        )}
      </div>

      <div style={{ padding: '11px 13px' }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 6 }}>
          <span style={{ fontSize: 13.5, fontWeight: 600, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
            {asset.name}
          </span>
          {asset.versions.length > 1 && (
            <span style={{ fontSize: 10, color: 'var(--muted-2)', flexShrink: 0 }}>v{asset.versions.length}</span>
          )}
        </div>

        <div
          style={{
            fontSize: 11, color: 'var(--muted)', margin: '5px 0 8px',
            display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
            overflow: 'hidden', lineHeight: 1.45, fontStyle: asset.prompt ? 'normal' : 'italic',
          }}
          title={asset.prompt || 'No prompt set'}
        >
          {asset.prompt || 'No prompt set'}
        </div>

        {refs.length > 0 && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 8, flexWrap: 'wrap' }}>
            {refs.slice(0, 4).map((r, i) => (
              <img
                key={i}
                src={storageUrl(r.path)}
                alt={r.label}
                title={r.label}
                className="checkerboard checkerboard-sm"
                style={{ width: 24, height: 24, borderRadius: 4, objectFit: 'contain', border: '1px solid var(--border-2)' }}
              />
            ))}
            <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>{refs[0].label}</span>
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 6 }}>
          <span style={{ fontSize: 10.5, color: 'var(--muted-2)' }}>{timeAgo(asset.updated_at)}</span>
          {usedOn && (
            <span
              style={{ fontSize: 10, color: 'var(--muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}
              title={`Placed on ${usedOn}`}
            >
              on {usedOn}
            </span>
          )}
        </div>
      </div>
      {editing && (
        <div onClick={(e) => e.stopPropagation()}>
          <ImageEditorModal
            asset={asset}
            onClose={() => setEditing(false)}
            onSaved={(a) => { onSaved(a); setEditing(false) }}
          />
        </div>
      )}
    </div>
  )
}

export default function AssetsPage() {
  const { project } = useApp()
  const navigate = useNavigate()
  const [assets, setAssets] = useState<Asset[]>([])
  const [atlases, setAtlases] = useState<Atlas[]>([])
  const [domainFilter, setDomainFilter] = useState<'all' | 'unassigned' | number>('all')
  const [filter, setFilter] = useState<'all' | AssetType>('all')
  const [search, setSearch] = useState('')
  const [showNew, setShowNew] = useState(false)
  const [deletingDomain, setDeletingDomain] = useState<AtlasNode | null>(null)
  const [mockups, setMockups] = useState<Mockup[]>([])

  const reload = () => {
    if (!project) return
    api.listAssets(project.id).then(setAssets).catch(() => setAssets([]))
    api.listAtlases(project.id).then((fetchedAtlases) => {
      setAtlases(fetchedAtlases)
      setDomainFilter((curr) => (typeof curr === 'number' && !fetchedAtlases.some((a) => a.id === curr) ? 'all' : curr))
    }).catch(() => setAtlases([]))
    // Which screen each asset is placed on — the reverse of the "Open →" link the screen
    // process offers, so the two tabs read as one tool rather than two lists.
    api.listMockups(project.id).then(setMockups).catch(() => setMockups([]))
  }

  useEffect(reload, [project?.id])

  const tree = useMemo(() => buildTree(atlases), [atlases])

  const usage = useMemo(() => {
    const m: Record<number, string> = {}
    mockups.forEach((mk, i) => {
      const label = `Screen ${mockups.length - i}`
      for (const r of mk.regions) {
        if (r.asset_id) m[r.asset_id] = label
        if (r.icon_asset_id) m[r.icon_asset_id] = label
      }
    })
    return m
  }, [mockups])

  const filtered = assets.filter(
    (a) =>
      (filter === 'all' || a.type === filter) &&
      (domainFilter === 'all' || (domainFilter === 'unassigned' ? a.atlas_id == null : a.atlas_id === domainFilter)) &&
      (!search || a.name.toLowerCase().includes(search.toLowerCase())),
  )

  // Grouped by domain when nothing is filtered, so a big library reads as a structure
  // rather than one long wall of cards. Filtering to a single domain gives one group,
  // which renders without a heading.
  const groups = useMemo(() => {
    if (domainFilter !== 'all') {
      return [{ key: 'flat', name: '', items: filtered }]
    }
    const byId = new Map(atlases.map((a) => [a.id, a.name]))
    const buckets = new Map<string, { key: string; name: string; items: Asset[] }>()
    for (const a of filtered) {
      const key = a.atlas_id == null ? 'unassigned' : String(a.atlas_id)
      const name = a.atlas_id == null ? 'Unassigned' : byId.get(a.atlas_id) ?? 'Unknown domain'
      if (!buckets.has(key)) buckets.set(key, { key, name, items: [] })
      buckets.get(key)!.items.push(a)
    }
    return [...buckets.values()].sort((x, y) =>
      x.key === 'unassigned' ? 1 : y.key === 'unassigned' ? -1 : x.name.localeCompare(y.name))
  }, [filtered, atlases, domainFilter])

  // Must stay above the early return below: a render with no project would otherwise run
  // one hook fewer than a render with one, which React rejects ("Rendered more hooks than
  // during the previous render") the moment a project gets selected.
  const [browsingExportPath, setBrowsingExportPath] = useState<number | null>(null)

  if (!project) {
    return <div style={{ color: 'var(--muted)' }}>No project selected. Create one on the Projects page.</div>
  }

  const addAtlas = async (parentId: number | null) => {
    const name = window.prompt(parentId == null ? 'New domain name' : 'New child domain name')
    if (!name) return
    await api.createAtlas(project.id, { name, parent_id: parentId })
    reload()
  }
  const renameAtlas = async (id: number, name: string) => { await api.updateAtlas(id, { name }); reload() }
  const browseDomainExportPath = async (id: number) => {
    setBrowsingExportPath(id)
    try {
      const res = await api.browseAtlasExportPath(id)
      if (res.path != null) { await api.updateAtlas(id, { export_path: res.path }); reload() }
    } catch (e) {
      console.error('Failed to open folder picker', e)
    } finally {
      setBrowsingExportPath(null)
    }
  }
  const deleteAtlas = async (id: number, mode: 'cascade' | 'only' | 'content_only') => {
    await api.deleteAtlas(id, mode)
    reload()
  }
  const moveAsset = async (assetId: number, atlasId: number | null) => {
    await api.updateAsset(assetId, { atlas_id: atlasId })
    reload()
  }
  const deleteAsset = async (id: number, name: string) => {
    if (!window.confirm(`Are you sure you want to delete asset "${name}"?`)) return
    await api.deleteAsset(id)
    reload()
  }

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 18, gap: 16 }}>
        <h1 style={{ whiteSpace: 'nowrap' }}>Assets</h1>
        <div
          style={{
            flex: 1, maxWidth: 340, display: 'flex', alignItems: 'center', gap: 8,
            border: '1px solid var(--border)', background: 'var(--card)', borderRadius: 7, padding: '8px 12px',
          }}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="#7d8590" strokeWidth="1.4">
            <circle cx="6" cy="6" r="4.3" /><line x1="9.2" y1="9.2" x2="13" y2="13" />
          </svg>
          <input
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search assets…"
            style={{ background: 'none', border: 'none', outline: 'none', fontSize: 12.5, color: 'var(--text)', width: '100%' }}
          />
        </div>
        <button
          className="btn btn-accent"
          onClick={() => setShowNew(true)}
          title="Create a new asset item in project"
          style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontWeight: 700 }}
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2">
            <line x1="8" y1="2" x2="8" y2="14" /><line x1="2" y1="8" x2="14" y2="8" />
          </svg>
          <span>New Asset</span>
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '200px 1fr', gap: 20, alignItems: 'start' }}>
        {/* Domain tree */}
        <div className="card" style={{ padding: 10 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '4px 8px 8px' }}>
            <span style={{ fontSize: 10, fontWeight: 600, color: 'var(--muted)', letterSpacing: '0.04em', textTransform: 'uppercase' }}>Domains</span>
            <span title="New root domain" onClick={() => addAtlas(null)} style={{ fontSize: 13, color: 'var(--accent)', cursor: 'pointer' }}>+</span>
          </div>
          <div
            onClick={() => setDomainFilter('all')}
            style={{ padding: '6px 8px', borderRadius: 6, cursor: 'pointer', fontSize: 12.5, background: domainFilter === 'all' ? '#22262d' : 'transparent' }}
          >
            All ({assets.length})
          </div>
          {tree.map((n) => (
            <DomainTree
              key={n.id} node={n} depth={0}
              selected={typeof domainFilter === 'number' ? domainFilter : null}
              onSelect={setDomainFilter} onAddChild={addAtlas} onRename={renameAtlas} onDelete={(node) => setDeletingDomain(node)}
              onSetExportPath={browseDomainExportPath} browsingExportPath={browsingExportPath}
            />
          ))}
          <div
            onClick={() => setDomainFilter('unassigned')}
            style={{ padding: '6px 8px', borderRadius: 6, cursor: 'pointer', fontSize: 12.5, color: 'var(--muted)', background: domainFilter === 'unassigned' ? '#22262d' : 'transparent' }}
          >
            Unassigned ({assets.filter((a) => a.atlas_id == null).length})
          </div>
        </div>

        {/* Assets grid */}
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 18 }}>
            <select
              value={filter}
              onChange={(e) => setFilter(e.target.value as 'all' | AssetType)}
              title="Filter by asset type"
              style={{
                fontSize: 11.5, background: 'var(--input)', color: 'var(--text-2)',
                border: '1px solid var(--border-2)', borderRadius: 6, padding: '5px 8px',
              }}
            >
              <option value="all">All types</option>
              {ASSET_TYPES.map((t) => <option key={t} value={t}>{TYPE_LABEL[t]}</option>)}
            </select>
            <span style={{ fontSize: 11.5, color: 'var(--muted)' }}>
              {filtered.length} asset{filtered.length === 1 ? '' : 's'}
            </span>
          </div>

          {filtered.length === 0 ? (
            <div className="card" style={{ padding: 40, textAlign: 'center', color: 'var(--muted)' }}>
              {assets.length === 0
                ? 'No assets yet. Break down a screen, or create one here and generate its first version.'
                : 'No assets match the current filter.'}
            </div>
          ) : (
            groups.map((g) => (
              <div key={g.key} style={{ marginBottom: 24 }}>
                {groups.length > 1 && (
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: 8, marginBottom: 10 }}>
                    <span style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)' }}>{g.name}</span>
                    <span style={{ fontSize: 11, color: 'var(--muted-2)' }}>{g.items.length}</span>
                  </div>
                )}
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(216px,1fr))', gap: 18 }}>
                  {g.items.map((a) => (
                    <AssetCard
                      key={a.id}
                      asset={a}
                      atlases={atlases}
                      usedOn={usage[a.id]}
                      onOpen={() => navigate(`/assets/${a.id}`)}
                      onMove={(id) => moveAsset(a.id, id)}
                      onDelete={() => deleteAsset(a.id, a.name)}
                      onSaved={reload}
                    />
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      </div>

      {showNew && (
        <NewAssetModal
          projectId={project.id}
          atlases={atlases}
          defaultAtlasId={typeof domainFilter === 'number' ? domainFilter : null}
          onClose={() => setShowNew(false)}
          onCreated={(asset) => navigate(`/assets/${asset.id}`)}
        />
      )}

      {deletingDomain && (
        <DeleteDomainModal
          node={deletingDomain}
          onClose={() => setDeletingDomain(null)}
          onConfirm={async (mode) => {
            const targetId = deletingDomain.id
            setDeletingDomain(null)
            await deleteAtlas(targetId, mode)
          }}
        />
      )}
    </>
  )
}
