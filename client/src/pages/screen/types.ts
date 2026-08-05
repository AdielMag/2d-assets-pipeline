import type { Atlas, Mockup, MockupScore, ProgressEvent, Project } from '../../api'
import type { StoredRun } from '../../AppContext'
import type { ProgressItem } from '../../components/RunProgress'

export type Busy = 'idle' | 'detecting' | 'proposing' | 'building' | 'previewing' | 'polishing' | 'texting'

/** Everything the process shell owns and its steps borrow. Passed explicitly rather than
 *  through context: there are five consumers, all siblings, and a prop list that has to
 *  be written down is a useful brake on how much state this page is allowed to have. */
export interface StepProps {
  project: Project
  mockup: Mockup | null
  mockups: Mockup[]
  atlases: Atlas[]
  atlasId: number | null
  setAtlasId: (id: number | null) => void
  reload: () => Promise<void>
  setMockupId: (id: number | null) => void
  busy: Busy
  progress: ProgressItem[]
  stop: () => void
  runStream: (
    kind: Busy,
    job: (onEvent: (e: ProgressEvent) => void, signal: AbortSignal) => Promise<unknown>,
  ) => Promise<unknown>
  error: string
  setError: (s: string) => void
  selectedRegionId: number | null
  setSelectedRegionId: (id: number | null) => void
  previewPath: string
  missing: string[]
  score: MockupScore | null
  refreshPreview: (id: number) => Promise<void>
  runs: StoredRun[]
  next: () => void
  onSavePending?: (saveFn: (() => Promise<void>) | null) => void
}
