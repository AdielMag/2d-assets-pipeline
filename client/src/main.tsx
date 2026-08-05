import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { createBrowserRouter, Navigate, RouterProvider } from 'react-router-dom'
import './index.css'
import App from './App'
import { AppProvider } from './AppContext'
import ProjectsPage from './pages/ProjectsPage'
import ProjectSettingsPage from './pages/ProjectSettingsPage'
import AssetsPage from './pages/AssetsPage'
import AssetDetailPage from './pages/AssetDetailPage'
import ImportPage from './pages/ImportPage'
import ScreenProcessPage from './pages/screen/ScreenProcessPage'
import ExportPage from './pages/ExportPage'
import ProvidersPage from './pages/ProvidersPage'

const router = createBrowserRouter([
  {
    path: '/',
    element: <App />,
    children: [
      { index: true, element: <ProjectsPage /> },
      { path: 'settings', element: <ProjectSettingsPage /> },
      { path: 'assets', element: <AssetsPage /> },
      { path: 'assets/:assetId', element: <AssetDetailPage /> },
      { path: 'import', element: <ImportPage /> },
      { path: 'screens', element: <ScreenProcessPage /> },
      { path: 'mockup', element: <Navigate to="/screens" replace /> },
      { path: 'screen-atlas', element: <Navigate to="/screens" replace /> },
      { path: 'export', element: <ExportPage /> },
      { path: 'providers', element: <ProvidersPage /> },
    ],
  },
])

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProvider>
      <RouterProvider router={router} />
    </AppProvider>
  </StrictMode>,
)
