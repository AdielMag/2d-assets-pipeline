import { useMemo, useState } from 'react'
import { storageUrl, TYPE_DOT, TYPE_LABEL } from '../../api'
import type { ReuseCandidate } from '../../api'

/** The approval step in front of Build's reuse.
 *
 *  Build used to bind a region to any asset whose name matched and quietly move on, which
 *  is only ever right when the names happen to mean the same thing — "play" matching
 *  "PlayButton" is the same lookup as "play" matching "PlayerAvatar". So the match is now a
 *  proposal: the region's own pixels sit next to the asset being offered for it, and
 *  nothing is bound until someone says so here.
 *
 *  Exact name+type matches start ticked, fuzzy ones do not — a fuzzy hit is a guess about
 *  what a name means, and the cost of accepting a wrong one (a region silently wearing
 *  another element's art) is worse than the cost of rejecting a right one (it gets cut out
 *  of the screenshot, free). */
export function ReuseApprovalModal({
  candidates, busy, onCancel, onConfirm,
}: {
  candidates: ReuseCandidate[]
  busy?: boolean
  onCancel: () => void
  onConfirm: (approvals: Record<string, number>) => void
}) {
  const [choice, setChoice] = useState<Record<number, number | null>>(() =>
    Object.fromEntries(candidates.map((c) => [c.region_id, c.match === 'exact' ? c.asset_id : null])),
  )

  const approvals = useMemo(() => {
    const out: Record<string, number> = {}
    for (const c of candidates) {
      const assetId = choice[c.region_id]
      if (assetId != null) out[String(c.region_id)] = assetId
    }
    return out
  }, [candidates, choice])

  const reusing = Object.keys(approvals).length
  const building = candidates.length - reusing
  const setAll = (fn: (c: ReuseCandidate) => number | null) =>
    setChoice(Object.fromEntries(candidates.map((c) => [c.region_id, fn(c)])))

  const thumb = (path: string | null, size = 46) => (
    <div
      className="checkerboard checkerboard-sm"
      style={{ width: size, height: size, borderRadius: 5, flexShrink: 0, overflow: 'hidden' }}
    >
      {path ? (
        <img src={storageUrl(path)} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain' }} />
      ) : (
        <div style={{
          width: '100%', height: '100%', display: 'flex', alignItems: 'center',
          justifyContent: 'center', color: 'var(--muted-2)', fontSize: 10,
        }}>?</div>
      )}
    </div>
  )

  return (
    <div
      onClick={onCancel}
      style={{
        position: 'fixed', inset: 0, zIndex: 70, background: 'rgba(0,0,0,0.62)',
        display: 'flex', alignItems: 'center', justifyContent: 'center', padding: 24,
      }}
    >
      <div
        onClick={(e) => e.stopPropagation()}
        className="card"
        style={{
          width: 'min(760px, 100%)', maxHeight: '86vh', display: 'flex', flexDirection: 'column',
          padding: 0, overflow: 'hidden',
        }}
      >
        <div style={{ padding: '14px 18px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
          <div style={{ fontSize: 14.5, fontWeight: 650 }}>
            {candidates.length} element{candidates.length === 1 ? '' : 's'} already look{candidates.length === 1 ? 's' : ''} familiar
          </div>
          <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.6, marginTop: 4 }}>
            These regions match something already in this domain by name. Nothing is assigned
            until you say so — anything you leave on <b>Build new</b> is cut out of the
            screenshot as usual.
          </div>
          <div style={{ display: 'flex', gap: 6, marginTop: 10 }}>
            <button
              className="btn btn-secondary" style={{ padding: '4px 10px', fontSize: 11 }}
              onClick={() => setAll((c) => c.asset_id)}
            >
              Use all existing
            </button>
            <button
              className="btn btn-secondary" style={{ padding: '4px 10px', fontSize: 11 }}
              onClick={() => setAll(() => null)}
            >
              Build all new
            </button>
          </div>
        </div>

        <div style={{ overflowY: 'auto', padding: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {candidates.map((c) => {
            const picked = choice[c.region_id] ?? null
            const shownAsset = c.options.find((o) => o.id === (picked ?? c.asset_id))
            return (
              <div
                key={c.region_id}
                style={{
                  border: `1px solid ${picked != null ? 'var(--accent)' : 'var(--border)'}`,
                  background: picked != null ? 'rgba(108,140,255,0.08)' : 'rgba(255,255,255,0.02)',
                  borderRadius: 8, padding: 10, display: 'flex', flexDirection: 'column', gap: 8,
                }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                  {thumb(c.region_crop)}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12.5, fontWeight: 550, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {c.region_name}
                    </div>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 5, marginTop: 3 }}>
                      <span style={{ width: 5, height: 5, borderRadius: '50%', background: TYPE_DOT[c.asset_type] }} />
                      <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>{TYPE_LABEL[c.asset_type]}</span>
                    </div>
                  </div>
                  <span style={{ fontSize: 15, color: 'var(--muted-2)', flexShrink: 0 }}>→</span>
                  {thumb(shownAsset?.path ?? c.asset_path)}
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontSize: 12.5, fontWeight: 550, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                      {shownAsset?.name ?? c.asset_name}
                    </div>
                    <div
                      style={{ fontSize: 10, marginTop: 3, color: c.match === 'fuzzy' ? 'var(--warm)' : 'var(--muted-2)' }}
                      title={c.match === 'fuzzy'
                        ? 'Matched only because one name contains the other — check this one'
                        : 'Same name and type'}
                    >
                      {c.match === 'fuzzy' ? '≈ loose name match' : '= exact name match'}
                    </div>
                  </div>
                </div>

                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <button
                    className={`pill ${picked != null ? 'active' : ''}`}
                    style={{ padding: '3px 10px', fontSize: 10.5 }}
                    onClick={() => setChoice((s) => ({ ...s, [c.region_id]: c.asset_id }))}
                  >
                    Use existing
                  </button>
                  <button
                    className={`pill ${picked == null ? 'active' : ''}`}
                    style={{ padding: '3px 10px', fontSize: 10.5 }}
                    onClick={() => setChoice((s) => ({ ...s, [c.region_id]: null }))}
                  >
                    Build new
                  </button>
                  {picked != null && c.options.length > 1 && (
                    <select
                      className="input"
                      style={{ flex: 1, padding: '3px 7px', fontSize: 11, height: 26, background: '#14171b' }}
                      value={picked}
                      onChange={(e) => setChoice((s) => ({ ...s, [c.region_id]: Number(e.target.value) }))}
                      title="Use a different asset for this region"
                    >
                      {c.options.map((o) => (
                        <option key={o.id} value={o.id}>{o.name}</option>
                      ))}
                    </select>
                  )}
                </div>
              </div>
            )
          })}
        </div>

        <div style={{
          padding: '12px 18px', borderTop: '1px solid var(--border)', flexShrink: 0,
          display: 'flex', alignItems: 'center', gap: 10,
        }}>
          <div style={{ flex: 1, fontSize: 11.5, color: 'var(--muted)' }}>
            {reusing} reused · {building} built fresh
          </div>
          <button className="btn btn-secondary" style={{ padding: '7px 14px' }} onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button
            className="btn btn-accent"
            style={{ padding: '7px 16px', fontWeight: 650 }}
            onClick={() => onConfirm(approvals)}
            disabled={busy}
          >
            Build
          </button>
        </div>
      </div>
    </div>
  )
}

export default ReuseApprovalModal
