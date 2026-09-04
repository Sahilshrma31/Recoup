import { useEffect, useState } from 'react'
import { NavLink, Navigate, Route, Routes } from 'react-router-dom'
import Overview from './pages/Overview.jsx'
import Transactions from './pages/Transactions.jsx'
import TransactionDetail from './pages/TransactionDetail.jsx'
import Experiment from './pages/Experiment.jsx'
import { api } from './lib/api.js'

export default function App() {
  const [health, setHealth] = useState(null)

  useEffect(() => {
    const load = () => api.health().then(setHealth).catch(() => setHealth(null))
    load()
    const id = setInterval(load, 15000)
    return () => clearInterval(id)
  }, [])

  const live = health?.execution_mode === 'live-razorpay'
  const running = health?.runtime?.running

  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="brand-mark">
            <span className={`brand-dot${running ? '' : ' idle'}`} />
            Recoup
          </span>
          <span className="brand-sub">Razorpay revenue recovery</span>
        </div>

        <nav className="nav">
          <NavLink to="/overview">Overview</NavLink>
          <NavLink to="/transactions">At-risk queue</NavLink>
          <NavLink to="/experiment">Baseline vs agent</NavLink>
        </nav>

        <div className="sidebar-foot">
          {/* Deployment posture is stated on screen so a simulated demo can
              never be mistaken for live money movement. */}
          <div className="mode-chip">
            Execution: <b>{live ? 'Live Razorpay' : 'Simulated'}</b>
            <br />
            Reasoning: <b>{health?.ai_configured ? health.model : 'Rules only'}</b>
            <br />
            Auto-action cap: <b>₹{(health?.policy?.auto_action_limit_rupees ?? 0).toLocaleString('en-IN')}</b>
          </div>
          <div className="mode-chip">
            Queue depth: <b>{health?.runtime?.queue_depth ?? 0}</b>
            <br />
            Processed: <b>{health?.runtime?.processed ?? 0}</b>
          </div>
        </div>
      </aside>

      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/overview" element={<Overview />} />
          <Route path="/transactions" element={<Transactions />} />
          <Route path="/transactions/:id" element={<TransactionDetail />} />
          <Route path="/experiment" element={<Experiment />} />
        </Routes>
      </main>
    </div>
  )
}
