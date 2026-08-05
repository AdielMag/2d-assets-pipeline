import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, ASSET_TYPES, storageUrl, TYPE_DOT, TYPE_LABEL } from '../../api'
import type { AssetType, LlmSelection, Region, SplitProposal } from '../../api'
import { RunProgress } from '../../components/RunProgress'
import { RegionCanvas } from './RegionCanvas'
import type { Rect } from './RegionCanvas'
import { proposalGhosts, proposalKey, SplitPanel, useProposalSelection } from './SplitPanel'
import type { StepProps } from './types'

const REGION_COLORS = ['#6c8cff', '#2dd4bf', '#c084fc', '#f5a623', '#f472b6', '#3ecf8e']

function hasRegionChanges(local: Region[], saved: Region[]): boolean {
  if (local.length !== saved.length) return true
  const savedMap = new Map(saved.map((r) => [r.id, r]))
  for (const r of local) {
    const s = savedMap.get(r.id)
    if (!s) return true
    if (
      Math.abs(r.x - s.x) > 0.001 ||
      Math.abs(r.y - s.y) > 0.001 ||
      Math.abs(r.w - s.w) > 0.001 ||
      Math.abs(r.h - s.h) > 0.001 ||
      r.name !== s.name ||
      r.asset_type !== s.asset_type
    ) {
      return true
    }
  }
  return false
}

export function StepElements({
  mockup, reload, busy, progress, stop, runStream, runs,
  selectedRegionId, setSelectedRegionId, next, setError, error, onSavePending,
  proposals, setProposals,
}: StepProps & {
  proposals: SplitProposal[] | null
  setProposals: (p: SplitProposal[] | null) => void
}) {
  const [local, setLocal] = useState<Region[]>([])
  const [llm, setLlm] = useState<LlmSelection>({})
  const [cropPath, setCropPath] = useState('')
  const [applying, setApplying] = useState(false)
  const { accepted, setAccepted, focused, setFocused } = useProposalSelection(proposals)

  useEffect(() => { api.llmSettings().then(setLlm).catch(() => {}) }, [])
  const [saving, setSaving] = useState(false)
  const [isDirty, setIsDirty] = useState(false)
  const savedRegions = mockup?.regions ?? []
  const hasChanges = useMemo(() => isDirty && hasRegionChanges(local, savedRegions), [isDirty, local, savedRegions])

  useEffect(() => {
    if (!isDirty) {
      setLocal(mockup?.regions ?? [])
    }
  }, [mockup?.id, mockup?.regions, isDirty])

  const saveChanges = useCallback(async () => {
    if (!mockup || saving) return
    setSaving(true)
    setError('')
    try {
      const savedIds = new Set(mockup.regions.map((r) => r.id))
      const localIds = new Set(local.map((r) => r.id))
      for (const id of savedIds) {
        if (!localIds.has(id)) await api.deleteRegion(id)
      }
      let newSelectedId = selectedRegionId
      for (const r of local) {
        if (!savedIds.has(r.id) || r.id < 0) {
          const created = await api.createRegion(mockup.id, {
            name: r.name, x: r.x, y: r.y, w: r.w, h: r.h,
            color: r.color, prompt: r.prompt, asset_type: r.asset_type, resolution: r.resolution || undefined,
          })
          if (r.id === selectedRegionId) newSelectedId = created.id
        } else {
          const s = mockup.regions.find((x) => x.id === r.id)
          if (
            s && (
              Math.abs(r.x - s.x) > 0.001 ||
              Math.abs(r.y - s.y) > 0.001 ||
              Math.abs(r.w - s.w) > 0.001 ||
              Math.abs(r.h - s.h) > 0.001 ||
              r.name !== s.name ||
              r.asset_type !== s.asset_type
            )
          ) {
            await api.updateRegion(r.id, {
              name: r.name, x: r.x, y: r.y, w: r.w, h: r.h, asset_type: r.asset_type,
            })
          }
        }
      }
      if (newSelectedId !== selectedRegionId) setSelectedRegionId(newSelectedId)
      await reload()
      if (newSelectedId && newSelectedId > 0) {
        api.regionCrop(newSelectedId).then(({ path }) => setCropPath(path)).catch(() => {})
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save changes')
    } finally {
      setSaving(false)
      setIsDirty(false)
    }
  }, [mockup, local, saving, selectedRegionId, reload, setError, setSelectedRegionId])

  useEffect(() => {
    onSavePending?.(hasChanges ? saveChanges : null)
    return () => { onSavePending?.(null) }
  }, [hasChanges, saveChanges, onSavePending])

  const selected = local.find((r) => r.id === selectedRegionId) ?? null
  const ghosts = useMemo(
    () => proposalGhosts(proposals, accepted, focused),
    [proposals, accepted, focused],
  )

  useEffect(() => {
    setCropPath('')
    if (selectedRegionId) {
      api.regionCrop(selectedRegionId).then(({ path }) => setCropPath(path)).catch(() => {})
    }
  }, [selectedRegionId])

  if (!mockup) return null

  /** Detection, then straight on to looking for sub-assets.
   *
   *  One run, not two: splitting asks the same question detection just answered, one level
   *  down — "is this box one element, or a frame with things on it?" — and there is no
   *  point at which the answer to the first is useful without the second. Chained inside a
   *  single `runStream` so the progress log reads as one pass rather than resetting
   *  halfway; a proposal failure is swallowed because the elements are the real output
   *  here and are worth keeping even when the finer pass errors. */
  const detect = async () => {
    setIsDirty(false)
    setProposals(null)
    await runStream('detecting', async (onEvent, signal) => {
      await api.detectRegionsStream(mockup.id, llm, onEvent, signal)
      // Show the new boxes before the finer pass starts, not after both finish. The split
      // half takes as long again as detection did, and it names the elements it is
      // analysing — reading "Analysing GemCurrencyPill" over a canvas still showing the
      // previous run's boxes makes it look like it is working on the wrong screen.
      setSelectedRegionId(null)
      await reload()
      try {
        const result = await api.proposeSplitsStream(mockup.id, {}, onEvent, signal)
        setProposals((result as { proposals?: SplitProposal[] } | undefined)?.proposals ?? [])
      } catch (e) {
        if (e instanceof Error && e.name === 'AbortError') throw e
      }
    })
    await reload()
  }

  /** The same finer pass on demand — after hand-drawing a box, or when the automatic run
   *  was skipped because the elements were already there. */
  const propose = async () => {
    setProposals(null)
    const result = await runStream('proposing', (onEvent, signal) =>
      api.proposeSplitsStream(mockup.id, {}, onEvent, signal))
    if (result === undefined) return
    setProposals((result as { proposals?: SplitProposal[] } | undefined)?.proposals ?? [])
  }

  const applySplits = async () => {
    if (!proposals) return
    const chosen = proposals.filter((p) => accepted.has(proposalKey(p)))
    if (chosen.length === 0) return
    setApplying(true)
    setError('')
    try {
      const before = new Set(mockup.regions.map((r) => r.id))
      const updated = await api.applySplits(mockup.id, chosen)
      setProposals(null)
      setIsDirty(false)
      await reload()
      // Land on the first new box. The canvas here is already the editing surface, so the
      // sub-assets a split just created are nudgeable immediately — which is why this no
      // longer needs a review mode of its own.
      const created = updated.regions.filter((r) => !before.has(r.id)).map((r) => r.id)
      setSelectedRegionId(created[0] ?? null)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not apply the splits')
    } finally {
      setApplying(false)
    }
  }

  const patchLocal = (id: number, rect: Rect) => {
    setLocal((rs) => rs.map((r) => (r.id === id ? { ...r, ...rect } : r)))
    setIsDirty(true)
  }

  const commit = (id: number, rect: Rect) => {
    patchLocal(id, rect)
  }

  const draw = (rect: Rect) => {
    const newId = -Date.now()
    setLocal((rs) => [
      ...rs,
      {
        id: newId,
        mockup_id: mockup.id,
        name: `Element ${local.length + 1}`,
        ...rect,
        color: REGION_COLORS[local.length % REGION_COLORS.length],
        prompt: '',
        asset_type: 'ui_element',
        asset_id: null,
      } as Region,
    ])
    setSelectedRegionId(newId)
    setIsDirty(true)
  }

  const remove = (id: number) => {
    setLocal((rs) => rs.filter((r) => r.id !== id))
    if (selectedRegionId === id) setSelectedRegionId(null)
    setIsDirty(true)
  }

  const rename = (id: number, name: string) => {
    setLocal((rs) => rs.map((r) => (r.id === id ? { ...r, name } : r)))
    setIsDirty(true)
  }

  const setType = (id: number, t: AssetType) => {
    setLocal((rs) => rs.map((r) => (r.id === id ? { ...r, asset_type: t } : r)))
    setIsDirty(true)
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 300px', gap: 20, alignItems: 'start' }}>
      <div>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8, gap: 8 }}>
          <div className="field-label" style={{ marginBottom: 0 }}>
            {ghosts.length > 0
              ? 'Dashed boxes are the sub-assets a split would create'
              : `${local.length} element${local.length === 1 ? '' : 's'} on this screen`}
          </div>
          <div style={{ display: 'flex', gap: 6, flexShrink: 0 }}>
            {local.length > 0 && (
              <button
                className="btn btn-secondary"
                disabled={busy !== 'idle' || applying}
                onClick={propose}
                style={{ padding: '5px 11px', fontSize: 11.5 }}
                title="Check the current boxes for pieces that should be their own asset"
              >
                {busy === 'proposing' ? 'Looking…' : 'Check for sub-assets'}
              </button>
            )}
            <button
              className="btn btn-secondary"
              disabled={busy !== 'idle' || applying}
              onClick={detect}
              style={{ padding: '5px 11px', fontSize: 11.5, display: 'inline-flex', alignItems: 'center', gap: 5 }}
              title={`Vision-detect every reusable element, then check them for sub-assets (${llm.provider ?? 'default model'})`}
            >
              <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                <circle cx="7" cy="7" r="5" /><line x1="11" y1="11" x2="15" y2="15" />
              </svg>
              {busy === 'detecting' ? 'Finding elements…' : local.length ? 'Detect again' : 'Find elements'}
            </button>
          </div>
        </div>

        <RegionCanvas
          imagePath={mockup.image_path}
          regions={local}
          ghosts={ghosts}
          selectedId={selectedRegionId}
          onSelect={setSelectedRegionId}
          onRegionChange={patchLocal}
          onRegionCommit={commit}
          onDraw={draw}
          onDelete={remove}
        />
        <div style={{ fontSize: 11.5, color: 'var(--muted-2)', marginTop: 8 }}>
          Drag on the screen to add an element · drag a handle to resize · arrows nudge · Delete removes
        </div>
      </div>

      <div>
        {(busy !== 'idle' || progress.length > 0 || runs.length > 0) && (
          <RunProgress runs={runs} items={progress} active={busy !== 'idle'} onStop={stop} />
        )}

        {proposals && (
          <SplitPanel
            proposals={proposals}
            accepted={accepted}
            setAccepted={setAccepted}
            focused={focused}
            setFocused={setFocused}
            onApply={applySplits}
            onRerun={propose}
            applying={applying}
            busy={busy !== 'idle'}
            error={error}
          />
        )}

        <div className="card" style={{ padding: 12, marginBottom: 12 }}>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, maxHeight: 320, overflowY: 'auto' }}>
            {local.length === 0 && (
              <div style={{ fontSize: 12, color: 'var(--muted)', padding: '6px 2px' }}>
                Nothing here yet — press <strong>Find elements</strong>, or drag a box on the screen.
              </div>
            )}
            {local.map((r) => (
              <div
                key={r.id}
                onClick={() => setSelectedRegionId(r.id === selectedRegionId ? null : r.id)}
                style={{
                  display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px', borderRadius: 6,
                  background: r.id === selectedRegionId ? '#20242c' : 'transparent',
                  border: `1px solid ${r.id === selectedRegionId ? r.color + '88' : 'transparent'}`,
                  cursor: 'pointer',
                }}
              >
                <span style={{ width: 8, height: 8, borderRadius: 2, background: r.color, flexShrink: 0 }} />
                <span style={{ fontSize: 12, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {r.name}
                </span>
                <span
                  style={{ width: 6, height: 6, borderRadius: '50%', background: TYPE_DOT[r.asset_type], flexShrink: 0 }}
                  title={TYPE_LABEL[r.asset_type]}
                />
                {r.asset_id && <span style={{ fontSize: 10, color: 'var(--green)' }}>✓</span>}
              </div>
            ))}
          </div>
        </div>

        {selected && (
          <div className="card" style={{ padding: 12, marginBottom: 12 }}>
            <div style={{ display: 'flex', gap: 10, marginBottom: 10 }}>
              <div className="checkerboard checkerboard-sm" style={{ width: 54, height: 54, borderRadius: 7, flexShrink: 0, overflow: 'hidden' }}>
                {cropPath && <img src={storageUrl(cropPath)} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />}
              </div>
              <div style={{ flex: 1, minWidth: 0 }}>
                <input
                  className="input"
                  style={{ padding: '5px 8px', fontSize: 12.5, fontWeight: 600 }}
                  value={selected.name}
                  onChange={(e) => rename(selected.id, e.target.value)}
                />
                <div style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: 4 }}>
                  {selected.resolution || 'auto'} · from the screenshot
                </div>
              </div>
            </div>
            <div style={{ display: 'flex', gap: 5, flexWrap: 'wrap', marginBottom: 10 }}>
              {ASSET_TYPES.map((t) => (
                <div
                  key={t}
                  className={`pill${selected.asset_type === t ? ' active' : ''}`}
                  style={{ padding: '3px 8px', fontSize: 10 }}
                  onClick={() => setType(selected.id, t)}
                >
                  <span style={{ width: 6, height: 6, borderRadius: '50%', background: TYPE_DOT[t], display: 'inline-block', marginRight: 5 }} />
                  {TYPE_LABEL[t]}
                </div>
              ))}
            </div>
            <button
              className="btn btn-danger"
              style={{ padding: '4px 10px', fontSize: 11 }}
              onClick={() => remove(selected.id)}
            >
              Delete element
            </button>
          </div>
        )}

        {hasChanges && (
          <button
            className="btn btn-primary"
            style={{
              width: '100%', fontWeight: 700, padding: 10, marginBottom: 8,
              background: 'var(--accent)', color: '#fff', border: 'none',
              boxShadow: '0 0 12px rgba(108, 140, 255, 0.4)',
            }}
            disabled={saving}
            onClick={saveChanges}
          >
            {saving ? 'Saving changes…' : '💾 Save changes'}
          </button>
        )}

        <button
          className="btn btn-accent"
          style={{ width: '100%', fontWeight: 700, padding: 10 }}
          disabled={local.length === 0 || saving || applying}
          onClick={async () => {
            if (hasChanges) await saveChanges()
            next()
          }}
        >
          {hasChanges ? 'Save changes & next →' : 'Next: build these elements →'}
        </button>
      </div>
    </div>
  )
}
