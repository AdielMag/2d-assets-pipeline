import type { Fidelity, ScreenFidelity } from '../api'

/** Score bands. Extraction should land in "good"; a regenerated element that drifted in
 *  colour typically lands in "poor" — the gap is the whole point of the metric. */
function bandColor(score: number): string {
  if (score >= 85) return 'var(--green, #3ecf8e)'
  if (score >= 70) return '#a3d977'
  if (score >= 50) return '#f5a623'
  return '#f4685f'
}

function tooltip(f: Fidelity): string {
  const parts = [
    `score ${f.score.toFixed(1)}/100`,
    `ΔE2000 ${f.delta_e.toFixed(2)} (colour; <2 imperceptible)`,
    `SSIM ${f.ssim.toFixed(3)} (structure; 1 = identical)`,
    `coverage ${(f.coverage * 100).toFixed(0)}% of the slot painted`,
  ]
  if (f.alpha_iou != null) parts.push(`alpha IoU ${f.alpha_iou.toFixed(3)} (silhouette)`)
  if (f.method) parts.push(`measured ${f.method === 'composite' ? 'on the composited screen' : 'in isolation'}`)
  return parts.join('\n')
}

/** Compact per-region fidelity chip: how close this element is to the reference pixels. */
export function FidelityBadge({ fidelity, compact }: { fidelity?: Fidelity | null; compact?: boolean }) {
  if (!fidelity) return null
  const color = bandColor(fidelity.score)
  return (
    <span
      title={tooltip(fidelity)}
      style={{
        fontSize: compact ? 9.5 : 10.5,
        fontWeight: 700,
        color,
        border: `1px solid ${color}55`,
        background: `${color}18`,
        borderRadius: 4,
        padding: compact ? '1px 4px' : '2px 6px',
        flexShrink: 0,
        fontVariantNumeric: 'tabular-nums',
      }}
    >
      {Math.round(fidelity.score)}
    </span>
  )
}

/** Whole-screen fidelity summary: quality of what was rebuilt, discounted by how much of
 *  the screen exists at all. Both numbers matter — a perfect half-screen is still half. */
export function ScreenFidelityBar({ screen }: { screen?: ScreenFidelity | null }) {
  if (!screen) return null
  const color = bandColor(screen.score)
  return (
    <div
      title={`Whole-screen reconstruction vs. the source screenshot.\nΔE2000 ${screen.delta_e.toFixed(2)}  SSIM ${screen.ssim.toFixed(3)}\nquality ${screen.quality.toFixed(1)} over the ${(screen.filled * 100).toFixed(0)}% that was rebuilt`}
      style={{
        display: 'flex', alignItems: 'center', gap: 8, padding: '6px 10px',
        borderRadius: 6, background: '#1c2027', border: '1px solid #2a2f38', fontSize: 11,
      }}
    >
      <span style={{ color: 'var(--muted-2)' }}>Screen match</span>
      <span style={{ fontWeight: 700, color, fontVariantNumeric: 'tabular-nums' }}>
        {screen.score.toFixed(1)}
      </span>
      <span style={{ color: 'var(--muted-2)' }}>
        ({screen.quality.toFixed(0)} quality × {(screen.filled * 100).toFixed(0)}% built)
      </span>
      <div style={{ flex: 1, height: 4, borderRadius: 2, background: '#2a2f38', overflow: 'hidden', minWidth: 40 }}>
        <div style={{ width: `${Math.min(100, screen.score)}%`, height: '100%', background: color }} />
      </div>
    </div>
  )
}
