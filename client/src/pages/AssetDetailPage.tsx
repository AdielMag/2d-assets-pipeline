import { useEffect, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { api, isQuotaError, referenceLabel, resolveAtlasParent, resolveDomainFolder, storageUrl, timeAgo, TYPE_DOT, TYPE_LABEL } from '../api'
import type { Asset, Atlas, LlmSelection, ProgressEvent, PromptMode, ReferenceOp, ReferenceOpGroup } from '../api'
import { useApp } from '../AppContext'
import { RunProgress, fmtTokens } from '../components/RunProgress'
import type { ProgressItem } from '../components/RunProgress'
import ImageProviderChooser, { useImageProviders } from '../components/ImageProviderChooser'
import LlmChooser from '../components/LlmChooser'
import NineSliceEditor from '../components/NineSliceEditor'
import type { NineSlice } from '../components/NineSliceEditor'
import TilePreview from '../components/TilePreview'
import ImageEditorModal from '../components/ImageEditorModal'

const PROVIDER_NOTE: Record<string, (model: string) => string> = {
  antigravity: (model) =>
    `${model ? `${model} — ` : ''}free on your Google AI Pro subscription (Antigravity CLI); chroma-key removal applied automatically.`,
  higgsfield: (model) =>
    `${model ? `${model} — ` : ''}via your Higgsfield plan (official CLI); chroma-key removal applied automatically.`,
}

function SheetGridPanel({ asset, imageUrl, onSaved }: {
  asset: Asset
  imageUrl: string
  onSaved: (a: Asset) => void
}) {
  const rows = asset.sheet_rows ?? 1
  const cols = asset.sheet_cols ?? 4

  const save = async (r: number, c: number) => {
    onSaved(await api.updateAsset(asset.id, { sheet_rows: r, sheet_cols: c }))
  }

  const numStyle: React.CSSProperties = {
    width: 64, background: 'var(--input)', border: '1px solid var(--border-2)',
    borderRadius: 6, padding: '6px 8px', fontSize: 12, outline: 'none',
  }

  return (
    <div>
      <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 14 }}>Sprite Sheet Grid</div>
      <div className="checkerboard" style={{ position: 'relative', width: '100%', borderRadius: 8, overflow: 'hidden', marginBottom: 12 }}>
        <img src={imageUrl} alt="" style={{ width: '100%', display: 'block' }} />
        <div
          style={{
            position: 'absolute', inset: 0, display: 'grid', pointerEvents: 'none',
            gridTemplateColumns: `repeat(${cols}, 1fr)`, gridTemplateRows: `repeat(${rows}, 1fr)`,
          }}
        >
          {Array.from({ length: rows * cols }).map((_, i) => (
            <div key={i} style={{ border: '1px dashed rgba(108,140,255,0.55)' }} />
          ))}
        </div>
      </div>
      <div style={{ display: 'flex', gap: 12, alignItems: 'center' }}>
        <div className="field-label" style={{ marginBottom: 0 }}>Rows</div>
        <input type="number" min={1} max={32} style={numStyle} value={rows} onChange={(e) => save(Math.max(1, Number(e.target.value) || 1), cols)} />
        <div className="field-label" style={{ marginBottom: 0 }}>Columns</div>
        <input type="number" min={1} max={32} style={numStyle} value={cols} onChange={(e) => save(rows, Math.max(1, Number(e.target.value) || 1))} />
      </div>
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 10, lineHeight: 1.5 }}>
        Unity will slice the sheet on this grid via the exported import settings.
      </div>
    </div>
  )
}

function StandardSpritePreview({ imageUrl, onEnable9Slice }: {
  imageUrl: string
  onEnable9Slice: () => void
}) {
  const [imgSize, setImgSize] = useState<{ w: number; h: number } | null>(null)

  useEffect(() => {
    const img = new Image()
    img.onload = () => setImgSize({ w: img.naturalWidth, h: img.naturalHeight })
    img.src = imageUrl
  }, [imageUrl])

  const aspectStyle = imgSize ? `${imgSize.w} / ${imgSize.h}` : '4 / 3'

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600 }}>
          Sprite Preview {imgSize && <span style={{ fontSize: 11, color: 'var(--muted)', fontWeight: 400 }}>({imgSize.w}×{imgSize.h}px)</span>}
        </div>
        <div
          onClick={onEnable9Slice}
          style={{
            fontSize: 10.5, fontWeight: 600, color: 'var(--accent)',
            border: '1px solid #2a3a5c', background: 'rgba(108,140,255,0.08)',
            borderRadius: 5, padding: '4px 8px', cursor: 'pointer',
          }}
        >
          + Enable 9-Slice
        </div>
      </div>

      <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 12 }}>
        <div
          className="checkerboard"
          style={{
            position: 'relative', width: '100%', aspectRatio: aspectStyle, maxHeight: 380, borderRadius: 8,
            overflow: 'hidden',
          }}
        >
          <img src={imageUrl} alt="" style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }} />
        </div>
      </div>

      <div style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 1.5 }}>
        Standard sprite (no 9-slice borders). Unity will import this as a Single Sprite.
      </div>
    </div>
  )
}

export default function AssetDetailPage() {
  const { assetId } = useParams()
  const navigate = useNavigate()
  const { status, project, generationStore, pushGenerationEvent, startGenerationRun, endGenerationRun, loadGenerationHistory } = useApp()
  const [asset, setAsset] = useState<Asset | null>(null)
  const [atlases, setAtlases] = useState<Atlas[]>([])
  const [sections, setSections] = useState<{ style: string; rules: string; aspect: string; resolution: string; user: string } | null>(null)
  const [estimatedTokens, setEstimatedTokens] = useState<{ input: number; output: number; thinking: number; total: number } | null>(null)
  const [costEstimate, setCostEstimate] = useState<{ credits: number | null; loading: boolean }>({ credits: null, loading: false })
  // Bumped whenever the model's per-model params are changed and saved. The estimate is
  // computed server-side from the SAVED params, so without this the pill keeps showing the
  // old price — verified live: switching gpt_image_2 to `quality: high` left "~0.75
  // credits" on screen for a call that now costs 7.
  const [paramsRev, setParamsRev] = useState(0)
  const [prompt, setPrompt] = useState('')
  const [aspectRatio, setAspectRatio] = useState('')
  const [resolution, setResolution] = useState('')
  const [overrideEntirePrompt, setOverrideEntirePrompt] = useState(false)
  const [promptMode, setPromptMode] = useState<PromptMode>('generate')
  const [referenceOps, setReferenceOps] = useState<string[]>([])
  const [opCatalogue, setOpCatalogue] = useState<ReferenceOp[]>([])
  const [opGroups, setOpGroups] = useState<ReferenceOpGroup[]>([])
  // Which ticked ops make this an extraction ("keep only X") — the model warning below
  // only applies to those, never to a plain reference edit.
  const [extractionKeys, setExtractionKeys] = useState<string[]>([])
  // Picker position per group while its toggle is OFF — nothing is ticked yet, so there is
  // no op key to read it back from, and without this the dropdown snaps back to the first
  // choice the moment you untick. Deliberately not persisted: the saved state is the op
  // key itself, and a remembered-but-inactive choice would be invisible state on reload.
  const [groupChoice, setGroupChoice] = useState<Record<string, string>>({})
  const opsRef = useRef<string[]>([])   // latest ticked ops, immune to stale closures
  const opsSeq = useRef(0)              // drops responses superseded by a newer click
  const [provider, setProvider] = useState('higgsfield')
  const [imageModel, setImageModel] = useState('')
  const [visualModel, setVisualModel] = useState('')
  const imageProviders = useImageProviders()
  const currentImageProviderInfo = imageProviders.find((p) => p.name === provider)
  const effectiveImageModel = imageModel || currentImageProviderInfo?.selected_model || currentImageProviderInfo?.default_model || ''
  // A "keep only X" tick (text_only / element_only) is an extraction, not a normal
  // reference edit, and only nano_banana_pro was measured to actually remove leftover
  // background at it — every wording tried on gpt_image_2 left it behind. Warn rather
  // than silently switch, since the pin still applies automatically when no model is
  // explicitly selected; this just makes a visible mismatch visible before you generate.
  const isExtracting = promptMode === 'reference' && referenceOps.some((k) => extractionKeys.includes(k))
  const extractionModel = currentImageProviderInfo?.extraction_model
  const offPinExtraction = isExtracting && !!extractionModel && effectiveImageModel !== extractionModel
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [refining, setRefining] = useState(false)
  const [llm, setLlm] = useState<LlmSelection>({})
  const [showLlm, setShowLlm] = useState(false)
  const [copyState, setCopyState] = useState<'idle' | 'copying' | 'copied'>('idle')
  const [uploading, setUploading] = useState(false)
  const [upscaling, setUpscaling] = useState(false)
  const [downscaling, setDownscaling] = useState(false)
  const [editing, setEditing] = useState(false)
  const [progress, setProgress] = useState<ProgressItem[]>([])
  const [editingName, setEditingName] = useState(false)
  const [nameDraft, setNameDraft] = useState('')
  const nsTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const uploadRef = useRef<HTMLInputElement>(null)
  const refFileRef = useRef<HTMLInputElement>(null)
  const abortRef = useRef<AbortController | null>(null)

  const id = Number(assetId)

  const handleUploadRef = async (file: File) => {
    if (!asset) return
    setError('')
    try {
      const updated = await api.uploadAssetReference(asset.id, file)
      setAsset(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Reference upload failed')
    }
  }

  const handleRemoveRef = async (relPath: string) => {
    if (!asset) return
    setError('')
    try {
      const updated = await api.removeAssetReference(asset.id, relPath)
      setAsset(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to remove reference')
    }
  }

  const toggleVersionRef = async (imgPath: string) => {
    if (!asset || !imgPath) return
    setError('')
    try {
      const currentRefs = asset.reference_images || []
      const isRef = currentRefs.includes(imgPath)
      const updatedRefs = isRef
        ? currentRefs.filter((p) => p !== imgPath)
        : [...currentRefs, imgPath]
      const updated = await api.updateAsset(asset.id, { reference_images: updatedRefs })
      setAsset(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update reference image')
    }
  }

  const stopGeneration = () => {
    if (abortRef.current) {
      abortRef.current.abort()
      abortRef.current = null
    }
    setBusy(false)
    const cancelEv: ProgressEvent = { step: 'cancel', status: 'error', message: 'Generation stopped by user.' }
    pushProgress(cancelEv)
    if (id) pushGenerationEvent(`asset-${id}`, cancelEv)
    if (id) endGenerationRun(`asset-${id}`)
  }

  const toggleOverrideEntirePrompt = async (val: boolean) => {
    setOverrideEntirePrompt(val)
    if (!asset) return
    try {
      const updated = await api.updateAsset(asset.id, { override_entire_prompt: val })
      setAsset(updated)
      const c = await api.composedPrompt(asset.id, val)
      setSections(c.sections)
      setEstimatedTokens(c.estimated_tokens)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to update prompt override mode')
    }
  }

  /** Persist a mode/op change and refresh the read-only prompt preview from the server,
   *  so what the user sees is literally what generation will send.
   *
   *  Ticking chips is a rapid-fire action, so neither the selection nor the preview may
   *  depend on React having re-rendered in between: `opsRef` carries the authoritative
   *  list forward (a render-closure read drops every toggle but the last), and `opsSeq`
   *  discards the response of any request a newer click has already superseded. */
  const applyPromptMode = async (mode: PromptMode, ops: string[]) => {
    opsRef.current = ops
    const seq = ++opsSeq.current
    setPromptMode(mode)
    setReferenceOps(ops)
    if (!asset) return
    try {
      const updated = await api.updateAsset(asset.id, { prompt_mode: mode, reference_ops: ops })
      const c = await api.composedPrompt(asset.id, overrideEntirePrompt, mode, ops)
      if (seq !== opsSeq.current) return
      setAsset(updated)
      setSections(c.sections)
      setEstimatedTokens(c.estimated_tokens)
    } catch (e) {
      if (seq === opsSeq.current) setError(e instanceof Error ? e.message : 'Failed to update prompt mode')
    }
  }

  const toggleReferenceOp = (key: string) => {
    const current = opsRef.current
    const next = current.includes(key)
      ? current.filter((k) => k !== key)
      : [...current, key]
    applyPromptMode('reference', next)
  }

  /** Which member of a group is currently ticked, if any. */
  const activeGroupOp = (group: ReferenceOpGroup) =>
    group.choices.find((c) => referenceOps.includes(c.key))

  /** What the group's picker shows: the ticked member, else the last one picked while
   *  the toggle was off, else the group's first choice. */
  const groupChoiceKey = (group: ReferenceOpGroup) =>
    activeGroupOp(group)?.key ?? groupChoice[group.key] ?? group.choices[0].key

  const toggleReferenceGroup = (group: ReferenceOpGroup) => {
    const current = opsRef.current
    const keys = group.choices.map((c) => c.key)
    const next = keys.some((k) => current.includes(k))
      ? current.filter((k) => !keys.includes(k))
      : [...current, groupChoiceKey(group)]
    applyPromptMode('reference', next)
  }

  const pickReferenceGroupChoice = (group: ReferenceOpGroup, key: string) => {
    setGroupChoice((g) => ({ ...g, [group.key]: key }))
    const current = opsRef.current
    const keys = group.choices.map((c) => c.key)
    // Picking while the toggle is off only arms the dropdown — it must not silently tick
    // the op, since that would spend a generation on an operation nobody switched on.
    if (!keys.some((k) => current.includes(k))) return
    const others = group.exclusive ? current.filter((k) => !keys.includes(k)) : current
    applyPromptMode('reference', [...others, key])
  }

  useEffect(() => {
    if (!id) return
    const entityKey = `asset-${id}`
    loadGenerationHistory(entityKey, id, 'asset').catch(() => {})
    api.getAsset(id).then((a) => {
      setAsset(a)
      setPrompt(a.prompt)
      setAspectRatio(a.aspect_ratio ?? '')
      setResolution(a.resolution ?? '')
      setOverrideEntirePrompt(a.override_entire_prompt ?? false)
      const mode = a.prompt_mode ?? 'generate'
      const ops = a.reference_ops ?? []
      setPromptMode(mode)
      setReferenceOps(ops)
      opsRef.current = ops
      api.composedPrompt(id, a.override_entire_prompt ?? false, mode, ops)
        .then((c) => { setSections(c.sections); setEstimatedTokens(c.estimated_tokens) }).catch(() => {})
    }).catch(() => setAsset(null))
    api.llmSettings().then(setLlm).catch(() => {})
    api.referenceOps().then((r) => {
      setOpCatalogue(r.ops); setOpGroups(r.groups ?? []); setExtractionKeys(r.extraction_keys ?? [])
    }).catch(() => {})
  }, [id, loadGenerationHistory])

  useEffect(() => {
    if (!project) return
    api.listAtlases(project.id).then(setAtlases).catch(() => setAtlases([]))
  }, [project?.id])

  useEffect(() => {
    api.providers().then((p) => setProvider(p.settings.default_image_provider || 'higgsfield')).catch(() => {})
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // Whether the selected model is unlimited on the account's higgsfield.ai
  // *website*. Per Higgsfield's own docs ("Unlimited only applies on the main
  // website with that toggle on... not included on MCP, CLI...") this NEVER
  // applies to Generate here — this app only ever talks to Higgsfield through the
  // CLI, so every generation spends real credits regardless. This is shown as
  // context only ("you could get this one free on the website instead"); the cost
  // estimate below always reflects what hitting Generate *here* will actually cost.
  const webUnlimitedModels = currentImageProviderInfo?.web_unlimited_models
  const isWebUnlimited = provider === 'higgsfield' && !!effectiveImageModel && (
    webUnlimitedModels === true ||
    (Array.isArray(webUnlimitedModels) && webUnlimitedModels.includes(effectiveImageModel))
  )

  // Higgsfield bills per request — a real credit cost (from its own `generate cost`,
  // no job created) is worth previewing before Generate. Antigravity is a flat
  // subscription with nothing to estimate, so this stays idle for it. Debounced since
  // it's a live CLI round-trip, not a local calculation.
  useEffect(() => {
    if (!asset || provider !== 'higgsfield') {
      setCostEstimate({ credits: null, loading: false })
      return
    }
    let cancelled = false
    setCostEstimate((prev) => ({ ...prev, loading: true }))
    const t = setTimeout(() => {
      api.estimateCost(asset.id, provider, effectiveImageModel || undefined, overrideEntirePrompt, promptMode, referenceOps)
        .then((r) => { if (!cancelled) setCostEstimate({ credits: r.supported ? r.credits : null, loading: false }) })
        .catch(() => { if (!cancelled) setCostEstimate({ credits: null, loading: false }) })
    }, 400)
    return () => { cancelled = true; clearTimeout(t) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [asset?.id, provider, effectiveImageModel, overrideEntirePrompt, promptMode, referenceOps, paramsRev])

  if (!asset) {
    return <div style={{ color: 'var(--muted)' }}>Asset not found.</div>
  }

  const assetAtlas = atlases.find((a) => a.id === asset.atlas_id)
  const exportFolder = assetAtlas
    ? `${resolveAtlasParent(assetAtlas)}/${assetAtlas.name.replace(/\s+/g, '')}`
    : resolveDomainFolder(asset.type)
  const exportPath = `Assets/${exportFolder}/${asset.name.replace(/\s+/g, '')}.png`
  const selected = asset.versions.find((v) => v.id === asset.selected_version_id)
  const previewImg = selected ? selected.processed_path || selected.raw_path : ''

  const pushProgress = (e: ProgressEvent) => setProgress((prev) => {
    const item: ProgressItem = {
      step: e.step, status: e.status, message: e.message,
      image: e.image, index: e.index, total: e.total, data: e.data, timestamp: e.timestamp,
    }
    const last = prev[prev.length - 1]
    if (last && last.step === e.step && last.status === 'running') return [...prev.slice(0, -1), item]
    return [...prev, item]
  })

  const generate = async () => {
    const controller = new AbortController()
    abortRef.current = controller
    setBusy(true)
    setError('')
    setProgress([])
    const entityKey = `asset-${asset.id}`
    const runId = startGenerationRun(entityKey)

    try {
      await api.updateAsset(asset.id, {
        aspect_ratio: aspectRatio.trim() || null,
        resolution: resolution.trim() || null,
      })
      await api.generateStream(
        asset.id,
        {
          provider, model: imageModel || undefined, visual_model: visualModel || undefined,
          prompt, override_entire_prompt: overrideEntirePrompt,
          prompt_mode: promptMode, reference_ops: referenceOps,
        },
        (e) => {
          pushProgress(e)
          pushGenerationEvent(entityKey, e, runId)
        },
        controller.signal,
      )
      const updated = await api.getAsset(asset.id)
      setAsset(updated)
      api.composedPrompt(asset.id, overrideEntirePrompt)
        .then((c) => { setSections(c.sections); setEstimatedTokens(c.estimated_tokens) }).catch(() => {})
    } catch (e) {
      if (e instanceof Error && e.name === 'AbortError') return
      setError(e instanceof Error ? e.message : 'Generation failed')
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      setBusy(false)
      endGenerationRun(entityKey, runId)
    }
  }

  const refine = async () => {
    setRefining(true)
    setError('')
    try {
      // persist the current prompt first so the LLM refines what's on screen
      await api.updateAsset(asset.id, { prompt })
      const { prompt: suggestion } = await api.refinePrompt(asset.id, llm)
      if (suggestion) setPrompt(suggestion)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Refine failed')
    } finally {
      setRefining(false)
    }
  }

  const copyPrompt = async () => {
    setCopyState('copying')
    setError('')
    try {
      await api.updateAsset(asset.id, {
        prompt,
        aspect_ratio: aspectRatio.trim() || null,
        resolution: resolution.trim() || null,
        override_entire_prompt: overrideEntirePrompt,
      })
      const { external } = await api.composedPrompt(asset.id, overrideEntirePrompt, promptMode, referenceOps)
      await navigator.clipboard.writeText(external)
      setCopyState('copied')
      setTimeout(() => setCopyState('idle'), 1500)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Copy failed')
      setCopyState('idle')
    }
  }

  const uploadVersion = async (file: File) => {
    setUploading(true)
    setError('')
    try {
      setAsset(await api.uploadAssetVersion(asset.id, file))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upload failed')
    } finally {
      setUploading(false)
    }
  }

  const upscale = async () => {
    setUpscaling(true)
    setError('')
    try {
      const updated = await api.upscaleAsset(asset.id)
      setAsset(updated)
      setResolution(updated.resolution ?? '')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Upscale failed')
    } finally {
      setUpscaling(false)
    }
  }

  const downscale = async () => {
    setDownscaling(true)
    setError('')
    try {
      const updated = await api.downscaleAsset(asset.id)
      setAsset(updated)
      setResolution(updated.resolution ?? '')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Downscale failed')
    } finally {
      setDownscaling(false)
    }
  }

  const selectVersion = async (versionId: number) => {
    const updated = await api.updateAsset(asset.id, { selected_version_id: versionId })
    setAsset(updated)
  }

  const revealVersion = async (versionId: number) => {
    setError('')
    try {
      await api.revealAssetVersion(asset.id, versionId)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to open folder')
    }
  }

  const deleteVersion = async (versionId: number) => {
    if (!window.confirm('Delete this version? This cannot be undone.')) return
    setError('')
    try {
      const updated = await api.deleteAssetVersion(asset.id, versionId)
      setAsset(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to delete version')
    }
  }

  const saveNineSlice = (ns: NineSlice | null) => {
    setAsset((a) => (a ? { ...a, nine_slice: ns } : a))
    if (nsTimer.current) clearTimeout(nsTimer.current)
    nsTimer.current = setTimeout(() => {
      api.updateAsset(asset.id, { nine_slice: ns }).catch(() => {})
    }, 400)
  }

  const disableNineSlice = () => {
    saveNineSlice(null)
  }

  const autoDetect = async () => {
    setError('')
    try {
      const { nine_slice } = await api.detectNineSlice(asset.id)
      setAsset((a) => (a ? { ...a, nine_slice } : a))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Auto-detect failed')
    }
  }

  const enableNineSlice = () => {
    autoDetect()
  }

  const trim = async () => {
    setError('')
    try {
      setAsset(await api.trimAsset(asset.id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Trim failed')
    }
  }

  const startRename = () => {
    if (!asset) return
    setNameDraft(asset.name)
    setEditingName(true)
  }

  const commitRename = async () => {
    if (!asset) return
    setEditingName(false)
    const trimmed = nameDraft.trim()
    if (!trimmed || trimmed === asset.name) return
    setError('')
    try {
      const updated = await api.updateAsset(asset.id, { name: trimmed })
      setAsset(updated)
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to rename asset')
    }
  }

  const deleteAsset = async () => {
    if (!asset) return
    if (!window.confirm(`Are you sure you want to delete asset "${asset.name}"?`)) return
    try {
      await api.deleteAsset(asset.id)
      navigate('/assets')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Delete failed')
    }
  }

  return (
    <>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 20 }}>
        <div
          onClick={() => navigate('/assets')}
          title="Back to assets overview"
          style={{
            width: 30, height: 30, borderRadius: 7, border: '1px solid var(--border)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', cursor: 'pointer',
          }}
        >
          <svg width="14" height="14" viewBox="0 0 14 14" fill="none" stroke="#c3c9d1" strokeWidth="1.5">
            <polyline points="8.5,2 3.5,7 8.5,12" />
          </svg>
        </div>
        {editingName ? (
          <input
            autoFocus
            value={nameDraft}
            onChange={(e) => setNameDraft(e.target.value)}
            onBlur={commitRename}
            onKeyDown={(e) => {
              if (e.key === 'Enter') (e.target as HTMLInputElement).blur()
              if (e.key === 'Escape') setEditingName(false)
            }}
            style={{
              fontSize: 18, fontWeight: 600, background: 'var(--input)',
              border: '1px solid var(--border-2)', borderRadius: 6, padding: '3px 8px',
              outline: 'none', color: 'inherit', minWidth: 120,
            }}
          />
        ) : (
          <h1
            title="Click to rename"
            onClick={startRename}
            style={{ fontSize: 18, cursor: 'pointer' }}
          >
            {asset.name}
          </h1>
        )}
        <div
          style={{
            display: 'flex', alignItems: 'center', gap: 5, background: '#1e232a',
            border: '1px solid var(--border)', borderRadius: 5, padding: '4px 9px',
            fontSize: 11, fontWeight: 600,
          }}
        >
          <span style={{ width: 6, height: 6, borderRadius: '50%', background: TYPE_DOT[asset.type] }} />
          {TYPE_LABEL[asset.type]}
        </div>
        <div className="mono" style={{ fontSize: 11.5, color: 'var(--muted-2)' }}>{exportPath}</div>

        {asset.type !== 'tile' && asset.type !== 'sprite_sheet' && (
          <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginLeft: 'auto' }}>
            <span style={{ fontSize: 11, fontWeight: 600, color: 'var(--muted)' }}>Slicing:</span>
            <div className="seg-row" style={{ display: 'inline-flex' }}>
              <div
                className={`seg${asset.nine_slice !== null ? ' active' : ''}`}
                onClick={enableNineSlice}
                style={{ padding: '3px 9px', fontSize: 10.5 }}
                title="Enable 9-slice resizable borders for UI scaling"
              >
                ✂ Sliced (9-Slice)
              </div>
              <div
                className={`seg${asset.nine_slice === null ? ' active' : ''}`}
                onClick={disableNineSlice}
                style={{ padding: '3px 9px', fontSize: 10.5 }}
                title="Disable 9-slice borders and export as a single sprite"
              >
                🖼 Non-Sliced (Single)
              </div>
            </div>
          </div>
        )}

        <button
          className="btn btn-danger"
          onClick={deleteAsset}
          style={{ padding: '4px 10px', fontSize: 11, marginLeft: asset.type === 'tile' || asset.type === 'sprite_sheet' ? 'auto' : 4 }}
          title="Delete this asset"
        >
          🗑 Delete Asset
        </button>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: 20, alignItems: 'start' }}>
        {/* Composed Prompt */}
        <div className="card" style={{ padding: 20, display: 'flex', flexDirection: 'column', gap: 14 }}>
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
            <div style={{ fontSize: 12.5, fontWeight: 600 }}>Composed Prompt</div>
            <div style={{ display: 'flex', border: '1px solid var(--border-2)', borderRadius: 6, overflow: 'hidden' }}>
              {([
                ['generate', '✎ Generate', 'Free-text prompt — invent the asset from a description.'],
                ['reference', '⧉ From reference', 'Reproduce the reference image faithfully, changing only the operations you tick below.'],
              ] as const).map(([mode, label, tip]) => (
                <div
                  key={mode}
                  onClick={() => promptMode !== mode && applyPromptMode(mode, referenceOps)}
                  title={tip}
                  style={{
                    fontSize: 10.5, fontWeight: 600, padding: '4px 9px', cursor: 'pointer',
                    background: promptMode === mode ? 'rgba(108,140,255,0.16)' : 'transparent',
                    color: promptMode === mode ? 'var(--accent)' : 'var(--muted)',
                  }}
                >
                  {label}
                </div>
              ))}
            </div>
            <label
              style={{
                display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, fontWeight: 600,
                color: overrideEntirePrompt ? '#f5a623' : 'var(--muted)', cursor: 'pointer', userSelect: 'none',
                background: overrideEntirePrompt ? 'rgba(245, 166, 35, 0.1)' : 'transparent',
                border: `1px solid ${overrideEntirePrompt ? 'rgba(245, 166, 35, 0.4)' : 'var(--border)'}`,
                borderRadius: 5, padding: '3px 8px', transition: 'all 0.15s ease',
              }}
              title="When enabled, project art style, color palette, asset type rules, aspect ratio and resolution instructions are bypassed for generation."
            >
              <input
                type="checkbox"
                checked={overrideEntirePrompt}
                onChange={(e) => toggleOverrideEntirePrompt(e.target.checked)}
                style={{ cursor: 'pointer', accentColor: '#f5a623' }}
              />
              <span>⚡ Override entire prompt</span>
            </label>
          </div>
          <div
            className="mono"
            style={{
              border: `1px solid ${overrideEntirePrompt ? 'rgba(245, 166, 35, 0.4)' : 'var(--border-2)'}`,
              background: 'var(--input)', borderRadius: 8,
              padding: '12px 13px', fontSize: 11, lineHeight: 1.7, maxHeight: 200, overflowY: 'auto',
            }}
          >
            {overrideEntirePrompt ? (
              <div>
                <div style={{ color: '#f5a623', fontWeight: 600, marginBottom: 6 }}>
                  {'// ⚡ OVERRIDDEN: Project art style, palette, type rules, aspect & resolution instructions are bypassed.'}
                </div>
                <div style={{ color: 'var(--muted-2)' }}>{'// raw prompt sent to image model:'}</div>
                <div style={{ color: '#dfe2e6', whiteSpace: 'pre-wrap', marginTop: 4 }}>{prompt || '(empty)'}</div>
              </div>
            ) : promptMode === 'reference' ? (
              <>
                <div style={{ color: 'var(--muted-2)' }}>{'// mode'}</div>
                <div style={{ color: 'var(--accent)', marginBottom: 8 }}>
                  Reference reproduction — art style &amp; palette intentionally omitted so the
                  model copies rather than restyles.
                </div>
                <div style={{ color: 'var(--muted-2)' }}>{`// type rules — ${TYPE_LABEL[asset.type]}`}</div>
                <div style={{ color: '#8b93a0', marginBottom: 8 }}>{sections?.rules}</div>
                <div style={{ color: 'var(--muted-2)' }}>{'// aspect ratio'}</div>
                <div style={{ color: aspectRatio ? '#8b93a0' : 'var(--muted-2)', marginBottom: 8 }}>
                  {aspectRatio ? `${aspectRatio} (width:height)` : '(not set)'}
                </div>
                <div style={{ color: 'var(--muted-2)' }}>{'// reference instructions (from toggles)'}</div>
                <div style={{ color: '#dfe2e6', whiteSpace: 'pre-wrap' }}>{sections?.user}</div>
              </>
            ) : (
              <>
                <div style={{ color: 'var(--muted-2)' }}>{'// art style'}</div>
                <div style={{ color: '#8b93a0', marginBottom: 8 }}>{sections?.style || '(no art style defined)'}</div>
                <div style={{ color: 'var(--muted-2)' }}>{`// type rules — ${TYPE_LABEL[asset.type]}`}</div>
                <div style={{ color: '#8b93a0', marginBottom: 8 }}>{sections?.rules}</div>
                <div style={{ color: 'var(--muted-2)' }}>{'// aspect ratio'}</div>
                <div style={{ color: aspectRatio ? '#8b93a0' : 'var(--muted-2)', marginBottom: 8, fontStyle: aspectRatio ? 'normal' : 'italic' }}>
                  {aspectRatio ? `${aspectRatio} (width:height)` : '(not set — model may default to the wrong shape)'}
                </div>
                <div style={{ color: 'var(--muted-2)' }}>{'// target resolution'}</div>
                <div style={{ color: resolution ? '#8b93a0' : 'var(--muted-2)', marginBottom: 8, fontStyle: resolution ? 'normal' : 'italic' }}>
                  {resolution ? `${resolution}px, enforced after generation` : '(not set)'}
                </div>
                <div style={{ color: 'var(--muted-2)' }}>{'// user prompt'}</div>
                <div style={{ color: '#dfe2e6' }}>{prompt || '(empty)'}</div>
              </>
            )}
          </div>

          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            <div className="field-label" style={{ marginBottom: 0, whiteSpace: 'nowrap' }}>Aspect ratio</div>
            <input
              className="input"
              value={aspectRatio}
              onChange={(e) => setAspectRatio(e.target.value)}
              onBlur={() => api.updateAsset(asset.id, { aspect_ratio: aspectRatio.trim() || null }).catch(() => {})}
              placeholder="1:1, 3.7:1, 16:9…"
              style={{ padding: '5px 9px', fontSize: 12, width: 110 }}
            />
            <div className="field-label" style={{ marginBottom: 0, whiteSpace: 'nowrap' }}>Resolution</div>
            <input
              className="input"
              value={resolution}
              onChange={(e) => setResolution(e.target.value)}
              onBlur={() => api.updateAsset(asset.id, { resolution: resolution.trim() || null }).catch(() => {})}
              placeholder="256x256"
              style={{ padding: '5px 9px', fontSize: 12, width: 100 }}
            />
          </div>
          <div style={{ fontSize: 10.5, color: 'var(--muted-2)', marginTop: -8 }}>
            Aspect ratio specifies shape; resolution is calculated per asset by the LLM (or custom set) and enforced after generation. Click Upscale (2x) or Downscale (0.5x) to resize cleanly from the raw source image.
          </div>

          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
            <div className="field-label" style={{ marginBottom: 0 }}>
              {promptMode === 'reference' ? 'What to change' : 'Your prompt'}
            </div>
            <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
              {promptMode === 'generate' && (
                <div
                  onClick={() => setShowLlm((s) => !s)}
                  title="Choose which LLM CLI refines the prompt"
                  style={{
                    fontSize: 10.5, fontWeight: 600, color: 'var(--muted)', cursor: 'pointer',
                    border: '1px solid var(--border-2)', borderRadius: 5, padding: '4px 8px',
                  }}
                >
                  {showLlm ? 'Hide LLM' : `LLM: ${llm.provider ?? 'claude'}`}
                </div>
              )}
              {promptMode === 'generate' && (
                <div
                  onClick={refining ? undefined : refine}
                  style={{
                    fontSize: 10.5, fontWeight: 600, color: 'var(--accent)',
                    border: '1px solid #2a3a5c', background: 'rgba(108,140,255,0.08)',
                    borderRadius: 5, padding: '4px 8px', cursor: 'pointer',
                    opacity: refining ? 0.6 : 1,
                  }}
                >
                  {refining ? 'Refining…' : '✨ Refine'}
                </div>
              )}
              <div
                onClick={copyState === 'copying' ? undefined : copyPrompt}
                title="Copy the full prompt (style + rules + magenta chroma-key instruction) to paste into your own LLM"
                style={{
                  fontSize: 10.5, fontWeight: 600, color: copyState === 'copied' ? 'var(--green)' : 'var(--muted)',
                  border: '1px solid var(--border-2)', borderRadius: 5, padding: '4px 8px', cursor: 'pointer',
                  opacity: copyState === 'copying' ? 0.6 : 1,
                }}
              >
                {copyState === 'copied' ? '✓ Copied' : copyState === 'copying' ? 'Copying…' : '⧉ Copy Prompt'}
              </div>
            </div>
          </div>
          {showLlm && promptMode === 'generate' && <LlmChooser compact value={llm} onChange={setLlm} />}
          {promptMode === 'reference' ? (
            <>
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
                {opCatalogue.map((op) => {
                  const on = referenceOps.includes(op.key)
                  return (
                    <div
                      key={op.key}
                      onClick={() => toggleReferenceOp(op.key)}
                      title={op.instruction}
                      style={{
                        fontSize: 11, fontWeight: 600, padding: '5px 10px', borderRadius: 6,
                        cursor: 'pointer', userSelect: 'none', transition: 'all 0.15s ease',
                        border: `1px solid ${on ? 'rgba(108,140,255,0.55)' : 'var(--border-2)'}`,
                        background: on ? 'rgba(108,140,255,0.14)' : 'transparent',
                        color: on ? 'var(--accent)' : 'var(--muted)',
                      }}
                    >
                      {on ? '✓ ' : ''}{op.label}
                    </div>
                  )
                })}
                {opGroups.map((group) => {
                  const active = activeGroupOp(group)
                  const on = !!active
                  const choiceKey = groupChoiceKey(group)
                  return (
                    <div
                      key={group.key}
                      onClick={() => toggleReferenceGroup(group)}
                      title={on ? (active as ReferenceOp).instruction : group.help}
                      style={{
                        display: 'flex', alignItems: 'center', gap: 6,
                        fontSize: 11, fontWeight: 600, padding: '5px 6px 5px 10px', borderRadius: 6,
                        cursor: 'pointer', userSelect: 'none', transition: 'all 0.15s ease',
                        border: `1px solid ${on ? 'rgba(108,140,255,0.55)' : 'var(--border-2)'}`,
                        background: on ? 'rgba(108,140,255,0.14)' : 'transparent',
                        color: on ? 'var(--accent)' : 'var(--muted)',
                      }}
                    >
                      <span>{on ? '✓ ' : ''}{group.label}</span>
                      <select
                        value={choiceKey}
                        // The picker sits inside the chip, so without this every choice
                        // would also toggle the chip off.
                        onClick={(e) => e.stopPropagation()}
                        onChange={(e) => pickReferenceGroupChoice(group, e.target.value)}
                        title={group.choices.find((c) => c.key === choiceKey)?.instruction}
                        style={{
                          fontSize: 10.5, fontWeight: 600, padding: '2px 4px', borderRadius: 4,
                          background: 'var(--input)', color: on ? 'var(--accent)' : 'var(--muted)',
                          border: '1px solid var(--border-2)', cursor: 'pointer', outline: 'none',
                        }}
                      >
                        {group.choices.map((c) => (
                          <option key={c.key} value={c.key}>{c.label}</option>
                        ))}
                      </select>
                    </div>
                  )
                })}
              </div>
              <div style={{ fontSize: 10.5, color: 'var(--muted-2)' }}>
                {asset.reference_images?.length
                  ? 'The prompt above is composed from these toggles and cannot be typed into — it reproduces the reference image, changing only what you tick.'
                  : '⚠ No reference image attached. Add one below, otherwise there is nothing to reproduce.'}
              </div>
            </>
          ) : (
            <textarea
              className="textarea"
              rows={3}
              value={prompt}
              onChange={(e) => setPrompt(e.target.value)}
              placeholder="Rounded rectangular button, carved stone texture with a faint mossy-green glow along the top edge."
            />
          )}

          {/* Reference Images section */}
          <div style={{ marginTop: 4 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <div className="field-label" style={{ marginBottom: 0 }}>Reference Images ({asset.reference_images?.length || 0})</div>
              <div style={{ display: 'flex', gap: 6, alignItems: 'center' }}>
                {asset.versions.length > 0 && (
                  <select
                    value=""
                    onChange={(e) => {
                      if (e.target.value) toggleVersionRef(e.target.value)
                    }}
                    style={{
                      fontSize: 11, padding: '3px 6px', background: 'var(--input)', color: 'var(--text)',
                      border: '1px solid var(--border-2)', borderRadius: 5, cursor: 'pointer', outline: 'none',
                    }}
                    title="Add a previous version as a generation reference"
                  >
                    <option value="">+ Version Ref…</option>
                    {asset.versions.map((v, idx) => {
                      const imgPath = v.processed_path || v.raw_path
                      const isRef = asset.reference_images?.includes(imgPath)
                      return (
                        <option key={v.id} value={imgPath}>
                          {isRef ? '✓ ' : ''}Version {idx + 1} ({v.provider})
                        </option>
                      )
                    })}
                  </select>
                )}
                <button
                  className="btn btn-secondary"
                  style={{ fontSize: 11, padding: '3px 9px' }}
                  onClick={() => refFileRef.current?.click()}
                >
                  + Upload Reference
                </button>
              </div>
              <input
                ref={refFileRef}
                type="file"
                accept="image/png,image/jpeg,image/webp"
                style={{ display: 'none' }}
                onChange={async (e) => {
                  const f = e.target.files?.[0]
                  if (f) await handleUploadRef(f)
                  e.target.value = ''
                }}
              />
            </div>
            {asset.reference_images && asset.reference_images.length > 0 ? (
              <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                {asset.reference_images.map((rel) => {
                  const versionIdx = asset.versions.findIndex((v) => (v.processed_path || v.raw_path) === rel)
                  return (
                    <div key={rel} style={{ position: 'relative', width: 56, height: 56 }}>
                      <img
                        src={storageUrl(rel)}
                        alt="ref"
                        style={{ width: '100%', height: '100%', borderRadius: 6, objectFit: 'cover', display: 'block', border: '1px solid var(--border-2)' }}
                      />
                      {versionIdx !== -1 && (
                        <div
                          style={{
                            position: 'absolute', bottom: 2, left: 2, background: 'rgba(108,140,255,0.9)',
                            color: '#fff', fontSize: 9, fontWeight: 700, borderRadius: 3, padding: '1px 4px',
                            pointerEvents: 'none',
                          }}
                          title={`Version ${versionIdx + 1}`}
                        >
                          v{versionIdx + 1}
                        </div>
                      )}
                      <div
                        onClick={(e) => { e.stopPropagation(); handleRemoveRef(rel) }}
                        title="Remove reference"
                        style={{
                          position: 'absolute', top: -5, right: -5, width: 18, height: 18, borderRadius: '50%',
                          background: '#e5484d', color: '#fff', display: 'flex',
                          alignItems: 'center', justifyContent: 'center', cursor: 'pointer', fontSize: 10, fontWeight: 'bold',
                          boxShadow: '0 1px 4px rgba(0,0,0,0.5)',
                        }}
                      >
                        ✕
                      </div>
                    </div>
                  )
                })}
              </div>
            ) : (
              <div style={{ fontSize: 11, color: 'var(--muted-2)', fontStyle: 'italic' }}>
                No reference images attached. Click + Upload Reference or pick a previous version to attach as a reference.
              </div>
            )}
          </div>

          <div className="field-label" style={{ marginBottom: 0, marginTop: 4 }}>Image Provider & Model</div>
          {offPinExtraction && (
            <div style={{
              fontSize: 11, lineHeight: 1.55, padding: '7px 9px', borderRadius: 6,
              color: '#e0c15c', border: '1px solid #5d5423', background: 'rgba(224,193,92,0.10)',
            }}>
              ⚠ '{effectiveImageModel}' hasn't been tested for "keep only" extraction —
              every wording tried on gpt_image_2 left leftover background behind, at every
              quality setting.{' '}
              <span
                onClick={() => { setImageModel(extractionModel!); api.saveProviderSettings({ provider_models: { [provider]: extractionModel! } }).catch(() => {}) }}
                style={{ textDecoration: 'underline', cursor: 'pointer', fontWeight: 600 }}
                title={`Switch to ${extractionModel}`}
              >
                Switch to '{extractionModel}'
              </span>, the only model measured to remove it cleanly.
            </div>
          )}
          <ImageProviderChooser
            provider={provider}
            onChangeProvider={setProvider}
            model={imageModel}
            onChangeModel={setImageModel}
            visualModel={visualModel}
            onChangeVisualModel={setVisualModel}
            onChangeParams={() => setParamsRev((v) => v + 1)}
          />
          <div style={{ fontSize: 11, color: 'var(--muted)', lineHeight: 1.5 }}>
            {PROVIDER_NOTE[provider]?.(effectiveImageModel) ?? ''}
            {status && !status.enabled[provider as keyof typeof status.enabled] && ' — ⚠ disabled in Providers & Settings.'}
            {provider === 'antigravity' && status && status.enabled.antigravity && !status.antigravity.ok && ` — ⚠ ${status.antigravity.detail}`}
            {provider === 'higgsfield' && status && status.enabled.higgsfield && !status.higgsfield.ok && ` — ⚠ ${status.higgsfield.detail}`}
          </div>
          {provider === 'higgsfield' ? (
            <>
              {(costEstimate.loading || costEstimate.credits != null) && (
                <div
                  title="Real credit cost from Higgsfield's own pricing (`generate cost`) for a generation run through this app — no job created, updates as the model or prompt changes."
                  style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: -6 }}
                >
                  {costEstimate.loading
                    ? '🪙 Estimating cost…'
                    : `🪙 ~${costEstimate.credits} credit${costEstimate.credits === 1 ? '' : 's'} to generate here`}
                </div>
              )}
              {isWebUnlimited && (
                <div
                  title="Higgsfield's own docs: Unlimited only applies on higgsfield.ai's website with its toggle on — not the CLI/API this app uses. Generating here still spends the credits shown above; to actually use the free allowance, generate this model on higgsfield.ai directly."
                  style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: -6 }}
                >
                  🌐 This model is unlimited on higgsfield.ai's website — but not through this app
                </div>
              )}
            </>
          ) : (
            estimatedTokens && estimatedTokens.input > 0 && (
              <div
                title="Estimated input tokens for the composed prompt + reference images. Output tokens aren't known until the model responds, so this is a floor, not the final total."
                style={{ fontSize: 10.5, color: 'var(--muted)', marginTop: -6 }}
              >
                🪙 ~{fmtTokens(estimatedTokens.input)} tokens to send
                {asset.reference_images?.length ? ` (incl. ${asset.reference_images.length} reference image${asset.reference_images.length === 1 ? '' : 's'})` : ''}
              </div>
            )
          )}
          {error && (
            <div
              style={{
                color: isQuotaError(error) ? '#f5a623' : '#ff7b7b', fontSize: 12, marginTop: 6,
                display: 'flex', alignItems: 'center', gap: 6,
              }}
            >
              {isQuotaError(error) && <span>⚠</span>}
              {error}
            </div>
          )}
          {busy ? (
            <button
              className="btn"
              onClick={stopGeneration}
              style={{ fontSize: 13, fontWeight: 700, padding: 11, marginTop: 4, background: '#e5484d', color: '#fff', border: 'none' }}
            >
              ⏹ Stop Generation
            </button>
          ) : (
            <button
              className="btn btn-accent"
              onClick={generate}
              style={{ fontSize: 13, fontWeight: 700, padding: 11, marginTop: 4 }}
            >
              ⚡ Generate
            </button>
          )}
          <RunProgress
            runs={generationStore[`asset-${asset.id}`] || []}
            items={progress}
            active={busy}
            onStop={stopGeneration}
          />
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginTop: 2 }}>
            <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
            <span style={{ fontSize: 10.5, color: 'var(--muted-2)' }}>or</span>
            <div style={{ flex: 1, height: 1, background: 'var(--border)' }} />
          </div>
          <button
            className="btn btn-secondary"
            disabled={uploading}
            onClick={() => uploadRef.current?.click()}
            style={{ fontSize: 12, padding: 9 }}
          >
            {uploading ? 'Processing upload…' : '⬆ Upload Image (from Copy Prompt)'}
          </button>
          <input
            ref={uploadRef}
            type="file"
            accept="image/png,image/jpeg,image/webp"
            style={{ display: 'none' }}
            onChange={async (e) => {
              const f = e.target.files?.[0]
              if (f) await uploadVersion(f)
              e.target.value = ''
            }}
          />
          <div style={{ fontSize: 10.5, color: 'var(--muted-2)', lineHeight: 1.5 }}>
            Paste the copied prompt into any LLM/chat UI yourself, then upload the image it returns — it's keyed/trimmed through the same pipeline as an in-tool generation.
          </div>
        </div>

        {/* Versions */}
        <div className="card" style={{ padding: 20 }}>
          <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 14 }}>
            Versions · {asset.versions.length}
          </div>
          {asset.versions.length === 0 && (
            <div style={{ fontSize: 12, color: 'var(--muted)' }}>
              No versions yet — write a prompt and hit Generate.
            </div>
          )}
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {[...asset.versions].reverse().map((v) => {
              const sel = v.id === asset.selected_version_id
              const imgPath = v.processed_path || v.raw_path
              const isRef = asset.reference_images?.includes(imgPath)
              return (
                <div
                  key={v.id}
                  onClick={() => selectVersion(v.id)}
                  style={{
                    display: 'flex', flexDirection: 'column', gap: 6, padding: 9, borderRadius: 9,
                    cursor: 'pointer',
                    border: `1px solid ${sel ? 'var(--accent-border)' : 'transparent'}`,
                    background: sel ? 'rgba(108,140,255,0.08)' : 'transparent',
                  }}
                >
                  <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
                    <div className="checkerboard checkerboard-sm" style={{ position: 'relative', width: 60, height: 60, borderRadius: 7, flexShrink: 0, overflow: 'hidden' }}>
                      <img
                        src={storageUrl(imgPath)}
                        alt=""
                        style={{ width: '100%', height: '100%', objectFit: 'contain', display: 'block' }}
                      />
                    </div>
                    <div style={{ flex: 1, minWidth: 0 }}>
                      <div style={{ fontSize: 12, fontWeight: 600, marginBottom: 3, textTransform: 'capitalize' }}>
                        {v.provider}{v.model ? ` · ${v.model}` : ''}
                      </div>
                      <div style={{ fontSize: 10.5, color: 'var(--muted)' }}>{timeAgo(v.created_at)}</div>
                    </div>
                    <button
                      type="button"
                      onClick={(e) => {
                        e.stopPropagation()
                        toggleVersionRef(imgPath)
                      }}
                      style={{
                        fontSize: 10.5,
                        fontWeight: 600,
                        padding: '3px 8px',
                        borderRadius: 5,
                        cursor: 'pointer',
                        border: isRef ? '1px solid #6c8cff' : '1px solid var(--border-2)',
                        background: isRef ? 'rgba(108,140,255,0.18)' : 'var(--input)',
                        color: isRef ? '#6c8cff' : 'var(--muted)',
                        display: 'inline-flex',
                        alignItems: 'center',
                        gap: 4,
                        flexShrink: 0,
                      }}
                      title={isRef ? 'Remove from generation references' : 'Use this previous version as a reference for generation'}
                    >
                      {isRef ? '✓ Used as Ref' : '+ Use as Ref'}
                    </button>
                    <button
                      type="button"
                      onClick={(e) => { e.stopPropagation(); revealVersion(v.id) }}
                      style={{
                        fontSize: 10.5, fontWeight: 600, padding: '3px 6px', borderRadius: 5,
                        cursor: 'pointer', border: '1px solid var(--border-2)', background: 'var(--input)',
                        color: 'var(--muted)', display: 'inline-flex', alignItems: 'center', flexShrink: 0,
                      }}
                      title="Show this version's file in the folder"
                    >
                      📂
                    </button>
                    {asset.versions.length > 1 && (
                      <button
                        type="button"
                        onClick={(e) => { e.stopPropagation(); deleteVersion(v.id) }}
                        style={{
                          fontSize: 10.5, fontWeight: 600, padding: '3px 6px', borderRadius: 5,
                          cursor: 'pointer', border: '1px solid var(--border-2)', background: 'var(--input)',
                          color: '#ff7b7b', display: 'inline-flex', alignItems: 'center', flexShrink: 0,
                        }}
                        title="Delete this version"
                      >
                        🗑
                      </button>
                    )}
                    <div
                      title={sel ? 'Active version' : 'Click to select as active version'}
                      style={{
                        width: 16, height: 16, borderRadius: '50%', flexShrink: 0,
                        border: `2px solid ${sel ? 'var(--accent)' : 'var(--border-3)'}`,
                        background: sel ? 'var(--accent)' : 'transparent',
                      }}
                    />
                  </div>
                  {/* Prompt & References used info */}
                  <div style={{ fontSize: 10.5, color: 'var(--muted)', background: 'var(--input)', padding: '6px 8px', borderRadius: 6, lineHeight: 1.4 }}>
                    <div style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden', fontStyle: 'italic', marginBottom: v.reference_paths && v.reference_paths.length > 0 ? 4 : 0 }}>
                      {v.composed_prompt ? `"${v.composed_prompt}"` : 'No prompt stored'}
                    </div>
                    {v.reference_paths && v.reference_paths.length > 0 && (
                      <div style={{ display: 'flex', alignItems: 'center', gap: 4, marginTop: 4 }}>
                        <span style={{ fontSize: 9.5, color: 'var(--muted-2)', fontWeight: 600 }}>
                          {referenceLabel(v.reference_paths[0], asset)}:
                        </span>
                        {v.reference_paths.map((rp, i) => (
                          <img
                            key={i}
                            src={storageUrl(rp)}
                            alt={referenceLabel(rp, asset)}
                            title={referenceLabel(rp, asset)}
                            className="checkerboard checkerboard-sm"
                            style={{ width: 20, height: 20, borderRadius: 3, objectFit: 'contain', border: '1px solid var(--border-2)' }}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                </div>
              )
            })}
          </div>
        </div>

        {/* Processing panel */}
        <div className="card" style={{ padding: 20 }}>
          {!previewImg && (
            <>
              <div style={{ fontSize: 12.5, fontWeight: 600, marginBottom: 14 }}>Processing</div>
              <div style={{ fontSize: 12, color: 'var(--muted)' }}>
                Generate a version first — trimming, 9-slice and tiling tools appear here.
              </div>
            </>
          )}
          {previewImg && asset.type === 'tile' && <TilePreview assetId={asset.id} />}
          {previewImg && asset.type === 'sprite_sheet' && (
            <SheetGridPanel asset={asset} imageUrl={storageUrl(previewImg)} onSaved={setAsset} />
          )}
          {previewImg && asset.type !== 'tile' && asset.type !== 'sprite_sheet' && (
            asset.nine_slice !== null ? (
              <NineSliceEditor
                imageUrl={storageUrl(previewImg)}
                value={asset.nine_slice}
                onChange={saveNineSlice}
                onAutoDetect={autoDetect}
                onDisable={disableNineSlice}
              />
            ) : (
              <StandardSpritePreview
                imageUrl={storageUrl(previewImg)}
                onEnable9Slice={enableNineSlice}
              />
            )
          )}
          {previewImg && (
            <div style={{ borderTop: '1px solid var(--border)', marginTop: 16, paddingTop: 14 }}>
              <div style={{ display: 'flex', gap: 8, marginBottom: 10, flexWrap: 'wrap' }}>
                <button
                  className="btn btn-secondary"
                  style={{ padding: '7px 12px', fontSize: 11.5, display: 'inline-flex', alignItems: 'center', gap: 6 }}
                  onClick={trim}
                  title="Auto-crop transparent padding around sprite bounds"
                >
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path d="M4 1v11a1 1 0 0 0 1 1h10" /><path d="M12 15V4a1 1 0 0 0-1-1H1" />
                  </svg>
                  <span>Trim to content</span>
                </button>
                <button
                  className="btn btn-secondary"
                  style={{ padding: '7px 12px', fontSize: 11.5, display: 'inline-flex', alignItems: 'center', gap: 6 }}
                  disabled={downscaling}
                  onClick={downscale}
                  title="Re-derive image downscaled by 0.5x from raw source"
                >
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path d="M8 3v10M4 9l4 4 4-4" />
                  </svg>
                  <span>{downscaling ? 'Downscaling…' : 'Downscale (0.5x)'}</span>
                </button>
                <button
                  className="btn btn-secondary"
                  style={{ padding: '7px 12px', fontSize: 11.5, display: 'inline-flex', alignItems: 'center', gap: 6 }}
                  disabled={upscaling}
                  onClick={upscale}
                  title="Re-derive image upscaled by 2x from raw source"
                >
                  <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
                    <path d="M8 13V3M4 7l4-4 4 4" />
                  </svg>
                  <span>{upscaling ? 'Upscaling…' : 'Upscale (2x)'}</span>
                </button>
                <button
                  className="btn btn-secondary"
                  style={{ padding: '7px 12px', fontSize: 11.5, display: 'inline-flex', alignItems: 'center', gap: 6 }}
                  onClick={() => setEditing(true)}
                  title="Open the image editor — erase, clone, select, move, flip"
                >
                  <span>✏️</span>
                  <span>Edit</span>
                </button>
              </div>
              <div style={{ fontSize: 10.5, color: 'var(--muted-2)', lineHeight: 1.5 }}>
                Upscale (2x) and Downscale (0.5x) re-derive this image from its pristine raw generation — not a blurry resize of the current copy.
              </div>
            </div>
          )}
        </div>
      </div>
      {editing && asset && (
        <ImageEditorModal
          asset={asset}
          onClose={() => setEditing(false)}
          onSaved={(a) => { setAsset(a); setEditing(false) }}
        />
      )}
    </>
  )
}
