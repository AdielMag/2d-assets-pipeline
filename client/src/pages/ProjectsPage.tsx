import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api, storageUrl, timeAgo } from '../api'
import { useApp } from '../AppContext'

function NewProjectModal({ onClose }: { onClose: () => void }) {
  const { refreshProjects, selectProject } = useApp()
  const navigate = useNavigate()
  const [name, setName] = useState('')
  const [style, setStyle] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const create = async () => {
    if (!name.trim()) return
    setBusy(true)
    setError('')
    try {
      const project = await api.createProject({ name: name.trim(), style_description: style.trim() })
      await refreshProjects()
      selectProject(project.id)
      onClose()
      navigate('/settings')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to create project')
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 16 }}>New Project</div>
        <div className="field-label">Project name</div>
        <input
          className="input"
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && create()}
          placeholder="Pixel Dungeon"
          style={{ marginBottom: 14 }}
        />
        <div className="field-label">Art style (you can refine this later in Project Settings)</div>
        <textarea
          className="textarea"
          rows={3}
          value={style}
          onChange={(e) => setStyle(e.target.value)}
          placeholder="Dark fantasy pixel art, 32px grid, desaturated stone & earth palette…"
          style={{ marginBottom: 16 }}
        />
        {error && <div style={{ color: '#ff7b7b', fontSize: 12, marginBottom: 10 }}>{error}</div>}
        <div style={{ display: 'flex', justifyContent: 'flex-end', gap: 8 }}>
          <button className="btn btn-secondary" onClick={onClose}>Cancel</button>
          <button className="btn btn-accent" disabled={busy || !name.trim()} onClick={create}>
            Create Project
          </button>
        </div>
      </div>
    </div>
  )
}

function ProjectCard({ p, onOpen }: { p: import('../api').Project; onOpen: () => void }) {
  const [hover, setHover] = useState(false)
  return (
    <div
      className="card hover-border"
      style={{
        overflow: 'hidden', cursor: 'pointer', display: 'flex', flexDirection: 'column',
        transform: hover ? 'translateY(-2px)' : 'none',
        boxShadow: hover ? '0 10px 28px rgba(0,0,0,0.35)' : '0 1px 0 rgba(0,0,0,0)',
        transition: 'transform 150ms ease, box-shadow 150ms ease, border-color 150ms ease',
      }}
      onMouseEnter={() => setHover(true)}
      onMouseLeave={() => setHover(false)}
      onClick={onOpen}
    >
      {p.reference_images.length > 0 ? (
        <img
          src={storageUrl(p.reference_images[0])}
          alt=""
          style={{ width: '100%', height: 140, objectFit: 'cover', display: 'block' }}
        />
      ) : (
        <div
          style={{
            width: '100%', height: 140, display: 'flex', alignItems: 'center', justifyContent: 'center',
            background: '#20242b', color: 'var(--muted-2)', fontSize: 12,
          }}
        >
          <svg width="28" height="28" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.2">
            <rect x="1.5" y="1.5" width="13" height="13" rx="1.5" /><circle cx="5.5" cy="5.5" r="1.3" fill="currentColor" stroke="none" />
            <polyline points="2,12.5 6,8.5 9,11 12,7.5 14.5,10.5" />
          </svg>
        </div>
      )}
      <div style={{ padding: '15px 16px', display: 'flex', flexDirection: 'column', flex: 1 }}>
        <div style={{ fontSize: 14.5, fontWeight: 600, marginBottom: 5 }}>{p.name}</div>
        <div
          style={{
            fontSize: 12, color: 'var(--text-3)', lineHeight: 1.5, marginBottom: 11,
            display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden',
            minHeight: 36,
          }}
        >
          {p.style_description || 'No art style defined yet.'}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 10, minHeight: 14 }}>
          {p.palette.map((c, i) => (
            <span
              key={i}
              style={{ width: 14, height: 14, borderRadius: 4, border: '1px solid rgba(255,255,255,0.12)', background: c }}
            />
          ))}
        </div>
        <div style={{ flex: 1 }} />
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', fontSize: 11, color: 'var(--muted)' }}>
          <span>{p.asset_count} assets</span>
          <span>Updated {timeAgo(p.updated_at)}</span>
        </div>
      </div>
    </div>
  )
}

export default function ProjectsPage() {
  const { projects, selectProject } = useApp()
  const navigate = useNavigate()
  const [showNew, setShowNew] = useState(false)
  const [query, setQuery] = useState('')

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return projects
    return projects.filter((p) => p.name.toLowerCase().includes(q) || p.style_description.toLowerCase().includes(q))
  }, [projects, query])

  const openProject = (id: number) => {
    selectProject(id)
    navigate('/assets')
  }

  return (
    <div style={{ width: '100%', height: '100%', overflowY: 'auto', display: 'flex', justifyContent: 'center' }}>
      <div style={{ width: '100%', maxWidth: 1080, padding: '48px 32px 64px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12, marginBottom: 6 }}>
          <div style={{ width: 36, height: 36, borderRadius: 9, background: 'var(--accent)', flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <svg width="19" height="19" viewBox="0 0 16 16">
              <rect x="1" y="1" width="8" height="8" rx="1.5" fill="#14161a" />
              <rect x="7" y="7" width="8" height="8" rx="1.5" fill="#14161a" opacity="0.55" />
            </svg>
          </div>
          <div>
            <div style={{ fontSize: 16, fontWeight: 600, lineHeight: 1.2 }}>2D Assets Pipeline</div>
            <div style={{ fontSize: 11.5, color: 'var(--muted)', lineHeight: 1.2 }}>local asset generator</div>
          </div>
        </div>

        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', margin: '36px 0 20px', gap: 16, flexWrap: 'wrap' }}>
          <div>
            <h1 style={{ marginBottom: 4 }}>Your Projects</h1>
            <div style={{ fontSize: 12.5, color: 'var(--muted)' }}>
              Pick a project to open its workspace, or start a new one.
            </div>
          </div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
            {projects.length > 0 && (
              <input
                className="input"
                placeholder="Search projects…"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                style={{ width: 220 }}
              />
            )}
            <button className="btn btn-accent" onClick={() => setShowNew(true)}>+ New Project</button>
          </div>
        </div>

        {projects.length === 0 && (
          <div className="card" style={{ padding: '56px 40px', textAlign: 'center' }}>
            <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 16 }}>
              <div style={{ width: 52, height: 52, borderRadius: 14, background: '#20242b', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
                <svg width="26" height="26" viewBox="0 0 16 16" fill="none" stroke="var(--accent)" strokeWidth="1.3">
                  <rect x="1.5" y="1.5" width="5.5" height="5.5" rx="1" /><rect x="9" y="1.5" width="5.5" height="5.5" rx="1" />
                  <rect x="1.5" y="9" width="5.5" height="5.5" rx="1" /><rect x="9" y="9" width="5.5" height="5.5" rx="1" />
                </svg>
              </div>
            </div>
            <div style={{ fontSize: 15, fontWeight: 600, marginBottom: 8 }}>No projects yet</div>
            <div style={{ color: 'var(--muted)', fontSize: 12.5, marginBottom: 20 }}>
              Create your first project to define an art style and start generating assets.
            </div>
            <button className="btn btn-accent" onClick={() => setShowNew(true)}>+ New Project</button>
          </div>
        )}

        {projects.length > 0 && filtered.length === 0 && (
          <div className="card" style={{ padding: 40, textAlign: 'center', color: 'var(--muted)' }}>
            No projects match “{query}”.
          </div>
        )}

        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill,minmax(260px,1fr))', gap: 20 }}>
          {filtered.map((p) => (
            <ProjectCard key={p.id} p={p} onOpen={() => openProject(p.id)} />
          ))}
        </div>
      </div>

      {showNew && <NewProjectModal onClose={() => setShowNew(false)} />}
    </div>
  )
}
