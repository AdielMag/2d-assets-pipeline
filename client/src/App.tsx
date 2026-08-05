import { useEffect, useState } from 'react'
import { NavLink, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { useApp } from './AppContext'

const NAV = [
  {
    to: '/settings',
    label: 'Project Settings',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
        <line x1="2" y1="4" x2="14" y2="4" /><circle cx="10" cy="4" r="1.6" fill="currentColor" stroke="none" />
        <line x1="2" y1="8" x2="14" y2="8" /><circle cx="6" cy="8" r="1.6" fill="currentColor" stroke="none" />
        <line x1="2" y1="12" x2="14" y2="12" /><circle cx="11.5" cy="12" r="1.6" fill="currentColor" stroke="none" />
      </svg>
    ),
  },
  {
    to: '/assets',
    label: 'Assets',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
        <rect x="1.5" y="1.5" width="13" height="13" rx="1.5" /><circle cx="5.5" cy="5.5" r="1.3" fill="currentColor" stroke="none" />
        <polyline points="2,12.5 6,8.5 9,11 12,7.5 14.5,10.5" />
      </svg>
    ),
  },
  {
    to: '/import',
    label: 'Import Cutouts',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
        <path d="M8 1.8v8.7M8 10.5l-3-3M8 10.5l3-3" />
        <path d="M1.8 9.5v3.7a1 1 0 0 0 1 1h10.4a1 1 0 0 0 1-1V9.5" />
      </svg>
    ),
  },
  {
    to: '/screens',
    label: 'Screens',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
        <rect x="1.5" y="2.5" width="13" height="9" rx="1.5" /><polyline points="5.5,14 10.5,14" />
        <rect x="4" y="5" width="3" height="3.5" rx="0.5" /><polyline points="9,5.5 12,5.5" /><polyline points="9,8 11,8" />
      </svg>
    ),
  },
  {
    to: '/export',
    label: 'Export to Unity',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
        <path d="M8 10.5V1.8M8 1.8L5 4.8M8 1.8l3 3" />
        <path d="M1.8 9.5v3.7a1 1 0 0 0 1 1h10.4a1 1 0 0 0 1-1V9.5" />
      </svg>
    ),
  },
  {
    to: '/providers',
    label: 'Providers & Settings',
    icon: (
      <svg width="16" height="16" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4">
        <circle cx="8" cy="8" r="2.3" /><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M12.6 3.4l-1.4 1.4M4.8 11.2l-1.4 1.4" />
      </svg>
    ),
  },
]

function StatusDot({ ok }: { ok: boolean }) {
  return (
    <span
      style={{
        width: 6, height: 6, borderRadius: '50%', flexShrink: 0,
        background: ok ? 'var(--green)' : '#5a616b',
      }}
    />
  )
}

function ProviderCard() {
  const { status } = useApp()
  // A provider row is "on" only when it's both usable and not disabled in Settings.
  const en = status?.enabled
  const items: { name: string; icon: React.ReactNode; label: string; ok: boolean; title: string }[] = status
    ? [
        {
          name: 'antigravity',
          icon: (
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" style={{ flexShrink: 0 }}>
              <path d="M8 1.5C9.5 4 12 5.5 14 6c-1 2.5-3.5 5.5-6 8.5C5.5 11.5 3 8.5 2 6c2-.5 4.5-2 6-4.5z" />
              <circle cx="8" cy="6.5" r="1.3" fill="currentColor" />
            </svg>
          ),
          label: `Antigravity · ${!en?.antigravity ? 'off' : status.antigravity?.ok ? 'ready' : 'not set up'}`,
          ok: !!en?.antigravity && !!status.antigravity?.ok,
          title: status.antigravity?.detail || 'Google Antigravity CLI status',
        },
        {
          name: 'higgsfield',
          icon: (
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" style={{ flexShrink: 0 }}>
              <rect x="3" y="13" width="4.5" height="8" rx="1" fill="#7C5CFC" />
              <rect x="9.75" y="7" width="4.5" height="14" rx="1" fill="#A78BFA" />
              <rect x="16.5" y="3" width="4.5" height="18" rx="1" fill="#7C5CFC" />
            </svg>
          ),
          label: `Higgsfield · ${!en?.higgsfield ? 'off' : status.higgsfield?.ok ? 'ready' : 'not set up'}`,
          ok: !!en?.higgsfield && !!status.higgsfield?.ok,
          title: status.higgsfield?.detail || 'Higgsfield CLI status',
        },
        {
          name: 'claude',
          icon: (
            <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor" style={{ flexShrink: 0 }}>
              <path d="M8 1a1 1 0 0 1 1 1v4.2a1 1 0 0 0 .8.98L14 8a1 1 0 0 1 0 2l-4.2.82a1 1 0 0 0-.8.98V15a1 1 0 0 1-2 0v-4.2a1 1 0 0 0-.8-.98L2 9a1 1 0 0 1 0-2l4.2-.82a1 1 0 0 0 .8-.98V2a1 1 0 0 1 1-1z" />
            </svg>
          ),
          label: `Claude · ${status.llm_clis?.claude?.ok ? 'found' : 'missing'}`,
          ok: !!status.llm_clis?.claude?.ok,
          title: status.llm_clis?.claude?.ok ? 'Claude CLI executable detected on PATH' : 'Claude CLI executable not found on PATH',
        },
      ]
    : []

  return (
    <NavLink
      to="/providers"
      className="nav-item"
      title="Manage AI providers, CLI agents & API keys"
      style={{ background: '#1e232a', border: '1px solid var(--border)', borderRadius: 9, padding: '11px 12px', display: 'flex', flexDirection: 'column', gap: 7, alignItems: 'stretch' }}
    >
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 1 }}>
        <span style={{ fontSize: 10, fontWeight: 600, color: 'var(--muted)', letterSpacing: '0.04em', textTransform: 'uppercase' }}>
          Providers
        </span>
        <span style={{ fontSize: 10, color: 'var(--muted-2)' }}>manage →</span>
      </div>
      {items.length === 0 && <div style={{ fontSize: 11.5, color: 'var(--muted-2)' }}>Checking…</div>}
      {items.map((item) => (
        <div key={item.name} title={item.title} style={{ display: 'flex', alignItems: 'center', gap: 7, fontSize: 11.5, color: 'var(--text-2)' }}>
          <StatusDot ok={item.ok} />
          {item.icon}
          <span style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{item.label}</span>
        </div>
      ))}
    </NavLink>
  )
}

function BackToProjects() {
  const navigate = useNavigate()
  return (
    <div
      onClick={() => navigate('/')}
      title="Back to all projects"
      className="back-to-projects"
      style={{
        display: 'flex', alignItems: 'center', gap: 7, cursor: 'pointer', userSelect: 'none',
        color: 'var(--muted)', fontSize: 12, fontWeight: 500, padding: '5px 4px 5px 2px',
      }}
    >
      <svg width="13" height="13" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.6" style={{ flexShrink: 0 }}>
        <polyline points="9.5,3 4.5,8 9.5,13" />
      </svg>
      <span>All Projects</span>
    </div>
  )
}

function ProjectSwitcher() {
  const { projects, project, selectProject } = useApp()
  const navigate = useNavigate()
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    const close = () => setOpen(false)
    window.addEventListener('click', close)
    return () => window.removeEventListener('click', close)
  }, [open])

  return (
    <div style={{ position: 'relative' }} onClick={(e) => e.stopPropagation()}>
      <div
        onClick={() => setOpen((o) => !o)}
        title="Switch active project workspace"
        style={{
          display: 'flex', alignItems: 'center', gap: 8, background: '#1e232a',
          border: '1px solid var(--border-2)', borderRadius: 7, padding: '6px 12px',
          fontSize: 12.5, fontWeight: 600, cursor: 'pointer', userSelect: 'none',
        }}
      >
        <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.4" style={{ flexShrink: 0, color: 'var(--accent)' }}>
          <rect x="1.5" y="1.5" width="5.5" height="5.5" rx="1" /><rect x="9" y="1.5" width="5.5" height="5.5" rx="1" />
          <rect x="1.5" y="9" width="5.5" height="5.5" rx="1" /><rect x="9" y="9" width="5.5" height="5.5" rx="1" />
        </svg>
        {project?.name ?? 'No project'}
        <svg width="10" height="10" viewBox="0 0 10 10" fill="none" stroke="#7d8590" strokeWidth="1.4">
          <polyline points="2,3.5 5,6.5 8,3.5" />
        </svg>
      </div>
      {open && (
        <div
          style={{
            position: 'absolute', top: '110%', left: 0, minWidth: 200, zIndex: 40,
            background: 'var(--card)', border: '1px solid var(--border-3)', borderRadius: 8, padding: 4,
            boxShadow: '0 8px 24px rgba(0,0,0,0.35)',
          }}
        >
          {projects.map((p) => (
            <div
              key={p.id}
              onClick={() => { selectProject(p.id); setOpen(false) }}
              className="nav-item"
              title={`Switch to ${p.name}`}
              style={{ padding: '8px 10px', fontSize: 12.5, color: p.id === project?.id ? '#eef0f2' : undefined }}
            >
              {p.name}
            </div>
          ))}
          {projects.length === 0 && (
            <div style={{ padding: '8px 10px', fontSize: 12, color: 'var(--muted)' }}>No projects yet</div>
          )}
          <div style={{ height: 1, background: 'var(--border)', margin: '4px 2px' }} />
          <div
            onClick={() => { setOpen(false); navigate('/') }}
            className="nav-item"
            style={{ padding: '8px 10px', fontSize: 12.5, color: 'var(--accent)', display: 'flex', alignItems: 'center', gap: 6 }}
          >
            <svg width="12" height="12" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5">
              <rect x="1.5" y="1.5" width="5.5" height="5.5" rx="1" /><rect x="9" y="1.5" width="5.5" height="5.5" rx="1" />
              <rect x="1.5" y="9" width="5.5" height="5.5" rx="1" /><rect x="9" y="9" width="5.5" height="5.5" rx="1" />
            </svg>
            Manage all projects
          </div>
        </div>
      )}
    </div>
  )
}

export default function App() {
  const location = useLocation()
  const navigate = useNavigate()
  const { project, projectsLoaded } = useApp()
  const isLanding = location.pathname === '/'

  useEffect(() => {
    if (!isLanding && projectsLoaded && !project) {
      navigate('/', { replace: true })
    }
  }, [isLanding, projectsLoaded, project, navigate])

  if (isLanding) {
    return <Outlet />
  }

  return (
    <div style={{ width: '100vw', height: '100vh', display: 'flex', overflow: 'hidden' }}>
      {/* Sidebar */}
      <div style={{ width: 228, flexShrink: 0, background: 'var(--sidebar)', borderRight: '1px solid var(--border)', display: 'flex', flexDirection: 'column', padding: '18px 12px' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '2px 8px 14px', marginBottom: 8 }}>
          <div style={{ width: 30, height: 30, borderRadius: 8, background: 'var(--accent)', flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
            <svg width="16" height="16" viewBox="0 0 16 16">
              <rect x="1" y="1" width="8" height="8" rx="1.5" fill="#14161a" />
              <rect x="7" y="7" width="8" height="8" rx="1.5" fill="#14161a" opacity="0.55" />
            </svg>
          </div>
          <div>
            <div style={{ fontSize: 13.5, fontWeight: 600, lineHeight: 1.2 }}>2D Assets Pipeline</div>
            <div style={{ fontSize: 10.5, color: 'var(--muted)', lineHeight: 1.2 }}>local asset generator</div>
          </div>
        </div>

        <div style={{ padding: '0 8px 12px', borderBottom: '1px solid var(--border)', marginBottom: 14 }}>
          <BackToProjects />
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
          {NAV.map((item) => {
            const active = item.to === '/assets'
              ? location.pathname.startsWith('/assets')
              : location.pathname.startsWith(item.to)
            return (
              <NavLink key={item.to} to={item.to} className={`nav-item${active ? ' active' : ''}`} title={item.label}>
                {item.icon}
                <span>{item.label}</span>
              </NavLink>
            )
          })}
        </div>

        <div style={{ flex: 1 }} />
        <ProviderCard />
      </div>

      {/* Main */}
      <div style={{ flex: 1, display: 'flex', flexDirection: 'column', minWidth: 0 }}>
        <div style={{ height: 56, flexShrink: 0, display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 28px', borderBottom: '1px solid #23272e' }}>
          <ProjectSwitcher />
          <div
            title="User Profile & Settings"
            style={{ width: 28, height: 28, borderRadius: '50%', background: '#2c3340', border: '1px solid var(--border-3)', flexShrink: 0, cursor: 'pointer' }}
          />
        </div>
        <div style={{ flex: 1, overflowY: 'auto', padding: '28px 34px 60px' }}>
          <Outlet />
        </div>
      </div>
    </div>
  )
}
