import { useCallback, useEffect, useMemo, useState } from 'react'
import { api, storageUrl, TYPE_DOT } from '../../api'
import type { Asset, Label, Region, StepStatus } from '../../api'
import ImageProviderChooser, { fmtCredits, usePerCallCost, useImageProviders } from '../../components/ImageProviderChooser'
import { RunProgress } from '../../components/RunProgress'
import type { StepProps } from './types'

const selectedImage = (a: Asset | undefined) => {
  if (!a) return ''
  const v = a.versions.find((x) => x.id === a.selected_version_id) ?? a.versions[0]
  return v ? (v.processed_path || v.raw_path) : ''
}

function labelInRegion(label: Label, region: Region): boolean {
  const cx = label.x + label.w / 2
  const cy = label.y + label.h / 2
  return cx >= region.x && cx <= region.x + region.w && cy >= region.y && cy <= region.y + region.h
}

/** What to do with the lettering on a frame.
 *
 *  'erase'   — the frame comes back blank and the words survive only as data (the
 *              MockupLabel), for Unity to re-render with a real font. Localisable, but the
 *              caption's own artwork — its outline, gradient, drop shadow — is gone.
 *  'extract' — the same blank frame, plus the lettering redrawn by the AI as a separate
 *              sprite layered back over it. Keeps the look of the original artwork, at the
 *              cost of being a picture of words rather than words — and, since it's an AI
 *              redraw rather than a pixel-for-pixel cut, it can deviate slightly from the
 *              source in exchange for coming out clean. `null` (elsewhere in this file)
 *              means neither: the lettering is left baked into the frame untouched. */
type TextMode = 'erase' | 'extract'

const CHOICES: { id: TextMode; label: string; hint: string }[] = [
  {
    id: 'erase',
    label: 'Remove',
    hint: 'The words survive as data only. Unity draws them with a real font, so they can be restyled and translated — but the original lettering’s outline, gradient and shadow are gone.',
  },
  {
    id: 'extract',
    label: 'Extract',
    hint: 'Same blank frame, plus the lettering redrawn by the AI as its own asset layered over it. Keeps the look of the original artwork — it is a picture of the words, so it can’t be re-typed or translated, and may deviate slightly from the source pixels.',
  },
]

/** Removing (or isolating) the lettering baked into each built element's cut — one
 *  caption, several, or all of them at once. Runs after Build, because there has to be
 *  a built asset to redraw, and before Polish, which only does the cosmetic
 *  upscale/clean-edges pass and never touches text.
 *
 *  Each caption on an element gets its own Keep/Remove/Extract choice — a nav bar with
 *  "Shop", "Cards" and "Battle" printed on it can extract one, erase another and leave
 *  the third baked in, all in the same pass. Picking choices below and hitting Apply for
 *  that element is the whole step: nothing is deferred to a later pass to maybe pick up.
 *  Apply saves each changed caption's choice, then — if the element has anything not
 *  left on "Keep" — redraws it through the provider ONCE, as a single call informed of
 *  every caption's choice (see the server's `build_text_removal_instruction`), plus one
 *  further call per caption marked Extract. An element whose captions are all still on
 *  "Keep" costs nothing, even when swept up by "Apply to all". */
export function StepText({
  project, mockup, busy, progress, stop, runStream, runs, previewPath, reload, refreshPreview, next,
}: StepProps) {
  const [provider, setProvider] = useState('higgsfield')
  const [model, setModel] = useState('')
  const [visualModel, setVisualModel] = useState('')
  const [assetList, setAssetList] = useState<Asset[]>([])
  // label id -> chosen mode. Absent = left alone (lettering stays baked into the frame).
  const [choices, setChoices] = useState<Map<number, TextMode>>(new Map())
  const [selected, setSelected] = useState<Set<number>>(new Set())
  const [savingIds, setSavingIds] = useState<Set<number>>(new Set())
  const [runningIds, setRunningIds] = useState<Set<number> | null>(null)
  const [paramsRev, setParamsRev] = useState(0)
  const [status, setStatus] = useState<StepStatus | null>(null)
  // See StepPolish: off means a re-run only buys what a stopped run never got to.
  const [redoDone, setRedoDone] = useState(false)

  useEffect(() => {
    api.providers()
      .then((p) => setProvider(p.settings.default_image_provider || 'higgsfield'))
      .catch(() => {})
  }, [])

  const imageProviders = useImageProviders()
  const currentProviderInfo = imageProviders.find((p) => p.name === provider)
  const effectiveModel = model || currentProviderInfo?.selected_model || currentProviderInfo?.default_model || ''
  const perCall = usePerCallCost(provider, effectiveModel, paramsRev)

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

  const assets = useMemo(() => Object.fromEntries(assetList.map((a) => [a.id, a])), [assetList])
  const statusFor = useMemo(
    () => Object.fromEntries((status?.regions ?? []).map((r) => [r.region_id, r])),
    [status],
  )
  /** Whether this caption's sprite is already sitting in the catalogue. Only "extract"
   *  captions have one — "erase" leaves nothing behind by design. */
  const spriteReady = (regionId: number, labelId: number) =>
    !!statusFor[regionId]?.captions.find((c) => c.label_id === labelId)?.sprite_ready

  // Only already-built elements are actionable here — there is no asset yet to redraw
  // for anything Build hasn't produced.
  const groups = useMemo(() => {
    if (!mockup) return []
    return mockup.regions
      .filter((region) => region.asset_id && region.source !== 'text')
      .map((region) => ({ region, labels: mockup.labels.filter((l) => labelInRegion(l, region)) }))
      .filter((g) => g.labels.length > 0)
  }, [mockup])

  useEffect(() => {
    const initial = new Map<number, TextMode>()
    for (const g of groups) {
      for (const label of g.labels) {
        if (label.text_mode === 'erase' || label.text_mode === 'extract') {
          initial.set(label.id, label.text_mode)
        }
      }
    }
    setChoices(initial)
    setSelected(new Set())
    // Only re-seed when the mockup itself changes — otherwise every click would stomp the
    // in-progress selection back to whatever was last saved.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mockup?.id])

  // Provider calls a run against these region ids will actually make: one shared "base
  // redraw" call per element that has ANY caption marked Remove or Extract (never one
  // call per caption — see the comment on `apply` above), plus one further call per
  // caption marked Extract specifically. Zero for an element left entirely on Keep.
  //
  // Mirrors the server's resume rule exactly (see `_redraw_built_regions`), so the number
  // shown is the number that will be spent: an element already cleaned costs nothing for
  // the base redraw and only pays for the caption sprites that are actually missing —
  // while one that still needs the redraw re-extracts all of its captions, since they are
  // cut from the frame that redraw produces.
  const callsForRun = (ids: number[]) => ids.reduce((sum, id) => {
    const g = groups.find((x) => x.region.id === id)
    if (!g) return sum
    const modes = g.labels.map((l) => choices.get(l.id))
    if (!modes.some((m) => m)) return sum
    const extracts = g.labels.filter((l) => choices.get(l.id) === 'extract')
    if (redoDone || !statusFor[id]?.text_cleaned) return sum + 1 + extracts.length
    return sum + extracts.filter((l) => !spriteReady(id, l.id)).length
  }, 0)

  if (!mockup) return null

  const setChoice = (labelId: number, mode: TextMode | null) => {
    setChoices((prev) => {
      const next = new Map(prev)
      if (mode === null) next.delete(labelId)
      else next.set(labelId, mode)
      return next
    })
  }

  const toggleSel = (id: number) => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  const allSelected = groups.length > 0 && groups.every((g) => selected.has(g.region.id))
  const toggleAll = () => setSelected(allSelected ? new Set() : new Set(groups.map((g) => g.region.id)))

  /** Save whichever of these elements' captions actually changed choice, then redraw
   *  whichever elements have anything not on "Keep". Saving and running are one action
   *  from the user's side, but kept as two calls here since only a changed caption needs
   *  writing, and only an element with a non-Keep caption needs a provider call. */
  const apply = async (ids: number[], force = redoDone) => {
    const targetLabels = ids.flatMap((id) => groups.find((x) => x.region.id === id)?.labels ?? [])
    setSavingIds(new Set(ids))
    try {
      await Promise.all(targetLabels.map(async (label) => {
        const want = choices.get(label.id) ?? null
        if ((label.text_mode ?? null) !== want) {
          await api.updateLabel(label.id, { text_mode: want })
        }
      }))
    } finally {
      setSavingIds(new Set())
    }
    await reload()

    const runIds = ids.filter((id) => groups.find((x) => x.region.id === id)?.labels.some((l) => choices.get(l.id)))
    if (runIds.length === 0) return
    setRunningIds(new Set(runIds))
    await runStream('texting', (onEvent, signal) =>
      api.applyTextStream(mockup.id, provider, runIds, onEvent, signal, model, force))
    setRunningIds(null)
    await reload()
    await loadAssets()
    await loadStatus()
    await refreshPreview(mockup.id).catch(() => {})
  }

  if (groups.length === 0) {
    const nothingBuilt = !mockup.regions.some((r) => r.asset_id)
    return (
      <div className="card" style={{ padding: 16, maxWidth: 440 }}>
        <div style={{ fontSize: 12.5, color: 'var(--text-2)', marginBottom: 14 }}>
          {nothingBuilt
            ? 'Nothing built yet — go back to Build first.'
            : 'No text was detected on any built element — nothing to remove here.'}
        </div>
        <button className="btn btn-accent" style={{ width: '100%', fontWeight: 700, padding: 10 }} onClick={next}>
          Skip this step →
        </button>
      </div>
    )
  }

  // Counted over the elements this list actually shows, rather than taken from the
  // server's own totals. The server attaches a caption to every region it OVERLAPS — the
  // nav icon sitting under the word "Shop" counts as having text work there — while this
  // list shows the one region the caption sits inside. Deriving the headline from the
  // visible rows keeps the number and the list from telling different stories.
  const shown = groups.map((g) => statusFor[g.region.id]).filter(Boolean)
  const textNeeded = shown.filter((s) => s.text_needed).length
  const textDone = shown.filter((s) => s.text_done).length
  const spritesExpected = shown.reduce(
    (n, s) => n + s.captions.filter((c) => c.mode === 'extract').length, 0,
  )
  const spritesReady = shown.reduce(
    (n, s) => n + s.captions.filter((c) => c.mode === 'extract' && c.sprite_ready).length, 0,
  )

  const markedGroups = groups.filter((g) => g.labels.some((l) => choices.get(l.id)))
  const selectedRunnable = [...selected].filter(
    (id) => groups.find((g) => g.region.id === id)?.labels.some((l) => choices.get(l.id)),
  ).length

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 380px', gap: 20, alignItems: 'start' }}>
      <div>
        <div className="field-label">
          Choose what happens to each element's lettering, then apply it
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
              Nothing built yet.
            </div>
          )}
        </div>
      </div>

      <div>
        <div className="card" style={{ padding: 14, marginBottom: 12 }}>
          <div style={{ fontSize: 13, fontWeight: 600, marginBottom: 4 }}>
            {markedGroups.length} of {groups.length} element{groups.length === 1 ? '' : 's'} marked
          </div>
          {status && textNeeded > 0 && (
            <div style={{ fontSize: 11, marginBottom: 8, lineHeight: 1.7 }}>
              <div>
                <span style={{ color: '#3dd68c' }}>✓ {textDone} done</span>
                <span style={{ color: 'var(--muted-2)' }}> · </span>
                <span style={{ color: textNeeded - textDone ? '#e0c15c' : 'var(--muted)' }}>
                  {textNeeded - textDone} still to do
                </span>
                <span style={{ color: 'var(--muted-2)' }}> of {textNeeded} with text work</span>
              </div>
              {spritesExpected > 0 && (
                <div style={{ color: 'var(--muted)' }}>
                  Text sprites: {spritesReady} of {spritesExpected} generated
                  {spritesExpected > spritesReady && (
                    <span style={{ color: '#e0c15c' }}>
                      {' '}· {spritesExpected - spritesReady} missing
                    </span>
                  )}
                </div>
              )}
            </div>
          )}
          <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.6, marginBottom: 10 }}>
            Each caption on an element gets its own Keep / Remove / Extract choice. Keep
            leaves it baked in as drawn — free. An element with anything marked Remove or
            Extract redraws once through the provider (plus one further call per caption
            extracted) the moment you apply it, right here.
          </div>

          <label style={{
            display: 'flex', alignItems: 'flex-start', gap: 7, fontSize: 11,
            color: 'var(--muted)', lineHeight: 1.5, marginBottom: 10, cursor: 'pointer',
          }}>
            <input
              type="checkbox"
              checked={redoDone}
              onChange={(e) => setRedoDone(e.target.checked)}
              disabled={busy === 'texting'}
              style={{ marginTop: 1 }}
            />
            <span>
              Redo elements already cleaned.
              {' '}
              <span style={{ color: 'var(--muted-2)' }}>
                Off, applying again only buys the captions that are still missing — an
                element that came through the last run is left alone.
              </span>
            </span>
          </label>

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

          {provider === 'higgsfield' && (selectedRunnable > 0 || markedGroups.length > 0) && (
            <div style={{ fontSize: 10.5, color: 'var(--muted)', marginBottom: 12 }}>
              🪙{' '}
              {perCall.loading
                ? 'Estimating cost…'
                : perCall.credits != null
                  ? [
                      selectedRunnable > 0 && `~${fmtCredits(perCall.credits * callsForRun(Array.from(selected)))} for ${selected.size} selected`,
                      markedGroups.length > 0 && `~${fmtCredits(perCall.credits * callsForRun(groups.map((g) => g.region.id)))} for all marked`,
                    ].filter(Boolean).join(' · ')
                  : 'Cost unavailable'}
            </div>
          )}

          {busy === 'texting' ? (
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
                onClick={() => apply(Array.from(selected))}
              >
                Apply to {selected.size || ''} selected{selectedRunnable !== selected.size && selected.size > 0 ? ` (${selectedRunnable} will redraw)` : ''}
              </button>
              <button
                className="btn btn-secondary"
                style={{ width: '100%', padding: 9 }}
                onClick={() => apply(groups.map((g) => g.region.id))}
              >
                Apply to all {groups.length} element{groups.length === 1 ? '' : 's'}
              </button>
            </>
          )}
        </div>

        {(busy !== 'idle' || progress.length > 0 || runs.length > 0) && (
          <RunProgress runs={runs} items={progress} active={busy !== 'idle'} onStop={stop} />
        )}

        <div className="card" style={{ padding: 12, marginTop: 12 }}>
          <div style={{ fontSize: 12, fontWeight: 600, color: 'var(--text-2)', marginBottom: 8, display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span>Elements with text</span>
            <button className="pill" style={{ padding: '3px 8px', fontSize: 10.5 }} onClick={toggleAll}>
              {allSelected ? 'Deselect all' : 'Select all'}
            </button>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxHeight: 440, overflowY: 'auto', paddingRight: 2 }}>
            {groups.map(({ region, labels }) => {
              const asset = region.asset_id ? assets[region.asset_id] : undefined
              const img = selectedImage(asset)
              const isSel = selected.has(region.id)
              const isSaving = savingIds.has(region.id)
              const isRunningNow = busy === 'texting' && runningIds?.has(region.id)
              const isBusyRow = isSaving || isRunningNow

              return (
                <div
                  key={region.id}
                  className="card"
                  style={{
                    padding: 8, display: 'flex', flexDirection: 'column', gap: 6,
                    background: isSel ? '#20242c' : 'rgba(255,255,255,0.02)',
                    border: `1px solid ${isSel ? 'var(--accent-border)' : 'var(--border)'}`,
                    opacity: isBusyRow ? 0.6 : 1,
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 9 }}>
                    <input
                      type="checkbox"
                      checked={isSel}
                      onChange={() => toggleSel(region.id)}
                      disabled={busy === 'texting'}
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
                        {region.name}
                      </div>
                      <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 2 }}>
                        <span style={{ width: 5, height: 5, borderRadius: '50%', background: TYPE_DOT[region.asset_type] }} />
                        <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>
                          {labels.length} caption{labels.length === 1 ? '' : 's'}
                        </span>
                        {/* Where this element actually stands, so a run that stopped
                            partway is legible without opening every asset. */}
                        {statusFor[region.id]?.text_needed && (
                          <>
                            <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>·</span>
                            <span style={{
                              fontSize: 10,
                              color: statusFor[region.id]?.text_done ? '#3dd68c'
                                : statusFor[region.id]?.text_cleaned ? '#e0c15c' : 'var(--muted-2)',
                            }}>
                              {statusFor[region.id]?.text_done ? '✓ done'
                                : statusFor[region.id]?.text_cleaned
                                  ? `cleaned · ${statusFor[region.id]!.missing_sprites.length} sprite${statusFor[region.id]!.missing_sprites.length === 1 ? '' : 's'} missing`
                                  : 'not applied'}
                            </span>
                          </>
                        )}
                      </div>
                    </div>
                    {isRunningNow ? (
                      <span style={{ fontSize: 10, color: 'var(--accent)', flexShrink: 0 }}>Removing…</span>
                    ) : isSaving ? (
                      <span style={{ fontSize: 10, color: 'var(--muted)', flexShrink: 0 }}>Saving…</span>
                    ) : busy !== 'texting' && (
                      <button
                        className="btn btn-secondary"
                        style={{ padding: '3px 8px', fontSize: 10.5, flexShrink: 0 }}
                        title={statusFor[region.id]?.text_done
                          ? 'Already applied — this redraws it a second time'
                          : 'Save these choices and apply them to this element'}
                        // Explicitly forced: asking for one named element is a decision
                        // about that element, so it must do something even when the
                        // element is already done.
                        onClick={() => apply([region.id], true)}
                      >
                        {statusFor[region.id]?.text_done ? 'Redo' : 'Apply'}
                      </button>
                    )}
                  </div>

                  <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                    {labels.map((label) => {
                      const choice = choices.get(label.id) ?? null
                      return (
                        <div key={label.id} style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                          <span
                            style={{
                              fontSize: 10, color: 'var(--muted-2)', flex: 1, minWidth: 0,
                              overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                            }}
                            title={label.text}
                          >
                            "{label.text}"
                            {/* Only Extract produces a sprite of its own, so only Extract
                                can be missing one. Shown per caption because that is the
                                unit a run buys and the unit a failure loses. */}
                            {label.text_mode === 'extract' && (
                              <span style={{
                                marginLeft: 5,
                                color: spriteReady(region.id, label.id) ? '#3dd68c' : '#e0c15c',
                              }}>
                                {spriteReady(region.id, label.id) ? '✓ sprite' : '⚠ no sprite yet'}
                              </span>
                            )}
                          </span>
                          <div
                            style={{ display: 'flex', gap: 3, flexShrink: 0 }}
                            title={choice ? CHOICES.find((c) => c.id === choice)?.hint : 'Lettering stays baked into the frame'}
                          >
                            <button
                              className="btn"
                              disabled={busy === 'texting'}
                              onClick={() => setChoice(label.id, null)}
                              style={{
                                padding: '3px 6px', fontSize: 10, fontWeight: 700,
                                borderColor: choice === null ? 'var(--border-3)' : 'var(--border)',
                                background: choice === null ? 'var(--bg-2, rgba(255,255,255,0.06))' : 'transparent',
                              }}
                            >
                              Keep
                            </button>
                            {CHOICES.map((c) => (
                              <button
                                key={c.id}
                                className="btn"
                                disabled={busy === 'texting'}
                                onClick={() => setChoice(label.id, c.id)}
                                style={{
                                  padding: '3px 6px', fontSize: 10, fontWeight: 700,
                                  borderColor: choice === c.id ? '#ff9f4a' : 'var(--border)',
                                  background: choice === c.id ? 'rgba(255,159,74,0.14)' : 'transparent',
                                }}
                              >
                                {c.label}
                              </button>
                            ))}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        <div style={{ fontSize: 11, color: 'var(--muted-2)', lineHeight: 1.5, margin: '10px 0' }}>
          A wrong removal can&apos;t be undone once it's applied — the element is redrawn from
          whatever is currently built, not the original screenshot.
        </div>

        {busy === 'idle' && (
          <button className="btn btn-secondary" style={{ width: '100%', padding: 9 }} onClick={next}>
            Continue →
          </button>
        )}
      </div>
    </div>
  )
}
