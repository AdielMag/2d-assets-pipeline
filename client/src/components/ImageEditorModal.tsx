import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { api, storageUrl } from '../api'
import type { Asset } from '../api'

const UNDO_LIMIT = 20
const MIN_ZOOM = 0.02
const MAX_ZOOM = 32
const ZOOM_LEVELS = [5, 10, 25, 50, 100, 200, 400, 800, 1600, 3200]
// Past this the browser's smooth upscaling hides which native pixel you're actually on, so
// the canvas switches to nearest-neighbour — the point of zooming this far in.
const PIXELATE_ABOVE = 1.5
const CLONE_DAB_SPACING_RATIO = 0.25 // fraction of brush diameter between dabs
const SOFT_DAB_SPACING_RATIO = 0.1 // tighter for feathered dabs, else the falloff bands
const CLONE_MAX_DABS_PER_SEGMENT = 400 // safety clamp for huge fast-drag jumps
const PIXEL_MAX_STEPS_PER_SEGMENT = 4000 // same clamp for the half-pixel-step 1px brush
const FALLOFF_STOPS = 6 // gradient stops across the feathered edge, to keep it band-free
// How close (in on-screen pixels, so it feels the same at any zoom) a moved edge or centre
// has to come to a guide before it sticks to it.
const SNAP_THRESHOLD_PX = 7

type ToolId = 'erase' | 'clone' | 'select' | 'move'
type Rect = { x: number; y: number; w: number; h: number }

const TOOLS: { id: ToolId; label: string; hint: string }[] = [
  { id: 'erase', label: 'Erase', hint: 'Paint to erase to transparent — Edge sets a hard or feathered brush' },
  { id: 'clone', label: 'Clone Stamp', hint: 'Alt+Click to set a source, then drag to clone' },
  { id: 'select', label: 'Select', hint: 'Drag a rectangle — Erase and Clone stay inside it, Move carries it' },
  { id: 'move', label: 'Move', hint: 'Drag to move the selection (or the whole image) · snaps to centre/thirds/edges, hold Alt to bypass · arrow keys nudge, Shift = 10px' },
]

/** Full-screen image editor: a round brush that erases pixels to transparency
 *  (destination-out compositing), a Photoshop-style Clone Stamp (Alt+Click a source,
 *  drag to copy from a fixed offset — Aligned mode), a rectangular Select marquee that
 *  confines the paint tools, a Move tool that drags the selected pixels (or the whole
 *  image) as a floating layer, flip horizontal/vertical, guide overlays and zoom.
 *  No layers, filters, or AI edit — deliberately narrow scope, see
 *  feedback-mini-editor-rejected memory. Saves the result through the existing
 *  upload-version endpoint, same as a manual upload. */
export default function ImageEditorModal({ asset, onClose, onSaved }: {
  asset: Asset
  onClose: () => void
  onSaved: (asset: Asset) => void
}) {
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const previewCanvasRef = useRef<HTMLCanvasElement>(null)
  const scrollRef = useRef<HTMLDivElement>(null)
  const lastPoint = useRef<{ x: number; y: number } | null>(null)
  const lastHoverCanvas = useRef<{ x: number; y: number } | null>(null)
  const undoStack = useRef<ImageData[]>([])
  const redoStack = useRef<ImageData[]>([])
  const zoomRef = useRef(1)
  // Pending "keep this point of the image under this screen position" request, consumed by
  // the layout effect that runs once the new zoom has been committed to the DOM.
  const zoomAnchorRef = useRef<{ clientX: number; clientY: number; fx: number; fy: number } | null>(null)
  // Feathered-erase scratch buffers, alive only for the duration of one stroke: the canvas
  // as it was when the stroke started, plus the stroke's accumulated coverage mask. Each
  // frame re-composites base − mask instead of erasing straight onto the canvas, so
  // overlapping soft dabs can't eat through the same pixel twice (which is what turns a
  // feathered stroke into a hard-edged one). Hard strokes skip all this.
  const strokeBaseRef = useRef<HTMLCanvasElement | null>(null)
  const strokeMaskRef = useRef<HTMLCanvasElement | null>(null)
  // Reused scratch tile for one feathered clone dab.
  const cloneDabRef = useRef<HTMLCanvasElement | null>(null)

  const [brushSize, setBrushSize] = useState(28)
  // Brush edge feather, 0 = hard (a crisp circle) … 100 = fully soft (fades from the very
  // centre). Drives both Erase and Clone, since they share one brush.
  const [softness, setSoftness] = useState(0)
  const [drawing, setDrawing] = useState(false)
  const [dirty, setDirty] = useState(false)
  const [canUndo, setCanUndo] = useState(false)
  const [canRedo, setCanRedo] = useState(false)
  // True while a middle-button drag is panning the view, so the cursor can say so.
  const [panning, setPanning] = useState(false)
  const [nativeSize, setNativeSize] = useState({ w: 0, h: 0 })
  const [zoom, setZoom] = useState(1)
  const [saving, setSaving] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [saveError, setSaveError] = useState('')
  // Cursor position in on-screen (CSS) pixels relative to the canvas, for the brush-size
  // preview ring — kept separate from the canvas-space coordinates used for erasing since
  // the canvas is displayed at a zoom-dependent scale, not its native pixel resolution.
  const [hoverPos, setHoverPos] = useState<{ x: number; y: number } | null>(null)
  const [tool, setTool] = useState<ToolId>('erase')
  // Clone Stamp source anchor, canvas-space (native pixel) coords, set by Alt+Click.
  const [cloneSource, setCloneSource] = useState<{ x: number; y: number } | null>(null)
  // Aligned-mode offset (firstStrokeDest - cloneSource), locked once on the first clone
  // stroke after a source pick and reused by every stroke after that — a ref since it
  // never needs to trigger its own render; it only mutates synchronously inside
  // onPointerDown, always paired with a setState call in the same call, so any render
  // that reads it is already the render triggered by that update.
  const cloneOffsetRef = useRef<{ dx: number; dy: number } | null>(null)
  // Whether Alt is currently held down — while true and a paint tool is active, the
  // canvas is in "pick a source point" mode (a click sets the source instead of
  // painting), shown via a distinct crosshair so it's clear before you click.
  const [altHeld, setAltHeld] = useState(false)

  // Committed selection rectangle in canvas-space (native pixel) coords, or null for
  // "whole image". Confines Erase/Clone, and defines what Move and Flip act on.
  const [selection, setSelection] = useState<Rect | null>(null)
  // In-progress marquee drag (raw corners, not yet normalised), canvas-space.
  const [marquee, setMarquee] = useState<{ x0: number; y0: number; x1: number; y1: number } | null>(null)
  // Composition guides (centre cross + rule of thirds) drawn as a DOM overlay, never
  // onto the canvas itself — they must never end up in the saved PNG.
  const [showGuides, setShowGuides] = useState(false)
  // Whether a drag with the Move tool sticks to those same guides.
  const [snapEnabled, setSnapEnabled] = useState(true)
  // Which guides the current drag is stuck to, canvas-space, for the snap indicator lines.
  // Null on both axes whenever nothing is snapped.
  const [snapLines, setSnapLines] = useState<{ x: number | null; y: number | null }>({ x: null, y: null })

  // Move tool state. The moved pixels are lifted off the canvas into an offscreen buffer
  // ("floating layer"); `floatBaseRef` holds the canvas with that region already cleared,
  // so each drag frame is a cheap putImageData + drawImage instead of a re-composite.
  // Refs (not state) because they mutate on every pointermove; `hasFloating` mirrors
  // their presence for the parts of the render that need to know.
  const floatingRef = useRef<{ canvas: HTMLCanvasElement; x: number; y: number } | null>(null)
  const floatBaseRef = useRef<ImageData | null>(null)
  const moveStartRef = useRef<{ px: number; py: number; ox: number; oy: number } | null>(null)
  const [hasFloating, setHasFloating] = useState(false)

  zoomRef.current = zoom

  const selected = asset.versions.find((v) => v.id === asset.selected_version_id) ?? asset.versions[asset.versions.length - 1]
  const imgPath = selected ? selected.processed_path || selected.raw_path : ''
  const imgUrl = imgPath ? storageUrl(imgPath) : ''

  const fitZoom = (w: number, h: number) => {
    const el = scrollRef.current
    if (!el || w <= 0 || h <= 0) return 1
    const availW = Math.max(50, el.clientWidth - 64)
    const availH = Math.max(50, el.clientHeight - 64)
    return Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, Math.min(availW / w, availH / h)))
  }

  const loadImage = () => {
    const canvas = canvasRef.current
    if (!canvas || !imgUrl) return
    const img = new Image()
    img.onload = () => {
      canvas.width = img.naturalWidth
      canvas.height = img.naturalHeight
      const ctx = canvas.getContext('2d')
      if (!ctx) return
      ctx.clearRect(0, 0, canvas.width, canvas.height)
      ctx.drawImage(img, 0, 0)
      setNativeSize({ w: img.naturalWidth, h: img.naturalHeight })
      setZoom(fitZoom(img.naturalWidth, img.naturalHeight))
      undoStack.current = []
      redoStack.current = []
      setCanUndo(false)
      setCanRedo(false)
      setDirty(false)
      setLoadError('')
      setCloneSource(null)
      cloneOffsetRef.current = null
      dropFloating()
      setSelection(null)
      setMarquee(null)
    }
    img.onerror = () => setLoadError('Failed to load image')
    img.src = imgUrl
  }

  useEffect(loadImage, [imgUrl])

  /** Records which point of the image sits under a screen position, as a fraction of the
   *  image's size, so the zoom that follows can put it back there. */
  const setZoomAnchor = (clientX: number, clientY: number) => {
    const r = canvasRef.current?.getBoundingClientRect()
    zoomAnchorRef.current = {
      clientX,
      clientY,
      fx: r && r.width ? (clientX - r.left) / r.width : 0.5,
      fy: r && r.height ? (clientY - r.top) / r.height : 0.5,
    }
  }

  // Re-anchors the view after a zoom. A layout effect, not a requestAnimationFrame: React
  // commits the new canvas size during the render, and rAF can fire *before* that commit
  // lands (React's scheduler posts its work as a separate task), which measured the old
  // rect and left the correction at exactly zero — the view then drifted on every notch.
  useLayoutEffect(() => {
    const a = zoomAnchorRef.current
    zoomAnchorRef.current = null
    const el = scrollRef.current
    const canvas = canvasRef.current
    if (!a || !el || !canvas) return
    const after = canvas.getBoundingClientRect()
    el.scrollLeft += after.left + a.fx * after.width - a.clientX
    el.scrollTop += after.top + a.fy * after.height - a.clientY
  }, [zoom])

  // Wheel-zoom about the cursor. Native listener (not React's onWheel) because React's
  // synthetic wheel handler is passive and cannot preventDefault the page/container scroll.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    const onWheel = (e: WheelEvent) => {
      e.preventDefault()
      const prev = zoomRef.current
      const next = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, prev * (e.deltaY < 0 ? 1.15 : 1 / 1.15)))
      if (next === prev) return
      // Keep the point under the cursor fixed on screen. Derived from the image's own rect
      // rather than from content coordinates: the image sits behind padding and, while it
      // still fits, auto-margin centring — offsets that don't scale with zoom, so scaling a
      // content coordinate would drift. Re-measuring after layout is exact either way.
      setZoomAnchor(e.clientX, e.clientY)
      zoomRef.current = next
      setZoom(next)
    }
    el.addEventListener('wheel', onWheel, { passive: false })
    return () => el.removeEventListener('wheel', onWheel)
  }, [])

  // Middle-button drag pans the view, the way it does in every image editor — the natural
  // companion to zooming in far enough that scrollbars alone are painful. Native listeners
  // again: the browser's own middle-click autoscroll has to be preventDefault'ed on
  // mousedown, which React's passive-ish synthetic path can't do reliably.
  useEffect(() => {
    const el = scrollRef.current
    if (!el) return
    let start: { x: number; y: number; sl: number; st: number } | null = null
    const onPointerDown = (e: PointerEvent) => {
      if (e.button !== 1) return
      e.preventDefault()
      start = { x: e.clientX, y: e.clientY, sl: el.scrollLeft, st: el.scrollTop }
      el.setPointerCapture(e.pointerId)
      setPanning(true)
    }
    const onPointerMove = (e: PointerEvent) => {
      if (!start) return
      e.preventDefault()
      el.scrollLeft = start.sl - (e.clientX - start.x)
      el.scrollTop = start.st - (e.clientY - start.y)
    }
    const onPointerUp = (e: PointerEvent) => {
      if (!start) return
      start = null
      if (el.hasPointerCapture(e.pointerId)) el.releasePointerCapture(e.pointerId)
      setPanning(false)
    }
    // Suppresses the autoscroll widget (mousedown) and the paste-on-middle-click that some
    // platforms fire afterwards (auxclick).
    const stopMiddle = (e: MouseEvent) => { if (e.button === 1) e.preventDefault() }
    el.addEventListener('pointerdown', onPointerDown)
    el.addEventListener('pointermove', onPointerMove)
    el.addEventListener('pointerup', onPointerUp)
    el.addEventListener('pointercancel', onPointerUp)
    el.addEventListener('mousedown', stopMiddle)
    el.addEventListener('auxclick', stopMiddle)
    return () => {
      el.removeEventListener('pointerdown', onPointerDown)
      el.removeEventListener('pointermove', onPointerMove)
      el.removeEventListener('pointerup', onPointerUp)
      el.removeEventListener('pointercancel', onPointerUp)
      el.removeEventListener('mousedown', stopMiddle)
      el.removeEventListener('auxclick', stopMiddle)
    }
  }, [])

  // Global keydown/keyup for Alt gives the fastest feedback (shows the instant Alt goes
  // down, even before the next mouse move) and clears it on blur (e.g. alt-tabbing away).
  // It's a secondary signal, not the source of truth: a bare Alt press/release is a known
  // flaky case for keydown/keyup across browsers (accelerator-key/focus handling can eat
  // the event entirely) — pointerdown/pointermove's own `altKey` field, synced below, is
  // authoritative and self-corrects on the very next pointer event regardless.
  useEffect(() => {
    const onKeyDown = (e: KeyboardEvent) => { if (e.key === 'Alt') setAltHeld(true) }
    const onKeyUp = (e: KeyboardEvent) => { if (e.key === 'Alt') setAltHeld(false) }
    const onBlur = () => setAltHeld(false)
    window.addEventListener('keydown', onKeyDown)
    window.addEventListener('keyup', onKeyUp)
    window.addEventListener('blur', onBlur)
    return () => {
      window.removeEventListener('keydown', onKeyDown)
      window.removeEventListener('keyup', onKeyUp)
      window.removeEventListener('blur', onBlur)
    }
  }, [])

  const zoomToFit = () => setZoom(fitZoom(nativeSize.w, nativeSize.h))
  /** Jumps to an exact level, keeping whatever is at the centre of the viewport centred, so
   *  a big jump (100% → 1600%) doesn't leave you looking at a random corner. Anchors on the
   *  image's measured rect for the same reason the wheel zoom does. */
  const zoomToLevel = (next: number) => {
    const el = scrollRef.current
    const clamped = Math.min(MAX_ZOOM, Math.max(MIN_ZOOM, next))
    if (clamped === zoomRef.current) return
    if (el) {
      const r = el.getBoundingClientRect()
      setZoomAnchor(r.left + el.clientWidth / 2, r.top + el.clientHeight / 2)
    }
    zoomRef.current = clamped
    setZoom(clamped)
  }

  // The stepper and the 100% button anchor on the viewport centre too, so repeated clicks
  // stay on the same part of the image instead of creeping towards a corner.
  const zoomBy = (factor: number) => zoomToLevel(zoomRef.current * factor)
  const zoomTo100 = () => zoomToLevel(1)

  const pushUndo = () => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    undoStack.current.push(ctx.getImageData(0, 0, canvas.width, canvas.height))
    if (undoStack.current.length > UNDO_LIMIT) undoStack.current.shift()
    // A fresh edit forks the history: whatever was undone is no longer reachable.
    redoStack.current = []
    setCanRedo(false)
    setCanUndo(true)
  }

  const redo = () => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    const snap = redoStack.current.pop()
    if (!canvas || !ctx || !snap) return
    dropFloating()
    endSoftEraseStroke()
    undoStack.current.push(ctx.getImageData(0, 0, canvas.width, canvas.height))
    ctx.putImageData(snap, 0, 0)
    setCanUndo(true)
    setCanRedo(redoStack.current.length > 0)
    setDirty(true)
  }

  const undo = () => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    const snap = undoStack.current.pop()
    if (!canvas || !ctx || !snap) return
    redoStack.current.push(ctx.getImageData(0, 0, canvas.width, canvas.height))
    setCanRedo(true)
    // A move in progress is part of what the snapshot predates (undo is pushed before the
    // pixels are lifted), so the floating layer is dropped rather than committed. Same for
    // any half-finished feathered stroke: its base snapshot is about to be superseded.
    dropFloating()
    endSoftEraseStroke()
    ctx.putImageData(snap, 0, 0)
    setCanUndo(undoStack.current.length > 0)
    setDirty(undoStack.current.length > 0)
  }

  /* ---------------- selection / floating-layer plumbing ---------------- */

  /** Snaps a canvas-space rect to whole pixels and clips it to the canvas bounds, so a
   *  lift/flip never samples a fractional or out-of-bounds region. */
  const snapRect = (r: Rect): Rect => {
    const canvas = canvasRef.current
    const cw = canvas?.width ?? 0
    const ch = canvas?.height ?? 0
    const x0 = Math.max(0, Math.min(cw, Math.round(r.x)))
    const y0 = Math.max(0, Math.min(ch, Math.round(r.y)))
    const x1 = Math.max(0, Math.min(cw, Math.round(r.x + r.w)))
    const y1 = Math.max(0, Math.min(ch, Math.round(r.y + r.h)))
    return { x: Math.min(x0, x1), y: Math.min(y0, y1), w: Math.abs(x1 - x0), h: Math.abs(y1 - y0) }
  }

  const fullRect = (): Rect => {
    const canvas = canvasRef.current
    return { x: 0, y: 0, w: canvas?.width ?? 0, h: canvas?.height ?? 0 }
  }

  /** The region every region-scoped operation (move, flip) works on. */
  const targetRect = (): Rect => snapRect(selection ?? fullRect())

  const renderFloating = () => {
    const ctx = canvasRef.current?.getContext('2d')
    const f = floatingRef.current
    const base = floatBaseRef.current
    if (!ctx || !f || !base) return
    ctx.putImageData(base, 0, 0)
    ctx.drawImage(f.canvas, f.x, f.y)
  }

  /** Cuts `region` out of the canvas into an offscreen buffer that can then be dragged
   *  around freely, leaving transparency behind. */
  const liftFloating = (region: Rect) => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx || region.w < 1 || region.h < 1) return false
    const off = document.createElement('canvas')
    off.width = region.w
    off.height = region.h
    const octx = off.getContext('2d')
    if (!octx) return false
    octx.drawImage(canvas, region.x, region.y, region.w, region.h, 0, 0, region.w, region.h)
    ctx.clearRect(region.x, region.y, region.w, region.h)
    floatBaseRef.current = ctx.getImageData(0, 0, canvas.width, canvas.height)
    floatingRef.current = { canvas: off, x: region.x, y: region.y }
    setHasFloating(true)
    return true
  }

  /** Bakes the floating layer into the canvas at its current position. */
  const commitFloating = () => {
    if (!floatingRef.current) return
    renderFloating()
    floatingRef.current = null
    floatBaseRef.current = null
    moveStartRef.current = null
    setHasFloating(false)
  }

  /** Forgets the floating layer without drawing it — only valid when the canvas is about
   *  to be replaced wholesale (undo, reload). */
  const dropFloating = () => {
    floatingRef.current = null
    floatBaseRef.current = null
    moveStartRef.current = null
    setHasFloating(false)
  }

  const chooseTool = (t: ToolId) => {
    if (t !== 'move') commitFloating()
    setTool(t)
  }

  const clearSelection = () => {
    commitFloating()
    setSelection(null)
    setMarquee(null)
  }

  /** Pulls a dragged position onto the nearest guide it is close enough to, per axis.
   *  Each axis tries the moved layer's centre and its two edges against the canvas centre,
   *  edges and thirds — the same lines the Guides overlay draws — and returns the guide it
   *  locked onto so the drag can show it. The result is applied to the raw pointer-derived
   *  position rather than accumulated, so dragging further than the threshold unsticks it. */
  const applySnap = (x: number, y: number, lw: number, lh: number) => {
    const canvas = canvasRef.current
    if (!canvas) return { x, y, lineX: null as number | null, lineY: null as number | null }
    const tol = SNAP_THRESHOLD_PX / Math.max(zoom, 0.0001)
    const axis = (pos: number, len: number, size: number) => {
      // Centre first in both lists so a centre-to-centre hit wins any tie.
      const targets = [size / 2, 0, size, size / 3, (size * 2) / 3]
      const anchors = [pos + len / 2, pos, pos + len]
      let best: { d: number; t: number } | null = null
      for (const t of targets) {
        for (const a of anchors) {
          const d = t - a
          if (Math.abs(d) <= tol && (!best || Math.abs(d) < Math.abs(best.d))) best = { d, t }
        }
      }
      return best
    }
    const bx = axis(x, lw, canvas.width)
    const by = axis(y, lh, canvas.height)
    return {
      x: bx ? x + bx.d : x,
      y: by ? y + by.d : y,
      lineX: bx ? bx.t : null,
      lineY: by ? by.t : null,
    }
  }

  /** Shifts the moved pixels by whole-pixel steps, lifting them first if the drag hasn't
   *  started one yet — so arrow keys work as a standalone nudge, not just mid-drag. */
  const nudge = (dx: number, dy: number) => {
    if (!canvasRef.current) return
    if (!floatingRef.current) {
      const region = targetRect()
      if (region.w < 1 || region.h < 1) return
      pushUndo()
      if (!liftFloating(region)) return
    }
    floatingRef.current!.x += dx
    floatingRef.current!.y += dy
    renderFloating()
    setSelection((s) => (s ? { ...s, x: s.x + dx, y: s.y + dy } : s))
    setDirty(true)
  }

  // Escape drops the selection (committing anything mid-move first); arrow keys nudge the
  // Move tool. Skipped while a form control has focus so the brush-size slider keeps its
  // own arrow-key behaviour.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const t = e.target as HTMLElement | null
      if (t && (t.tagName === 'INPUT' || t.tagName === 'TEXTAREA' || t.isContentEditable)) return
      // Ctrl/Cmd+Z undoes, Ctrl/Cmd+Shift+Z (or Ctrl+Y, the Windows convention) redoes.
      if ((e.ctrlKey || e.metaKey) && !e.altKey) {
        const k = e.key.toLowerCase()
        if (k === 'z') {
          e.preventDefault()
          if (e.shiftKey) redo()
          else undo()
          return
        }
        if (k === 'y') {
          e.preventDefault()
          redo()
          return
        }
      }
      if (e.key === 'Escape') {
        if (selection || floatingRef.current || marquee) {
          e.preventDefault()
          clearSelection()
        }
        return
      }
      if (tool !== 'move') return
      const step = e.shiftKey ? 10 : 1
      const delta: Record<string, [number, number]> = {
        ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step],
      }
      const d = delta[e.key]
      if (!d) return
      e.preventDefault()
      nudge(d[0], d[1])
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tool, selection, marquee])

  /* ---------------- paint tools ---------------- */

  /** Confines a paint operation to the selection, if there is one — the marquee is a real
   *  mask for Erase and Clone, not just a visual hint. */
  const clipToSelection = (ctx: CanvasRenderingContext2D) => {
    if (!selection) return
    const r = snapRect(selection)
    if (r.w < 1 || r.h < 1) return
    ctx.beginPath()
    ctx.rect(r.x, r.y, r.w, r.h)
    ctx.clip()
  }

  /** Fraction of the radius that stays fully opaque before the feather starts. */
  const brushCore = 1 - softness / 100
  const softBrush = softness > 0
  // At 1px there is no round dab to draw: a radius-0.5 circle (feathered or not) only takes
  // a fraction of the alpha off two neighbouring pixels, which reads as a grey smear rather
  // than an erased pixel. The smallest brush therefore works on whole native pixels.
  const pixelBrush = brushSize <= 1

  /** One brush dab as a white radial gradient: solid out to the hard core, then a
   *  smoothstep falloff to nothing. Used as an alpha mask — for erasing (what to remove)
   *  and for clone dabs (what to keep) alike. */
  const brushGradient = (ctx: CanvasRenderingContext2D, cx: number, cy: number, r: number) => {
    const g = ctx.createRadialGradient(cx, cy, 0, cx, cy, r)
    const core = Math.max(0, Math.min(0.999, brushCore))
    g.addColorStop(0, 'rgba(255,255,255,1)')
    if (core > 0) g.addColorStop(core, 'rgba(255,255,255,1)')
    for (let i = 1; i < FALLOFF_STOPS; i++) {
      const t = i / FALLOFF_STOPS
      g.addColorStop(core + (1 - core) * t, `rgba(255,255,255,${1 - t * t * (3 - 2 * t)})`)
    }
    g.addColorStop(1, 'rgba(255,255,255,0)')
    return g
  }

  /** Walks a segment stamping feathered dabs into the stroke's coverage mask. */
  const stampMaskDabs = (from: { x: number; y: number }, to: { x: number; y: number }) => {
    const mask = strokeMaskRef.current
    const mctx = mask?.getContext('2d')
    if (!mask || !mctx) return
    const r = brushSize / 2
    const spacing = Math.max(1, brushSize * SOFT_DAB_SPACING_RATIO)
    const dist = Math.hypot(to.x - from.x, to.y - from.y)
    const steps = Math.min(CLONE_MAX_DABS_PER_SEGMENT, Math.max(1, Math.ceil(dist / spacing)))
    mctx.save()
    clipToSelection(mctx)
    for (let i = 1; i <= steps; i++) {
      const t = i / steps
      const cx = from.x + (to.x - from.x) * t
      const cy = from.y + (to.y - from.y) * t
      mctx.fillStyle = brushGradient(mctx, cx, cy, r)
      mctx.fillRect(cx - r, cy - r, r * 2, r * 2)
    }
    mctx.restore()
  }

  /** Snapshots the canvas and opens an empty coverage mask for a feathered erase stroke. */
  const beginSoftEraseStroke = () => {
    const canvas = canvasRef.current
    if (!canvas) return
    const base = document.createElement('canvas')
    base.width = canvas.width
    base.height = canvas.height
    base.getContext('2d')?.drawImage(canvas, 0, 0)
    const mask = document.createElement('canvas')
    mask.width = canvas.width
    mask.height = canvas.height
    strokeBaseRef.current = base
    strokeMaskRef.current = mask
  }

  const endSoftEraseStroke = () => {
    strokeBaseRef.current = null
    strokeMaskRef.current = null
  }

  /** Re-derives the canvas from the stroke's start state minus its coverage so far. */
  const compositeSoftErase = () => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    const base = strokeBaseRef.current
    const mask = strokeMaskRef.current
    if (!canvas || !ctx || !base || !mask) return
    ctx.save()
    ctx.globalCompositeOperation = 'copy'
    ctx.drawImage(base, 0, 0)
    ctx.globalCompositeOperation = 'destination-out'
    ctx.drawImage(mask, 0, 0)
    ctx.restore()
  }

  /** Walks a segment clearing whole native pixels — the 1px brush's exact eraser. */
  const erasePixelSegment = (from: { x: number; y: number }, to: { x: number; y: number }) => {
    const ctx = canvasRef.current?.getContext('2d')
    if (!ctx) return
    const dist = Math.hypot(to.x - from.x, to.y - from.y)
    // Half-pixel steps so a diagonal drag can't skip pixels between samples.
    const steps = Math.min(PIXEL_MAX_STEPS_PER_SEGMENT, Math.max(1, Math.ceil(dist * 2)))
    ctx.save()
    clipToSelection(ctx)
    for (let i = 0; i <= steps; i++) {
      const t = i / steps
      ctx.clearRect(Math.floor(from.x + (to.x - from.x) * t), Math.floor(from.y + (to.y - from.y) * t), 1, 1)
    }
    ctx.restore()
  }

  const eraseSegment = (from: { x: number; y: number }, to: { x: number; y: number }) => {
    const ctx = canvasRef.current?.getContext('2d')
    if (!ctx) return
    if (pixelBrush) {
      erasePixelSegment(from, to)
      return
    }
    if (softBrush) {
      stampMaskDabs(from, to)
      compositeSoftErase()
      return
    }
    ctx.save()
    clipToSelection(ctx)
    ctx.globalCompositeOperation = 'destination-out'
    ctx.lineCap = 'round'
    ctx.lineJoin = 'round'
    ctx.lineWidth = brushSize
    ctx.beginPath()
    ctx.moveTo(from.x, from.y)
    ctx.lineTo(to.x, to.y)
    ctx.stroke()
    // A dot too, so a plain click (no movement) still erases something.
    ctx.beginPath()
    ctx.arc(to.x, to.y, brushSize / 2, 0, Math.PI * 2)
    ctx.fill()
    ctx.restore()
  }

  const cloneStampSegment = (
    from: { x: number; y: number },
    to: { x: number; y: number },
    offset: { dx: number; dy: number },
  ) => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    const r = brushSize / 2
    if (pixelBrush) {
      // Whole-pixel copy for the same reason erasing has one: a half-pixel-radius dab would
      // blend a faded version of the source across two pixels instead of copying one.
      const dist = Math.hypot(to.x - from.x, to.y - from.y)
      const steps = Math.min(PIXEL_MAX_STEPS_PER_SEGMENT, Math.max(1, Math.ceil(dist * 2)))
      ctx.save()
      clipToSelection(ctx)
      for (let i = 0; i <= steps; i++) {
        const t = i / steps
        const dx = Math.floor(from.x + (to.x - from.x) * t)
        const dy = Math.floor(from.y + (to.y - from.y) * t)
        ctx.clearRect(dx, dy, 1, 1) // else a semi-transparent source pixel blends with what's there
        ctx.drawImage(canvas, Math.floor(dx - offset.dx), Math.floor(dy - offset.dy), 1, 1, dx, dy, 1, 1)
      }
      ctx.restore()
      return
    }
    const spacing = Math.max(1, brushSize * (softBrush ? SOFT_DAB_SPACING_RATIO : CLONE_DAB_SPACING_RATIO))
    const dist = Math.hypot(to.x - from.x, to.y - from.y)
    const steps = Math.min(CLONE_MAX_DABS_PER_SEGMENT, Math.max(1, Math.ceil(dist / spacing)))
    for (let i = 1; i <= steps; i++) {
      const t = i / steps
      const dx = from.x + (to.x - from.x) * t
      const dy = from.y + (to.y - from.y) * t
      const sx = dx - offset.dx
      const sy = dy - offset.dy
      if (softBrush) {
        // A feathered dab can't be a clip path, so the sampled tile is cut out into a
        // scratch canvas, masked by the same falloff gradient, then laid down whole —
        // giving the copied patch an edge that blends instead of a visible circle seam.
        const size = Math.max(1, Math.ceil(brushSize))
        let dab = cloneDabRef.current
        if (!dab) { dab = document.createElement('canvas'); cloneDabRef.current = dab }
        if (dab.width !== size) dab.width = size
        if (dab.height !== size) dab.height = size
        const dctx = dab.getContext('2d')
        if (!dctx) continue
        dctx.globalCompositeOperation = 'copy'
        dctx.drawImage(canvas, sx - r, sy - r, brushSize, brushSize, 0, 0, size, size)
        dctx.globalCompositeOperation = 'destination-in'
        dctx.fillStyle = brushGradient(dctx, size / 2, size / 2, size / 2)
        dctx.fillRect(0, 0, size, size)
        ctx.save()
        clipToSelection(ctx)
        ctx.drawImage(dab, 0, 0, size, size, dx - r, dy - r, brushSize, brushSize)
        ctx.restore()
        continue
      }
      ctx.save()
      clipToSelection(ctx)
      ctx.beginPath()
      ctx.arc(dx, dy, r, 0, Math.PI * 2)
      ctx.clip()
      // Self-copy is spec-safe: drawImage(canvas,...) reading/writing the same canvas is
      // atomic per call. We deliberately sample the LIVE canvas (no snapshot-at-stroke-
      // start buffer) — matches real Photoshop, where Clone Stamp always reads current
      // pixel content, including your own earlier strokes. If source and destination
      // circles overlap mid-stroke you can get a smear/feedback look; that's expected
      // tool behavior, not a bug.
      ctx.drawImage(canvas, sx - r, sy - r, r * 2, r * 2, dx - r, dy - r, r * 2, r * 2)
      ctx.restore()
    }
  }

  /** Mirrors the selection, or the whole image when nothing is selected. */
  const flip = (axis: 'h' | 'v') => {
    const canvas = canvasRef.current
    const ctx = canvas?.getContext('2d')
    if (!canvas || !ctx) return
    commitFloating()
    const r = targetRect()
    if (r.w < 1 || r.h < 1) return
    pushUndo()
    const off = document.createElement('canvas')
    off.width = r.w
    off.height = r.h
    const octx = off.getContext('2d')
    if (!octx) return
    octx.drawImage(canvas, r.x, r.y, r.w, r.h, 0, 0, r.w, r.h)
    ctx.save()
    ctx.clearRect(r.x, r.y, r.w, r.h)
    ctx.translate(r.x + (axis === 'h' ? r.w : 0), r.y + (axis === 'v' ? r.h : 0))
    ctx.scale(axis === 'h' ? -1 : 1, axis === 'v' ? -1 : 1)
    ctx.drawImage(off, 0, 0)
    ctx.restore()
    setDirty(true)
  }

  const pointFromEvent = (e: React.PointerEvent<HTMLCanvasElement>) => {
    const canvas = canvasRef.current!
    const rect = canvas.getBoundingClientRect()
    return {
      x: ((e.clientX - rect.left) / rect.width) * canvas.width,
      y: ((e.clientY - rect.top) / rect.height) * canvas.height,
    }
  }

  // Redraws the small live-preview canvas (shown at the cursor) with the pixels that
  // would actually be copied if a clone stroke started right now — the same sample-point
  // math as the destination ring/crosshair, but rendering real content instead of an
  // outline, so lining up a patch (e.g. matching a repeating texture) doesn't require
  // trial and error.
  const updateClonePreview = (hc: { x: number; y: number } | null) => {
    const preview = previewCanvasRef.current
    const main = canvasRef.current
    if (!preview || !main) return
    if (preview.width !== brushSize) preview.width = brushSize
    if (preview.height !== brushSize) preview.height = brushSize
    const pctx = preview.getContext('2d')
    if (!pctx) return
    pctx.globalCompositeOperation = 'source-over'
    pctx.clearRect(0, 0, preview.width, preview.height)
    if (!hc || tool !== 'clone') return
    const sampleOrigin = cloneOffsetRef.current
      ? { x: hc.x - cloneOffsetRef.current.dx, y: hc.y - cloneOffsetRef.current.dy }
      : cloneSource
    if (!sampleOrigin) return
    const r = brushSize / 2
    pctx.drawImage(main, sampleOrigin.x - r, sampleOrigin.y - r, brushSize, brushSize, 0, 0, brushSize, brushSize)
    // Feather the preview the same way the dab itself will be feathered, so what you line
    // up under the cursor is what actually lands.
    if (softBrush && !pixelBrush) {
      pctx.globalCompositeOperation = 'destination-in'
      pctx.fillStyle = brushGradient(pctx, r, r, r)
      pctx.fillRect(0, 0, preview.width, preview.height)
      pctx.globalCompositeOperation = 'source-over'
    }
  }

  // Keeps the preview correct when something other than mouse movement changes what it
  // should show (brush size, tool switch, a fresh Alt+Click source) — pointermove alone
  // wouldn't catch these if the cursor happens to sit still while they change.
  useEffect(() => {
    updateClonePreview(lastHoverCanvas.current)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [brushSize, softness, tool, cloneSource])

  const isPaintTool = tool === 'erase' || tool === 'clone'

  const onPointerDown = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (e.altKey !== altHeld) setAltHeld(e.altKey)
    // Left button only: middle pans the view, right belongs to the context menu — neither
    // should paint, and middle-drag panning would otherwise erase a stripe across the image.
    if (e.button !== 0) return
    const p = pointFromEvent(e)
    if (e.altKey && isPaintTool) {
      // Alt+Click always defines/moves the Clone Stamp source and switches to that tool
      // if it wasn't already active — matches Photoshop's Alt+Click convention. Checking
      // this before `tool` matters: if this only worked while Clone Stamp was already
      // selected, an Alt+Click made before explicitly clicking that toolbar button would
      // fall through to the Erase tool's plain-click handling below and erase at that
      // point instead of picking a source — exactly the bug that caused a real save to
      // come out mostly erased. Restricted to the paint tools so Alt+dragging with
      // Select/Move can't silently throw you into Clone Stamp.
      if (tool !== 'clone') setTool('clone')
      setCloneSource(p)
      cloneOffsetRef.current = null // new source invalidates any locked offset
      return
    }
    if (tool === 'select') {
      e.currentTarget.setPointerCapture(e.pointerId)
      commitFloating() // starting a new selection settles whatever was mid-move
      setMarquee({ x0: p.x, y0: p.y, x1: p.x, y1: p.y })
      setDrawing(true)
      return
    }
    if (tool === 'move') {
      const region = targetRect()
      if (region.w < 1 || region.h < 1) return
      e.currentTarget.setPointerCapture(e.pointerId)
      if (!floatingRef.current) {
        pushUndo()
        if (!liftFloating(region)) return
        // A fractional marquee is snapped to the pixels actually lifted, so the outline
        // and the moved content stay in lockstep from here on.
        setSelection((s) => (s ? region : s))
      }
      moveStartRef.current = { px: p.x, py: p.y, ox: floatingRef.current!.x, oy: floatingRef.current!.y }
      setDrawing(true)
      setDirty(true)
      return
    }
    if (tool === 'clone') {
      if (!cloneSource) return // nothing to sample from yet
      e.currentTarget.setPointerCapture(e.pointerId)
      pushUndo()
      // Aligned mode: lock the offset only once, on the first stroke after a source pick.
      // Later strokes reuse it as-is, so the sample point keeps the same spatial
      // relationship to the source no matter where each new stroke starts.
      if (!cloneOffsetRef.current) {
        cloneOffsetRef.current = { dx: p.x - cloneSource.x, dy: p.y - cloneSource.y }
      }
      cloneStampSegment(p, p, cloneOffsetRef.current)
      lastPoint.current = p
      setDrawing(true)
      setDirty(true)
      return
    }
    e.currentTarget.setPointerCapture(e.pointerId)
    pushUndo()
    if (softBrush && !pixelBrush) beginSoftEraseStroke()
    eraseSegment(p, p)
    lastPoint.current = p
    setDrawing(true)
    setDirty(true)
  }

  const onPointerMove = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (e.altKey !== altHeld) setAltHeld(e.altKey)
    const rect = e.currentTarget.getBoundingClientRect()
    setHoverPos({ x: e.clientX - rect.left, y: e.clientY - rect.top })
    const p = pointFromEvent(e)
    lastHoverCanvas.current = p
    if (tool === 'clone') updateClonePreview(p)
    if (!drawing) return
    if (tool === 'select') {
      setMarquee((m) => (m ? { ...m, x1: p.x, y1: p.y } : m))
      return
    }
    if (tool === 'move') {
      const ms = moveStartRef.current
      const f = floatingRef.current
      if (!ms || !f) return
      const rawX = ms.ox + (p.x - ms.px)
      const rawY = ms.oy + (p.y - ms.py)
      // Alt is the standard "ignore snapping for this drag" modifier; it can't collide with
      // the Clone Stamp source pick, which only listens on the paint tools.
      const snapped = snapEnabled && !e.altKey
        ? applySnap(rawX, rawY, f.canvas.width, f.canvas.height)
        : { x: rawX, y: rawY, lineX: null, lineY: null }
      setSnapLines((s) => (s.x === snapped.lineX && s.y === snapped.lineY ? s : { x: snapped.lineX, y: snapped.lineY }))
      const nx = Math.round(snapped.x)
      const ny = Math.round(snapped.y)
      const dx = nx - f.x
      const dy = ny - f.y
      if (!dx && !dy) return
      f.x = nx
      f.y = ny
      renderFloating()
      setSelection((s) => (s ? { ...s, x: s.x + dx, y: s.y + dy } : s))
      return
    }
    if (tool === 'clone') {
      if (cloneOffsetRef.current && lastPoint.current) cloneStampSegment(lastPoint.current, p, cloneOffsetRef.current)
    } else if (lastPoint.current) {
      eraseSegment(lastPoint.current, p)
    }
    lastPoint.current = p
  }

  const endStroke = (e: React.PointerEvent<HTMLCanvasElement>) => {
    if (e.currentTarget.hasPointerCapture(e.pointerId)) e.currentTarget.releasePointerCapture(e.pointerId)
    if (marquee) {
      const r = snapRect({
        x: Math.min(marquee.x0, marquee.x1),
        y: Math.min(marquee.y0, marquee.y1),
        w: Math.abs(marquee.x1 - marquee.x0),
        h: Math.abs(marquee.y1 - marquee.y0),
      })
      // A click without a real drag reads as "deselect", the same as clicking outside a
      // selection in an image editor.
      setSelection(r.w >= 2 && r.h >= 2 ? r : null)
      setMarquee(null)
    }
    setDrawing(false)
    endSoftEraseStroke()
    lastPoint.current = null
    moveStartRef.current = null
    setSnapLines((s) => (s.x === null && s.y === null ? s : { x: null, y: null }))
  }

  const onPointerLeave = (e: React.PointerEvent<HTMLCanvasElement>) => {
    endStroke(e)
    setHoverPos(null)
    lastHoverCanvas.current = null
    updateClonePreview(null)
  }

  const save = async () => {
    const canvas = canvasRef.current
    if (!canvas) return
    commitFloating() // an un-dropped move must land in the exported pixels
    setSaving(true)
    setSaveError('')
    try {
      const blob = await new Promise<Blob>((resolve, reject) => {
        canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('Export failed'))), 'image/png')
      })
      const file = new File([blob], `${asset.name}-edited.png`, { type: 'image/png' })
      const updated = await api.uploadAssetVersion(asset.id, file, { preserveFraming: true })
      onSaved(updated)
      onClose()
    } catch (e) {
      setSaveError(e instanceof Error ? e.message : 'Save failed')
    } finally {
      setSaving(false)
    }
  }

  const canvasCssW = nativeSize.w * zoom
  const canvasCssH = nativeSize.h * zoom
  const ringDiameter = brushSize * zoom
  const zoomPct = Math.round(zoom * 100)
  // A ring this small is invisible, and the real cursor is hidden while painting — so a
  // crosshair takes over to show where the brush actually is (the 1px brush at any zoom
  // below ~800%, or any small brush zoomed out).
  const tinyRing = ringDiameter < 8
  // Nearest-neighbour past the threshold so zooming in shows actual pixels, not a blur.
  const pixelRendering: React.CSSProperties['imageRendering'] = zoom > PIXELATE_ABOVE ? 'pixelated' : 'auto'
  const hoverCanvas = hoverPos && zoom ? { x: hoverPos.x / zoom, y: hoverPos.y / zoom } : null
  const sourceDisplay =
    tool !== 'clone' || !hoverCanvas
      ? null
      : cloneOffsetRef.current
        // Offset already locked (Aligned): always project the live sample point, whether
        // hovering or actively dragging — matches Photoshop's continuous preview once
        // Aligned mode has established an offset.
        ? { x: (hoverCanvas.x - cloneOffsetRef.current.dx) * zoom, y: (hoverCanvas.y - cloneOffsetRef.current.dy) * zoom }
        // No offset locked yet: nothing to project, so just show the anchor itself.
        : cloneSource
          ? { x: cloneSource.x * zoom, y: cloneSource.y * zoom }
          : null
  // Alt held means a click right now sets the clone source, not paint — true for either
  // paint tool (Alt+Click always wins, see onPointerDown), so swap every paint-mode
  // indicator out for the dedicated picking crosshair whenever it's held, not just while
  // Clone Stamp happens to already be the active tool.
  const pickingSource = altHeld && isPaintTool
  const hasCloneSample = tool === 'clone' && !!(cloneOffsetRef.current || cloneSource)
  const showBrushRing = isPaintTool && !!hoverPos && !pickingSource

  // Outline of whatever the marquee currently describes, in on-screen pixels.
  const marqueeRect = marquee
    ? {
        x: Math.min(marquee.x0, marquee.x1),
        y: Math.min(marquee.y0, marquee.y1),
        w: Math.abs(marquee.x1 - marquee.x0),
        h: Math.abs(marquee.y1 - marquee.y0),
      }
    : selection
  const activeTool = TOOLS.find((t) => t.id === tool)!

  const btnStyle: React.CSSProperties = {
    background: 'none', border: '1px solid var(--border-2)', color: 'var(--text-2)',
    width: 26, height: 26, borderRadius: 5, cursor: 'pointer', fontSize: 13,
    display: 'flex', alignItems: 'center', justifyContent: 'center',
  }
  const cursor = panning ? 'grabbing' : tool === 'select' ? 'crosshair' : tool === 'move' ? 'move' : 'none'
  const guideLine = (style: React.CSSProperties): React.CSSProperties => ({ position: 'absolute', pointerEvents: 'none', ...style })

  return (
    <div
      onClick={(e) => e.stopPropagation()}
      style={{
        position: 'fixed', inset: 0, zIndex: 60, background: 'var(--card)',
        display: 'flex', flexDirection: 'column',
      }}
    >
      {/* Header */}
      <div style={{
        display: 'flex', alignItems: 'center', justifyContent: 'space-between',
        padding: '14px 20px', borderBottom: '1px solid var(--border)', flexShrink: 0,
      }}>
        <div style={{ fontSize: 15, fontWeight: 600 }}>
          Edit — {asset.name}
          <span style={{ fontSize: 12, fontWeight: 500, color: 'var(--muted)', marginLeft: 8 }}>{activeTool.label}</span>
        </div>
        <button
          onClick={onClose}
          title="Close"
          style={{ background: 'none', border: 'none', color: 'var(--muted)', fontSize: 18, cursor: 'pointer', lineHeight: 1 }}
        >
          ✕
        </button>
      </div>

      {/* Toolbar */}
      <div style={{
        display: 'flex', alignItems: 'center', gap: 20, padding: '10px 20px',
        borderBottom: '1px solid var(--border)', flexShrink: 0, flexWrap: 'wrap',
      }}>
        <div style={{ display: 'flex', gap: 4 }}>
          {TOOLS.map((t) => (
            <button
              key={t.id}
              className={tool === t.id ? 'btn btn-accent' : 'btn btn-secondary'}
              onClick={() => chooseTool(t.id)}
              disabled={saving}
              title={t.hint}
              style={{ padding: '6px 12px', fontSize: 12 }}
            >
              {t.label}
            </button>
          ))}
        </div>

        {isPaintTool && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 11.5, color: 'var(--muted)', whiteSpace: 'nowrap' }}>Brush size</span>
            <input
              type="range"
              min={1}
              max={200}
              value={brushSize}
              onChange={(e) => setBrushSize(Number(e.target.value))}
              title="1px is a single native pixel — zoom in to place it"
              style={{ width: 160 }}
            />
            <span style={{ fontSize: 11, color: 'var(--muted-2)', width: 34 }}>{brushSize}px</span>
            <span
              title="Hard edge cuts on a crisp circle; softer feathers the edge out so erased or cloned areas blend instead of leaving a visible rim"
              style={{ fontSize: 11.5, color: 'var(--muted)', whiteSpace: 'nowrap', marginLeft: 6, cursor: 'help' }}
            >
              Edge
            </span>
            <input
              type="range"
              min={0}
              max={100}
              value={softness}
              onChange={(e) => setSoftness(Number(e.target.value))}
              disabled={pixelBrush}
              title={pixelBrush ? 'A 1px brush is always hard — it works on whole pixels' : '0 = hard edge, 100 = fully soft'}
              style={{ width: 110, opacity: pixelBrush ? 0.4 : 1 }}
            />
            <span style={{ fontSize: 11, color: 'var(--muted-2)', width: 54 }}>
              {pixelBrush ? '1px' : softness === 0 ? 'Hard' : softness === 100 ? 'Soft' : `${softness}%`}
            </span>
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontSize: 11.5, color: 'var(--muted)', whiteSpace: 'nowrap' }}>Zoom</span>
          <button onClick={() => zoomBy(1 / 1.25)} title="Zoom out" style={btnStyle}>−</button>
          <select
            value={ZOOM_LEVELS.includes(zoomPct) ? String(zoomPct) : 'custom'}
            onChange={(e) => zoomToLevel(Number(e.target.value) / 100)}
            title="Jump to a zoom level"
            style={{
              background: 'none', border: '1px solid var(--border-2)', color: 'var(--text-2)',
              borderRadius: 5, fontSize: 11, height: 26, padding: '0 4px', cursor: 'pointer',
            }}
          >
            {!ZOOM_LEVELS.includes(zoomPct) && <option value="custom">{zoomPct}%</option>}
            {ZOOM_LEVELS.map((l) => (
              <option key={l} value={l}>{l}%</option>
            ))}
          </select>
          <button onClick={() => zoomBy(1.25)} title="Zoom in" style={btnStyle}>+</button>
          <button onClick={zoomToFit} title="Fit to window" style={{ ...btnStyle, width: 'auto', padding: '0 8px', fontSize: 11 }}>Fit</button>
          <button onClick={zoomTo100} title="100%" style={{ ...btnStyle, width: 'auto', padding: '0 8px', fontSize: 11 }}>100%</button>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
          <span style={{ fontSize: 11.5, color: 'var(--muted)', whiteSpace: 'nowrap' }}>Flip</span>
          <button
            onClick={() => flip('h')}
            disabled={saving || drawing}
            title={selection ? 'Flip the selection horizontally' : 'Flip the whole image horizontally'}
            style={{ ...btnStyle, width: 'auto', padding: '0 9px', fontSize: 11 }}
          >
            ⇄ H
          </button>
          <button
            onClick={() => flip('v')}
            disabled={saving || drawing}
            title={selection ? 'Flip the selection vertically' : 'Flip the whole image vertically'}
            style={{ ...btnStyle, width: 'auto', padding: '0 9px', fontSize: 11 }}
          >
            ⇅ V
          </button>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <label style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: 'var(--muted)', cursor: 'pointer' }}>
            <input type="checkbox" checked={showGuides} onChange={(e) => setShowGuides(e.target.checked)} />
            Guides
          </label>
          <label
            title="Moving sticks to the centre, thirds and edges — hold Alt while dragging to bypass"
            style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11.5, color: 'var(--muted)', cursor: 'pointer' }}
          >
            <input type="checkbox" checked={snapEnabled} onChange={(e) => setSnapEnabled(e.target.checked)} />
            Snap
          </label>
        </div>

        {tool === 'clone' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 11.5, color: cloneSource ? 'var(--text-2)' : 'var(--muted)' }}>
              {cloneSource ? 'Source set — drag to clone' : 'Alt+Click anywhere to set a clone source'}
            </span>
            {cloneSource && (
              <button
                className="btn btn-secondary"
                onClick={() => { setCloneSource(null); cloneOffsetRef.current = null }}
                disabled={drawing || saving}
                style={{ padding: '4px 10px', fontSize: 11 }}
              >
                Clear source
              </button>
            )}
          </div>
        )}

        {selection && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span style={{ fontSize: 11.5, color: 'var(--text-2)', whiteSpace: 'nowrap' }}>
              Selection {Math.round(selection.w)}×{Math.round(selection.h)}
            </span>
            <button
              className="btn btn-secondary"
              onClick={clearSelection}
              disabled={drawing || saving}
              style={{ padding: '4px 10px', fontSize: 11 }}
            >
              Deselect
            </button>
          </div>
        )}

        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <button className="btn btn-secondary" onClick={undo} disabled={!canUndo || saving} title="Undo (Ctrl+Z)" style={{ padding: '6px 12px', fontSize: 12 }}>
            ↶ Undo
          </button>
          <button className="btn btn-secondary" onClick={redo} disabled={!canRedo || saving} title="Redo (Ctrl+Shift+Z)" style={{ padding: '6px 12px', fontSize: 12 }}>
            ↷ Redo
          </button>
          <button className="btn btn-secondary" onClick={loadImage} disabled={saving} style={{ padding: '6px 12px', fontSize: 12 }}>
            Reset
          </button>
        </div>

        <div style={{ flex: 1, minWidth: 20 }} />

        {saveError && <div style={{ color: '#ff7b7b', fontSize: 12 }}>{saveError}</div>}

        <div style={{ display: 'flex', gap: 8 }}>
          <button className="btn btn-secondary" onClick={onClose} disabled={saving} style={{ padding: '7px 14px', fontSize: 12 }}>
            Cancel
          </button>
          <button className="btn btn-accent" onClick={save} disabled={saving || !dirty} style={{ padding: '7px 14px', fontSize: 12, fontWeight: 700 }}>
            {saving ? 'Saving…' : 'Save as new version'}
          </button>
        </div>
      </div>

      {loadError && <div style={{ color: '#ff7b7b', fontSize: 12, padding: '8px 20px' }}>{loadError}</div>}

      {/* Scrollable/zoomable canvas area */}
      <div
        ref={scrollRef}
        style={{
          flex: 1, overflow: 'auto', background: 'var(--bg, #14161a)', minHeight: 0,
          cursor: panning ? 'grabbing' : 'default',
        }}
      >
        {/* Centring must come from auto margins on the image, NOT justify/align-center on
            this box: with centring the overflow of a zoomed-in image is split evenly to
            both sides, and the left/top half is then unreachable because scroll offsets
            can't go negative. Auto margins collapse to 0 once the content overflows, so
            every pixel stays scrollable — and it still centres while the image fits. */}
        {/* width/height: max-content so this box grows with the zoomed image instead of
            being overflowed by it — that keeps the trailing padding inside the scrollable
            area, so the right/bottom edges get the same 40px breathing room as the left/top. */}
        <div style={{ minWidth: '100%', minHeight: '100%', width: 'max-content', height: 'max-content', display: 'flex', padding: 40 }}>
          <div
            className="checkerboard"
            style={{
              width: canvasCssW, height: canvasCssH, borderRadius: 4, overflow: 'hidden',
              outline: '1px solid var(--border-2)', touchAction: 'none', position: 'relative',
              flexShrink: 0, margin: 'auto',
            }}
          >
            <canvas
              ref={canvasRef}
              style={{ width: '100%', height: '100%', display: 'block', cursor, imageRendering: pixelRendering }}
              onPointerDown={onPointerDown}
              onPointerMove={onPointerMove}
              onPointerUp={endStroke}
              onPointerLeave={onPointerLeave}
            />

            {/* Composition guides — centre cross plus rule of thirds, overlay only. */}
            {showGuides && (
              <div style={{ position: 'absolute', inset: 0, pointerEvents: 'none' }}>
                {[1 / 3, 2 / 3].map((f) => (
                  <div key={`v${f}`} style={guideLine({ left: `${f * 100}%`, top: 0, bottom: 0, width: 1, background: 'rgba(255,255,255,0.22)' })} />
                ))}
                {[1 / 3, 2 / 3].map((f) => (
                  <div key={`h${f}`} style={guideLine({ top: `${f * 100}%`, left: 0, right: 0, height: 1, background: 'rgba(255,255,255,0.22)' })} />
                ))}
                <div style={guideLine({ left: '50%', top: 0, bottom: 0, width: 1, background: 'var(--accent)', opacity: 0.85 })} />
                <div style={guideLine({ top: '50%', left: 0, right: 0, height: 1, background: 'var(--accent)', opacity: 0.85 })} />
                <div style={guideLine({
                  left: '50%', top: '50%', width: 11, height: 11, marginLeft: -5.5, marginTop: -5.5,
                  border: '1px solid var(--accent)', borderRadius: '50%',
                })} />
              </div>
            )}

            {/* Snap indicators — the guide the drag is currently stuck to, in a colour the
                composition guides never use so the two can't be confused. Shown whether or
                not the Guides overlay is on, since this is drag feedback, not composition. */}
            {snapLines.x !== null && (
              <div style={{ position: 'absolute', left: snapLines.x * zoom, top: 0, bottom: 0, width: 1, background: '#ff5fa2', pointerEvents: 'none' }} />
            )}
            {snapLines.y !== null && (
              <div style={{ position: 'absolute', top: snapLines.y * zoom, left: 0, right: 0, height: 1, background: '#ff5fa2', pointerEvents: 'none' }} />
            )}

            {/* Marching-ants outline: the live marquee while dragging, else the committed
                selection — which the Move tool keeps pinned to the pixels it carries. */}
            {marqueeRect && marqueeRect.w > 0 && marqueeRect.h > 0 && (
              <div
                className="marching-ants"
                style={{
                  position: 'absolute',
                  left: marqueeRect.x * zoom,
                  top: marqueeRect.y * zoom,
                  width: marqueeRect.w * zoom,
                  height: marqueeRect.h * zoom,
                  pointerEvents: 'none',
                }}
              />
            )}

            {showBrushRing && !hasCloneSample && (
              <div
                style={{
                  position: 'absolute',
                  left: hoverPos!.x,
                  top: hoverPos!.y,
                  width: ringDiameter,
                  height: ringDiameter,
                  transform: 'translate(-50%, -50%)',
                  borderRadius: '50%',
                  // A feathered brush shows its falloff instead of a solid disc, and a
                  // dashed rim, so the outer circle reads as "fades out by here".
                  border: softBrush ? '1.5px dashed rgba(255,255,255,0.85)' : '1.5px solid #fff',
                  background: softBrush
                    ? `radial-gradient(circle, rgba(255,255,255,0.45) 0%, rgba(255,255,255,0.45) ${brushCore * 100}%, rgba(255,255,255,0) 100%)`
                    : 'rgba(255,255,255,0.25)',
                  mixBlendMode: 'difference',
                  pointerEvents: 'none',
                }}
              />
            )}
            {/* Live preview of the actual pixels a clone stroke would copy right now —
                shown instead of the plain ring once a source is available, so aligning a
                patch (e.g. against a repeating texture) doesn't take trial and error. */}
            {showBrushRing && hasCloneSample && (
              <canvas
                ref={previewCanvasRef}
                style={{
                  position: 'absolute',
                  left: hoverPos!.x,
                  top: hoverPos!.y,
                  width: ringDiameter,
                  height: ringDiameter,
                  transform: 'translate(-50%, -50%)',
                  borderRadius: '50%',
                  border: softBrush ? '1.5px dashed rgba(255,255,255,0.85)' : '1.5px solid #fff',
                  imageRendering: pixelRendering,
                  pointerEvents: 'none',
                }}
              />
            )}
            {/* Crosshair for brushes too small to see as a ring — with a gap in the middle
                so the pixel being targeted stays visible. */}
            {showBrushRing && tinyRing && (
              <div style={{ position: 'absolute', left: hoverPos!.x, top: hoverPos!.y, pointerEvents: 'none', mixBlendMode: 'difference' }}>
                <div style={{ position: 'absolute', left: -0.5, top: -11, width: 1, height: 7, background: '#fff' }} />
                <div style={{ position: 'absolute', left: -0.5, top: 4, width: 1, height: 7, background: '#fff' }} />
                <div style={{ position: 'absolute', top: -0.5, left: -11, height: 1, width: 7, background: '#fff' }} />
                <div style={{ position: 'absolute', top: -0.5, left: 4, height: 1, width: 7, background: '#fff' }} />
              </div>
            )}
            {showBrushRing && sourceDisplay && (
              <div
                style={{
                  position: 'absolute',
                  left: sourceDisplay.x,
                  top: sourceDisplay.y,
                  width: ringDiameter,
                  height: ringDiameter,
                  transform: 'translate(-50%, -50%)',
                  borderRadius: '50%',
                  border: '1.5px dashed var(--accent)',
                  pointerEvents: 'none',
                }}
              >
                <div style={{ position: 'absolute', left: '50%', top: 0, bottom: 0, width: 1, background: 'var(--accent)' }} />
                <div style={{ position: 'absolute', top: '50%', left: 0, right: 0, height: 1, background: 'var(--accent)' }} />
              </div>
            )}
            {/* Alt is held with a paint tool active: a click now sets the clone source,
                not paint — a distinct crosshair makes that unambiguous before you commit. */}
            {hoverPos && pickingSource && (
              <div style={{ position: 'absolute', left: hoverPos.x, top: hoverPos.y, pointerEvents: 'none' }}>
                <div style={{ position: 'absolute', left: -11, top: -11, width: 22, height: 22, borderRadius: '50%', border: '1.5px solid #4ade80' }} />
                <div style={{ position: 'absolute', left: -1, top: -11, width: 2, height: 22, background: '#4ade80' }} />
                <div style={{ position: 'absolute', left: -11, top: -1, width: 22, height: 2, background: '#4ade80' }} />
                <div style={{
                  position: 'absolute', left: 14, top: -9, whiteSpace: 'nowrap', fontSize: 10, color: '#4ade80',
                  background: 'rgba(0,0,0,0.65)', padding: '1px 5px', borderRadius: 3,
                }}>
                  Click to set source
                </div>
              </div>
            )}
          </div>
        </div>
      </div>

      <div style={{ fontSize: 10.5, color: 'var(--muted-2)', padding: '8px 20px', borderTop: '1px solid var(--border)', flexShrink: 0 }}>
        {activeTool.hint}
        {selection && isPaintTool ? ' · confined to the selection' : ''}
        {hasFloating ? ' · moved pixels settle when you switch tool, deselect or save' : ''}
        {' · '}Esc deselects · Ctrl+Z / Ctrl+Shift+Z undo & redo · scroll to zoom (up to {MAX_ZOOM * 100}%) · middle-drag to pan · saving adds this as a new version — the original stays in the version history.
      </div>
    </div>
  )
}
