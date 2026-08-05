export type AssetType = 'ui_element' | 'icon' | 'sprite' | 'tile' | 'sprite_sheet'
/** 'generate' = free-text prompt; 'reference' = prompt composed from ticked ops. */
export type PromptMode = 'generate' | 'reference'
export type ReferenceOp = { key: string; label: string; instruction: string }
/** Several ops answering the same question ("keep only WHAT?") rendered as one toggle
 *  plus a picker. Members are ordinary op keys — only the presentation is grouped. */
export type ReferenceOpGroup = {
  key: string
  label: string
  help: string
  exclusive: boolean
  choices: ReferenceOp[]
}

export const TYPE_LABEL: Record<AssetType, string> = {
  ui_element: 'UI Element',
  icon: 'Icon',
  sprite: 'Sprite',
  tile: 'Tile',
  sprite_sheet: 'Sprite Sheet',
}

export const TYPE_DOT: Record<AssetType, string> = {
  ui_element: '#6c8cff',
  icon: '#2dd4bf',
  sprite: '#c084fc',
  tile: '#f5a623',
  sprite_sheet: '#f472b6',
}

// Default subfolder (relative to Assets/Sprites/) each type exports into — mirrors the
// server's TYPE_FOLDER_DEFAULTS. 'sprite' is '' because it lands straight in Sprites/
// itself. Not overridable — only a domain's own export path is (see resolveAtlasParent).
const TYPE_FOLDER_DEFAULTS: Record<AssetType, string> = {
  ui_element: 'UI',
  icon: 'Icons',
  sprite: '',
  tile: 'Tiles',
  sprite_sheet: 'SpriteSheets',
}

/** Mirrors the server's unity_export.resolve_domain_folder(): the fixed folder an
 *  unassigned (no domain) asset's type exports into. */
export function resolveDomainFolder(type: AssetType): string {
  const sub = TYPE_FOLDER_DEFAULTS[type]
  return sub ? `Sprites/${sub}` : 'Sprites'
}

export const ASSET_TYPES = Object.keys(TYPE_LABEL) as AssetType[]

export interface Project {
  id: number
  name: string
  style_description: string
  palette: string[]
  reference_images: string[]
  unity_path: string
  ppu: number
  filter_mode: 'point' | 'bilinear'
  wrap_mode: 'clamp' | 'repeat'
  power_of_two: boolean
  created_at: string
  updated_at: string
  asset_count: number
}

export interface AssetVersion {
  id: number
  provider: string
  model: string | null
  composed_prompt: string
  reference_paths?: string[]
  raw_path: string
  processed_path: string
  created_at: string
}

export interface Asset {
  id: number
  project_id: number
  atlas_id: number | null
  name: string
  type: AssetType
  prompt: string
  aspect_ratio: string | null
  resolution: string | null
  nine_slice: { l: number; t: number; r: number; b: number } | null
  sheet_rows: number | null
  sheet_cols: number | null
  ppu_override: number | null
  reference_images?: string[]
  override_entire_prompt?: boolean
  prompt_mode?: PromptMode
  reference_ops?: string[]
  selected_version_id: number | null
  fidelity?: Fidelity | null
  created_at: string
  updated_at: string
  versions: AssetVersion[]
}

/** How close a produced asset is to the reference pixels it came from.
 *  `score` is 0-100 (higher is better); `delta_e` is CIEDE2000 colour distance
 *  (lower is better, <2 is imperceptible). See server/app/processing/fidelity.py. */
export interface Fidelity {
  delta_e: number
  ssim: number
  alpha_iou: number | null
  coverage: number
  score: number
  method?: 'isolated' | 'composite'
}

export interface ScreenFidelity {
  delta_e: number
  ssim: number
  filled: number
  quality: number
  score: number
  missing?: string[]
}

export interface MockupScore {
  mockup_id: number
  regions: {
    region_id: number
    name: string
    asset_id: number | null
    type: AssetType
    fidelity: Fidelity | null
  }[]
  bound: number
  total: number
  mean_score: number | null
  min_score: number | null
  screen?: ScreenFidelity | null
}

export interface Atlas {
  id: number
  project_id: number
  name: string
  parent_id: number | null
  /** Override for where this domain's own-named folder sits, relative to Sprites/
   *  (e.g. "UI" -> Sprites/UI/<Name>). null uses the default parent (Sprites/Atlases).
   *  The leaf folder is always the domain's own name — only its parent is overridable. */
  export_path: string | null
  created_at: string
  updated_at: string
  asset_count: number
}

/** Mirrors the server's unity_export.resolve_atlas_parent(). */
export function resolveAtlasParent(atlas: Atlas): string {
  const raw = atlas.export_path != null ? atlas.export_path : 'Atlases'
  const parts = raw.split(/[\\/]+/).filter((p) => p !== '' && p !== '.' && p !== '..')
  if (parts[0]?.toLowerCase() === 'sprites') parts.shift()
  return parts.length ? `Sprites/${parts.join('/')}` : 'Sprites'
}

export interface Region {
  id: number
  mockup_id: number
  name: string
  x: number
  y: number
  w: number
  h: number
  color: string
  prompt: string
  asset_type: AssetType
  resolution?: string | null
  asset_id: number | null
  icon_asset_id?: number | null
  template?: string | null
  icon_prompt?: string | null
  mirror?: boolean
  source?: string | null
  detect_rect?: number[] | null
  fidelity?: Fidelity | null
}

/** A region a build is about to point at an asset the library already holds, offered for
 *  approval instead of bound on the spot. `match` is how the server found it: `exact` on
 *  name + type, `fuzzy` on one name merely containing the other — which is where a wrong
 *  guess usually comes from, so the UI says which it was. `options` lists the other assets
 *  of the same type in this domain, for pointing the region somewhere else. */
export interface ReuseCandidate {
  region_id: number
  region_name: string
  asset_type: AssetType
  /** The region's own pixels, cut from the screenshot — what the asset has to look like. */
  region_crop: string | null
  asset_id: number
  asset_name: string
  asset_path: string | null
  match: 'exact' | 'fuzzy'
  options: { id: number; name: string; path: string | null }[]
}

/** Where the Text and Polish steps have actually got to on one element.
 *
 *  `polished` / `text_cleaned` mean the step's output exists *for the element as it stands
 *  now* — a polish made before the element was last rebuilt doesn't count, because the
 *  artwork it polished is gone. `missing_sprites` names the Extract captions that have no
 *  sprite yet, which is what a run stopped halfway leaves behind and what re-running will
 *  actually spend money on. */
export interface StepStatusRegion {
  region_id: number
  name: string
  asset_id: number
  built: boolean
  polished: boolean
  /** Any caption on this element is marked Remove or Extract, so the Text step has work here. */
  text_needed: boolean
  /** The element itself has been redrawn without its lettering. */
  text_cleaned: boolean
  /** Cleaned *and* every Extract caption has its sprite — the step is fully done here. */
  text_done: boolean
  captions: {
    label_id: number
    text: string
    mode: 'keep' | 'erase' | 'extract'
    sprite_ready: boolean
    sprite_asset_id: number | null
  }[]
  missing_sprites: string[]
}

export interface StepStatus {
  regions: StepStatusRegion[]
  totals: {
    elements: number
    built: number
    polished: number
    polish_missing: number
    text_needed: number
    text_done: number
    text_missing: number
    sprites_expected: number
    sprites_ready: number
  }
}

/** A run of text sitting on the mockup (a button caption, a currency amount) — see
 *  MockupLabel on the server. Stored as data, not baked into any sprite, so its box is
 *  what its own Keep/Remove/Extract choice actually erases.
 *
 *  `text_mode` is this caption's OWN choice from the Text step — `null`/undefined keeps
 *  it baked into its parent element untouched, `'erase'` lifts it off and discards it,
 *  `'extract'` lifts it off and redraws it as its own sprite. Per caption, not per
 *  element: one element (e.g. a nav bar) commonly carries several independent captions
 *  that don't all want the same fate. */
export interface Label {
  id: number
  mockup_id: number
  name: string
  text: string
  x: number
  y: number
  w: number
  h: number
  color: string
  align: string
  text_mode?: 'erase' | 'extract' | null
}

/** One proposed way to divide an element into sub-assets. Produced by
 *  `proposeSplits`, which mutates nothing — the user approves a subset and posts it
 *  back to `applySplits`, which materialises the children as real regions. */
export interface SplitProposal {
  region_id: number
  region_name: string
  /** frame_icon — a container plus the one glyph on it (a currency pill and its gem).
   *  container  — a bar/panel plus several distinct children (a nav bar and its icons).
   *  repeat     — one box that is really N copies of the same element side by side. */
  kind: 'frame_icon' | 'container' | 'repeat'
  confidence: number
  reason: string
  /** `repeat` children ARE the element, so the parent box is retired in their favour. */
  replace_parent: boolean
  /** The parent already has an asset — applying discards that binding so it is rebuilt
   *  as an empty frame. The old asset stays in the library. */
  rebuilds_parent: boolean
  children: SplitChild[]
}

export interface SplitChild {
  name: string
  asset_type: AssetType
  prompt: string
  x: number
  y: number
  w: number
  h: number
}

export interface Mockup {
  id: number
  project_id: number
  /** User-editable label shown in place of the "Screen N" fallback, and used as the
   *  default Unity screen/prefab name on export. */
  name: string
  image_path: string
  prompt: string
  created_at: string
  regions: Region[]
  labels: Label[]
}

export interface LlmProviderInfo {
  name: string
  label: string
  models: string[]
  default_model: string
  supports_effort: boolean
  efforts: string[]
  supports_context_window: boolean
  supports_vision: boolean
  available: boolean
}

export interface LlmSelection {
  provider?: string
  model?: string
  effort?: string
  context_window?: number | null
}

export interface StatusInfo {
  antigravity: { ok: boolean; detail: string }
  higgsfield: { ok: boolean; detail: string }
  enabled: Record<'antigravity' | 'higgsfield', boolean>
  llm_clis: Record<'claude' | 'antigravity', { ok: boolean; path: string }>
}

export interface ImageProviderInfo {
  name: string
  label: string
  cost: 'free' | 'paid'
  models?: string[]
  default_model?: string
  selected_model?: string
  visual_models?: string[]
  default_visual_model?: string
  selected_visual_model?: string
  how_used: string
  requires: string
  enabled: boolean
  is_default: boolean
  configured: boolean
  detail: string
  active_path?: string
  /** Higgsfield only: model ids unlimited on the account's higgsfield.ai *website*
   *  (with its toggle on) — per Higgsfield's own docs this does NOT extend to the
   *  CLI/API this app uses, so it's account context, never a claim that Generate
   *  here will be free. `true` means the whole plan is unlimited rather than
   *  specific models. */
  web_unlimited_models?: string[] | boolean | null
  credits?: number | string | null
  /** Saved per-model generation knobs for the currently selected model
   *  (e.g. `{ quality: 'low', aspect_ratio: 'auto' }`). */
  selected_params?: Record<string, string> | null
  /** The model measured to work for an extraction ("keep only X") generation on this
   *  provider, or null when nothing has been measured yet. See server EXTRACTION_MODEL —
   *  every other model tried left leftover background behind on this provider's best
   *  wording, so the UI warns when the selected model isn't this one. */
  extraction_model?: string | null
}

/** One tunable knob a model accepts, as declared by the provider itself. Fetched per
 *  model rather than bundled into `/api/providers`, because the option lists come from a
 *  CLI round trip each and there are ~28 image models. */
export interface ModelParamSpec {
  options: string[]
  default?: string | null
}

export interface ProvidersInfo {
  image: ImageProviderInfo[]
  text_llm: LlmProviderInfo[]
  settings: { default_image_provider: string }
}

export interface ProviderSettingsPatch {
  enabled?: Record<string, boolean>
  default_image_provider?: string
  provider_models?: Record<string, string>
  provider_visual_models?: Record<string, string>
  /** provider -> model -> { param: value }. Keyed by model because the accepted set
   *  differs per model, and the CLI rejects a flag a model doesn't declare. */
  provider_params?: Record<string, Record<string, Record<string, string>>>
}

async function request<T>(url: string, init?: RequestInit): Promise<T> {
  const res = await fetch(url, init)
  if (!res.ok) {
    let detail = res.statusText
    try {
      const body = await res.json()
      detail = body.detail ?? detail
    } catch { /* not json */ }
    throw new Error(detail)
  }
  return res.json()
}

const json = (method: string, body: unknown): RequestInit => ({
  method,
  headers: { 'Content-Type': 'application/json' },
  body: JSON.stringify(body),
})

export interface ProgressEvent {
  step: string
  status: 'running' | 'done' | 'error'
  message: string
  image?: string | null   // storage-relative path of an intermediate thumbnail
  index?: number | null
  total?: number | null
  timestamp?: number
  data?: unknown
}

/** POST a request and consume its Server-Sent-Events progress stream. Calls onEvent for
 *  each step event; resolves with the terminal `data` payload, rejects on an error event. */
/** POST a request and consume its Server-Sent-Events progress stream. Calls onEvent for
 *  each step event; resolves with the terminal `data` payload, rejects on an error event. */
export async function streamSSE(
  url: string, body: unknown, onEvent: (e: ProgressEvent) => void, signal?: AbortSignal,
): Promise<unknown> {
  const res = await fetch(url, { ...json('POST', body), signal })
  if (!res.ok || !res.body) {
    let detail = res.statusText
    try { detail = (await res.json()).detail ?? detail } catch { /* not json */ }
    throw new Error(detail)
  }
  const reader = res.body.getReader()
  const decoder = new TextDecoder()
  let buf = ''
  let result: unknown
  for (;;) {
    const { value, done } = await reader.read()
    if (done) break
    buf += decoder.decode(value, { stream: true })
    const frames = buf.split('\n\n')
    buf = frames.pop() ?? ''
    for (const frame of frames) {
      const line = frame.split('\n').find((l) => l.startsWith('data:'))
      if (!line) continue
      const ev = JSON.parse(line.slice(5).trim()) as ProgressEvent
      if (ev.step === '__error__') throw new Error(ev.message || 'Generation failed')
      if (ev.step === '__done__') { result = ev.data; continue }
      onEvent(ev)
    }
  }
  return result
}

export const api = {
  status: () => request<StatusInfo>('/api/status'),

  listProjects: () => request<Project[]>('/api/projects'),
  getProject: (id: number) => request<Project>(`/api/projects/${id}`),
  createProject: (body: { name: string; style_description?: string; palette?: string[] }) =>
    request<Project>('/api/projects', json('POST', body)),
  updateProject: (id: number, body: Partial<Project>) =>
    request<Project>(`/api/projects/${id}`, json('PATCH', body)),
  deleteProject: (id: number) => request<{ ok: boolean }>(`/api/projects/${id}`, { method: 'DELETE' }),
  uploadReference: (id: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Project>(`/api/projects/${id}/references`, { method: 'POST', body: form })
  },
  removeReference: (id: number, path: string) =>
    request<Project>(`/api/projects/${id}/references?path=${encodeURIComponent(path)}`, { method: 'DELETE' }),

  listAssets: (projectId: number) => request<Asset[]>(`/api/projects/${projectId}/assets`),
  getAsset: (id: number) => request<Asset>(`/api/assets/${id}`),
  createAsset: (projectId: number, body: { name: string; type: AssetType; prompt?: string; atlas_id?: number | null; aspect_ratio?: string | null; resolution?: string | null; is_sliced?: boolean; override_entire_prompt?: boolean }) =>
    request<Asset>(`/api/projects/${projectId}/assets`, json('POST', body)),
  updateAsset: (id: number, body: Partial<Asset>) =>
    request<Asset>(`/api/assets/${id}`, json('PATCH', body)),
  deleteAsset: (id: number) => request<{ ok: boolean }>(`/api/assets/${id}`, { method: 'DELETE' }),
  uploadAssetReference: (id: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Asset>(`/api/assets/${id}/references`, { method: 'POST', body: form })
  },
  removeAssetReference: (id: number, path: string) =>
    request<Asset>(`/api/assets/${id}/references?path=${encodeURIComponent(path)}`, { method: 'DELETE' }),

  /** Key the magenta (#FF00FF) background out of a hand-made image and save the result as
   *  a new asset in `atlas_id`. Blank `resolution` keeps the image at its own size. */
  importCutout: (projectId: number, file: File, body: {
    name: string
    type: AssetType
    atlas_id: number | null
    resolution?: string
    is_sliced?: boolean
    trim?: boolean
  }) => {
    const form = new FormData()
    form.append('file', file)
    form.append('name', body.name)
    form.append('type', body.type)
    if (body.atlas_id != null) form.append('atlas_id', String(body.atlas_id))
    if (body.resolution) form.append('resolution', body.resolution)
    if (body.is_sliced !== undefined) form.append('is_sliced', String(body.is_sliced))
    if (body.trim !== undefined) form.append('trim', String(body.trim))
    return request<Asset>(`/api/projects/${projectId}/assets/import-cutout`, { method: 'POST', body: form })
  },
  /** Same keying pass as `importCutout`, but returns the cut-out PNG as a blob URL and
   *  saves nothing — for previewing a drop before committing it. Caller revokes the URL. */
  cutoutPreview: async (file: File, opts: { trim?: boolean; sliced?: boolean } = {}) => {
    const form = new FormData()
    form.append('file', file)
    if (opts.trim !== undefined) form.append('trim', String(opts.trim))
    if (opts.sliced !== undefined) form.append('sliced', String(opts.sliced))
    const res = await fetch('/api/cutout-preview', { method: 'POST', body: form })
    if (!res.ok) {
      let detail = res.statusText
      try { detail = (await res.json()).detail ?? detail } catch { /* not json */ }
      throw new Error(detail)
    }
    return URL.createObjectURL(await res.blob())
  },

  listAtlases: (projectId: number) => request<Atlas[]>(`/api/projects/${projectId}/atlases`),
  createAtlas: (projectId: number, body: { name: string; parent_id?: number | null }) =>
    request<Atlas>(`/api/projects/${projectId}/atlases`, json('POST', body)),
  updateAtlas: (id: number, body: Partial<Atlas>) =>
    request<Atlas>(`/api/atlases/${id}`, json('PATCH', body)),
  browseAtlasExportPath: (id: number) =>
    request<{ path: string | null }>(`/api/atlases/${id}/browse-export-path`, { method: 'POST' }),
  deleteAtlas: (id: number, mode: 'cascade' | 'only' | 'content_only' = 'only') =>
    request<{ ok: boolean }>(`/api/atlases/${id}?mode=${mode}`, { method: 'DELETE' }),
  availableAssets: (atlasId: number) => request<Asset[]>(`/api/atlases/${atlasId}/available-assets`),

  referenceOps: () => request<{ ops: ReferenceOp[]; groups: ReferenceOpGroup[]; extraction_keys: string[] }>(`/api/reference-ops`),

  composedPrompt: (assetId: number, overrideEntirePrompt?: boolean, promptMode?: PromptMode, referenceOps?: string[]) =>
    request<{
      sections: { style: string; rules: string; aspect: string; resolution: string; user: string; prompt_mode?: PromptMode; override_entire_prompt?: boolean }
      full: string
      external: string
      estimated_tokens: { input: number; output: number; thinking: number; total: number }
    }>(
      `/api/assets/${assetId}/composed-prompt?${new URLSearchParams({
        ...(overrideEntirePrompt !== undefined ? { override_entire_prompt: String(overrideEntirePrompt) } : {}),
        ...(promptMode ? { prompt_mode: promptMode } : {}),
        ...(referenceOps ? { reference_ops: referenceOps.join(',') } : {}),
      }).toString()}`,
    ),
  /** Real per-request credit cost from the provider's own pricing (Higgsfield's
   *  `generate cost`, no job created) — `supported: false` for a flat-subscription
   *  provider like Antigravity, which has nothing to estimate. */
  estimateCost: (
    assetId: number, provider: string, model?: string,
    overrideEntirePrompt?: boolean, promptMode?: PromptMode, referenceOps?: string[],
  ) =>
    request<{ supported: boolean; credits: number | null }>(
      `/api/assets/${assetId}/estimate-cost?${new URLSearchParams({
        provider,
        ...(model ? { model } : {}),
        ...(overrideEntirePrompt !== undefined ? { override_entire_prompt: String(overrideEntirePrompt) } : {}),
        ...(promptMode ? { prompt_mode: promptMode } : {}),
        ...(referenceOps ? { reference_ops: referenceOps.join(',') } : {}),
      }).toString()}`,
    ),
  getAssetGenerations: (assetId: number) =>
    request<{ id: string; timestamp: number; events: ProgressEvent[] }[]>(`/api/assets/${assetId}/generations`),
  getMockupGenerations: (mockupId: number) =>
    request<{ id: string; timestamp: number; events: ProgressEvent[] }[]>(`/api/mockups/${mockupId}/generations`),
  generate: (assetId: number, body: { provider: string; model?: string; visual_model?: string; prompt?: string; reference_paths?: string[]; override_entire_prompt?: boolean; prompt_mode?: PromptMode; reference_ops?: string[] }) =>
    request<Asset>(`/api/assets/${assetId}/generate`, json('POST', body)),
  generateStream: (
    assetId: number,
    body: { provider: string; model?: string; visual_model?: string; prompt?: string; reference_paths?: string[]; override_entire_prompt?: boolean; prompt_mode?: PromptMode; reference_ops?: string[] },
    onEvent: (e: ProgressEvent) => void,
    signal?: AbortSignal,
  ) => streamSSE(`/api/assets/${assetId}/generate/stream`, body, onEvent, signal),
  uploadAssetVersion: (assetId: number, file: File, opts?: { preserveFraming?: boolean }) => {
    const form = new FormData()
    form.append('file', file)
    if (opts?.preserveFraming) form.append('preserve_framing', 'true')
    return request<Asset>(`/api/assets/${assetId}/upload-version`, { method: 'POST', body: form })
  },
  upscaleAsset: (assetId: number) =>
    request<Asset>(`/api/assets/${assetId}/upscale`, { method: 'POST' }),
  downscaleAsset: (assetId: number) =>
    request<Asset>(`/api/assets/${assetId}/downscale`, { method: 'POST' }),
  deleteAssetVersion: (assetId: number, versionId: number) =>
    request<Asset>(`/api/assets/${assetId}/versions/${versionId}`, { method: 'DELETE' }),
  revealAssetVersion: (assetId: number, versionId: number) =>
    request<{ ok: boolean }>(`/api/assets/${assetId}/versions/${versionId}/reveal`, { method: 'POST' }),

  detectNineSlice: (assetId: number) =>
    request<{ nine_slice: { l: number; t: number; r: number; b: number }; width: number; height: number }>(
      `/api/assets/${assetId}/detect-nine-slice`, { method: 'POST' },
    ),
  trimAsset: (assetId: number) =>
    request<Asset>(`/api/assets/${assetId}/trim`, { method: 'POST' }),

  listMockups: (projectId: number) => request<Mockup[]>(`/api/projects/${projectId}/mockups`),
  uploadMockup: (projectId: number, file: File) => {
    const form = new FormData()
    form.append('file', file)
    return request<Mockup>(`/api/projects/${projectId}/mockups/upload`, { method: 'POST', body: form })
  },
  generateMockup: (projectId: number, body: { prompt: string; provider: string; model?: string; visual_model?: string }) =>
    request<Mockup>(`/api/projects/${projectId}/mockups/generate`, json('POST', body)),
  updateMockup: (id: number, body: Partial<Pick<Mockup, 'name'>>) =>
    request<Mockup>(`/api/mockups/${id}`, json('PATCH', body)),
  deleteMockup: (id: number) => request<{ ok: boolean }>(`/api/mockups/${id}`, { method: 'DELETE' }),

  createRegion: (mockupId: number, body: Omit<Region, 'id' | 'mockup_id' | 'asset_id'>) =>
    request<Region>(`/api/mockups/${mockupId}/regions`, json('POST', body)),
  updateRegion: (id: number, body: Partial<Region>) =>
    request<Region>(`/api/regions/${id}`, json('PATCH', body)),
  deleteRegion: (id: number) => request<{ ok: boolean }>(`/api/regions/${id}`, { method: 'DELETE' }),
  /** Saves one caption's own Keep ('' / null) / Remove ('erase') / Extract ('extract')
   *  choice from the Text step — see `Label.text_mode`. */
  updateLabel: (id: number, body: { text_mode: 'erase' | 'extract' | null }) =>
    request<Label>(`/api/labels/${id}`, json('PATCH', body)),
  regionCrop: (id: number) => request<{ path: string }>(`/api/regions/${id}/crop`),
  draftRegionPrompt: (id: number, llm?: LlmSelection) =>
    request<Region>(`/api/regions/${id}/draft-prompt`, json('POST', { llm })),
  generateAssetFromRegion: (
    id: number, provider: string, atlasId?: number | null, resolution?: string,
  ) =>
    request<Asset>(`/api/regions/${id}/generate-asset`, json('POST', {
      provider, atlas_id: atlasId ?? null, resolution: resolution ?? null,
    })),

  proposeSplits: (mockupId: number, body: { llm?: LlmSelection; region_ids?: number[] } = {}) =>
    request<{ proposals: SplitProposal[]; mockup_id: number }>(
      `/api/mockups/${mockupId}/propose-splits`, json('POST', body),
    ),
  proposeSplitsStream: (
    mockupId: number, body: { llm?: LlmSelection; region_ids?: number[] },
    onEvent: (e: ProgressEvent) => void, signal?: AbortSignal,
  ) => streamSSE(`/api/mockups/${mockupId}/propose-splits/stream`, body, onEvent, signal) as
    Promise<{ proposals: SplitProposal[]; mockup_id: number } | undefined>,
  applySplits: (mockupId: number, proposals: SplitProposal[]) =>
    request<Mockup>(`/api/mockups/${mockupId}/apply-splits`, json('POST', { proposals })),

  detectRegions: (mockupId: number, llm?: LlmSelection) =>
    request<Mockup>(`/api/mockups/${mockupId}/detect-regions`, json('POST', { llm })),
  detectRegionsStream: (mockupId: number, llm: LlmSelection | undefined, onEvent: (e: ProgressEvent) => void, signal?: AbortSignal) =>
    streamSSE(`/api/mockups/${mockupId}/detect-regions/stream`, { llm }, onEvent, signal),
  buildAtlas: (mockupId: number, atlasId: number, provider = 'antigravity', resolution?: string) =>
    request<{ results: { region_id: number; asset_id: number; reused: boolean }[]; errors: { region_id: number; name: string; error: string }[] }>(
      `/api/mockups/${mockupId}/build-atlas`, json('POST', { atlas_id: atlasId, provider, resolution: resolution ?? null }),
    ),
  /** The regions this build would bind to an asset that already exists, for the user to
   *  approve before anything is written. Nothing is reused unless it comes back in the
   *  `reuse` map below. */
  reuseCandidates: (mockupId: number, atlasId: number, rebuild?: boolean) =>
    request<{ candidates: ReuseCandidate[]; mockup_id: number }>(
      `/api/mockups/${mockupId}/reuse-candidates`,
      json('POST', { atlas_id: atlasId, rebuild: rebuild ?? false }),
    ),
  buildAtlasStream: (
    mockupId: number, atlasId: number, provider: string, resolution: string | undefined,
    onEvent: (e: ProgressEvent) => void, signal?: AbortSignal, rebuild?: boolean,
    reuse?: Record<string, number>,
  ) => streamSSE(`/api/mockups/${mockupId}/build-atlas/stream`, {
    atlas_id: atlasId, provider, resolution: resolution ?? null, rebuild: rebuild ?? false,
    // Always an object, never null: the server treats a missing map as "no one was asked"
    // and falls back to binding every name match by itself. From the UI someone was always
    // asked, so an empty map is the honest answer — build everything, reuse nothing.
    reuse: reuse ?? {},
  }, onEvent, signal),
  /** Cosmetic AI polish pass (upscale + clean edges) over a chosen set of already-built
   *  regions — omit `regionIds` (or pass an empty array) to polish every built element.
   *  Does not touch lettering; see `applyTextStream` for that. */
  polishRegionsStream: (
    mockupId: number, provider: string, regionIds: number[] | undefined,
    onEvent: (e: ProgressEvent) => void, signal?: AbortSignal, model?: string,
    force?: boolean,
  ) => streamSSE(`/api/mockups/${mockupId}/polish-regions/stream`, {
    provider, region_ids: regionIds && regionIds.length > 0 ? regionIds : null,
    // Omitted (null) falls back to the provider's saved default, so an unset picker
    // behaves exactly as this call did before the field existed.
    model: model || null,
    // Left out (false), the run resumes: elements that already carry a polish made from
    // their current build are kept as they are instead of being paid for twice — what a
    // re-run after a half-finished run wants. True says redo them anyway.
    force: force ?? false,
  }, onEvent, signal) as Promise<{ polished: number | null; skipped?: number; errors: unknown[] } | undefined>,
  /** Runs the Text step's saved Remove/Extract choices against a chosen set of
   *  already-built regions — omit `regionIds` to run every built region that has a
   *  choice set. A region left on "Keep" is skipped even by an unfiltered run. */
  applyTextStream: (
    mockupId: number, provider: string, regionIds: number[] | undefined,
    onEvent: (e: ProgressEvent) => void, signal?: AbortSignal, model?: string,
    force?: boolean,
  ) => streamSSE(`/api/mockups/${mockupId}/apply-text/stream`, {
    provider, region_ids: regionIds && regionIds.length > 0 ? regionIds : null,
    model: model || null,
    // Same resume/redo switch as polish: an element already cleaned, whose Extract
    // captions already have their sprites, costs nothing on a re-run.
    force: force ?? false,
  }, onEvent, signal) as Promise<{ applied: number | null; skipped?: number; sprites?: number; errors: unknown[] } | undefined>,
  /** Per-element state of the Text and Polish steps: what each one has actually produced
   *  and what is still missing. Computed server-side from the same predicates the steps
   *  resume on, so what the UI marks done is exactly what a re-run will skip. */
  stepStatus: (mockupId: number) =>
    request<StepStatus>(`/api/mockups/${mockupId}/step-status`),
  scoreMockup: (mockupId: number) =>
    request<MockupScore>(`/api/mockups/${mockupId}/score`, { method: 'POST' }),
  scoreRegion: (regionId: number) =>
    request<Fidelity>(`/api/regions/${regionId}/score`, { method: 'POST' }),
  previewScreen: (mockupId: number) =>
    request<{ path: string; missing: string[] }>(`/api/mockups/${mockupId}/preview`, { method: 'POST' }),
  exportMockupScreen: (mockupId: number, name?: string) =>
    request<{
      screen: string
      path: string
      reference: string
      count: number
      missing: string[]
      export_errors: { asset_id: number; error: string }[]
    }>(`/api/mockups/${mockupId}/export/screen${name ? `?name=${encodeURIComponent(name)}` : ''}`, { method: 'POST' }),

  exportStatus: (projectId: number) =>
    request<{ unity_path: string; importer_installed: boolean }>(`/api/projects/${projectId}/export/status`),
  browseUnityPath: (projectId: number) =>
    request<{ unity_path: string | null }>(`/api/projects/${projectId}/export/browse-unity-path`, { method: 'POST' }),
  installImporter: (projectId: number) =>
    request<{ ok: boolean; path: string }>(`/api/projects/${projectId}/export/install-importer`, { method: 'POST' }),
  exportAssets: (projectId: number, assetIds: number[]) =>
    request<{
      exported: { asset_id: number; path: string }[]
      errors: { asset_id: number; error: string }[]
      atlases: { path: string; count: number; built_atlases: string[] } | null
    }>(`/api/projects/${projectId}/export`, json('POST', { asset_ids: assetIds })),
  importSettingsPreview: (assetId: number) =>
    request<{ filename: string; path: string; settings: Record<string, unknown>; size: [number, number] | null }>(
      `/api/assets/${assetId}/import-settings`,
    ),

  llmProviders: () => request<LlmProviderInfo[]>('/api/llm/providers'),
  llmSettings: () => request<Required<LlmSelection>>('/api/llm/settings'),
  saveLlmSettings: (body: LlmSelection) =>
    request<Required<LlmSelection>>('/api/llm/settings', json('PUT', body)),
  refinePrompt: (assetId: number, llm?: LlmSelection) =>
    request<{ prompt: string }>(`/api/llm/assets/${assetId}/refine-prompt`, json('POST', { llm })),

  providers: () => request<ProvidersInfo>('/api/providers'),
  saveProviderSettings: (body: ProviderSettingsPatch) =>
    request<ProvidersInfo>('/api/providers/settings', json('PUT', body)),
  /** Which knobs this model accepts, and their option lists — straight from the
   *  provider's own model description, so the picker can never offer a flag the model
   *  would reject. */
  modelParams: (provider: string, model: string) =>
    request<{ params: Record<string, ModelParamSpec> }>(
      `/api/providers/${encodeURIComponent(provider)}/models/${encodeURIComponent(model)}/params`,
    ),
  /** Credit cost of a single generation call at this model's currently saved params —
   *  not tied to any one asset's prompt, since pricing is driven by model + params, not
   *  prompt text. `supported: false` for a flat-subscription provider (Antigravity). */
  perCallCost: (provider: string, model: string) =>
    request<{ supported: boolean; credits: number | null }>(
      `/api/providers/${encodeURIComponent(provider)}/models/${encodeURIComponent(model)}/estimate-cost`,
    ),
  refreshStatus: () => request<StatusInfo>('/api/status/refresh', { method: 'POST' }),
}

/** Storage paths now contain domain and asset names, which are user-typed and routinely
 *  contain spaces ("NavBarFrame Text Shop"). Each segment is encoded so the URL survives
 *  them; the slashes are not, or the path would stop being a path. */
export const storageUrl = (rel: string) =>
  rel ? `/storage/${rel.split('/').map(encodeURIComponent).join('/')}` : ''

/** What a reference image *was*. Every file an asset owns now lives in that asset's own
 *  folder (see server/app/layout.py), so the folder alone no longer separates a screen
 *  crop from an earlier render — `asset` is what tells them apart: anything a version of
 *  this asset actually produced is a previous version, and the rest is the crop it was
 *  cut from. Project-level references and mockup screenshots still read off the path. */
export const referenceLabel = (path: string, asset?: Asset): string => {
  if (/\/mockups\//.test(path)) return 'Source screen'
  if (/\/refs\//.test(path)) return 'Style reference'
  if (asset?.versions?.some((v) => v.raw_path === path || v.processed_path === path)) {
    return 'Previous version'
  }
  return /\/domains\//.test(path) ? 'Cut from screen' : 'Reference'
}

const QUOTA_ERROR_RE = /quota|rate.?limit|429|resource_exhausted/i

/** Whether an error message looks like a provider quota/rate-limit exhaustion rather
 * than a one-off failure — mirrors the server's `is_quota_error` so the UI can flag it
 * distinctly wherever a plain thrown Error is all that's available (outside SSE steps,
 * which already carry an explicit `data.quota_exceeded` flag from the server). */
export const isQuotaError = (message: string | null | undefined) => QUOTA_ERROR_RE.test(message || '')

export function timeAgo(iso: string): string {
  const then = new Date(iso.endsWith('Z') ? iso : iso + 'Z').getTime()
  const mins = Math.max(0, Math.round((Date.now() - then) / 60000))
  if (mins < 1) return 'Just now'
  if (mins < 60) return `${mins} min ago`
  const hours = Math.round(mins / 60)
  if (hours < 24) return `${hours}h ago`
  const days = Math.round(hours / 24)
  if (days < 7) return `${days}d ago`
  return `${Math.round(days / 7)}w ago`
}
