import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../lib/api.js'
import { timeOfDay } from '../lib/format.js'
import { STAGE_COLOR } from '../lib/actions.js'

const MAX_ITEMS = 80

/** Live AI activity feed.
 *
 * Seeds from the REST endpoint (so the panel is never empty on load) and then
 * follows the server-sent event stream. EventSource reconnects on its own, so
 * there is no polling fallback to keep in sync.
 */
export default function ActivityFeed({ transactionId = null, height }) {
  const [events, setEvents] = useState([])
  const seen = useRef(new Set())

  useEffect(() => {
    let cancelled = false
    seen.current = new Set()

    api
      .activity({ limit: 40, transaction_id: transactionId })
      .then((rows) => {
        if (cancelled) return
        rows.forEach((r) => seen.current.add(r.id))
        setEvents(rows)
      })
      .catch(() => {})

    const source = new EventSource('/api/activity/stream')
    source.onmessage = (message) => {
      const event = JSON.parse(message.data)
      if (transactionId && event.transaction_id !== transactionId) return
      if (seen.current.has(event.id)) return
      seen.current.add(event.id)
      setEvents((prev) => [event, ...prev].slice(0, MAX_ITEMS))
    }

    return () => {
      cancelled = true
      source.close()
    }
  }, [transactionId])

  if (!events.length) {
    return <div className="empty">Nothing yet. Run the queue or trigger a scenario.</div>
  }

  return (
    <div className="feed" style={height ? { maxHeight: height } : undefined}>
      {events.map((event) => (
        <div key={event.id} className={`feed-item ${event.level}`}>
          <span className="feed-time">{timeOfDay(event.ts)}</span>
          <span className="feed-stage" style={{ color: STAGE_COLOR[event.stage] || 'var(--text-muted)' }}>
            {event.stage}
          </span>
          <span className="feed-msg">
            {event.message}
            {!transactionId && event.transaction_id && (
              <>
                {' '}
                <Link className="txn" to={`/transactions/${event.transaction_id}`}>
                  {event.transaction_id}
                </Link>
              </>
            )}
          </span>
        </div>
      ))}
    </div>
  )
}
