import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, storageUrl, TYPE_DOT, TYPE_LABEL } from '../../api'
import type { Asset } from '../../api'
import { FidelityBadge, ScreenFidelityBar } from '../../components/FidelityBadge'
import ImageEditorModal from '../../components/ImageEditorModal'
import { RegionCanvas } from './RegionCanvas'
import type { StepProps } from './types'

const selectedImage = (a: Asset | undefined) => {
  if (!a) return ''
  const v = a.versions.find((x) => x.id === a.selected_version_id) ?? a.versions[0]
  return v ? (v.processed_path || v.raw_path) : ''
}

/** The finished screen: the original on one side of the slider, the version rebuilt
 *  entirely out of the generated assets on the other.
 *
 *  Deliberately has no generate button. Rebuilding one element from here used to call the
 *  *redraw* path, which replaces a faithful cut of the screenshot with the image model's
 *  impression of it — the single biggest source of elements drifting away from the
 *  reference. Fixing one asset is asset work, so this links into the Assets tab. */
export function StepResult({
  project, mockup, previewPath, missing, score, selectedRegionId, setSelectedRegionId, reload, refreshPreview,
}: StepProps) {
  const navigate = useNavigate()
  const [assetList, setAssetList] = useState<Asset[]>([])
  const [assigningRegionId, setAssigningRegionId] = useState<number | null>(null)
  const [assetSearch, setAssetSearch] = useState('')
  const [filterTypeOnly, setFilterTypeOnly] = useState(false)
  const [buildBusy, setBuildBusy] = useState(false)
  const [buildMessage, setBuildMessage] = useState('')
  const [buildErrors, setBuildErrors] = useState<string[]>([])
  const [buildMissing, setBuildMissing] = useState<string[]>([])
  const [prefabName, setPrefabName] = useState('')
  const [editingAsset, setEditingAsset] = useState<Asset | null>(null)

  const loadAssets = useCallback(async () => {
    try {
      const list = await api.listAssets(project.id)
      setAssetList(list)
    } catch { /* ignore */ }
  }, [project.id])

  useEffect(() => {
    loadAssets()
  }, [loadAssets, previewPath])

  const assets = useMemo(() => {
    return Object.fromEntries(assetList.map((a) => [a.id, a]))
  }, [assetList])

  const scoreByRegion = useMemo(() => {
    const m = new Map<number, NonNullable<typeof score>['regions'][number]['fidelity']>()
    for (const r of score?.regions ?? []) if (r.fidelity) m.set(r.region_id, r.fidelity)
    return m
  }, [score])

  const handleAssignAsset = async (regionId: number, assetId: number | null) => {
    setAssigningRegionId(regionId)
    try {
      await api.updateRegion(regionId, { asset_id: assetId })
      await reload()
      await loadAssets()
      if (mockup) {
        await refreshPreview(mockup.id).catch(() => {})
      }
    } catch (e) {
      console.error('Failed to assign asset', e)
    } finally {
      setAssigningRegionId(null)
    }
  }

  const buildScreenPrefab = async () => {
    if (!mockup) return
    setBuildBusy(true)
    setBuildMessage('')
    setBuildErrors([])
    setBuildMissing([])
    try {
      const res = await api.exportMockupScreen(mockup.id, prefabName.trim() || undefined)
      setBuildMessage(`Built ${res.path} (${res.count} element${res.count === 1 ? '' : 's'})`)
      setBuildMissing(res.missing)
      setBuildErrors(res.export_errors.map((e) => `Asset ${e.asset_id}: ${e.error}`))
    } catch (e) {
      setBuildErrors([e instanceof Error ? e.message : 'Build failed'])
    } finally {
      setBuildBusy(false)
    }
  }

  if (!mockup) return null

  const regions = mockup.regions
  const built = regions.filter((r) => r.asset_id)
  const selectedRegion = regions.find((r) => r.id === selectedRegionId)

  // Filter assets for visual picker
  const filteredAssets = assetList.filter((a) => {
    if (filterTypeOnly && selectedRegion && a.type !== selectedRegion.asset_type) return false
    if (assetSearch.trim()) {
      const q = assetSearch.toLowerCase()
      return a.name.toLowerCase().includes(q) || (a.prompt && a.prompt.toLowerCase().includes(q))
    }
    return true
  })

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 360px', gap: 20, alignItems: 'start' }}>
      <div>
        <div className="field-label">Drag the handle to compare the original with your rebuilt screen</div>
        <RegionCanvas
          imagePath={mockup.image_path}
          regions={regions}
          selectedId={selectedRegionId}
          onSelect={setSelectedRegionId}
          overlayPath={previewPath}
          compare={!!previewPath}
          readOnly
        />
        {score?.screen && <div style={{ marginTop: 10 }}><ScreenFidelityBar screen={score.screen} /></div>}
        {missing.length > 0 && (
          <div style={{ fontSize: 11.5, color: 'var(--warm)', marginTop: 8 }}>
            Not yet built: {missing.join(', ')}
          </div>
        )}
      </div>

      <div>
        {/* Top summary card */}
        <div className="card" style={{ padding: 14, marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
            {built.length} of {regions.length} elements built
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.6 }}>
            Assign existing assets to regions below, or open them in your asset library to adjust prompts and 9-slices.
          </div>
          <button
            className="btn btn-secondary"
            style={{ width: '100%', marginTop: 12, padding: 9 }}
            onClick={() => navigate('/export')}
          >
            Export to Unity →
          </button>
          <input
            type="text"
            className="input"
            style={{ width: '100%', marginTop: 8 }}
            placeholder={`Prefab name (default: ${mockup.name || `Mockup${mockup.id}`})`}
            value={prefabName}
            onChange={(e) => setPrefabName(e.target.value)}
          />
          <button
            className="btn btn-secondary"
            style={{ width: '100%', marginTop: 8, padding: 9 }}
            disabled={buildBusy || built.length === 0 || !project.unity_path}
            onClick={buildScreenPrefab}
            title="Export every built element's PNG and reconstruct this screen as a Unity prefab, with each sprite placed, sized and anchored to match this mockup"
          >
            {buildBusy ? 'Building…' : 'Build Screen Prefab →'}
          </button>
          {buildMessage && <div style={{ color: 'var(--green)', fontSize: 11.5, marginTop: 8 }}>{buildMessage}</div>}
          {buildMissing.length > 0 && (
            <div style={{ fontSize: 11, color: 'var(--warm)', marginTop: 6 }}>
              Skipped (not built): {buildMissing.join(', ')}
            </div>
          )}
          {buildErrors.map((e) => (
            <div key={e} style={{ color: '#ff7b7b', fontSize: 11.5, marginTop: 6 }}>{e}</div>
          ))}
        </div>

        {/* Region List */}
        <div className="card" style={{ padding: 12, marginBottom: selectedRegion ? 12 : 0 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)', marginBottom: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>Regions & Asset Assignments</span>
            <span style={{ fontSize: 11, color: 'var(--muted)' }}>{regions.length} regions</span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 380, overflowY: 'auto', paddingRight: 2 }}>
            {regions.map((r) => {
              const asset = r.asset_id ? assets[r.asset_id] : undefined
              const img = selectedImage(asset)
              const sel = r.id === selectedRegionId
              const isBusy = assigningRegionId === r.id
              const matchingTypeAssets = assetList.filter((a) => a.type === r.asset_type)
              const otherTypeAssets = assetList.filter((a) => a.type !== r.asset_type)

              return (
                <div
                  key={r.id}
                  onClick={() => setSelectedRegionId(sel ? null : r.id)}
                  style={{
                    display: 'flex', flexDirection: 'column', gap: 6, padding: '7px 8px', borderRadius: 8,
                    background: sel ? '#20242c' : 'rgba(255,255,255,0.02)',
                    border: `1px solid ${sel ? r.color + 'aa' : 'var(--border)'}`,
                    cursor: 'pointer', opacity: isBusy ? 0.7 : 1, transition: 'background 0.15s ease',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                    <div
                      className="checkerboard checkerboard-sm"
                      style={{ width: 32, height: 32, borderRadius: 5, flexShrink: 0, overflow: 'hidden' }}
                    >
                      {img ? (
                        <img src={storageUrl(img)} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
                      ) : (
                        <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--muted-2)', fontSize: 10 }}>
                          ?
                        </div>
                      )}
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {r.name}
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 2 }}>
                        <span style={{ width: 5, height: 5, borderRadius: '50%', background: TYPE_DOT[r.asset_type] }} />
                        <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>{TYPE_LABEL[r.asset_type]}</span>
                        <FidelityBadge fidelity={scoreByRegion.get(r.id)} compact />
                      </div>
                    </div>
                    {r.asset_id && asset && (
                      <span
                        onClick={(e) => { e.stopPropagation(); setEditingAsset(asset) }}
                        title="Edit image"
                        style={{ fontSize: 12, flexShrink: 0, cursor: 'pointer' }}
                      >
                        ✏️
                      </span>
                    )}
                    {r.asset_id && (
                      <span
                        onClick={(e) => { e.stopPropagation(); navigate(`/assets/${r.asset_id}`) }}
                        style={{ fontSize: 10.5, color: 'var(--accent)', flexShrink: 0, whiteSpace: 'nowrap' }}
                        title="Open this asset to edit or regenerate it"
                      >
                        Open →
                      </span>
                    )}
                  </div>

                  {/* Asset Selector Dropdown */}
                  <div style={{ display: 'flex', alignItems: 'center', gap: 6 }} onClick={(e) => e.stopPropagation()}>
                    <select
                      className="input"
                      style={{ padding: '4px 7px', fontSize: 11, height: 26, background: '#14171b' }}
                      value={r.asset_id ?? ''}
                      disabled={isBusy}
                      onChange={(e) => {
                        const val = e.target.value ? Number(e.target.value) : null
                        handleAssignAsset(r.id, val)
                      }}
                    >
                      <option value="">(Unassigned / Not built)</option>
                      {matchingTypeAssets.length > 0 && (
                        <optgroup label={`Matching (${TYPE_LABEL[r.asset_type]})`}>
                          {matchingTypeAssets.map((a) => (
                            <option key={a.id} value={a.id}>
                              {a.name}
                            </option>
                          ))}
                        </optgroup>
                      )}
                      {otherTypeAssets.length > 0 && (
                        <optgroup label="Other Created Assets">
                          {otherTypeAssets.map((a) => (
                            <option key={a.id} value={a.id}>
                              {a.name} ({TYPE_LABEL[a.type]})
                            </option>
                          ))}
                        </optgroup>
                      )}
                    </select>
                    {isBusy && <span style={{ fontSize: 10, color: 'var(--accent)' }}>Saving…</span>}
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Visual Asset Picker Card for Selected Region */}
        {selectedRegion && (
          <div className="card" style={{ padding: 12 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <div>
                <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text)' }}>
                  Assign to {selectedRegion.name}
                </div>
                <div style={{ fontSize: 10.5, color: 'var(--muted)' }}>
                  Pick from created assets in this project
                </div>
              </div>
              {selectedRegion.asset_id && (
                <button
                  className="btn btn-secondary"
                  style={{ padding: '3px 8px', fontSize: 10.5 }}
                  onClick={() => handleAssignAsset(selectedRegion.id, null)}
                  disabled={assigningRegionId === selectedRegion.id}
                >
                  Unassign
                </button>
              )}
            </div>

            {/* Filter controls */}
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginBottom: 8 }}>
              <input
                className="input"
                type="text"
                placeholder="Search assets…"
                value={assetSearch}
                onChange={(e) => setAssetSearch(e.target.value)}
                style={{ padding: '4px 8px', fontSize: 11, height: 26 }}
              />
              <div style={{ display: 'flex', gap: 4 }}>
                <button
                  className={`pill ${!filterTypeOnly ? 'active' : ''}`}
                  style={{ padding: '3px 8px', fontSize: 10.5 }}
                  onClick={() => setFilterTypeOnly(false)}
                >
                  All ({assetList.length})
                </button>
                <button
                  className={`pill ${filterTypeOnly ? 'active' : ''}`}
                  style={{ padding: '3px 8px', fontSize: 10.5 }}
                  onClick={() => setFilterTypeOnly(true)}
                >
                  {TYPE_LABEL[selectedRegion.asset_type]} only ({assetList.filter((a) => a.type === selectedRegion.asset_type).length})
                </button>
              </div>
            </div>

            {/* Asset Thumbnails Grid */}
            {filteredAssets.length === 0 ? (
              <div style={{ fontSize: 11, color: 'var(--muted)', textAlign: 'center', padding: '16px 8px' }}>
                {assetList.length === 0 ? 'No assets created yet in this project.' : 'No matching assets found.'}
              </div>
            ) : (
              <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3, 1fr)', gap: 6, maxHeight: 220, overflowY: 'auto', paddingRight: 2 }}>
                {filteredAssets.map((a) => {
                  const img = selectedImage(a)
                  const isCurrent = selectedRegion.asset_id === a.id
                  return (
                    <div
                      key={a.id}
                      onClick={() => handleAssignAsset(selectedRegion.id, a.id)}
                      style={{
                        display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4,
                        padding: 6, borderRadius: 6, cursor: 'pointer',
                        background: isCurrent ? 'rgba(108,140,255,0.18)' : '#191c21',
                        border: `1px solid ${isCurrent ? 'var(--accent)' : 'var(--border)'}`,
                        transition: 'all 0.15s ease',
                      }}
                      title={`Assign ${a.name} (${TYPE_LABEL[a.type]})`}
                    >
                      <div className="checkerboard checkerboard-sm" style={{ width: 44, height: 44, borderRadius: 4, overflow: 'hidden' }}>
                        {img ? (
                          <img src={storageUrl(img)} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
                        ) : (
                          <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--muted-2)', fontSize: 9 }}>
                            no img
                          </div>
                        )}
                      </div>
                      <span style={{ fontSize: 10, fontWeight: 500, width: '100%', textAlign: 'center', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {a.name}
                      </span>
                      <span style={{ fontSize: 9, color: isCurrent ? 'var(--accent)' : 'var(--muted-2)' }}>
                        {isCurrent ? 'Selected ✓' : TYPE_LABEL[a.type]}
                      </span>
                    </div>
                  )
                })}
              </div>
            )}
          </div>
        )}
      </div>
      {editingAsset && (
        <ImageEditorModal
          asset={editingAsset}
          onClose={() => setEditingAsset(null)}
          onSaved={async () => {
            await loadAssets()
            if (mockup) await refreshPreview(mockup.id).catch(() => {})
            setEditingAsset(null)
          }}
        />
      )}
    </div>
  )
}

