/** Action identity: one colour per action, used in every view.
 *
 * Colour follows the entity, never its rank -- a payment link is the same
 * orange in the pipeline chart, the queue badge and the decision detail, so a
 * filter that changes which actions are on screen never repaints the rest.
 *
 * Slots 1-3 of the categorical palette are validated all-pairs for this dark
 * surface; the retry family shares slot 1 because "re-present the charge" is
 * one idea, and the two non-series outcomes (stop, escalate) use muted ink and
 * a reserved status colour rather than a fourth series hue.
 */

export const ACTION_META = {
  RETRY:               { label: 'Retry',                color: 'var(--series-1)', short: 'Retry' },
  RETRY_DELAYED:       { label: 'Delayed retry',        color: 'var(--series-1)', short: 'Retry +delay' },
  RETRY_SUBSCRIPTION:  { label: 'Subscription retry',   color: 'var(--series-1)', short: 'Sub retry' },
  CREATE_PAYMENT_LINK: { label: 'Payment link',         color: 'var(--series-2)', short: 'Link' },
  SEND_REMINDER:       { label: 'Customer reminder',    color: 'var(--series-3)', short: 'Reminder' },
  ESCALATE:            { label: 'Escalate to merchant', color: 'var(--serious)',  short: 'Escalate' },
  NO_ACTION:           { label: 'No action (stop)',     color: 'var(--text-muted)', short: 'Stop' },
}

export const actionMeta = (action) =>
  ACTION_META[action] || { label: action || '—', color: 'var(--text-muted)', short: action || '—' }

/** Recovery state -> reserved status colour. Never used as a series colour. */
export const STATE_META = {
  DETECTED:          { color: 'var(--text-muted)', label: 'Detected' },
  ANALYZING:         { color: 'var(--series-1)',   label: 'Analysing' },
  PLANNED:           { color: 'var(--series-1)',   label: 'Planned' },
  AWAITING_APPROVAL: { color: 'var(--warning)',    label: 'Needs approval' },
  EXECUTING:         { color: 'var(--series-1)',   label: 'Executing' },
  ATTEMPT_FAILED:    { color: 'var(--serious)',    label: 'Attempt failed' },
  RECOVERED:         { color: 'var(--good)',       label: 'Recovered' },
  STOPPED:           { color: 'var(--text-muted)', label: 'Stopped' },
}

export const stateMeta = (state) =>
  STATE_META[state] || { color: 'var(--text-muted)', label: state || '—' }

export const STAGE_COLOR = {
  detect:   'var(--text-muted)',
  diagnose: 'var(--series-1)',
  predict:  'var(--series-3)',
  decide:   'var(--text-primary)',
  guard:    'var(--warning)',
  act:      'var(--series-2)',
  verify:   'var(--good)',
}
