import { useEffect, useMemo, useState } from 'react'
import { TYPE_DOT, TYPE_LABEL } from '../../api'
import type { SplitProposal } from '../../api'
import type { GhostBox } from './RegionCanvas'

const KIND_LABEL: Record<SplitProposal['kind'], string> = {
  frame_icon: 'Frame + icon',
  container: 'Frame + pieces',
  repeat: 'Repeated copies',
}

const KIND_COLOR: Record<SplitProposal['kind'], string> = {
  frame_icon: '#2dd4bf',
  container: '#c084fc',
  repeat: '#f5a623',
}

export const proposalKey = (p: SplitProposal) => `${p.region_id}:${p.kind}`

/** Which proposals start ticked: the confident ones. The rest are opt-in. */
export const preAccepted = (proposals: SplitProposal[]) =>
  new Set(proposals.filter((p) => p.confidence >= 0.85).map(proposalKey))

/** Dashed boxes for the sub-assets a proposal would create — hovered one if any, else
 *  everything currently accepted. */
export function proposalGhosts(
  proposals: SplitProposal[] | null, accepted: Set<string>, focused: string | null,
): GhostBox[] {
  if (!proposals) return []
  const shown = proposals.filter((p) => (focused ? proposalKey(p) === focused : accepted.has(proposalKey(p))))
  return shown.flatMap((p) =>
    p.children.map((c) => ({
      rect: { x: c.x, y: c.y, w: c.w, h: c.h },
      color: KIND_COLOR[p.kind],
      label: c.name,
    })),
  )
}

/** Deciding how far to break each element down.
 *
 *  This is what turns a currency pill into a reusable capsule plus a gem, rather than one
 *  flat sprite with the gem painted into it. It proposes and never acts: a wrong box costs
 *  a bad crop, but a wrong split writes rows that the next build turns into assets, so the
 *  decision stays with the person who can see the screen.
 *
 *  It lives inside the Elements step rather than owning a step of its own. Splitting is not
 *  a separate decision from "what are the elements here" — it is the same decision at a
 *  finer grain, and the answer lands as more boxes on the same canvas. Being here also
 *  means the boxes an applied split creates are immediately editable by the tools already
 *  on screen, which used to need a review mode of its own to offer. */
export function SplitPanel({
  proposals, accepted, setAccepted, focused, setFocused, onApply, onRerun, applying, busy, error,
}: {
  proposals: SplitProposal[]
  accepted: Set<string>
  setAccepted: (s: Set<string>) => void
  focused: string | null
  setFocused: (k: string | null) => void
  onApply: () => void
  onRerun: () => void
  applying: boolean
  busy: boolean
  error: string
}) {
  const acceptedCount = useMemo(
    () => proposals.filter((p) => accepted.has(proposalKey(p))).length,
    [proposals, accepted],
  )

  const toggle = (p: SplitProposal) => {
    const key = proposalKey(p)
    const nextSet = new Set(accepted)
    if (nextSet.has(key)) nextSet.delete(key)
    else nextSet.add(key)
    setAccepted(nextSet)
  }

  if (proposals.length === 0) {
    return (
      <div className="card" style={{ padding: 12, marginBottom: 12 }}>
        <div style={{ fontSize: 12, color: 'var(--muted)', lineHeight: 1.5 }}>
          {error
            ? 'Could not check for sub-assets.'
            : 'Every element reads as a single piece — nothing to divide.'}
        </div>
        <button
          className="btn btn-secondary"
          style={{ width: '100%', marginTop: 10, padding: 7, fontSize: 11.5 }}
          disabled={busy}
          onClick={onRerun}
        >
          {error ? 'Try again' : 'Look again'}
        </button>
      </div>
    )
  }

  return (
    <div className="card" style={{ padding: 12, marginBottom: 12 }}>
      <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 3 }}>
        {proposals.length} element{proposals.length === 1 ? '' : 's'} could be split
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 10 }}>
        An element that holds something else is only reusable once the pieces are separated.
        Ticked ones become sub-assets; the frame is then cut empty.
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8, maxHeight: 340, overflowY: 'auto', marginBottom: 10 }}>
        {proposals.map((p) => {
          const key = proposalKey(p)
          const on = accepted.has(key)
          const isFocused = focused === key
          return (
            <div
              key={key}
              className="card"
              onMouseEnter={() => setFocused(key)}
              onMouseLeave={() => setFocused(null)}
              style={{
                padding: 10, cursor: 'pointer',
                borderColor: isFocused ? KIND_COLOR[p.kind] : on ? 'var(--accent-border)' : 'var(--border)',
              }}
              onClick={() => toggle(p)}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <span
                  style={{
                    width: 15, height: 15, borderRadius: 4, flexShrink: 0,
                    border: `1.5px solid ${on ? 'var(--accent)' : 'var(--border-3)'}`,
                    background: on ? 'var(--accent)' : 'transparent',
                    color: '#0e1116', fontSize: 10, fontWeight: 800,
                    display: 'flex', alignItems: 'center', justifyContent: 'center',
                  }}
                >
                  {on ? '✓' : ''}
                </span>
                <span style={{ fontSize: 12, fontWeight: 600, flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {p.region_name}
                </span>
                <span
                  style={{
                    fontSize: 9.5, fontWeight: 700, padding: '2px 6px', borderRadius: 4, flexShrink: 0,
                    color: KIND_COLOR[p.kind], border: `1px solid ${KIND_COLOR[p.kind]}55`,
                    background: `${KIND_COLOR[p.kind]}18`,
                  }}
                >
                  {KIND_LABEL[p.kind]}
                </span>
              </div>
              <div style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 1.5, marginBottom: 7 }}>
                {p.reason}
              </div>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 5 }}>
                {p.replace_parent && (
                  <span style={{ fontSize: 10, color: 'var(--warm)', padding: '2px 0', width: '100%' }}>
                    The original box is replaced by these.
                  </span>
                )}
                {p.rebuilds_parent && !p.replace_parent && (
                  <span style={{ fontSize: 10, color: 'var(--warm)', padding: '2px 0', width: '100%' }}>
                    Already built — {p.region_name} will be cut again, empty this time.
                  </span>
                )}
                {p.children.map((c, i) => (
                  <span
                    key={i}
                    style={{
                      fontSize: 10, padding: '2px 7px', borderRadius: 4,
                      background: 'var(--input)', border: '1px solid var(--border-2)', color: 'var(--text-3)',
                      display: 'inline-flex', alignItems: 'center', gap: 4,
                    }}
                    title={c.prompt || TYPE_LABEL[c.asset_type]}
                  >
                    <span style={{ width: 5, height: 5, borderRadius: '50%', background: TYPE_DOT[c.asset_type] }} />
                    {c.name}
                  </span>
                ))}
              </div>
            </div>
          )
        })}
      </div>

      <button
        className="btn btn-accent"
        style={{ width: '100%', fontWeight: 700, padding: 9 }}
        disabled={applying || acceptedCount === 0}
        onClick={onApply}
      >
        {applying
          ? 'Splitting…'
          : `Split ${acceptedCount} element${acceptedCount === 1 ? '' : 's'}`}
      </button>
      <button
        className="btn btn-secondary"
        style={{ width: '100%', marginTop: 8, padding: 7, fontSize: 11.5 }}
        disabled={busy || applying}
        onClick={onRerun}
      >
        Look again
      </button>
    </div>
  )
}

/** Keep `accepted`/`focused` in step with whichever proposal set is on screen. */
export function useProposalSelection(proposals: SplitProposal[] | null) {
  const [accepted, setAccepted] = useState<Set<string>>(new Set())
  const [focused, setFocused] = useState<string | null>(null)
  useEffect(() => {
    setAccepted(proposals ? preAccepted(proposals) : new Set())
    setFocused(null)
  }, [proposals])
  return { accepted, setAccepted, focused, setFocused }
}
