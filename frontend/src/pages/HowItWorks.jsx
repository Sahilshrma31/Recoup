import { useEffect } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ACTION_META, STAGE_COLOR } from '../lib/actions.js'

/* The seven stages, in the order a failed payment travels through them. Stage
   colours are the same ones the live activity feed uses, so a reader who has
   watched the feed recognises the pipeline here without a legend. */
const STAGES = [
  {
    key: 'detect',
    name: 'Detect',
    body:
      'A Razorpay webhook (or a simulated event) lands and is verified, de-duplicated and written before anything else happens. Nothing is inferred from a payload that has not been persisted.',
  },
  {
    key: 'diagnose',
    name: 'Diagnose',
    body:
      'Failure codes lie. The same do_not_honour covers a degraded bank rail and a customer who cannot pay. Recoup separates them with merchant-wide failure rates, the customer’s payment history and the age of the attempt.',
  },
  {
    key: 'predict',
    name: 'Predict',
    body:
      'An additive scorecard produces a recovery probability per candidate action, squashed through a logistic curve so stacked evidence compresses instead of piling up at “97% certain”. Every factor is shown in the UI.',
  },
  {
    key: 'decide',
    name: 'Decide',
    body:
      'Expected value, not intuition: amount × P(success) − cost of acting − fatigue penalty. Each ignored message makes the next one cost more, which is why stopping emerges from the economics.',
  },
  {
    key: 'guard',
    name: 'Guard',
    body:
      'A deterministic policy engine re-plans the decision from scratch and can veto the model outright. Every rule runs on every decision, so the trail records what passed as well as what blocked.',
  },
  {
    key: 'act',
    name: 'Act',
    body:
      'Re-present the charge, issue an alternative payment link, send one reminder, escalate to the merchant — or deliberately do nothing. One decision authorises exactly one idempotent action.',
  },
  {
    key: 'verify',
    name: 'Verify',
    body:
      'Recoup checks whether the money actually arrived, attributes it to the action that earned it, and feeds the outcome back into the next decision for that customer.',
  },
]

const GUARDRAILS = [
  ['futile_retry', 'Never re-present an instrument that cannot work — insufficient funds, dead card'],
  ['attempt_limit', 'At most 2 automatic retries per transaction'],
  ['outreach_limit', 'At most 2 customer messages per transaction'],
  ['customer_opt_out', 'Opted-out customers are never contacted (they may still be silently retried)'],
  ['probability_floor', 'Below 20% recovery probability → no action'],
  ['expected_value_floor', 'The expected recovery must justify the cost of the attempt'],
  ['recovery_window', 'Nothing is attempted after 14 days'],
  ['retry_cooldown', 'A minimum gap is enforced between attempts'],
  ['amount_limit', 'Above ₹10,000 → merchant approval required before anything executes'],
]

const ACTIONS = [
  ['RETRY', 'Re-present the same instrument once the transient condition has cleared.'],
  ['RETRY_DELAYED', 'Hold the retry until the failing rail recovers, rather than burning an attempt now.'],
  ['RETRY_SUBSCRIPTION', 'Re-present a mandate charge on the schedule the subscription allows.'],
  ['CREATE_PAYMENT_LINK', 'Issue an alternative route to pay when the original instrument is dead.'],
  ['SEND_REMINDER', 'One nudge, to a customer who has not opted out and is not already fatigued.'],
  ['NO_ACTION', 'Stop. Another attempt would cost more than the payment is worth.'],
]

const RESULTS = [
  { label: 'Recovery rate', value: '51.7%', foot: 'baseline 29.6% · +22.1 pp' },
  { label: 'Revenue recovered', value: '₹30.0 L', foot: 'baseline ₹16.5 L · +82%' },
  { label: 'Futile retry rate', value: '20.8%', foot: 'baseline 60.5% · −39.7 pp' },
  { label: 'Avg recovery time', value: '47 min', foot: 'baseline 102 min · −54%' },
]

const FAQ = [
  [
    'Is this moving real money?',
    'Only if you configure it to. Without Razorpay keys the agent executes against an in-memory mock gateway, and the bar at the top of every page states which posture is active. A simulated demo can never be mistaken for live money movement.',
  ],
  [
    'What happens when the model is unavailable?',
    'A model outage, a rate limit, a malformed response or a safety refusal all raise LLMUnavailable, and the agent continues on the deterministic path with degraded explanation quality and identical safety. A circuit breaker stops hammering a model that is down. If the AI fails, the payment system does not fail with it.',
  ],
  [
    'What does the model actually see?',
    'A closed allowlist of already-computed features. Names, emails, phone numbers, card data and internal ids never leave the process, so a new column cannot silently start leaking. The model must return a validated schema; anything else is discarded and the deterministic decision stands.',
  ],
  [
    'Where do the measured results come from?',
    'A paired experiment over the same 2,500 transactions, drawn from a hidden ground-truth model that is structurally different from the agent’s scorecard — the agent sees only a noisy emission of the true cause, exactly as a merchant would. The comparison arm is the deterministic agent, so the figures are a floor, not a ceiling.',
  ],
  [
    'How is “recovery rate” defined?',
    'Revenue recovered ÷ estimated recoverable, where estimated recoverable is money already collected plus amount × P for open, already-scored transactions. It is a forecast, and it is labelled as one everywhere it appears.',
  ],
]

export default function HowItWorks() {
  const { hash } = useLocation()

  /* Anchors arrive from the top-bar menu and the footer; the router does not
     scroll to a hash on its own. */
  useEffect(() => {
    if (!hash) {
      window.scrollTo({ top: 0 })
      return
    }
    document.querySelector(hash)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
  }, [hash])

  return (
    <>
      <section className="hero">
        <div className="hero-inner">
          <div className="hero-copy">
            <span className="eyebrow">How Recoup works</span>
            <h1>A failed payment is not lost revenue.</h1>
            <p className="lede">
              It becomes lost revenue when nobody takes the right next action. Recoup answers the
              questions that are actually worth money — why did it fail, is it recoverable, what
              should we do, when, and when should we stop trying?
            </p>
            <div className="hero-cta">
              <Link className="btn btn-primary" to="/overview">
                See it running
                <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                  <path d="M2 6h7M6 3l3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
                </svg>
              </Link>
              <a className="btn" href="#architecture">Read the architecture</a>
            </div>
            <div className="hero-strip">
              {STAGES.map((stage, index) => (
                <span key={stage.key} className="hero-strip-item">
                  <span className="hero-strip-dot" style={{ background: STAGE_COLOR[stage.key] }} />
                  {stage.name}
                  {index < STAGES.length - 1 && <span className="hero-strip-arrow">→</span>}
                </span>
              ))}
            </div>
          </div>

          {/* The signature behaviour, shown rather than described: the model
              proposes, the policy engine disposes. */}
          <div className="hero-figure">
            <div className="decision-mock">
              <div className="mock-head">
                <span className="mock-id">pay_R7x2K9mQ</span>
                <span className="badge badge-neutral">
                  <span className="badge-dot" style={{ background: 'var(--serious)' }} />
                  do_not_honour
                </span>
              </div>
              <div className="mock-row">
                <span className="mock-tag" style={{ color: 'var(--series-1)' }}>Model</span>
                <span className="mock-body">“Retry the payment — the bank looks flaky right now.”</span>
              </div>
              <div className="mock-arrow">↓</div>
              <div className="mock-row blocked">
                <span className="mock-tag" style={{ color: 'var(--critical)' }}>Policy</span>
                <span className="mock-body">
                  <b>BLOCKED · futile_retry</b>
                  <br />
                  insufficient_funds cannot be fixed by re-presenting the same instrument.
                </span>
              </div>
              <div className="mock-arrow">↓</div>
              <div className="mock-row executed">
                <span className="mock-tag" style={{ color: 'var(--series-2)' }}>Executed</span>
                <span className="mock-body">
                  <b>CREATE_PAYMENT_LINK</b> · ₹4,280 · P(success) 0.46
                </span>
              </div>
              <p className="mock-foot">
                Recorded as a policy override, counted on the dashboard, and answerable to
                “why did the AI do this?”
              </p>
            </div>
          </div>
        </div>
      </section>

      <div className="container">
        <section className="section section-first" id="results">
          <div className="grid grid-kpi">
            {RESULTS.map((row) => (
              <div className="stat" key={row.label}>
                <span className="stat-label">{row.label}</span>
                <span className="stat-value">{row.value}</span>
                <span className="stat-foot">{row.foot}</span>
              </div>
            ))}
          </div>
          <p className="note">
            Measured on 2,500 transactions against a retry-once-then-remind baseline —
            standard merchant practice, not a straw man. <b>These are simulated outcomes,
            not production data.</b> Reproduce with <code>python -m scripts.experiment --limit 2500</code>,
            or read the full comparison on <Link to="/experiment">Baseline vs agent</Link>.
          </p>
        </section>

        <section className="section" id="pipeline">
          <div className="section-head">
            <h2 className="section-title">Seven stages, every failed payment</h2>
            <p className="section-sub">
              Within seconds of a failure, a transaction travels the whole pipeline. Every step is
              recorded, so any decision can answer why it was made.
            </p>
          </div>
          <div className="steps">
            {STAGES.map((stage, index) => (
              <article className="step" key={stage.key}>
                <span className="step-rail" style={{ background: STAGE_COLOR[stage.key] }} />
                <span className="step-num">Step {index + 1}</span>
                <h3>{stage.name}</h3>
                <p>{stage.body}</p>
              </article>
            ))}
          </div>
        </section>

        <section className="section" id="architecture">
          <div className="section-head">
            <h2 className="section-title">The LLM reasons. It does not move money.</h2>
            <p className="section-sub">
              The model receives an already-computed deterministic analysis and proposes an action.
              That proposal is then re-planned and re-guarded from scratch: it can reorder the
              candidate actions, but it cannot skip a single check.
            </p>
          </div>

          <div className="flow">
            <span className="flow-node">Transaction</span>
            <span className="flow-arrow">→</span>
            <span className="flow-node">Feature engine</span>
            <span className="flow-arrow">→</span>
            <span className="flow-node">Rule engine</span>
            <span className="flow-arrow">→</span>
            <span className="flow-node advisory">AI reasoning<small>advisory</small></span>
            <span className="flow-arrow">→</span>
            <span className="flow-node">Action planner</span>
            <span className="flow-arrow">→</span>
            <span className="flow-node authoritative">Policy guard<small>authoritative</small></span>
            <span className="flow-arrow">→</span>
            <span className="flow-node">Execution</span>
          </div>

          <div className="split">
            <div className="card">
              <div className="card-head">
                <h2 className="card-title">Deterministic code owns</h2>
              </div>
              <ul className="owner-list">
                {[
                  'Money arithmetic and every rupee figure',
                  'Eligibility, retry counts and limits',
                  'Stopping conditions',
                  'Idempotency keys and the enforced state machine',
                  'All API execution against Razorpay',
                ].map((item) => (
                  <li key={item}>
                    <span className="owner-icon owns">✓</span>
                    {item}
                  </li>
                ))}
              </ul>
            </div>
            <div className="card">
              <div className="card-head">
                <h2 className="card-title">The model contributes</h2>
              </div>
              <ul className="owner-list">
                {[
                  'Ambiguity resolution on a contested failure code',
                  'A preference order over the candidate actions',
                  'The human-readable reason attached to a decision',
                ].map((item) => (
                  <li key={item}>
                    <span className="owner-icon advises">•</span>
                    {item}
                  </li>
                ))}
                {[
                  'It cannot call the executor',
                  'It cannot see names, emails, phone numbers or card data',
                ].map((item) => (
                  <li key={item}>
                    <span className="owner-icon denies">✕</span>
                    {item}
                  </li>
                ))}
              </ul>
              <p className="note">
                If the AI fails, the payment system does not fail with it — the agent falls back to
                the deterministic path with identical safety.
              </p>
            </div>
          </div>
        </section>

        <section className="section" id="guardrails">
          <div className="section-head">
            <h2 className="section-title">Nine checks that can overrule the AI</h2>
            <p className="section-sub">
              Every rule runs on every decision, so the audit trail records what passed as well as
              what blocked.
            </p>
          </div>
          <div className="table-wrap table-static">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 210 }}>Check</th>
                  <th>Rule</th>
                </tr>
              </thead>
              <tbody>
                {GUARDRAILS.map(([name, rule]) => (
                  <tr key={name}>
                    <td className="cell-id">{name}</td>
                    <td>{rule}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </section>

        <section className="section" id="actions">
          <div className="section-head">
            <h2 className="section-title">A bounded action space</h2>
            <p className="section-sub">
              Six actions, not arbitrary API access. Colour follows the action everywhere it appears —
              in the pipeline chart, the queue badge and the decision detail.
            </p>
          </div>
          <div className="feature-grid">
            {ACTIONS.map(([key, body]) => {
              const meta = ACTION_META[key]
              return (
                <article className="feature" key={key}>
                  <span className="feature-dot" style={{ background: meta.color }} />
                  <h3>{meta.label}</h3>
                  <p>{body}</p>
                </article>
              )
            })}
          </div>
        </section>

        <section className="section" id="faq">
          <div className="section-head">
            <h2 className="section-title">Questions worth asking</h2>
            <p className="section-sub">What is real, what is simulated, and what happens when things break.</p>
          </div>
          <div className="faq">
            {FAQ.map(([question, answer]) => (
              <details key={question}>
                <summary>{question}</summary>
                <p>{answer}</p>
              </details>
            ))}
          </div>
        </section>

        <section className="section section-last">
          <div className="cta-band">
            <div>
              <h2>Watch it decide, one payment at a time.</h2>
              <p>
                Run the queue, advance the clock, then open any transaction to see the diagnosis,
                the scorecard behind the decision and every guardrail that ran.
              </p>
            </div>
            <div className="cta-actions">
              <Link className="btn btn-invert" to="/overview">Open the dashboard</Link>
              <Link className="btn btn-ghost-invert" to="/experiment">See the measured comparison</Link>
            </div>
          </div>
        </section>
      </div>
    </>
  )
}
