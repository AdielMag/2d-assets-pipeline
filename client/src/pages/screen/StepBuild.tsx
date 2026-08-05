import { useEffect, useMemo, useState } from 'react'
import { api, storageUrl } from '../../api'
import type { Atlas, ReuseCandidate } from '../../api'
import ImageProviderChooser, { fmtCredits, usePerCallCost, useImageProviders } from '../../components/ImageProviderChooser'
import { RunProgress } from '../../components/RunProgress'
import { ReuseApprovalModal } from './ReuseApprovalModal'
import type { StepProps } from './types'

interface AtlasNode extends Atlas { children: AtlasNode[] }

function flatten(atlases: Atlas[]): { atlas: Atlas; depth: number }[] {
  const nodes = new Map<number, AtlasNode>(atlases.map((a) => [a.id, { ...a, children: [] }]))
  const roots: AtlasNode[] = []
  for (const a of nodes.values()) {
    if (a.parent_id != null && nodes.has(a.parent_id)) nodes.get(a.parent_id)!.children.push(a)
    else roots.push(a)
  }
  const walk = (ns: AtlasNode[], depth = 0): { atlas: Atlas; depth: number }[] =>
    ns.flatMap((n) => [{ atlas: n, depth }, ...walk(n.children, depth + 1)])
  return walk(roots)
}

export function StepBuild({
  project, mockup, atlases, atlasId, setAtlasId, reload, busy, progress, stop, runStream,
  runs, previewPath, next, refreshPreview,
}: StepProps) {
  const [provider, setProvider] = useState('higgsfield')
  const [model, setModel] = useState('')
  const [visualModel, setVisualModel] = useState('')
  const [livePreview, setLivePreview] = useState('')
  // Matches the server would bind to existing assets, held here until the user decides.
  // Non-null means the approval sheet is up and no build has started yet.
  const [reuseAsk, setReuseAsk] = useState<{ candidates: ReuseCandidate[]; rebuild: boolean } | null>(null)
  const [checkingReuse, setCheckingReuse] = useState(false)
  // Bumped when a per-model param (quality, resolution, ...) is saved, so the cost
  // estimate below refetches instead of showing the price for the old params.
  const [paramsRev, setParamsRev] = useState(0)

  useEffect(() => {
    api.providers()
      .then((p) => setProvider(p.settings.default_image_provider || 'higgsfield'))
      .catch(() => {})
  }, [])

  const flat = useMemo(() => flatten(atlases), [atlases])
  const regions = mockup?.regions ?? []
  const pendingRegions = regions.filter((r) => !r.asset_id)
  const pending = pendingRegions.length
  const built = regions.length - pending
  // Build always extracts pixels from the screenshot for free UNLESS a region was
  // explicitly marked to be generated instead (region.source === 'generate') — the only
  // path here that actually spends a provider call. See _build_atlas's `want` resolution
  // on the server, which this mirrors. When nothing is pending the button rebuilds
  // everything (unbinding every region first), so the target set is all of them, not
  // just the currently-unbuilt ones.
  const targetRegions = pending === 0 ? regions : pendingRegions
  const generateCount = targetRegions.filter((r) => r.source === 'generate').length
  const imageProviders = useImageProviders()
  const currentProviderInfo = imageProviders.find((p) => p.name === provider)
  const effectiveModel = model || currentProviderInfo?.selected_model || currentProviderInfo?.default_model || ''
  const perCall = usePerCallCost(provider, generateCount > 0 ? effectiveModel : undefined, paramsRev)
  const totalCredits = generateCount > 0 && perCall.credits != null ? generateCount * perCall.credits : null

  if (!mockup) return null

  const newDomain = async () => {
    const name = window.prompt('Name this domain (e.g. Common, Store, Lobby)')
    if (!name) return
    const a = await api.createAtlas(project.id, { name })
    await reload()
    setAtlasId(a.id)
  }

  const runBuild = async (shouldRebuild: boolean, reuse: Record<string, number>) => {
    if (atlasId == null) return
    setLivePreview('')
    const res = await runStream('building', (onEvent, signal) =>
      api.buildAtlasStream(mockup.id, atlasId, provider, undefined, (e) => {
        onEvent(e)
        // The server composites after every element, so the screen assembles as it goes.
        if (e.step === 'preview' && e.status === 'done') {
          const path = (e.data as { path?: string } | undefined)?.path
          if (path) setLivePreview(path + '?t=' + Date.now())
        }
      }, signal, shouldRebuild, reuse))
    await reload()
    await refreshPreview(mockup.id).catch(() => {})
    if (res && typeof res === 'object' && 'errors' in res && Array.isArray((res as any).errors) && (res as any).errors.length === 0) {
      next()
    }
  }

  // Reuse is never decided by the build itself: ask the server what it would bind to an
  // asset that already exists and, if there is anything, let the user rule on each one
  // before a single region is written. If that lookup fails we build rather than reuse —
  // the wrong build costs a free re-extraction, the wrong reuse costs a wrong element.
  const build = async () => {
    if (atlasId == null) return
    const shouldRebuild = pending === 0
    setCheckingReuse(true)
    try {
      const { candidates } = await api.reuseCandidates(mockup.id, atlasId, shouldRebuild)
      if (candidates.length > 0) {
        setReuseAsk({ candidates, rebuild: shouldRebuild })
        return
      }
    } catch (e) {
      console.error('Could not check for reusable assets', e)
    } finally {
      setCheckingReuse(false)
    }
    await runBuild(shouldRebuild, {})
  }

  const shown = livePreview || previewPath

  return (
    <div style={{ display: 'grid', gridTemplateColumns: '1fr 340px', gap: 20, alignItems: 'start' }}>
      <div>
        <div className="field-label">
          {built} of {regions.length} element{regions.length === 1 ? '' : 's'} placed
          {built < regions.length && ' · the original shows faintly behind what has been cut so far'}
        </div>
        <div
          className="checkerboard"
          style={{
            position: 'relative', width: '100%', height: 'min(72vh, 640px)', borderRadius: 12,
            overflow: 'hidden', border: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          {/* The source, faint, underneath. Without it a screen that is only part-built
              reads as a broken preview rather than an unfinished one — which is exactly
              how it looks right after a split releases every frame for recutting. */}
          <img
            src={storageUrl(mockup.image_path)}
            alt=""
            style={{
              position: 'absolute', inset: 0, width: '100%', height: '100%',
              objectFit: 'contain', opacity: built === regions.length ? 0 : 0.14,
              transition: 'opacity 0.3s ease', pointerEvents: 'none',
            }}
          />
          {shown ? (
            <img
              src={storageUrl(shown)}
              alt=""
              style={{ position: 'relative', width: '100%', height: '100%', objectFit: 'contain' }}
            />
          ) : (
            <div style={{ position: 'relative', color: 'var(--muted-2)', fontSize: 12.5, textAlign: 'center', padding: 20 }}>
              Nothing built yet.<br />The rebuilt screen appears here as each element is cut out.
            </div>
          )}
        </div>
      </div>

      <div>
        <div className="card" style={{ padding: 14, marginBottom: 12 }}>
          <div className="field-label">Save the assets into</div>
          <div style={{ display: 'flex', gap: 6, marginBottom: 12 }}>
            <select
              className="input"
              style={{ flex: 1, padding: '7px 9px', fontSize: 12 }}
              value={atlasId ?? ''}
              onChange={(e) => setAtlasId(e.target.value ? Number(e.target.value) : null)}
            >
              <option value="">Choose a domain…</option>
              {flat.map(({ atlas, depth }) => (
                <option key={atlas.id} value={atlas.id}>{'— '.repeat(depth)}{atlas.name}</option>
              ))}
            </select>
            <button className="btn btn-secondary" style={{ padding: '6px 10px', fontSize: 11.5 }} onClick={newDomain}>
              + New
            </button>
          </div>

          <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.6, marginBottom: 12 }}>
            {pending === 0
              ? 'Every element already has an asset.'
              : `${pending} element${pending === 1 ? '' : 's'} to build${built ? `, ${built} already done` : ''}.`}
            {' '}Elements are cut straight out of the screenshot, so they keep the original
            pixels and cost no provider quota. If something here matches an asset already in
            this domain or a parent domain, you get to approve that swap before it happens.
          </div>

          {generateCount > 0 && (
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
          )}

          {generateCount > 0 && provider === 'higgsfield' && (
            <div style={{ fontSize: 10.5, color: 'var(--muted)', marginBottom: 12 }}>
              {generateCount === 0
                ? '🪙 Free — every pending element is extracted, not generated.'
                : perCall.loading
                  ? '🪙 Estimating cost…'
                  : totalCredits != null
                    ? `🪙 ~${fmtCredits(totalCredits)} credit${totalCredits === 1 ? '' : 's'} for ${generateCount} element${generateCount === 1 ? '' : 's'} marked to generate (the rest are free extracts)`
                    : `${generateCount} element${generateCount === 1 ? '' : 's'} marked to generate — cost unavailable`}
            </div>
          )}

          {busy === 'building' ? (
            <button
              className="btn"
              style={{ width: '100%', fontWeight: 700, padding: 11, background: '#e5484d', color: '#fff' }}
              onClick={stop}
            >
              ⏹ Stop
            </button>
          ) : (
            <button
              className="btn btn-accent"
              style={{ width: '100%', fontWeight: 700, padding: 11 }}
              disabled={atlasId == null || regions.length === 0 || checkingReuse}
              onClick={build}
            >
              {checkingReuse
                ? 'Checking the library…'
                : pending === 0 ? 'Rebuild all elements' : `Build ${pending} element${pending === 1 ? '' : 's'}`}
            </button>
          )}
          {atlasId == null && (
            <div style={{ fontSize: 11, color: 'var(--warm)', marginTop: 8, textAlign: 'center' }}>
              Pick a domain first.
            </div>
          )}
        </div>

        {(busy !== 'idle' || progress.length > 0 || runs.length > 0) && (
          <RunProgress runs={runs} items={progress} active={busy !== 'idle'} onStop={stop} />
        )}

        {built > 0 && busy === 'idle' && (
          <button className="btn btn-secondary" style={{ width: '100%', padding: 9 }} onClick={next}>
            Continue to Text →
          </button>
        )}
      </div>

      {reuseAsk && (
        <ReuseApprovalModal
          candidates={reuseAsk.candidates}
          onCancel={() => setReuseAsk(null)}
          onConfirm={(approvals) => {
            const { rebuild } = reuseAsk
            setReuseAsk(null)
            runBuild(rebuild, approvals)
          }}
        />
      )}
    </div>
  )
}
