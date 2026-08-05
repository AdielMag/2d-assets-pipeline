import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, storageUrl, TYPE_DOT, TYPE_LABEL } from '../../api'
import type { Asset, StepStatus } from '../../api'
import ImageProviderChooser, { fmtCredits, usePerCallCost, useImageProviders } from '../../components/ImageProviderChooser'
import { RunProgress } from '../../components/RunProgress'
import type { StepProps } from './types'

const selectedImage = (a: Asset | undefined) => {
  if (!a) return ''
  const v = a.versions.find((x) => x.id === a.selected_version_id) ?? a.versions[0]
  return v ? (v.processed_path || v.raw_path) : ''
}

/** The AI polish pass, run on demand against a chosen set of already-built elements
 *  rather than folded into Build. Touch up one element, a handful, or everything —
 *  each run only re-touches the regions it was asked for, so polishing one element
 *  never redraws the rest of the screen. */
export function StepPolish({
  project, mockup, busy, progress, stop, runStream, runs, previewPath, reload, refreshPreview, next,
}: StepProps) {
  const [provider, setProvider] = useState('higgsfield')
  const [model, setModel] = useState('')
  const [visualModel, setVisualModel] = useState('')
  const [assetList, setAssetList] = useState<Asset[]>([])
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [runningIds, setRunningIds] = useState<Set<number> | null>(null)
  const [paramsRev, setParamsRev] = useState(0)
  const [status, setStatus] = useState<StepStatus | null>(null)
  // Off by default: a run that stopped halfway has already paid for everything it got
  // through, and re-buying it is both a wasted charge and a second redraw of an
  // already-redrawn sprite. On, this is a deliberate second pass.
  const [redoDone, setRedoDone] = useState(false)

  useEffect(() => {
    api.providers()
      .then((p) => setProvider(p.settings.default_image_provider || 'higgsfield'))
      .catch(() => {})
  }, [])

  const loadAssets = useCallback(async () => {
    if (!project) return
    try {
      setAssetList(await api.listAssets(project.id))
    } catch { /* ignore */ }
  }, [project?.id])

  const loadStatus = useCallback(async () => {
    if (!mockup) return
    try {
      setStatus(await api.stepStatus(mockup.id))
    } catch { /* ignore */ }
  }, [mockup?.id])

  useEffect(() => { loadAssets() }, [loadAssets, previewPath])
  useEffect(() => { loadStatus() }, [loadStatus, previewPath])

  // Selection belongs to the screen it was made for.
  useEffect(() => { setSelected(new Set()) }, [mockup?.id])

  const assets = useMemo(() => Object.fromEntries(assetList.map((a) => [a.id, a])), [assetList])

  const regions = mockup?.regions ?? []
  const polishable = regions.filter((r) => r.asset_id && r.source !== 'text')

  const statusFor = useMemo(
    () => Object.fromEntries((status?.regions ?? []).map((r) => [r.region_id, r])),
    [status],
  )
  // What a run would actually spend on, which is not the same as what is selected: an
  // element already polished off its current build is skipped unless Redo is on.
  const willRun = (ids: number[]) =>
    redoDone ? ids.length : ids.filter((id) => !statusFor[id]?.polished).length
  // Counted over the rows this list shows, so the headline can never disagree with what
  // is under it.
  const polishedCount = polishable.filter((r) => statusFor[r.id]?.polished).length

  const imageProviders = useImageProviders()
  const currentProviderInfo = imageProviders.find((p) => p.name === provider)
  const effectiveModel = model || currentProviderInfo?.selected_model || currentProviderInfo?.default_model || ''
  const perCall = usePerCallCost(provider, effectiveModel, paramsRev)

  if (!mockup) return null

  const toggle = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const allSelected = polishable.length > 0 && polishable.every((r) => selected.has(r.id))
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(polishable.map((r) => r.id)))

  /** `force` defaults to the Redo toggle; the per-element button passes true explicitly,
   *  since clicking Polish on one element that is already polished can only mean "do it
   *  again" — silently doing nothing there would look broken. */
  const runPolish = async (ids: number[] | undefined, force = redoDone) => {
    setRunningIds(new Set(ids ?? polishable.map((r) => r.id)))
    await runStream('polishing', (onEvent, signal) =>
      api.polishRegionsStream(mockup.id, provider, ids, onEvent, signal, model, force))
    setRunningIds(null)
    await reload()
    await loadAssets()
    await loadStatus()
    await refreshPreview(mockup.id).catch(() => {})
  }

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 360px', gap: 20, alignItems: 'start' }}>
      <div>
        <div className="field-label">
          Pick one element to touch up, or polish everything at once
        </div>
        <div
          className="checkerboard"
          style={{
            position: 'relative', width: '100%', height: 'min(72vh, 640px)', borderRadius: 12,
            overflow: 'hidden', border: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          {previewPath ? (
            <img
              src={storageUrl(previewPath)}
              alt=""
              style={{ position: 'relative', width: '100%', height: '100%', objectFit: 'contain' }}
            />
          ) : (
            <div style={{ position: 'relative', color: 'var(--muted-2)', fontSize: 12.5, textAlign: 'center', padding: 20 }}>
              Nothing built yet.<br />Go back to Build first.
            </div>
          )}
        </div>
      </div>

      <div>
        <div className="card" style={{ padding: 14, marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
            {polishable.length} element{polishable.length === 1 ? '' : 's'} can be polished
          </div>
          {status && (
            <div style={{
              display: 'flex', gap: 8, alignItems: 'center', flexWrap: 'wrap',
              fontSize: 11, marginBottom: 8,
            }}>
              <span style={{ color: '#3dd68c' }}>✓ {polishedCount} polished</span>
              <span style={{ color: 'var(--muted-2)' }}>·</span>
              <span style={{ color: polishable.length - polishedCount ? '#e0c15c' : 'var(--muted)' }}>
                {polishable.length - polishedCount} not polished yet
              </span>
            </div>
          )}
          <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.6, marginBottom: 10 }}>
            Redraws the selected elements through the provider to upscale them and clean up
            their edges. Costs one provider call per element. Lettering isn't touched here;
            that's the Text step, earlier in this wizard.
          </div>
          {/* Measured, not guessed: a 6-model sweep over 4 elements (see
              tools/sweep_polish_text.py) scored every polished result BELOW the untouched
              extraction, by 7-35 points. The step does sharpen — 1.5-2.5x edge energy — but
              every model repaints the palette while doing it, and colour drift costs more
              fidelity than sharpness buys back. Saying so here rather than removing the
              step: it may still be the right call on a bad extraction, which is a judgement
              the person looking at the element can make and this code cannot. */}
          <div style={{
            fontSize: 11, lineHeight: 1.55, marginBottom: 10, padding: '7px 9px', borderRadius: 6,
            color: '#e0c15c', border: '1px solid #5d5423', background: 'rgba(224,193,92,0.10)',
          }}>
            ⚠ Measured across 6 models: polishing scored <strong>7-35 points worse</strong> than
            leaving the extraction alone. It does sharpen edges, but every model shifts the
            colours while redrawing. Worth it to rescue a visibly bad cut — not as a routine pass.
          </div>

          <div style={{ marginBottom: 8 }}>
            <ImageProviderChooser
              compact
              provider={provider}
              onChangeProvider={setProvider}
              model={model}
              onChangeModel={setModel}
              visualModel={visualModel}
              onChangeVisualModel={setVisualModel}
              onChangeParams={() => setParamsRev((v) => v + 1)}
            />
          </div>

          <label style={{
            display: 'flex', alignItems: 'flex-start', gap: 7, fontSize: 11,
            color: 'var(--muted)', lineHeight: 1.5, marginBottom: 10, cursor: 'pointer',
          }}>
            <input
              type="checkbox"
              checked={redoDone}
              onChange={(e) => setRedoDone(e.target.checked)}
              disabled={busy === 'polishing'}
              style={{ marginTop: 1 }}
            />
            <span>
              Redo elements that are already polished.
              {' '}
              <span style={{ color: 'var(--muted-2)' }}>
                Off, a run picks up where a stopped one left off and only pays for what's
                missing.
              </span>
            </span>
          </label>

          {provider === 'higgsfield' && (selected.size > 0 || polishable.length > 0) && (
            <div style={{ fontSize: 10.5, color: 'var(--muted)', marginBottom: 12 }}>
              🪙{' '}
              {perCall.loading
                ? 'Estimating cost…'
                : perCall.credits != null
                  ? [
                      selected.size > 0 && `~${fmtCredits(perCall.credits * willRun(Array.from(selected)))} for ${selected.size} selected`,
                      `~${fmtCredits(perCall.credits * willRun(polishable.map((r) => r.id)))} for all ${polishable.length}`,
                    ].filter(Boolean).join(' · ')
                  : 'Cost unavailable'}
              {status && !redoDone && polishedCount > 0 && (
                <span> · {polishedCount} already done, not charged again</span>
              )}
            </div>
          )}

          {busy === 'polishing' ? (
            <button
              className="btn"
              style={{ width: '100%', fontWeight: 700, padding: 11, background: '#e5484d', color: '#fff' }}
              onClick={stop}
            >
              ⏹ Stop
            </button>
          ) : (
            <>
              <button
                className="btn btn-accent"
                style={{ width: '100%', fontWeight: 700, padding: 11, marginBottom: 8 }}
                disabled={selected.size === 0}
                onClick={() => runPolish(Array.from(selected))}
              >
                Polish {selected.size || ''} selected element{selected.size === 1 ? '' : 's'}
                {selected.size > 0 && willRun(Array.from(selected)) !== selected.size
                  && ` (${willRun(Array.from(selected))} to do)`}
              </button>
              <button
                className="btn btn-secondary"
                style={{ width: '100%', padding: 9 }}
                disabled={polishable.length === 0}
                onClick={() => runPolish(undefined)}
              >
                Polish all {polishable.length} element{polishable.length === 1 ? '' : 's'}
                {willRun(polishable.map((r) => r.id)) !== polishable.length
                  && ` (${willRun(polishable.map((r) => r.id))} still to do)`}
              </button>
            </>
          )}
        </div>

        {(busy !== 'idle' || progress.length > 0 || runs.length > 0) && (
          <RunProgress runs={runs} items={progress} active={busy !== 'idle'} onStop={stop} />
        )}

        <div className="card" style={{ padding: 12, marginTop: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)', marginBottom: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>Elements</span>
            <button
              className="pill"
              style={{ padding: '3px 8px', fontSize: 10.5 }}
              onClick={toggleAll}
              disabled={polishable.length === 0}
            >
              {allSelected ? 'Deselect all' : 'Select all'}
            </button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 420, overflowY: 'auto', paddingRight: 2 }}>
            {polishable.length === 0 && (
              <div style={{ fontSize: 11.5, color: 'var(--muted)', textAlign: 'center', padding: '10px 4px' }}>
                Nothing built yet — go back to Build first.
              </div>
            )}
            {polishable.map((r) => {
              const asset = r.asset_id ? assets[r.asset_id] : undefined
              const img = selectedImage(asset)
              const isSel = selected.has(r.id)
              const isRunningNow = busy === 'polishing' && runningIds?.has(r.id)
              return (
                <div
                  key={r.id}
                  onClick={() => busy !== 'polishing' && toggle(r.id)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 9, padding: '7px 8px', borderRadius: 8,
                    background: isSel ? '#20242c' : 'rgba(255,255,255,0.02)',
                    border: `1px solid ${isSel ? 'var(--accent-border)' : 'var(--border)'}`,
                    cursor: busy === 'polishing' ? 'default' : 'pointer', opacity: isRunningNow ? 0.6 : 1,
                    transition: 'background 0.15s ease',
                  }}
                >
                  <input
                    type="checkbox"
                    checked={isSel}
                    onChange={() => toggle(r.id)}
                    onClick={(e) => e.stopPropagation()}
                    disabled={busy === 'polishing'}
                  />
                  <div className="checkerboard checkerboard-sm" style={{ width: 30, height: 30, borderRadius: 5, flexShrink: 0, overflow: 'hidden' }}>
                    {img ? (
                      <img src={storageUrl(img)} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
                    ) : (
                      <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center', justifyContent: 'center', color: 'var(--muted-2)', fontSize: 9 }}>?</div>
                    )}
                  </div>
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12, fontWeight: 500, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {r.name}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 2 }}>
                      <span style={{ width: 5, height: 5, borderRadius: '50%', background: TYPE_DOT[r.asset_type] }} />
                      <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>{TYPE_LABEL[r.asset_type]}</span>
                      {/* The element's own answer to "is this one done?", so a screen left
                          half-finished by a stopped run reads at a glance instead of
                          having to be inferred from the preview. */}
                      <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>·</span>
                      <span style={{ fontSize: 10, color: statusFor[r.id]?.polished ? '#3dd68c' : '#e0c15c' }}>
                        {statusFor[r.id]?.polished ? '✓ polished' : 'not polished'}
                      </span>
                    </div>
                  </div>
                  {isRunningNow ? (
                    <span style={{ fontSize: 10, color: 'var(--accent)', flexShrink: 0 }}>Polishing…</span>
                  ) : busy !== 'polishing' && (
                    <button
                      className="btn btn-secondary"
                      style={{ padding: '3px 8px', fontSize: 10.5, flexShrink: 0 }}
                      title={statusFor[r.id]?.polished
                        ? 'Already polished — this polishes it a second time'
                        : 'Polish this element'}
                      onClick={(e) => { e.stopPropagation(); runPolish([r.id], true) }}
                    >
                      {statusFor[r.id]?.polished ? 'Redo' : 'Polish'}
                    </button>
                  )}
                </div>
              )
            })}
          </div>
        </div>

        {busy === 'idle' && (
          <button className="btn btn-secondary" style={{ width: '100%', padding: 9, marginTop: 12 }} onClick={next}>
            See the result →
          </button>
        )}
      </div>
    </div>
  )
}
