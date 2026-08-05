import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api } from '../../api'
import type { Atlas, Mockup, MockupScore, ProgressEvent, SplitProposal } from '../../api'
import { useApp } from '../../AppContext'
import type { ProgressItem } from '../../components/RunProgress'
import { StepBuild } from './StepBuild'
import { StepElements } from './StepElements'
import { StepPolish } from './StepPolish'
import { StepResult } from './StepResult'
import { StepSource } from './StepSource'
import { StepText } from './StepText'

export type Busy = 'idle' | 'detecting' | 'proposing' | 'building' | 'previewing' | 'polishing' | 'texting'

// Splitting used to be step 3 of six. It is not a separate decision from "what are the
// elements here" — it is the same one at a finer grain, it answers with more boxes on the
// same canvas, and as its own step it made every screen with nothing to split pay for a
// stop that had nothing to show. It now runs as the back half of Detect and reports into
// the Elements step, which can also ask for it on demand.
//
// Text used to sit before Build and only save a Remove/Extract flag for some later pass
// to maybe read. That pass needs a built asset to redraw from anyway, so Text now runs
// after Build, and Apply there is the actual AI redraw, not a flag for Polish to find —
// Polish itself is left with exactly one job, the cosmetic upscale/clean-edges pass.
const STEPS = [
  { key: 'source', label: 'Screen', hint: 'Pick or make the screen to break down' },
  { key: 'elements', label: 'Elements', hint: 'Find every reusable piece, and split the ones holding others' },
  { key: 'build', label: 'Build', hint: 'Cut each element into the asset library' },
  { key: 'text', label: 'Text', hint: 'Remove or extract the lettering on each element' },
  { key: 'polish', label: 'Polish', hint: 'Touch up one element, or all of them, with AI' },
  { key: 'result', label: 'Result', hint: 'The screen, rebuilt from your assets' },
] as const

/** Turning a screen into reusable assets, as the sequence it actually is.
 *
 *  It was one page with every control on it at once, which made a five-stage pipeline
 *  look like a control panel — the batch build sat next to per-element regeneration next
 *  to prompt editing, and nothing said which to press first. Each stage now owns the
 *  screen while it is the one being worked on, and the last stage is the payoff: the
 *  screen rebuilt out of the assets that were just made. */
export default function ScreenProcessPage() {
  const { project, generationStore, pushGenerationEvent, startGenerationRun, endGenerationRun, loadGenerationHistory } = useApp()

  const [step, setStep] = useState(0)
  const [atlases, setAtlases] = useState<Atlas[]>([])
  const [atlasId, setAtlasId] = useState<number | null>(null)
  const [mockups, setMockups] = useState<Mockup[]>([])
  const [mockupId, setMockupId] = useState<number | null>(null)
  const [selectedRegionId, setSelectedRegionId] = useState<number | null>(null)
  const [busy, setBusy] = useState<Busy>('idle')
  const [progress, setProgress] = useState<ProgressItem[]>([])
  const [error, setError] = useState('')
  const [previewPath, setPreviewPath] = useState('')
  const [missing, setMissing] = useState<string[]>([])
  const [score, setScore] = useState<MockupScore | null>(null)
  const [proposals, setProposals] = useState<SplitProposal[] | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const pendingSaveRef = useRef<(() => Promise<void>) | null>(null)

  const mockup = mockups.find((m) => m.id === mockupId) ?? null
  const regions = mockup?.regions ?? []
  const builtCount = regions.filter((r) => r.asset_id).length

  const reload = useCallback(async () => {
    if (!project) return
    const [a, m] = await Promise.all([api.listAtlases(project.id), api.listMockups(project.id)])
    setAtlases(a)
    setMockups(m)
    setAtlasId((cur) => (cur && a.some((x) => x.id === cur) ? cur : a[0]?.id ?? null))
    setMockupId((cur) => (cur && m.some((x) => x.id === cur) ? cur : m[0]?.id ?? null))
  }, [project?.id])

  useEffect(() => { reload().catch(() => {}) }, [reload])

  useEffect(() => {
    if (mockupId) loadGenerationHistory(`mockup-${mockupId}`, mockupId, 'mockup').catch(() => {})
  }, [mockupId, loadGenerationHistory])

  // Splits belong to the screen they were proposed for.
  useEffect(() => { setProposals(null); setSelectedRegionId(null) }, [mockupId])

  const assetFingerprint = regions.map((r) => `${r.id}:${r.asset_id ?? ''}`).join(',')

  // Keep the composited preview current — it is React state, so it does not survive
  // navigating away and back, and the Result step is useless without it.
  useEffect(() => {
    if (!mockup) return
    if (!mockup.regions.some((r) => r.asset_id)) {
      setPreviewPath(''); setMissing([]); setScore(null)
      return
    }
    api.previewScreen(mockup.id)
      .then((p) => { setPreviewPath(p.path + '?t=' + Date.now()); setMissing(p.missing) })
      .catch(() => {})
    // Scored separately and tolerantly: a scoring failure must never hide the preview.
    api.scoreMockup(mockup.id).then(setScore).catch(() => setScore(null))
  }, [mockup?.id, assetFingerprint])

  const pushProgress = useCallback((e: ProgressEvent) => setProgress((prev) => {
    const item: ProgressItem = {
      step: e.step, status: e.status, message: e.message,
      image: e.image, index: e.index, total: e.total, data: e.data, timestamp: e.timestamp,
    }
    const last = prev[prev.length - 1]
    if (last && last.step === e.step && last.status === 'running') return [...prev.slice(0, -1), item]
    return [...prev, item]
  }), [])

  /** Run one SSE job with the shared progress/abort plumbing. */
  const runStream = useCallback(async (
    kind: Busy,
    job: (onEvent: (e: ProgressEvent) => void, signal: AbortSignal) => Promise<unknown>,
  ) => {
    if (!mockup) return undefined
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(kind)
    setError('')
    setProgress([])
    const entityKey = `mockup-${mockup.id}`
    const runId = startGenerationRun(entityKey)
    try {
      return await job((e) => {
        pushProgress(e)
        pushGenerationEvent(entityKey, e, runId)
      }, controller.signal)
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return undefined
      setError(e instanceof Error ? e.message : 'Something went wrong')
      return undefined
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      setBusy('idle')
      endGenerationRun(entityKey, runId)
    }
  }, [mockup?.id, pushProgress, pushGenerationEvent, startGenerationRun, endGenerationRun])

  const stop = useCallback(() => {
    abortRef.current?.abort()
    abortRef.current = null
    setBusy('idle')
    const ev: ProgressEvent = { step: 'cancel', status: 'error', message: 'Stopped.' }
    pushProgress(ev)
    if (mockupId) {
      pushGenerationEvent(`mockup-${mockupId}`, ev)
      endGenerationRun(`mockup-${mockupId}`)
    }
    reload().catch(() => {})
  }, [mockupId, pushProgress, pushGenerationEvent, endGenerationRun, reload])

  const refreshPreview = useCallback(async (id: number) => {
    const p = await api.previewScreen(id)
    setPreviewPath(p.path + '?t=' + Date.now())
    setMissing(p.missing)
  }, [])

  // A step is reachable once the thing it operates on exists. Nothing is ever locked
  // behind a step you already passed — going back to re-draw a box is normal.
  const reachable = useMemo(() => [
    true,
    mockup != null,
    mockup != null && regions.length > 0,
    mockup != null && builtCount > 0,
    mockup != null && builtCount > 0,
    mockup != null && builtCount > 0,
  ], [mockup, regions.length, builtCount])

  if (!project) {
    return <div style={{ color: 'var(--muted)' }}>No project selected. Create one on the Projects page.</div>
  }

  const goto = async (i: number) => {
    if (reachable[i]) {
      if (pendingSaveRef.current) {
        try {
          await pendingSaveRef.current()
        } catch (e) {
          // ignore or let step handle error
        }
      }
      setStep(i)
      setError('')
    }
  }

  const shared = {
    project, mockup, mockups, atlases, atlasId, setAtlasId, reload, setMockupId,
    busy, progress, stop, runStream, error, setError,
    selectedRegionId, setSelectedRegionId,
    previewPath, missing, score, refreshPreview,
    runs: mockup ? generationStore[`mockup-${mockup.id}`] || [] : [],
    next: () => goto(step + 1),
    onSavePending: (fn: (() => Promise<void>) | null) => { pendingSaveRef.current = fn },
  }

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginBottom: 4 }}>
        <h1>Screen breakdown</h1>
        <span style={{ fontSize: 12.5, color: 'var(--muted)' }}>{STEPS[step].hint}</span>
      </div>

      {/* Stepper */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 0, margin: '16px 0 20px' }}>
        {STEPS.map((s, i) => {
          const active = i === step
          const done = i < step && reachable[i]
          const open = reachable[i]
          return (
            <div key={s.key} style={{ display: 'flex', alignItems: 'center', flex: i === STEPS.length - 1 ? 0 : 1 }}>
              <div
                onClick={() => goto(i)}
                title={open ? s.hint : 'Finish the earlier steps first'}
                style={{
                  display: 'flex', alignItems: 'center', gap: 7, padding: '6px 12px', borderRadius: 8,
                  cursor: open ? 'pointer' : 'default', opacity: open ? 1 : 0.4,
                  background: active ? 'rgba(108,140,255,0.14)' : 'transparent',
                  border: `1px solid ${active ? 'var(--accent-border)' : 'transparent'}`,
                  whiteSpace: 'nowrap',
                }}
              >
                <span
                  style={{
                    width: 20, height: 20, borderRadius: '50%', fontSize: 10.5, fontWeight: 700,
                    display: 'flex', alignItems: 'center', justifyContent: 'center', flexShrink: 0,
                    background: active ? 'var(--accent)' : done ? 'var(--green)' : '#262c35',
                    color: active || done ? '#0e1116' : 'var(--muted)',
                  }}
                >
                  {done ? '✓' : i + 1}
                </span>
                <span
                  style={{
                    fontSize: 12.5, fontWeight: active ? 700 : 500,
                    color: active ? '#a9bcff' : done ? 'var(--text-2)' : 'var(--muted)',
                  }}
                >
                  {s.label}
                </span>
              </div>
              {i < STEPS.length - 1 && (
                <div style={{ flex: 1, height: 1, background: i < step ? 'var(--green)' : 'var(--border)', margin: '0 6px', minWidth: 12 }} />
              )}
            </div>
          )
        })}
      </div>

      {error && (
        <div
          className="card"
          style={{
            padding: '9px 12px', marginBottom: 14, fontSize: 12,
            color: '#ff9b9b', borderColor: '#5a2a2e', background: 'rgba(229,72,77,0.08)',
          }}
        >
          {error}
        </div>
      )}

      {step === 0 && <StepSource {...shared} />}
      {step === 1 && <StepElements {...shared} proposals={proposals} setProposals={setProposals} />}
      {step === 2 && <StepBuild {...shared} />}
      {step === 3 && <StepText {...shared} />}
      {step === 4 && <StepPolish {...shared} />}
      {step === 5 && <StepResult {...shared} />}
    </>
  )
}
