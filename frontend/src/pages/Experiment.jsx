import { useEffect, useState } from 'react'
import { api } from '../lib/api.js'
import { duration, pct, rupees } from '../lib/format.js'
import { Card, Empty, Loading } from '../components/Primitives.jsx'

/* Two series -> a legend is always present, and each bar is directly labelled,
 * so identity never rests on colour alone. Slots 1 and 2 of the categorical
 * palette; the delta is text in an ink token, never a third series colour. */
const BASELINE_COLOR = 'var(--series-2)'
const AGENT_COLOR = 'var(--series-1)'

/** Metric rows. `better` says which direction is good, so the delta colour is
 *  direction x whether up is good rather than just the sign. */
const METRICS = [
  { key: 'recovery_rate', label: 'Recovery rate', kind: 'pct', better: 'up' },
  { key: 'revenue_recovered_paise', label: 'Revenue recovered', kind: 'money', better: 'up' },
  { key: 'value_recovery_rate', label: 'Value recovery rate', kind: 'pct', better: 'up' },
  { key: 'futile_retry_rate', label: 'Futile retries', kind: 'pct', better: 'down' },
  { key: 'customer_contact_rate', label: 'Customers contacted', kind: 'pct', better: 'down' },
  { key: 'retries_executed', label: 'Retries executed', kind: 'count', better: 'down' },
  { key: 'avg_recovery_minutes', label: 'Avg recovery time', kind: 'minutes', better: 'down' },
]

function formatValue(value, kind) {
  if (kind === 'pct') return pct(value)
  if (kind === 'money') return rupees(value, { compact: true })
  if (kind === 'minutes') return duration(value)
  return (value || 0).toLocaleString('en-IN')
}

function Row({ metric, baseline, agent }) {
  const b = baseline[metric.key] || 0
  const a = agent[metric.key] || 0
  const max = Math.max(b, a) || 1

  const improved = metric.better === 'up' ? a > b : a < b
  const changed = Math.abs(a - b) > 1e-9
  let deltaText = '—'
  if (changed) {
    const sign = a - b >= 0 ? '+' : ''
    if (metric.kind === 'pct') {
      deltaText = `${sign}${((a - b) * 100).toFixed(1)} pp`
    } else if (metric.kind === 'money') {
      deltaText = b ? `${a / b - 1 >= 0 ? '+' : ''}${((a / b - 1) * 100).toFixed(0)}%` : '—'
    } else if (metric.kind === 'minutes') {
      // Relative change reads better than a raw minute delta on a duration.
      deltaText = b ? `${a / b - 1 >= 0 ? '+' : ''}${((a / b - 1) * 100).toFixed(0)}%` : '—'
    } else {
      deltaText = `${sign}${(a - b).toLocaleString('en-IN', { maximumFractionDigits: 0 })}`
    }
  }

  return (
    <div className="ab-row">
      <div className="ab-metric">
        <span className="ab-metric-name">{metric.label}</span>
        <span
          className="ab-metric-delta"
          style={{ color: !changed ? 'var(--text-muted)' : improved ? 'var(--good)' : 'var(--critical)' }}
        >
          {deltaText}
        </span>
      </div>
      <div className="ab-bars">
        <div className="ab-bar-line" title={`Baseline: ${formatValue(b, metric.kind)}`}>
          <div className="ab-track">
            <div className="ab-fill" style={{ width: `${(b / max) * 100}%`, background: BASELINE_COLOR }} />
          </div>
          <span className="ab-num">{formatValue(b, metric.kind)}</span>
        </div>
        <div className="ab-bar-line" title={`Recovery agent: ${formatValue(a, metric.kind)}`}>
          <div className="ab-track">
            <div className="ab-fill" style={{ width: `${(a / max) * 100}%`, background: AGENT_COLOR }} />
          </div>
          <span className="ab-num">{formatValue(a, metric.kind)}</span>
        </div>
      </div>
    </div>
  )
}

export default function Experiment() {
  const [data, setData] = useState(undefined)

  useEffect(() => {
    api.experiment().then(setData).catch(() => setData(null))
  }, [])

  if (data === undefined) return <Loading label="Loading experiment" />
  if (!data) {
    return (
      <>
        <div className="page-head">
          <div>
            <h1 className="page-title">Baseline vs agent</h1>
          </div>
        </div>
        <Empty>
          No experiment has been run yet. From <code>backend/</code>:
          <div style={{ fontFamily: 'var(--mono)', marginTop: 10, color: 'var(--text-secondary)' }}>
            python -m scripts.experiment --limit 2000
          </div>
        </Empty>
      </>
    )
  }

  const { baseline, agent } = data.arms

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Baseline vs agent</h1>
          <p className="page-sub">
            The same {data.transactions.toLocaleString('en-IN')} transactions replayed through both
            strategies, paired on an identical random stream so the difference is the policy and
            not luck. Outcomes are sampled from a hidden ground-truth model the agent cannot read.
          </p>
        </div>
      </div>

      <div className="grid grid-kpi" style={{ marginBottom: 16 }}>
        <div className="stat hero">
          <span className="stat-label">Additional revenue recovered</span>
          <span className="stat-value">
            {rupees(agent.revenue_recovered_paise - baseline.revenue_recovered_paise, { compact: true })}
          </span>
          <span className="stat-foot">
            {rupees(baseline.revenue_recovered_paise, { compact: true })} → {rupees(agent.revenue_recovered_paise, { compact: true })}
          </span>
        </div>
        <div className="stat">
          <span className="stat-label">Recovery rate</span>
          <span className="stat-value">{pct(agent.recovery_rate)}</span>
          <span className="delta up">▲ {((agent.recovery_rate - baseline.recovery_rate) * 100).toFixed(1)} pp vs baseline</span>
        </div>
        <div className="stat">
          <span className="stat-label">Futile retries avoided</span>
          <span className="stat-value">{pct(baseline.futile_retry_rate - agent.futile_retry_rate)}</span>
          <span className="stat-foot">of all retries, vs baseline</span>
        </div>
        <div className="stat">
          <span className="stat-label">Deliberately stopped</span>
          <span className="stat-value">{agent.deliberately_stopped.toLocaleString('en-IN')}</span>
          <span className="stat-foot">baseline never stops</span>
        </div>
      </div>

      <Card title="Measured comparison" note={`seed ${data.seed} · ${data.counterfactual.replace(/_/g, ' ')}`}>
        <div className="ab-legend">
          <span><span className="ab-key" style={{ background: BASELINE_COLOR }} /> Baseline — retry once, then remind</span>
          <span><span className="ab-key" style={{ background: AGENT_COLOR }} /> Recovery agent</span>
        </div>
        {METRICS.map((metric) => (
          <Row key={metric.key} metric={metric} baseline={baseline} agent={agent} />
        ))}
        <p className="note">
          <b>How to read this.</b> The baseline is not a straw man — retry-once-then-remind is what
          most merchants actually do, and it recovers real money. The agent arm here is the{' '}
          <b>deterministic</b> agent (diagnosis, scorecard and policy engine, no model calls), so
          this is a floor rather than a ceiling: the LLM layer adds ambiguity resolution on top.
          These are simulated outcomes, not production data — the simulator’s ground truth is a
          separate model from the agent’s scorecard, and the agent only ever sees the noisy failure
          code a merchant would see.
        </p>
      </Card>
    </>
  )
}
