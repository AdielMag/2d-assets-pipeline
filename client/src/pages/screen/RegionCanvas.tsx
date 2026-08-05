import { useEffect, useRef, useState } from 'react'
import { storageUrl } from '../../api'
import type { Region } from '../../api'

export interface Rect { x: number; y: number; w: number; h: number }

type EditMode = 'move' | 'tl' | 't' | 'tr' | 'r' | 'br' | 'b' | 'bl' | 'l'

const HANDLE_SPECS: { id: EditMode; cursor: string; style: React.CSSProperties }[] = [
  { id: 'tl', cursor: 'nwse-resize', style: { top: -5, left: -5 } },
  { id: 't',  cursor: 'ns-resize',   style: { top: -5, left: 'calc(50% - 5px)' } },
  { id: 'tr', cursor: 'nesw-resize', style: { top: -5, right: -5 } },
  { id: 'r',  cursor: 'ew-resize',   style: { top: 'calc(50% - 5px)', right: -5 } },
  { id: 'br', cursor: 'nwse-resize', style: { bottom: -5, right: -5 } },
  { id: 'b',  cursor: 'ns-resize',   style: { bottom: -5, left: 'calc(50% - 5px)' } },
  { id: 'bl', cursor: 'nesw-resize', style: { bottom: -5, left: -5 } },
  { id: 'l',  cursor: 'ew-resize',   style: { top: 'calc(50% - 5px)', left: -5 } },
]

export interface GhostBox {
  rect: Rect
  color: string
  label: string
}

/** The screen with its element boxes on it: zoom, pan, draw, move, resize.
 *
 *  The one invariant worth protecting here is that a box's percentage coordinates map
 *  onto the *image*, not onto the container. The stage is fitted to the image's aspect
 *  ratio so there is never any letterbox for the percentages to drift into — every step
 *  that draws boxes over this screen depends on that, including the composited preview
 *  and the Unity export. */
export function RegionCanvas({
  imagePath,
  regions,
  selectedId,
  onSelect,
  onRegionChange,
  onRegionCommit,
  onDraw,
  onDelete,
  ghosts = [],
  overlayPath,
  compare = false,
  readOnly = false,
  height = 'min(72vh, 640px)',
}: {
  imagePath: string
  regions: Region[]
  selectedId?: number | null
  onSelect?: (id: number | null) => void
  onRegionChange?: (id: number, rect: Rect) => void
  onRegionCommit?: (id: number, rect: Rect) => void
  onDraw?: (rect: Rect) => void
  onDelete?: (id: number) => void
  /** Read-only boxes drawn on top — used to preview a proposed split. */
  ghosts?: GhostBox[]
  overlayPath?: string
  compare?: boolean
  readOnly?: boolean
  height?: number | string
}) {
  const containerRef = useRef<HTMLDivElement>(null)
  const stageRef = useRef<HTMLDivElement>(null)
  const imgRef = useRef<HTMLImageElement>(null)
  const dragStart = useRef<{ x: number; y: number } | null>(null)
  const panStartRef = useRef<{ x: number; y: number; panX: number; panY: number } | null>(null)
  const editRef = useRef<{ id: number; mode: EditMode; start: { x: number; y: number }; orig: Rect; curr: Rect } | null>(null)
  const sliderDraggingRef = useRef(false)

  const [imgAspect, setImgAspect] = useState<number | null>(null)
  const [imgDimensions, setImgDimensions] = useState<{ w: number; h: number } | null>(null)
  const [box, setBox] = useState({ w: 0, h: 0 })
  const [zoom, setZoom] = useState(1)
  const [pan, setPan] = useState({ x: 0, y: 0 })
  const [panMode, setPanMode] = useState(false)
  const [isSpacePressed, setIsSpacePressed] = useState(false)
  const [isDraggingPan, setIsDraggingPan] = useState(false)
  const [hoveredId, setHoveredId] = useState<number | null>(null)
  const [draft, setDraft] = useState<Rect | null>(null)
  const [sliderPos, setSliderPos] = useState(50)

  const zoomRef = useRef(zoom); zoomRef.current = zoom
  const panRef = useRef(pan); panRef.current = pan
  const selected = regions.find((r) => r.id === selectedId) ?? null

  useEffect(() => {
    setImgAspect(null); setImgDimensions(null); setZoom(1); setPan({ x: 0, y: 0 })
    if (!imagePath) return
    const img = new Image()
    img.onload = () => {
      if (img.naturalHeight > 0) {
        setImgAspect(img.naturalWidth / img.naturalHeight)
        setImgDimensions({ w: img.naturalWidth, h: img.naturalHeight })
      }
    }
    img.src = storageUrl(imagePath)
  }, [imagePath])

  // Wheel zoom about the cursor. Native listener because React's synthetic wheel handler
  // is passive and cannot preventDefault the page scroll.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const prevZoom = zoomRef.current
      const prevPan = panRef.current
      const next = Math.min(5, Math.max(1, Math.round((prevZoom + (e.deltaY < 0 ? 0.2 : -0.2)) * 10) / 10))
      if (next === prevZoom) return
      if (next === 1) { setZoom(1); setPan({ x: 0, y: 0 }); return }
      const rect = el.getBoundingClientRect()
      const ratio = next / prevZoom
      setZoom(next)
      setPan({
        x: (e.clientX - rect.left - rect.width / 2) * (1 - ratio) + prevPan.x * ratio,
        y: (e.clientY - rect.top - rect.height / 2) * (1 - ratio) + prevPan.y * ratio,
      })
    }
    const blockMiddle = (e: MouseEvent) => {
      if (e.button === 1) { e.preventDefault(); e.stopPropagation() }
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    el.addEventListener('mousedown', blockMiddle, { capture: true })
    el.addEventListener('auxclick', blockMiddle, { capture: true })
    return () => {
      el.removeEventListener('wheel', onWheel)
      el.removeEventListener('mousedown', blockMiddle, { capture: true })
      el.removeEventListener('auxclick', blockMiddle, { capture: true })
    }
  }, [imagePath])

  // Fit an image-aspect stage inside the container so %-coords land on the image exactly.
  useEffect(() => {
    const el = containerRef.current
    if (!el) return
    const recompute = () => {
      const cw = el.clientWidth, ch = el.clientHeight
      if (!cw || !ch) return
      let ia = imgAspect
      const img = imgRef.current
      if (!ia && img && img.naturalHeight > 0) {
        ia = img.naturalWidth / img.naturalHeight
        setImgAspect(ia)
        setImgDimensions({ w: img.naturalWidth, h: img.naturalHeight })
      }
      if (!ia) return
      setBox({ w: cw / ch > ia ? ch * ia : cw, h: cw / ch > ia ? ch : cw / ia })
    }
    recompute()
    const ro = new ResizeObserver(recompute)
    ro.observe(el)
    return () => ro.disconnect()
  }, [imgAspect, imagePath])

  useEffect(() => {
    if (readOnly) return
    const onKeyDown = (e: KeyboardEvent) => {
      const typing = e.target instanceof HTMLInputElement || e.target instanceof HTMLTextAreaElement
      if (e.code === 'Space' && !typing) setIsSpacePressed(true)
      if (typing) return
      if (e.code === 'Escape') { onSelect?.(null); return }
      if (!selected) return
      const step = e.shiftKey ? 2.0 : 0.5
      const nudge = (dx: number, dy: number, dw: number, dh: number) => {
        e.preventDefault()
        const x = Math.max(0, Math.min(100 - selected.w, selected.x + dx))
        const y = Math.max(0, Math.min(100 - selected.h, selected.y + dy))
        const w = Math.max(1, Math.min(100 - x, selected.w + dw))
        const h = Math.max(1, Math.min(100 - y, selected.h + dh))
        onRegionChange?.(selected.id, { x, y, w, h })
        onRegionCommit?.(selected.id, { x, y, w, h })
      }
      if (e.code === 'ArrowLeft') nudge(e.shiftKey ? 0 : -step, 0, e.shiftKey ? -step : 0, 0)
      else if (e.code === 'ArrowRight') nudge(e.shiftKey ? 0 : step, 0, e.shiftKey ? step : 0, 0)
      else if (e.code === 'ArrowUp') nudge(0, e.shiftKey ? 0 : -step, 0, e.shiftKey ? -step : 0)
      else if (e.code === 'ArrowDown') nudge(0, e.shiftKey ? 0 : step, 0, e.shiftKey ? step : 0)
      else if (e.code === 'Delete' || e.code === 'Backspace') { e.preventDefault(); onDelete?.(selected.id) }
    }
    const onKeyUp = (e: KeyboardEvent) => { if (e.code === 'Space') setIsSpacePressed(false) }
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
    }
  }, [selected, readOnly, onSelect, onRegionChange, onRegionCommit, onDelete])

  const pctPos = (e: React.PointerEvent) => {
    const rect = stageRef.current!.getBoundingClientRect()
    return {
      x: Math.max(0, Math.min(100, ((e.clientX - rect.left) / rect.width) * 100)),
      y: Math.max(0, Math.min(100, ((e.clientY - rect.top) / rect.height) * 100)),
    }
  }

  const isPanning = panMode || isSpacePressed

  const beginEdit = (e: React.PointerEvent, region: Region, mode: EditMode) => {
    e.stopPropagation()
    onSelect?.(region.id)
    editRef.current = {
      id: region.id, mode, start: pctPos(e),
      orig: { x: region.x, y: region.y, w: region.w, h: region.h },
      curr: { x: region.x, y: region.y, w: region.w, h: region.h },
    }
    stageRef.current!.setPointerCapture(e.pointerId)
  }

  const onStageDown = (e: React.PointerEvent) => {
    if (e.button || isPanning) {
      e.preventDefault()
      panStartRef.current = { x: e.clientX, y: e.clientY, panX: pan.x, panY: pan.y }
      setIsDraggingPan(true)
      stageRef.current!.setPointerCapture(e.pointerId)
      return
    }
    if (readOnly || !onDraw) return
    if (e.target !== e.currentTarget && (e.target as HTMLElement).tagName !== 'IMG') return
    dragStart.current = pctPos(e)
    stageRef.current!.setPointerCapture(e.pointerId)
  }

  const onStageMove = (e: React.PointerEvent) => {
    if (panStartRef.current) {
      e.preventDefault()
      setPan({
        x: panStartRef.current.panX + (e.clientX - panStartRef.current.x),
        y: panStartRef.current.panY + (e.clientY - panStartRef.current.y),
      })
      return
    }
    const ed = editRef.current
    if (ed) {
      const p = pctPos(e)
      const dx = p.x - ed.start.x
      const dy = p.y - ed.start.y
      let { x, y, w, h } = ed.orig
      if (ed.mode === 'move') {
        x = Math.max(0, Math.min(100 - w, ed.orig.x + dx))
        y = Math.max(0, Math.min(100 - h, ed.orig.y + dy))
      } else {
        if (ed.mode.includes('r')) {
          w = Math.max(1, Math.min(100 - ed.orig.x, ed.orig.w + dx))
        } else if (ed.mode.includes('l')) {
          const nx = Math.max(0, Math.min(ed.orig.x + ed.orig.w - 1, ed.orig.x + dx))
          w = ed.orig.w + (ed.orig.x - nx)
          x = nx
        }
        if (ed.mode.includes('b')) {
          h = Math.max(1, Math.min(100 - ed.orig.y, ed.orig.h + dy))
        } else if (ed.mode.includes('t')) {
          const ny = Math.max(0, Math.min(ed.orig.y + ed.orig.h - 1, ed.orig.y + dy))
          h = ed.orig.h + (ed.orig.y - ny)
          y = ny
        }
      }
      const newRect = { x, y, w, h }
      ed.curr = newRect
      onRegionChange?.(ed.id, newRect)
      return
    }
    if (!dragStart.current) return
    const p = pctPos(e)
    const s = dragStart.current
    setDraft({
      x: Math.min(s.x, p.x), y: Math.min(s.y, p.y),
      w: Math.abs(p.x - s.x), h: Math.abs(p.y - s.y),
    })
  }

  const onStageUp = () => {
    if (panStartRef.current) {
      panStartRef.current = null
      setIsDraggingPan(false)
      return
    }
    const ed = editRef.current
    if (ed) {
      editRef.current = null
      onRegionCommit?.(ed.id, ed.curr)
      return
    }
    const rect = draft
    dragStart.current = null
    setDraft(null)
    if (rect && rect.w >= 2 && rect.h >= 2) onDraw?.(rect)
  }

  return (
    <div
      ref={containerRef}
      style={{
        position: 'relative', width: '100%', height, borderRadius: 12, overflow: 'hidden',
        border: '1px solid var(--border)', background: '#181b21',
        display: 'flex', alignItems: 'center', justifyContent: 'center', userSelect: 'none',
      }}
    >
      {/* Zoom / pan cluster */}
      <div
        style={{
          position: 'absolute', top: 12, right: 12, zIndex: 40, display: 'flex', alignItems: 'center', gap: 4,
          background: 'rgba(24,27,33,0.85)', backdropFilter: 'blur(8px)',
          border: '1px solid var(--border-2)', borderRadius: 8, padding: '4px 6px',
          boxShadow: '0 4px 12px rgba(0,0,0,0.3)',
        }}
      >
        <button
          type="button" className="btn btn-secondary" title="Zoom out" disabled={zoom <= 1}
          style={{ padding: '3px 8px', fontSize: 13, height: 24, minWidth: 24 }}
          onClick={() => setZoom((z) => {
            const n = Math.max(1, Math.round((z - 0.25) * 100) / 100)
            if (n === 1) setPan({ x: 0, y: 0 })
            return n
          })}
        >
          −
        </button>
        <div
          onClick={() => { setZoom(1); setPan({ x: 0, y: 0 }) }}
          title="Reset zoom"
          style={{ fontSize: 11, fontWeight: 600, color: 'var(--text-2)', padding: '0 6px', minWidth: 40, textAlign: 'center', cursor: 'pointer' }}
        >
          {Math.round(zoom * 100)}%
        </div>
        <button
          type="button" className="btn btn-secondary" title="Zoom in"
          style={{ padding: '3px 8px', fontSize: 13, height: 24, minWidth: 24 }}
          onClick={() => setZoom((z) => Math.min(5, Math.round((z + 0.25) * 100) / 100))}
        >
          +
        </button>
        <div style={{ width: 1, height: 16, background: 'var(--border-2)', margin: '0 2px' }} />
        <button
          type="button" className="btn btn-secondary" title="Pan (or hold Space)"
          style={{
            padding: '3px 8px', fontSize: 11, height: 24,
            background: panMode ? 'var(--accent)' : undefined, color: panMode ? '#0e1116' : undefined,
          }}
          onClick={() => setPanMode((p) => !p)}
        >
          ✋
        </button>
      </div>

      <div
        ref={stageRef}
        onPointerDown={onStageDown}
        onPointerMove={onStageMove}
        onPointerUp={onStageUp}
        style={{
          position: 'relative', width: box.w || '100%', height: box.h || '100%', touchAction: 'none',
          cursor: isPanning ? (isDraggingPan ? 'grabbing' : 'grab') : (readOnly || !onDraw ? 'default' : 'crosshair'),
          transform: `translate(${pan.x}px, ${pan.y}px) scale(${zoom})`,
          transformOrigin: 'center center',
          transition: isDraggingPan ? 'none' : 'transform 0.05s ease-out',
        }}
      >
        <img
          ref={imgRef}
          src={storageUrl(imagePath)}
          alt=""
          draggable={false}
          onLoad={(e) => {
            if (e.currentTarget.naturalHeight > 0) {
              setImgAspect(e.currentTarget.naturalWidth / e.currentTarget.naturalHeight)
              setImgDimensions({ w: e.currentTarget.naturalWidth, h: e.currentTarget.naturalHeight })
            }
          }}
          style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block', userSelect: 'none' }}
        />

        {regions.map((r) => {
          const sel = r.id === selectedId
          const hov = r.id === hoveredId
          return (
            <div
              key={r.id}
              onClick={(e) => { e.stopPropagation(); onSelect?.(r.id) }}
              onPointerEnter={() => setHoveredId(r.id)}
              onPointerLeave={() => setHoveredId(null)}
              onPointerDown={(e) => { if (!isPanning && !readOnly) beginEdit(e, r, 'move') }}
              style={{
                position: 'absolute', left: `${r.x}%`, top: `${r.y}%`, width: `${r.w}%`, height: `${r.h}%`,
                borderRadius: 4,
                cursor: isPanning ? (isDraggingPan ? 'grabbing' : 'grab') : readOnly ? 'pointer' : 'move',
                border: `2px ${sel || !hov ? 'solid' : 'dashed'} ${sel || hov ? r.color : r.color + '88'}`,
                background: r.color + (sel ? '33' : hov ? '20' : '10'),
                boxShadow: sel ? `0 0 12px ${r.color}55` : hov ? `0 0 6px ${r.color}33` : 'none',
                zIndex: sel ? 10 : hov ? 5 : 1,
              }}
            >
              {!sel && (
                <div
                  title={r.name}
                  style={{
                    position: 'absolute', top: 2, left: 2, fontSize: 9.5, fontWeight: 600,
                    padding: '1px 5px', borderRadius: 3, whiteSpace: 'nowrap',
                    background: hov ? r.color : 'rgba(20,23,29,0.85)',
                    color: hov ? '#0e1116' : r.color,
                    border: `1px solid ${r.color}`, pointerEvents: 'none',
                    maxWidth: 'calc(100% - 4px)', overflow: 'hidden', textOverflow: 'ellipsis', lineHeight: 1.3,
                  }}
                >
                  {r.name}{r.asset_id ? ' ✓' : ''}
                </div>
              )}
              {sel && !readOnly && HANDLE_SPECS.map((h) => (
                <div
                  key={h.id}
                  onPointerDown={(e) => { if (!isPanning) beginEdit(e, r, h.id) }}
                  title={`Drag to resize (${h.id.toUpperCase()})`}
                  style={{
                    position: 'absolute', width: 10, height: 10, borderRadius: 3, background: r.color,
                    border: '1.5px solid #0e1116', boxShadow: '0 0 4px rgba(0,0,0,0.5)',
                    cursor: isPanning ? 'grab' : h.cursor, zIndex: 15, ...h.style,
                  }}
                />
              ))}
            </div>
          )
        })}

        {ghosts.map((g, i) => (
          <div
            key={i}
            style={{
              position: 'absolute', left: `${g.rect.x}%`, top: `${g.rect.y}%`,
              width: `${g.rect.w}%`, height: `${g.rect.h}%`,
              border: `2px dashed ${g.color}`, background: `${g.color}22`, borderRadius: 4,
              pointerEvents: 'none', zIndex: 20,
            }}
          >
            <div
              style={{
                position: 'absolute', top: -19, left: -2, fontSize: 9.5, fontWeight: 700,
                background: g.color, color: '#0e1116', padding: '1px 6px', borderRadius: 3, whiteSpace: 'nowrap',
              }}
            >
              {g.label}
            </div>
          </div>
        ))}

        {draft && (
          <div
            style={{
              position: 'absolute', left: `${draft.x}%`, top: `${draft.y}%`,
              width: `${draft.w}%`, height: `${draft.h}%`,
              border: '2px dashed var(--accent)', background: 'rgba(108,140,255,0.15)',
              borderRadius: 4, pointerEvents: 'none',
            }}
          >
            <div
              style={{
                position: 'absolute', top: -22, left: 0, fontSize: 10, fontWeight: 700,
                background: 'var(--accent)', color: '#fff', padding: '2px 6px', borderRadius: 4, whiteSpace: 'nowrap',
              }}
            >
              {imgDimensions
                ? `${Math.round((draft.w / 100) * imgDimensions.w)} × ${Math.round((draft.h / 100) * imgDimensions.h)} px`
                : `${draft.w.toFixed(1)}% × ${draft.h.toFixed(1)}%`}
            </div>
          </div>
        )}

        {compare && overlayPath && (
          <>
            <div style={{ position: 'absolute', inset: 0, overflow: 'hidden', clipPath: `inset(0 0 0 ${sliderPos}%)`, pointerEvents: 'none' }}>
              <img
                src={storageUrl(overlayPath)}
                alt=""
                draggable={false}
                style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
              />
            </div>
            {(['Source', 'Rebuilt'] as const).map((label, i) => (
              <div
                key={label}
                style={{
                  position: 'absolute', top: 8, [i === 0 ? 'left' : 'right']: 8, fontSize: 9.5, fontWeight: 700,
                  color: '#fff', background: 'rgba(0,0,0,0.55)', padding: '1px 6px', borderRadius: 3,
                  pointerEvents: 'none', zIndex: 25,
                }}
              >
                {label}
              </div>
            ))}
            <div
              onPointerDown={(e) => {
                e.stopPropagation()
                sliderDraggingRef.current = true
                ;(e.currentTarget as HTMLElement).setPointerCapture(e.pointerId)
              }}
              onPointerMove={(e) => {
                if (!sliderDraggingRef.current) return
                e.stopPropagation()
                const rect = stageRef.current!.getBoundingClientRect()
                setSliderPos(Math.max(0, Math.min(100, ((e.clientX - rect.left) / rect.width) * 100)))
              }}
              onPointerUp={(e) => { e.stopPropagation(); sliderDraggingRef.current = false }}
              style={{
                position: 'absolute', top: 0, bottom: 0, left: `calc(${sliderPos}% - 9px)`, width: 18,
                cursor: 'ew-resize', zIndex: 30, display: 'flex', justifyContent: 'center', touchAction: 'none',
              }}
            >
              <div style={{ width: 2, height: '100%', background: '#fff', boxShadow: '0 0 8px rgba(0,0,0,0.6)' }} />
              <div
                style={{
                  position: 'absolute', top: '50%', left: '50%', transform: 'translate(-50%,-50%)',
                  width: 26, height: 26, borderRadius: '50%', background: '#fff',
                  display: 'flex', alignItems: 'center', justifyContent: 'center',
                  boxShadow: '0 2px 8px rgba(0,0,0,0.5)', fontSize: 12, color: '#181b21', fontWeight: 700,
                }}
              >
                ⇔
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  )
}
