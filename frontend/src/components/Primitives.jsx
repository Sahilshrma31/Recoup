import { actionMeta, stateMeta } from '../lib/actions.js'

export function Card({ title, note, children, className = '', action }) {
  return (
    <section className={`card ${className}`}>
      {(title || note || action) && (
        <header className="card-head">
          <div>
            {title && <h2 className="card-title">{title}</h2>}
          </div>
          {action || (note && <span className="card-note">{note}</span>)}
        </header>
      )}
      {children}
    </section>
  )
}

/** Stat tile: label · value · optional footnote. `hero` is the one big number. */
export function Stat({ label, value, foot, hero = false, delta }) {
  return (
    <div className={`stat${hero ? ' hero' : ''}`}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {delta !== undefined && delta !== null && (
        <span className={`delta ${delta >= 0 ? 'up' : 'down'}`}>
          {delta >= 0 ? '▲' : '▼'} {Math.abs(delta).toFixed(1)} pp
        </span>
      )}
      {foot && <span className="stat-foot">{foot}</span>}
    </div>
  )
}

/** Horizontal bar meter. The fill carries action identity; the track is inert. */
export function Meter({ color, name, value, max, valueLabel, subLabel, title }) {
  const width = max > 0 ? Math.max(1.2, (value / max) * 100) : 0
  return (
    <div className="meter-row" title={title}>
      <span className="meter-label">
        <span className="meter-swatch" style={{ background: color }} />
        <span className="meter-name">{name}</span>
      </span>
      <div className="meter-track">
        <div className="meter-fill" style={{ width: `${width}%`, background: color }} />
      </div>
      <span className="meter-value">
        <b>{valueLabel}</b>
        {subLabel && <> · {subLabel}</>}
      </span>
    </div>
  )
}

export function ActionBadge({ action }) {
  const meta = actionMeta(action)
  return (
    <span className="badge badge-neutral" title={meta.label}>
      <span className="badge-dot" style={{ background: meta.color }} />
      {meta.short}
    </span>
  )
}

export function StateBadge({ state }) {
  const meta = stateMeta(state)
  return (
    <span className="badge badge-neutral">
      <span className="badge-dot" style={{ background: meta.color }} />
      {meta.label}
    </span>
  )
}

export function Empty({ children }) {
  return <div className="empty">{children}</div>
}

export function Loading({ label = 'Loading' }) {
  return (
    <div className="empty">
      <span className="spinner" /> <span style={{ marginLeft: 8 }}>{label}…</span>
    </div>
  )
}
