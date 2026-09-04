import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { api } from '../lib/api.js'
import { pct, relativeTime, rupees, timeOfDay, titleCase } from '../lib/format.js'
import { actionMeta } from '../lib/actions.js'
import { ActionBadge, Card, Empty, Loading, StateBadge } from '../components/Primitives.jsx'
import ActivityFeed from '../components/ActivityFeed.jsx'

const CHECK_ICON = { passed: '✓', blocked: '✕', requires_approval: '!' }
const CHECK_COLOR = {
  passed: 'var(--good)',
  blocked: 'var(--critical)',
  requires_approval: 'var(--warning)',
}

export default function TransactionDetail() {
  const { id } = useParams()
  const [txn, setTxn] = useState(null)
  const [busy, setBusy] = useState(null)
  const [error, setError] = useState(null)

  const load = useCallback(() => api.transaction(id).then(setTxn).catch((e) => setError(e.message)), [id])

  useEffect(() => {
    setTxn(null)
    load()
    const timer = setInterval(load, 4000)
    return () => clearInterval(timer)
  }, [load])

  const act = async (label, fn) => {
    setBusy(label)
    setError(null)
    try {
      await fn()
      await new Promise((r) => setTimeout(r, 700))
      await load()
    } catch (e) {
      setError(e.message)
    } finally {
      setBusy(null)
    }
  }

  if (error && !txn) return <Empty>{error}</Empty>
  if (!txn) return <Loading label="Loading transaction" />

  const decision = txn.decisions[0] || null
  const policy = decision?.policy_result || {}
  const checks = policy.checks || []
  const scores = decision?.action_scores || []
  const chosen = scores.find((s) => s.action === decision?.action)
  const awaitingApproval = txn.recovery_state === 'AWAITING_APPROVAL'
  const closed = txn.recovery_state === 'RECOVERED'
  // An action is already in motion. Offering "Recover" here would ask the agent
  // to authorise a second one against money that is already moving.
  const inFlight = txn.recovery_state === 'EXECUTING'

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">
            {rupees(txn.amount_paise)} · {txn.method.toUpperCase()}
          </h1>
          <p className="page-sub">
            <Link to="/transactions" style={{ color: 'var(--series-1)' }}>← At-risk queue</Link>
            {'  ·  '}<span style={{ fontFamily: 'var(--mono)' }}>{txn.id}</span>
            {'  ·  '}failed {relativeTime(txn.failed_at)}
          </p>
        </div>
        <div className="controls" style={{ margin: 0 }}>
          <StateBadge state={txn.recovery_state} />
          {inFlight && (
            <span className="cell-sub" style={{ alignSelf: 'center' }}>
              Action in flight — waiting for it to resolve
            </span>
          )}
          {!closed && !inFlight && (
            <>
              <button className="btn" disabled={!!busy} onClick={() => act('analyze', () => api.analyze(txn.id))}>
                {busy === 'analyze' ? <span className="spinner" /> : null} Re-analyse
              </button>
              {awaitingApproval ? (
                <button className="btn btn-primary" disabled={!!busy} onClick={() => act('approve', () => api.approve(txn.id))}>
                  {busy === 'approve' ? <span className="spinner" /> : null} Approve &amp; execute
                </button>
              ) : (
                <button className="btn btn-primary" disabled={!!busy} onClick={() => act('recover', () => api.recover(txn.id))}>
                  {busy === 'recover' ? <span className="spinner" /> : null} Recover payment
                </button>
              )}
              <button className="btn btn-danger" disabled={!!busy} onClick={() => act('stop', () => api.stop(txn.id))}>
                Stop
              </button>
            </>
          )}
        </div>
      </div>

      {error && <div className="card" style={{ borderColor: 'var(--critical)', marginBottom: 16 }}>{error}</div>}

      <div className="detail-grid">
        <div className="grid" style={{ gap: 16 }}>
          {decision ? (
            <Card
              title="AI analysis"
              note={`${decision.source === 'rules' ? 'Deterministic rules' : decision.model || 'model'} · ${titleCase(decision.category.slice(2))}`}
            >
              <div className="reason">{decision.reasoning_summary}</div>

              <div className="grid grid-kpi" style={{ gap: 12, marginTop: 16 }}>
                <div className="stat" style={{ padding: '12px 14px' }}>
                  <span className="stat-label">Likely cause</span>
                  <span className="stat-value" style={{ fontSize: 17 }}>{titleCase(decision.diagnosis)}</span>
                  <span className="stat-foot">{pct(decision.diagnosis_confidence, 0)} confidence</span>
                </div>
                <div className="stat" style={{ padding: '12px 14px' }}>
                  <span className="stat-label">Recovery probability</span>
                  <span className="stat-value" style={{ fontSize: 17 }}>{pct(decision.recovery_probability, 0)}</span>
                  <span className="stat-foot">for the chosen action</span>
                </div>
                <div className="stat" style={{ padding: '12px 14px' }}>
                  <span className="stat-label">Expected recovery</span>
                  <span className="stat-value" style={{ fontSize: 17 }}>{rupees(decision.expected_recovery_paise)}</span>
                  <span className="stat-foot">amount × probability</span>
                </div>
                <div className="stat" style={{ padding: '12px 14px' }}>
                  <span className="stat-label">Decision</span>
                  <span className="stat-value" style={{ fontSize: 17 }}>{actionMeta(decision.action).label}</span>
                  <span className="stat-foot">
                    {decision.delay_minutes ? `after ${decision.delay_minutes} min` : 'immediately'}
                  </span>
                </div>
              </div>

              {decision.recommended_action !== decision.action && (
                <div className="reason" style={{ borderLeftColor: 'var(--warning)', marginTop: 16 }}>
                  <b>Policy override.</b> The agent’s first choice was{' '}
                  <b>{actionMeta(decision.recommended_action).label}</b>; the guardrails
                  rejected it and {actionMeta(decision.action).label.toLowerCase()} was executed instead.
                  {policy.override_reason && <div style={{ marginTop: 6, color: 'var(--text-secondary)' }}>{policy.override_reason}</div>}
                </div>
              )}
            </Card>
          ) : (
            <Card title="AI analysis">
              <Empty>Not analysed yet — press “Recover payment” or “Re-analyse”.</Empty>
            </Card>
          )}

          {scores.length > 0 && (
            <Card title="Why this action" note="expected value per candidate">
              {scores.map((score) => {
                const meta = actionMeta(score.action)
                const isChosen = score.action === decision?.action
                return (
                  <div key={score.action} className={`action-card${isChosen ? ' chosen' : ''}`}>
                    <div className="action-card-head">
                      <span style={{ display: 'flex', alignItems: 'center', gap: 8, fontWeight: 600 }}>
                        <span className="meter-swatch" style={{ background: meta.color }} />
                        {meta.label}
                        {isChosen && <span className="badge badge-neutral" style={{ marginLeft: 4 }}>chosen</span>}
                      </span>
                      <span style={{ fontVariantNumeric: 'tabular-nums', fontSize: 12.5, color: 'var(--text-secondary)' }}>
                        {pct(score.probability, 0)} · {rupees(score.expected_value_paise)} expected
                        {' · net '}{rupees(score.net_expected_value_paise)}
                      </span>
                    </div>
                    {isChosen &&
                      score.factors.map((factor, i) => (
                        <div className="factor" key={i}>
                          <span className="factor-label">{factor.label}</span>
                          <span className={`factor-delta ${factor.delta >= 0 ? 'pos' : 'neg'}`}>
                            {factor.delta > 0 ? '+' : ''}{factor.delta}
                          </span>
                        </div>
                      ))}
                  </div>
                )
              })}
              <p className="note">
                Scores are points on a transparent scorecard, squashed to a probability.
                Net expected value subtracts the cost of acting — including the cost of
                spending another message on a customer who has already ignored several.
              </p>
            </Card>
          )}

          {checks.length > 0 && (
            <Card title="Policy checks" note={policy.requires_approval ? 'approval required' : 'all evaluated'}>
              {checks.map((check) => (
                <div key={check.name} className={`check ${check.status}`}>
                  <span className="check-icon" style={{ color: CHECK_COLOR[check.status] }}>
                    {CHECK_ICON[check.status] || '·'}
                  </span>
                  <span className="check-name">{check.name}</span>
                  <span className="check-detail">{check.detail}</span>
                </div>
              ))}
              <p className="note">
                Every rule runs on every decision, so the audit trail records the checks that
                passed as well as the one that blocked. A model recommendation is a proposal;
                only this layer authorises an action.
              </p>
            </Card>
          )}
        </div>

        <div className="grid" style={{ gap: 16 }}>
          <Card title="Transaction">
            <dl className="kv">
              <dt>Amount</dt><dd>{rupees(txn.amount_paise)} {txn.currency}</dd>
              <dt>Customer</dt><dd>{txn.customer.name}</dd>
              <dt>Payment method</dt><dd>{txn.method.toUpperCase()}{txn.card_type ? ` · ${txn.card_type}` : ''}{txn.bank ? ` · ${txn.bank}` : ''}</dd>
              <dt>Status</dt><dd>{titleCase(txn.status)}</dd>
              <dt>Failure reason</dt><dd>{txn.failure_reason || '—'}</dd>
              <dt>Previous payments</dt>
              <dd>{txn.customer.successful_payments} successful · {txn.customer.failed_payments} failed</dd>
              <dt>Lifetime value</dt><dd>{rupees(txn.customer.lifetime_value_paise)}</dd>
              <dt>Contactable</dt><dd>{txn.customer.opted_out ? 'Opted out' : 'Yes'}</dd>
              <dt>Recovery attempts</dt><dd>{txn.retry_count} retries · {txn.outreach_count} messages</dd>
              {txn.recovered_at && (<><dt>Recovered</dt><dd style={{ color: 'var(--good)' }}>{rupees(txn.recovered_amount_paise)} · {relativeTime(txn.recovered_at)}</dd></>)}
              {txn.stop_reason && (<><dt>Stopped because</dt><dd>{titleCase(txn.stop_reason)}</dd></>)}
            </dl>
          </Card>

          <Card title="Recovery attempts" note={`${txn.attempts.length} executed`}>
            {txn.attempts.length === 0 ? (
              <Empty>No action taken yet.</Empty>
            ) : (
              txn.attempts.map((attempt) => (
                <div className="action-card" key={attempt.id}>
                  <div className="action-card-head">
                    <ActionBadge action={attempt.action} />
                    <span style={{ fontSize: 12, color: 'var(--text-secondary)' }}>{titleCase(attempt.status)}</span>
                  </div>
                  <div className="cell-sub" style={{ fontFamily: 'var(--mono)', fontSize: 11 }}>
                    {attempt.idempotency_key}
                  </div>
                  {attempt.provider_url && (
                    <div className="cell-sub" style={{ marginTop: 4 }}>Link: {attempt.provider_url}</div>
                  )}
                  {attempt.scheduled_for && !attempt.executed_at && (
                    <div className="cell-sub" style={{ marginTop: 4 }}>Scheduled for {timeOfDay(attempt.scheduled_for)}</div>
                  )}
                  {attempt.error && (
                    <div className="cell-sub" style={{ marginTop: 4, color: 'var(--serious)' }}>{attempt.error}</div>
                  )}
                </div>
              ))
            )}
          </Card>

          <Card title="Decision trail" note="live">
            <ActivityFeed transactionId={txn.id} height={320} />
          </Card>
        </div>
      </div>
    </>
  )
}
