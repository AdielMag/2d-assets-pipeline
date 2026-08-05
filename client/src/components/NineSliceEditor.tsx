import { useEffect, useRef, useState } from 'react'

export interface NineSlice { l: number; t: number; r: number; b: number }

/** Interactive 9-slice border editor: four draggable guide lines over the sprite,
 * pixel readouts, and a live stretch preview at three sizes. Guides are stored as
 * border pixel values of the source image; drag positions are percentages. */
export default function NineSliceEditor({ imageUrl, value, onChange, onAutoDetect, onDisable }: {
  imageUrl: string
  value: NineSlice | null
  onChange: (v: NineSlice) => void
  onAutoDetect?: () => void
  onDisable?: () => void
}) {
  const frameRef = useRef<HTMLDivElement>(null)
  const [imgSize, setImgSize] = useState<{ w: number; h: number } | null>(null)
  const [drag, setDrag] = useState<'l' | 't' | 'r' | 'b' | null>(null)

  useEffect(() => {
    const img = new Image()
    img.onload = () => setImgSize({ w: img.naturalWidth, h: img.naturalHeight })
    img.src = imageUrl
  }, [imageUrl])

  const w = imgSize?.w ?? 100
  const h = imgSize?.h ?? 100
  const ns = value ?? { l: Math.round(w / 4), t: Math.round(h / 4), r: Math.round(w / 4), b: Math.round(h / 4) }

  // guide positions in % of the image box
  const pos = {
    l: (ns.l / w) * 100,
    r: 100 - (ns.r / w) * 100,
    t: (ns.t / h) * 100,
    b: 100 - (ns.b / h) * 100,
  }

  const move = (e: React.PointerEvent) => {
    if (!drag || !frameRef.current || !imgSize) return
    const rect = frameRef.current.getBoundingClientRect()
    let pct = drag === 'l' || drag === 'r'
      ? ((e.clientX - rect.left) / rect.width) * 100
      : ((e.clientY - rect.top) / rect.height) * 100
    pct = Math.max(2, Math.min(98, pct))
    const next = { ...ns }
    if (drag === 'l') next.l = Math.round(Math.min((pct / 100) * w, w - ns.r - w * 0.08))
    if (drag === 'r') next.r = Math.round(Math.min(((100 - pct) / 100) * w, w - ns.l - w * 0.08))
    if (drag === 't') next.t = Math.round(Math.min((pct / 100) * h, h - ns.b - h * 0.08))
    if (drag === 'b') next.b = Math.round(Math.min(((100 - pct) / 100) * h, h - ns.t - h * 0.08))
    next.l = Math.max(1, next.l); next.r = Math.max(1, next.r)
    next.t = Math.max(1, next.t); next.b = Math.max(1, next.b)
    onChange(next)
  }

  const guideCommon: React.CSSProperties = { position: 'absolute', background: 'var(--accent)' }
  const knob = (
    <div style={{
      position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
      width: 12, height: 12, borderRadius: '50%', background: 'var(--accent)', border: '2px solid #14161a',
    }} />
  )

  const borderImageStyle = (bw: number, bh: number): React.CSSProperties => {
    let [dl, dr] = [ns.l, ns.r]
    if (dl + dr > bw) {
      const scale = bw / Math.max(1, dl + dr)
      dl = Math.round(dl * scale)
      dr = Math.round(dr * scale)
    }
    let [dt, db] = [ns.t, ns.b]
    if (dt + db > bh) {
      const scale = bh / Math.max(1, dt + db)
      dt = Math.round(dt * scale)
      db = Math.round(db * scale)
    }
    return {
      width: bw, height: bh, borderRadius: 6, overflow: 'hidden',
      borderImageSource: `url("${imageUrl}")`,
      borderImageSlice: `${ns.t} ${ns.r} ${ns.b} ${ns.l} fill`,
      borderImageWidth: `${dt}px ${dr}px ${db}px ${dl}px`,
      borderImageRepeat: 'stretch',
      borderStyle: 'solid',
    }
  }

  const aspectStyle = imgSize ? `${imgSize.w} / ${imgSize.h}` : '4 / 3'

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600 }}>
          9-Slice Borders {imgSize && <span style={{ fontSize: 11, color: 'var(--muted)', fontWeight: 400 }}>({imgSize.w}×{imgSize.h}px)</span>}
        </div>
        <div style={{ display: 'flex', gap: 6 }}>
          {onAutoDetect && (
            <div
              onClick={onAutoDetect}
              style={{
                fontSize: 10.5, fontWeight: 600, color: 'var(--accent)',
                border: '1px solid #2a3a5c', background: 'rgba(108,140,255,0.08)',
                borderRadius: 5, padding: '4px 8px', cursor: 'pointer',
              }}
            >
              Auto-detect
            </div>
          )}
          {onDisable && (
            <div
              onClick={onDisable}
              title="Remove 9-slice borders and export as standard sprite"
              style={{
                fontSize: 10.5, fontWeight: 600, color: 'var(--muted)',
                border: '1px solid var(--border-2)', borderRadius: 5, padding: '4px 8px', cursor: 'pointer',
              }}
            >
              Disable 9-Slice
            </div>
          )}
        </div>
      </div>

      <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 12 }}>
        <div
          ref={frameRef}
          className="checkerboard"
          onPointerMove={move}
          onPointerUp={() => setDrag(null)}
          style={{
            position: 'relative', width: '100%', aspectRatio: aspectStyle, maxHeight: 380, borderRadius: 8,
            overflow: 'hidden', touchAction: 'none',
          }}
        >
          <img src={imageUrl} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }} />
          <div
            style={{ ...guideCommon, top: 0, bottom: 0, width: 2, cursor: 'ew-resize', left: `${pos.l}%` }}
            onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); setDrag('l') }}
          >{knob}</div>
          <div
            style={{ ...guideCommon, top: 0, bottom: 0, width: 2, cursor: 'ew-resize', left: `${pos.r}%` }}
            onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); setDrag('r') }}
          >{knob}</div>
          <div
            style={{ ...guideCommon, left: 0, right: 0, height: 2, cursor: 'ns-resize', top: `${pos.t}%` }}
            onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); setDrag('t') }}
          >{knob}</div>
          <div
            style={{ ...guideCommon, left: 0, right: 0, height: 2, cursor: 'ns-resize', top: `${pos.b}%` }}
            onPointerDown={(e) => { e.currentTarget.setPointerCapture(e.pointerId); setDrag('b') }}
          >{knob}</div>
        </div>
      </div>

      <div style={{ display: 'flex', gap: 6, marginBottom: 16 }}>
        {([['L', ns.l], ['T', ns.t], ['R', ns.r], ['B', ns.b]] as const).map(([k, v]) => (
          <div
            key={k}
            className="mono"
            style={{
              flex: 1, textAlign: 'center', background: 'var(--input)',
              border: '1px solid var(--border-2)', borderRadius: 6, padding: '5px 0',
              fontSize: 10.5, color: 'var(--text-3)',
            }}
          >
            {k} {v}px
          </div>
        ))}
      </div>

      <div style={{ fontSize: 11, color: 'var(--muted)', marginBottom: 10 }}>
        Live stretch preview · corners fixed, edges &amp; center stretch
      </div>
      <div style={{ display: 'flex', alignItems: 'flex-end', gap: 14, flexWrap: 'wrap' }}>
        <div style={borderImageStyle(64, 32)} />
        <div style={borderImageStyle(110, 46)} />
        <div style={borderImageStyle(190, 70)} />
      </div>
    </div>
  )
}
