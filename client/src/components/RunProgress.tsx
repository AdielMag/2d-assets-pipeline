import { useEffect, useMemo, useState } from 'react'
import { storageUrl } from '../api'
import type { ProgressEvent } from '../api'
import type { StoredRun } from '../AppContext'

export interface ProgressItem extends ProgressEvent {
  step: string
  status: 'running' | 'done' | 'error'
  message: string
  image?: string | null
  index?: number | null
  total?: number | null
  timestamp?: number
  data?: unknown
}

interface StepData {
  prompt?: string
  provider?: string
  model?: string
  reference_images?: string[]
  tokens?: { input?: number; output?: number; thinking?: number; total?: number }
  raw_output?: string
  command?: string
  backend?: string
  method?: string
  quota_exceeded?: boolean
  [key: string]: unknown
}

const DOT: Record<ProgressItem['status'], string> = {
  running: 'var(--accent)',
  done: 'var(--green)',
  error: '#ff7b7b',
}

const fmtSecs = (s: number) => (s >= 60 ? `${Math.floor(s / 60)}m ${Math.round(s % 60)}s` : `${s.toFixed(1)}s`)

export const fmtTokens = (n: number) => (n >= 1000 ? `${(n / 1000).toFixed(1)}k` : String(n))

/** Everything worth showing in a step's Details drawer, or null when there's nothing. */
function stepDetails(data: StepData | undefined) {
  if (!data) return null
  const refs = Array.isArray(data.reference_images) ? data.reference_images : []
  const has = data.prompt || data.command || data.raw_output || refs.length > 0 || data.tokens
  return has ? { refs, ...data } : null
}

function CopyButton({ text }: { text: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <button
      type="button"
      onClick={() => {
        navigator.clipboard?.writeText(text).then(() => {
          setCopied(true)
          setTimeout(() => setCopied(false), 1200)
        }).catch(() => {})
      }}
      style={{
        border: '1px solid var(--border-2)', background: 'var(--input)', color: 'var(--muted)',
        borderRadius: 5, padding: '2px 7px', fontSize: 10, cursor: 'pointer', flexShrink: 0,
      }}
    >
      {copied ? 'Copied' : 'Copy'}
    </button>
  )
}

function Block({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ marginBottom: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
        <span style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted-2)', letterSpacing: '0.04em', textTransform: 'uppercase' }}>
          {label}
        </span>
        <CopyButton text={value} />
      </div>
      <pre
        className="mono"
        style={{
          margin: 0, padding: '7px 9px', background: 'var(--input)', border: '1px solid var(--border-2)',
          borderRadius: 6, fontSize: 10.5, lineHeight: 1.5, color: 'var(--text-3)',
          whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: 220, overflowY: 'auto',
        }}
      >
        {value}
      </pre>
    </div>
  )
}

/** Live view of a generation run: what is happening now, and what already happened.
 *
 *  Deliberately a flat list. The steps a run emits are a sequence with occasional
 *  parallelism, and drawing that as a graph cost far more than it explained — the
 *  question being asked of this panel is "is it working, and if not which step broke",
 *  which a list answers at a glance. Everything a graph node held that was actually
 *  useful (the exact prompt, the command, the raw model output, the references) is one
 *  disclosure away on the step it belongs to. */
export function RunProgress({
  items = [],
  active = false,
  onStop,
  runs = [],
}: {
  items?: ProgressItem[]
  active?: boolean
  onStop?: () => void
  runs?: StoredRun[]
}) {
  const [selectedRunId, setSelectedRunId] = useState<string>('')
  const [openStep, setOpenStep] = useState<number | null>(null)
  const [lightbox, setLightbox] = useState<string | null>(null)

  // The live run wins while it has events; otherwise show whichever stored run is picked.
  const displayRun = useMemo(() => {
    if (selectedRunId) {
      const found = runs.find((r) => r.id === selectedRunId)
      if (found) return found
    }
    return runs.length > 0 && items.length === 0 ? runs[0] : null
  }, [selectedRunId, runs, items])

  const steps: ProgressItem[] = displayRun ? (displayRun.events as ProgressItem[]) : items
  const isActive = displayRun ? displayRun.active : active

  // Re-render once a second while a run is active so the elapsed clock (and a currently
  // multi-minute step with no events of its own, e.g. a CPU-bound super-resolution pass)
  // keeps visibly ticking instead of looking frozen between events.
  const [, setTick] = useState(0)
  useEffect(() => {
    if (!isActive) return
    const id = setInterval(() => setTick((t) => t + 1), 1000)
    return () => clearInterval(id)
  }, [isActive])

  const quotaExceeded = useMemo(
    () => steps.some((s) => (s.data as StepData | undefined)?.quota_exceeded),
    [steps],
  )
  const failed = useMemo(() => steps.filter((s) => s.status === 'error').length, [steps])

  const totalTokens = useMemo(
    () => steps.reduce((sum, s) => sum + ((s.data as StepData | undefined)?.tokens?.total || 0), 0),
    [steps],
  )

  const elapsed = (() => {
    if (steps.length === 0) return 0
    const first = steps[0].timestamp || displayRun?.timestamp || Date.now()
    const last = isActive ? Date.now() : (steps[steps.length - 1].timestamp || Date.now())
    return Math.max(0, (last - first) / 1000)
  })()

  const current = steps[steps.length - 1]

  // Work units (region builds, template families) are numbered against a total that's
  // fixed before the run starts (see `_build_atlas`'s upfront `total` in mockups.py), so
  // the bar can show a real 0-100 rather than a guess that grows as more work is found.
  // Tracked as "how many distinct step numbers have reached done/error" rather than just
  // the latest event's index/total, because up to 3 groups build concurrently and can
  // finish out of numeric order — this keeps the bar monotonic either way.
  const totalSteps = useMemo(() => {
    let t = 0
    for (const s of steps) if (s.total) t = Math.max(t, s.total)
    return t
  }, [steps])
  const completedSteps = useMemo(() => {
    const done = new Set<number>()
    for (const s of steps) {
      if ((s.status === 'done' || s.status === 'error') && s.index != null && s.index >= 1) done.add(s.index)
    }
    return done.size
  }, [steps])
  const percent = totalSteps > 0 ? Math.round((completedSteps / totalSteps) * 100) : null

  if (!isActive && steps.length === 0 && runs.length === 0) return null

  return (
    <div className="card" style={{ padding: 0, overflow: 'hidden', marginBottom: 10 }}>
      {/* Summary bar */}
      <div
        style={{
          display: 'flex', alignItems: 'center', gap: 8, padding: '9px 12px',
          borderBottom: steps.length > 0 ? '1px solid var(--border)' : 'none', flexWrap: 'wrap',
        }}
      >
        <span
          style={{
            width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
            background: isActive ? 'var(--accent)' : failed ? '#ff7b7b' : 'var(--green)',
            boxShadow: isActive ? '0 0 8px var(--accent)' : 'none',
          }}
        />
        <span style={{ fontSize: 12, fontWeight: 600 }}>
          {isActive ? (current?.message || 'Starting…') : failed ? `Finished with ${failed} error${failed === 1 ? '' : 's'}` : 'Finished'}
        </span>
        {percent != null && (
          <span style={{ fontSize: 11, color: 'var(--muted)' }}>{completedSteps}/{totalSteps} · {percent}%</span>
        )}
        <div style={{ flex: 1 }} />
        {quotaExceeded && (
          <span
            style={{
              fontSize: 10, fontWeight: 700, color: 'var(--warm)', border: '1px solid #5d4415',
              background: 'rgba(245,166,35,0.1)', borderRadius: 5, padding: '2px 7px',
            }}
            title="The image provider's quota or rate limit was reached — the run stopped early."
          >
            ⚠ Quota reached
          </span>
        )}
        {elapsed > 0 && <span style={{ fontSize: 11, color: 'var(--muted)' }}>⏱ {fmtSecs(elapsed)}</span>}
        {totalTokens > 0 && (
          <span style={{ fontSize: 11, color: 'var(--muted)' }} title="Estimated tokens across all steps">
            🪙 {fmtTokens(totalTokens)}
          </span>
        )}
        {runs.length > 0 && (
          <select
            value={displayRun?.id ?? ''}
            onChange={(e) => setSelectedRunId(e.target.value)}
            title="Show an earlier run"
            style={{
              fontSize: 10.5, background: 'var(--input)', color: 'var(--muted)',
              border: '1px solid var(--border-2)', borderRadius: 5, padding: '3px 5px', maxWidth: 150,
            }}
          >
            {items.length > 0 && <option value="">Current run</option>}
            {runs.map((r) => (
              <option key={r.id} value={r.id}>
                {r.active ? 'Running…' : new Date(r.timestamp).toLocaleTimeString()}
              </option>
            ))}
          </select>
        )}
        {isActive && onStop && (
          <button
            type="button"
            onClick={onStop}
            style={{
              border: '1px solid #6e2a2e', background: '#3e1b1e', color: '#ff8585',
              borderRadius: 5, padding: '3px 9px', fontSize: 10.5, fontWeight: 600, cursor: 'pointer',
            }}
          >
            ⏹ Stop
          </button>
        )}
      </div>

      {percent != null && (
        <div style={{ height: 4, background: 'var(--input)', position: 'relative' }}>
          <div
            role="progressbar"
            aria-valuenow={percent}
            aria-valuemin={0}
            aria-valuemax={100}
            style={{
              position: 'absolute', inset: 0, width: `${percent}%`,
              background: failed ? '#ff7b7b' : isActive ? 'var(--accent)' : 'var(--green)',
              transition: 'width 0.25s ease',
            }}
          />
        </div>
      )}

      {/* Steps */}
      {steps.length > 0 && (
        <div style={{ maxHeight: 380, overflowY: 'auto' }}>
          {steps.map((step, idx) => {
            const data = step.data as StepData | undefined
            const details = stepDetails(data)
            const isOpen = openStep === idx
            const next = steps[idx + 1]
            const dur = next?.timestamp && step.timestamp
              ? (next.timestamp - step.timestamp) / 1000
              : null
            return (
              <div key={idx} style={{ borderTop: idx === 0 ? 'none' : '1px solid #20242b' }}>
                <div
                  onClick={() => details && setOpenStep(isOpen ? null : idx)}
                  style={{
                    display: 'flex', alignItems: 'center', gap: 8, padding: '7px 12px',
                    cursor: details ? 'pointer' : 'default',
                    background: isOpen ? '#20242c' : 'transparent',
                  }}
                >
                  <span
                    style={{
                      width: 6, height: 6, borderRadius: '50%', background: DOT[step.status], flexShrink: 0,
                    }}
                  />
                  {step.image && (
                    <img
                      src={storageUrl(step.image)}
                      alt=""
                      className="checkerboard checkerboard-sm"
                      onClick={(e) => { e.stopPropagation(); setLightbox(storageUrl(step.image!)) }}
                      style={{
                        width: 26, height: 26, borderRadius: 4, objectFit: 'contain',
                        border: '1px solid var(--border-2)', flexShrink: 0, cursor: 'zoom-in',
                      }}
                    />
                  )}
                  <span
                    style={{
                      fontSize: 11.5, flex: 1, minWidth: 0, overflow: 'hidden',
                      textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                      color: step.status === 'error' ? '#ff9b9b' : 'var(--text-2)',
                    }}
                    title={step.message}
                  >
                    {step.message}
                  </span>
                  {step.index != null && step.total ? (
                    <span style={{ fontSize: 10, color: 'var(--muted-2)', flexShrink: 0 }}>
                      {step.index}/{step.total}
                    </span>
                  ) : null}
                  {dur != null && dur >= 0.5 && (
                    <span style={{ fontSize: 10, color: 'var(--muted-2)', flexShrink: 0 }}>{fmtSecs(dur)}</span>
                  )}
                  {details && (
                    <span style={{ fontSize: 9, color: 'var(--muted-2)', flexShrink: 0 }}>{isOpen ? '▲' : '▼'}</span>
                  )}
                </div>

                {isOpen && details && (
                  <div style={{ padding: '4px 12px 12px', background: '#181b21' }}>
                    <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap', marginBottom: 8, fontSize: 10.5, color: 'var(--muted)' }}>
                      {details.provider && <span>Provider: <strong style={{ color: 'var(--text-3)' }}>{details.provider}</strong></span>}
                      {details.model && <span>Model: <strong style={{ color: 'var(--text-3)' }}>{details.model}</strong></span>}
                      {details.backend && <span>Backend: <strong style={{ color: 'var(--text-3)' }}>{details.backend}</strong></span>}
                      {details.method && <span>Method: <strong style={{ color: 'var(--text-3)' }}>{details.method}</strong></span>}
                      {details.tokens?.total ? <span>Tokens: <strong style={{ color: 'var(--text-3)' }}>{fmtTokens(details.tokens.total)}</strong></span> : null}
                    </div>
                    {details.refs.length > 0 && (
                      <div style={{ marginBottom: 8 }}>
                        <div style={{ fontSize: 10, fontWeight: 700, color: 'var(--muted-2)', letterSpacing: '0.04em', textTransform: 'uppercase', marginBottom: 4 }}>
                          References sent
                        </div>
                        <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap' }}>
                          {details.refs.map((r, i) => (
                            <img
                              key={i}
                              src={storageUrl(r)}
                              alt=""
                              className="checkerboard checkerboard-sm"
                              onClick={() => setLightbox(storageUrl(r))}
                              style={{
                                width: 40, height: 40, borderRadius: 5, objectFit: 'contain',
                                border: '1px solid var(--border-2)', cursor: 'zoom-in',
                              }}
                            />
                          ))}
                        </div>
                      </div>
                    )}
                    {details.prompt && <Block label="Prompt" value={details.prompt} />}
                    {details.command && <Block label="Command" value={details.command} />}
                    {details.raw_output && <Block label="Raw model output" value={details.raw_output} />}
                  </div>
                )}
              </div>
            )
          })}
        </div>
      )}

      {lightbox && (
        <div className="modal-backdrop" onClick={() => setLightbox(null)} style={{ cursor: 'zoom-out' }}>
          <img
            src={lightbox}
            alt=""
            className="checkerboard"
            style={{ maxWidth: '82vw', maxHeight: '82vh', borderRadius: 10, border: '1px solid var(--border-3)' }}
          />
        </div>
      )}
    </div>
  )
}
