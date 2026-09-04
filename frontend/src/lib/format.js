/** Money arrives as paise (integer). The client only ever formats it. */

export function rupees(paise, { compact = false } = {}) {
  const value = (paise || 0) / 100
  if (compact) {
    if (Math.abs(value) >= 1e7) return `₹${(value / 1e7).toFixed(2)} Cr`
    if (Math.abs(value) >= 1e5) return `₹${(value / 1e5).toFixed(2)} L`
    if (Math.abs(value) >= 1000) return `₹${(value / 1000).toFixed(1)}K`
  }
  return `₹${value.toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
}

export const pct = (value, digits = 1) =>
  value === null || value === undefined ? '—' : `${(value * 100).toFixed(digits)}%`

export function timeOfDay(iso) {
  if (!iso) return '—'
  return new Date(iso).toLocaleTimeString('en-GB', {
    hour: '2-digit',
    minute: '2-digit',
    second: '2-digit',
  })
}

export function relativeTime(iso) {
  if (!iso) return '—'
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000
  if (seconds < 60) return `${Math.max(0, Math.round(seconds))}s ago`
  if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`
  if (seconds < 86400) return `${Math.round(seconds / 3600)}h ago`
  return `${Math.round(seconds / 86400)}d ago`
}

export const titleCase = (value) =>
  (value || '')
    .replace(/[_.]/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase())
    .trim()

export const duration = (minutes) => {
  if (!minutes) return '—'
  if (minutes < 60) return `${Math.round(minutes)} min`
  if (minutes < 1440) return `${(minutes / 60).toFixed(1)} h`
  return `${(minutes / 1440).toFixed(1)} d`
}
