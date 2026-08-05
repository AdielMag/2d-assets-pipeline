import { useState } from 'react'

/** Server-rendered seamless-tile check: the sprite repeated 3×3 with a half-tile
 * offset, so any seam lands mid-preview and is immediately visible. */
export default function TilePreview({ assetId }: { assetId: number }) {
  const [key, setKey] = useState(0)
  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
        <div style={{ fontSize: 12.5, fontWeight: 600 }}>Seamless Tile Preview</div>
        <div
          onClick={() => setKey((k) => k + 1)}
          style={{
            fontSize: 10.5, fontWeight: 600, color: 'var(--accent)',
            border: '1px solid #2a3a5c', background: 'rgba(108,140,255,0.08)',
            borderRadius: 5, padding: '4px 8px', cursor: 'pointer',
          }}
        >
          Refresh
        </div>
      </div>
      <img
        src={`/api/assets/${assetId}/tile-preview?k=${key}`}
        alt="Tile preview"
        style={{ width: '100%', borderRadius: 8, display: 'block', border: '1px solid var(--border-2)' }}
      />
      <div style={{ fontSize: 11, color: 'var(--muted)', marginTop: 10, lineHeight: 1.5 }}>
        Tiled 3×3 with a half-tile offset — seams (if any) appear as visible lines through the middle.
      </div>
    </div>
  )
}
