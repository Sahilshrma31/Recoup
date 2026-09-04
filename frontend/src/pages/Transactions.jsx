import { useCallback, useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../lib/api.js'
import { pct, relativeTime, rupees, titleCase } from '../lib/format.js'
import { ActionBadge, Empty, Loading, StateBadge } from '../components/Primitives.jsx'

const PAGE_SIZE = 40

export default function Transactions() {
  const navigate = useNavigate()
  const [rows, setRows] = useState(null)
  const [total, setTotal] = useState(0)
  const [offset, setOffset] = useState(0)
  const [filters, setFilters] = useState({ status: 'at_risk', action: '', method: '', sort: 'value', search: '' })

  const load = useCallback(async () => {
    const data = await api.transactions({ ...filters, limit: PAGE_SIZE, offset })
    setRows(data.items)
    setTotal(data.total)
  }, [filters, offset])

  useEffect(() => { load() }, [load])

  const update = (key) => (event) => {
    setOffset(0)
    setFilters((prev) => ({ ...prev, [key]: event.target.value }))
  }

  return (
    <>
      <div className="page-head">
        <div>
          <h1 className="page-title">At-risk queue</h1>
          <p className="page-sub">
            {total.toLocaleString('en-IN')} transactions, highest value first. Open one to see
            the diagnosis, the scorecard behind the decision and every guardrail that ran.
          </p>
        </div>
      </div>

      {/* Filters sit in one row above the data, never interleaved with it. */}
      <div className="controls">
        <select value={filters.status} onChange={update('status')}>
          <option value="at_risk">At risk</option>
          <option value="recovered">Recovered</option>
          <option value="stopped">Stopped</option>
          <option value="all">All non-captured</option>
        </select>
        <select value={filters.action} onChange={update('action')}>
          <option value="">Any action</option>
          <option value="RETRY">Retry</option>
          <option value="RETRY_DELAYED">Delayed retry</option>
          <option value="CREATE_PAYMENT_LINK">Payment link</option>
          <option value="SEND_REMINDER">Reminder</option>
          <option value="RETRY_SUBSCRIPTION">Subscription retry</option>
          <option value="NO_ACTION">No action</option>
        </select>
        <select value={filters.method} onChange={update('method')}>
          <option value="">Any method</option>
          <option value="upi">UPI</option>
          <option value="card">Card</option>
          <option value="netbanking">Netbanking</option>
          <option value="wallet">Wallet</option>
        </select>
        <select value={filters.sort} onChange={update('sort')}>
          <option value="value">Sort: value</option>
          <option value="recent">Sort: most recent</option>
          <option value="probability">Sort: recovery probability</option>
        </select>
        <input
          type="search"
          placeholder="Search id, customer, email…"
          value={filters.search}
          onChange={update('search')}
        />
      </div>

      {rows === null ? (
        <Loading label="Loading queue" />
      ) : rows.length === 0 ? (
        <Empty>No transactions match these filters.</Empty>
      ) : (
        <>
          <div className="table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Transaction</th>
                  <th>Customer</th>
                  <th className="num">Amount</th>
                  <th>Method</th>
                  <th>Failure</th>
                  <th>Diagnosis</th>
                  <th>Action</th>
                  <th className="num">Recovery&nbsp;P</th>
                  <th className="num">Expected</th>
                  <th>State</th>
                </tr>
              </thead>
              <tbody>
                {rows.map((row) => (
                  <tr key={row.id} onClick={() => navigate(`/transactions/${row.id}`)}>
                    <td>
                      <div className="cell-id">{row.id}</div>
                      <div className="cell-sub">{relativeTime(row.failed_at)}</div>
                    </td>
                    <td>
                      <div>{row.customer_name}</div>
                      <div className="cell-sub">
                        {row.retry_count} retries · {row.outreach_count} messages
                      </div>
                    </td>
                    <td className="num">{rupees(row.amount_paise)}</td>
                    <td>{row.method.toUpperCase()}</td>
                    <td className="cell-sub">{row.failure_reason || '—'}</td>
                    <td className="cell-sub">{row.diagnosis ? titleCase(row.diagnosis) : '—'}</td>
                    <td>{row.recommended_action ? <ActionBadge action={row.recommended_action} /> : <span className="cell-sub">not analysed</span>}</td>
                    <td className="num prob">{row.recovery_probability != null ? pct(row.recovery_probability, 0) : '—'}</td>
                    <td className="num">{row.expected_recovery_paise != null ? rupees(row.expected_recovery_paise) : '—'}</td>
                    <td><StateBadge state={row.recovery_state} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>

          <div className="pager">
            <span>
              {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total.toLocaleString('en-IN')}
            </span>
            <button className="btn" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}>
              Previous
            </button>
            <button className="btn" disabled={offset + PAGE_SIZE >= total} onClick={() => setOffset(offset + PAGE_SIZE)}>
              Next
            </button>
          </div>
        </>
      )}
    </>
  )
}
