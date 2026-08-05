import { createContext, useCallback, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'
import { api } from './api'
import type { ProgressEvent, Project, StatusInfo } from './api'

export interface StoredRun {
  id: string
  timestamp: number
  active: boolean
  events: ProgressEvent[]
}

interface AppState {
  projects: Project[]
  project: Project | null
  projectsLoaded: boolean
  status: StatusInfo | null
  selectProject: (id: number) => void
  refreshProjects: () => Promise<void>
  generationStore: Record<string, StoredRun[]>
  pushGenerationEvent: (entityKey: string, event: ProgressEvent, runId?: string) => void
  startGenerationRun: (entityKey: string) => string
  endGenerationRun: (entityKey: string, runId?: string) => void
  loadGenerationHistory: (entityKey: string, entityId: number, type: 'asset' | 'mockup') => Promise<void>
}

const Ctx = createContext<AppState>({
  projects: [],
  project: null,
  projectsLoaded: false,
  status: null,
  selectProject: () => {},
  refreshProjects: async () => {},
  generationStore: {},
  pushGenerationEvent: () => {},
  startGenerationRun: () => '',
  endGenerationRun: () => {},
  loadGenerationHistory: async () => {},
})

export const useApp = () => useContext(Ctx)

export function AppProvider({ children }: { children: ReactNode }) {
  const [projects, setProjects] = useState<Project[]>([])
  const [status, setStatus] = useState<StatusInfo | null>(null)
  const [projectId, setProjectId] = useState<number | null>(() => {
    const saved = localStorage.getItem('projectId')
    return saved ? Number(saved) : null
  })
  const [generationStore, setGenerationStore] = useState<Record<string, StoredRun[]>>({})
  const [projectsLoaded, setProjectsLoaded] = useState(false)

  const refreshProjects = useCallback(async () => {
    const list = await api.listProjects()
    setProjects(list)
    setProjectId((current) => {
      if (current && list.some((p) => p.id === current)) return current
      return null
    })
    setProjectsLoaded(true)
  }, [])

  useEffect(() => {
    refreshProjects().catch(() => {})
    api.status().then(setStatus).catch(() => {})
  }, [refreshProjects])

  useEffect(() => {
    if (projectId != null) localStorage.setItem('projectId', String(projectId))
  }, [projectId])

  const startGenerationRun = useCallback((entityKey: string): string => {
    const runId = `run-${Date.now()}`
    const newRun: StoredRun = {
      id: runId,
      timestamp: Date.now(),
      active: true,
      events: [],
    }
    setGenerationStore((prev) => {
      const existing = prev[entityKey] || []
      return { ...prev, [entityKey]: [newRun, ...existing] }
    })
    return runId
  }, [])

  const pushGenerationEvent = useCallback((entityKey: string, event: ProgressEvent, runId?: string) => {
    setGenerationStore((prev) => {
      const runs = [...(prev[entityKey] || [])]
      if (runs.length === 0) {
        const id = runId || `run-${Date.now()}`
        runs.push({ id, timestamp: Date.now(), active: true, events: [] })
      }
      const targetRun = runId ? runs.find((r) => r.id === runId) || runs[0] : runs[0]
      if (!targetRun) return prev

      const item: ProgressEvent = {
        ...event,
        timestamp: event.timestamp || Date.now(),
      }
      const prevEvents = targetRun.events
      const last = prevEvents[prevEvents.length - 1]
      let nextEvents: ProgressEvent[]
      if (last && last.step === event.step && last.status === 'running') {
        nextEvents = [...prevEvents.slice(0, -1), item]
      } else {
        nextEvents = [...prevEvents, item]
      }

      const updatedRun: StoredRun = { ...targetRun, events: nextEvents }
      const updatedRuns = runs.map((r) => (r.id === updatedRun.id ? updatedRun : r))
      return { ...prev, [entityKey]: updatedRuns }
    })
  }, [])

  const endGenerationRun = useCallback((entityKey: string, runId?: string) => {
    setGenerationStore((prev) => {
      const runs = prev[entityKey]
      if (!runs || runs.length === 0) return prev
      const updated = runs.map((r) => (runId ? (r.id === runId ? { ...r, active: false } : r) : { ...r, active: false }))
      return { ...prev, [entityKey]: updated }
    })
  }, [])

  const loadGenerationHistory = useCallback(async (entityKey: string, entityId: number, type: 'asset' | 'mockup') => {
    try {
      const runs = type === 'asset'
        ? await api.getAssetGenerations(entityId)
        : await api.getMockupGenerations(entityId)
      if (runs && runs.length > 0) {
        setGenerationStore((prev) => {
          // preserve any currently active local run, merge historical ones
          const existing = prev[entityKey] || []
          const activeRuns = existing.filter((r) => r.active)
          const fetchedRuns: StoredRun[] = runs.map((r) => ({
            id: r.id,
            timestamp: r.timestamp,
            active: false,
            events: r.events,
          }))
          // combine avoiding duplicates
          const combined = [...activeRuns]
          for (const fr of fetchedRuns) {
            if (!combined.some((x) => x.id === fr.id)) {
              combined.push(fr)
            }
          }
          return { ...prev, [entityKey]: combined }
        })
      }
    } catch {
      /* fallback silent */
    }
  }, [])

  const project = projects.find((p) => p.id === projectId) ?? null

  return (
    <Ctx.Provider
      value={{
        projects,
        project,
        projectsLoaded,
        status,
        selectProject: setProjectId,
        refreshProjects,
        generationStore,
        pushGenerationEvent,
        startGenerationRun,
        endGenerationRun,
        loadGenerationHistory,
      }}
    >
      {children}
    </Ctx.Provider>
  )
}
