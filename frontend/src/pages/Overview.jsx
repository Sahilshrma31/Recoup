import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api.js'
import { duration, pct, rupees } from '../lib/format.js'
import { actionMeta } from '../lib/actions.js'
import { Card, Empty, Loading, Meter, Stat } from '../components/Primitives.jsx'
import ActivityFeed from '../components/ActivityFeed.jsx'

const SCENARIOS = [
  { key: 'bank_outage', label: 'Bank outage → delayed retry' },
  { key: 'card_declined', label: 'Card declined → payment link' },
  { key: 'stop', label: 'Exhausted → stop' },
]

export default function Overview() {
  const navigate = useNavigate()
  const [data, setData] = useState(null)
  const [busy, setBusy] = useState(null)

  const load = useCallback(async () => {
    const [overview, pipeline, performance, agent, failures] = await Promise.all([
      api.overview(), api.pipeline(), api.performance(), api.agentStats(), api.failures(),
    ])
    setData({ overview, pipeline, performance, agent, failures })
  }, [])

  useEffect(() => {
    load()
    const id = setInterval(load, 5000)
    return () => clearInterval(id)
  }, [load])

  const run = async (label, fn) => {
    setBusy(label)
    try {
      await fn()
      await new Promise((r) => setTimeout(r, 900))
      await load()
    } finally {
      setBusy(null)
    }
  }

  if (!data) return <Loading label="Loading dashboard" />

  const { overview, pipeline, performance, agent } = data
  const maxCount = Math.max(1, ...pipeline.map((p) => p.count))
  const forecastOnly = overview.estimated_recoverable_paise - overview.revenue_recovered_paise

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">Revenue recovery</h1>
          <p className="page-sub">
            Every failed payment, diagnosed and acted on automatically — and deliberately
            left alone when another attempt would cost more than it is worth.
          </p>
        </div>
        <div className="controls" style={{ margin: 0 }}>
          <button className="btn" disabled={!!busy} onClick={() => run('queue', () => api.runQueue(40))}>
            {busy === 'queue' ? <span className="spinner" /> : null} Analyse 40
          </button>
          <button className="btn" disabled={!!busy} onClick={() => run('tick', api.tick)}>
            {busy === 'tick' ? <span className="spinner" /> : null} Advance clock
          </button>
          <button className="btn btn-danger" disabled={!!busy} onClick={() => run('reset', () => api.reset('all'))}>
            Reset
          </button>
        </div>
      </div>

      {/* Exactly one hero figure on this view: the number the product exists for. */}
      <div className="grid grid-kpi" style={{ marginBottom: 16 }}>
        <Stat
          hero
          label="Revenue recovered"
          value={rupees(overview.revenue_recovered_paise, { compact: true })}
          foot={`${overview.recovered_transactions.toLocaleString('en-IN')} payments recovered`}
        />
        <Stat
          label="Revenue at risk"
          value={rupees(overview.revenue_at_risk_paise, { compact: true })}
          foot={`${overview.at_risk_transactions.toLocaleString('en-IN')} open transactions`}
        />
        <Stat
          label="Estimated recoverable"
          value={rupees(overview.estimated_recoverable_paise, { compact: true })}
          foot={`incl. ${rupees(forecastOnly, { compact: true })} forecast on open items`}
        />
        <Stat
          label="Recovery rate"
          value={pct(overview.recovery_rate)}
          foot="recovered ÷ estimated recoverable"
        />
      </div>

      <div className="grid grid-2" style={{ marginBottom: 16 }}>
        <Card
          title="Recovery pipeline"
          note={`${pipeline.reduce((n, p) => n + p.count, 0).toLocaleString('en-IN')} decided`}
        >
          {pipeline.length === 0 ? (
            <Empty>No decisions yet — press “Analyse 40”.</Empty>
          ) : (
            <>
              {pipeline.map((row) => {
                const meta = actionMeta(row.action)
                return (
                  <Meter
                    key={row.action}
                    color={meta.color}
                    name={meta.label}
                    value={row.count}
                    max={maxCount}
                    valueLabel={row.count.toLocaleString('en-IN')}
                    subLabel={rupees(row.value_paise, { compact: true })}
                    title={`${meta.label}: ${row.count} transactions, ${rupees(row.value_paise)} at stake`}
                  />
                )
              })}
              <p className="note">
                Current recommended action per open transaction. “No action” is a decision
                the agent makes on expected value, not a queue it failed to reach.
              </p>
            </>
          )}
        </Card>

        <Card title="AI activity" note="live">
          <ActivityFeed height={330} />
        </Card>
      </div>

      <div className="grid grid-2">
        <Card title="Agent performance">
          <div className="grid grid-kpi" style={{ gap: 12 }}>
            <Stat label="Action precision" value={pct(performance.action_precision)}
                  foot={`${performance.attempts_completed} attempts resolved`} />
            <Stat label="False retry rate" value={pct(performance.false_retry_rate)}
                  foot={`${performance.retries_executed} retries executed`} />
            <Stat label="Recovery time" value={duration(performance.avg_agent_recovery_minutes)}
                  foot="first action → money in" />
            <Stat label="Deliberately stopped" value={performance.deliberately_stopped.toLocaleString('en-IN')}
                  foot="not worth another attempt" />
          </div>
          <p className="note">
            <b>{agent.policy_overrides.toLocaleString('en-IN')}</b> of {agent.decisions.toLocaleString('en-IN')} decisions
            ({pct(agent.override_rate)}) had the agent’s first-choice action overruled by the
            deterministic policy engine — usually a retry that could not have worked.
          </p>
        </Card>

        <Card title="Run a scenario" note="pinned demo cases">
          {SCENARIOS.map((s) => (
            <button
              key={s.key}
              className="btn"
              style={{ width: '100%', justifyContent: 'flex-start', marginBottom: 8 }}
              disabled={!!busy}
              onClick={() =>
                run(s.key, async () => {
                  const res = await api.scenario(s.key)
                  setTimeout(() => navigate(`/transactions/${res.transaction_id}`), 1200)
                })
              }
            >
              {busy === s.key ? <span className="spinner" /> : null} {s.label}
            </button>
          ))}
          <p className="note">
            Each resets one pinned transaction and pushes it through the live agent path —
            the same code a Razorpay webhook triggers.
          </p>
        </Card>
      </div>
    </>
  )
}
