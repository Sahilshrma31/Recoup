import { useEffect, useState } from 'react'
import { Link, NavLink, Navigate, Route, Routes, useLocation } from 'react-router-dom'
import Overview from './pages/Overview.jsx'
import Transactions from './pages/Transactions.jsx'
import TransactionDetail from './pages/TransactionDetail.jsx'
import Experiment from './pages/Experiment.jsx'
import HowItWorks from './pages/HowItWorks.jsx'
import { BrandLockup } from './components/Brand.jsx'
import { api } from './lib/api.js'

/** Model ids are vendor-prefixed and too long for the chrome; the vendor is
 *  already shown beside it, so display just the model name. */
function modelLabel(model) {
  if (!model) return 'Rules only'
  return model.includes('/') ? model.split('/').pop() : model
}

/** Sections of the How-it-works page, surfaced as a nav dropdown the way
 *  Razorpay hangs a product menu off a caret in its top bar. */
const HOW_MENU = [
  { to: '/how-it-works#pipeline', title: 'The seven stages', desc: 'Detect → diagnose → act → verify' },
  { to: '/how-it-works#architecture', title: 'Why the AI cannot move money', desc: 'Advisory model, authoritative policy engine' },
  { to: '/how-it-works#guardrails', title: 'Guardrails', desc: 'Nine checks that can overrule the model' },
  { to: '/how-it-works#actions', title: 'Action catalogue', desc: 'The six things the agent may do' },
  { to: '/how-it-works#faq', title: 'Questions', desc: 'What is real, what is simulated' },
]

function NavMenu({ open, onToggle, onClose }) {
  return (
    <div className="nav-item" onMouseEnter={() => onToggle(true)} onMouseLeave={() => onToggle(false)}>
      <NavLink
        to="/how-it-works"
        className="nav-link"
        onClick={onClose}
        onFocus={() => onToggle(true)}
        aria-expanded={open}
      >
        How it works
        <svg className={`nav-caret${open ? ' up' : ''}`} width="10" height="10" viewBox="0 0 10 10" aria-hidden="true">
          <path d="M1.5 3.5 5 7l3.5-3.5" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" />
        </svg>
      </NavLink>
      {open && (
        <div className="menu">
          {HOW_MENU.map((item) => (
            <Link key={item.to} to={item.to} onClick={onClose}>
              <span className="menu-title">{item.title}</span>
              <span className="menu-desc">{item.desc}</span>
            </Link>
          ))}
        </div>
      )}
    </div>
  )
}

export default function App() {
  const [health, setHealth] = useState(null)
  const [menuOpen, setMenuOpen] = useState(false)
  const [navOpen, setNavOpen] = useState(false)
  const location = useLocation()

  useEffect(() => {
    const load = () => api.health().then(setHealth).catch(() => setHealth(null))
    load()
    const id = setInterval(load, 15000)
    return () => clearInterval(id)
  }, [])

  /* Any navigation closes the chrome, so a menu never survives the page it
     belongs to. */
  useEffect(() => {
    setMenuOpen(false)
    setNavOpen(false)
  }, [location.pathname, location.hash])

  useEffect(() => {
    const onKey = (event) => event.key === 'Escape' && (setMenuOpen(false), setNavOpen(false))
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const live = health?.execution_mode === 'live-razorpay'
  const running = health?.runtime?.running

  /** Dashboard views sit in the centred shell; the marketing-style
   *  How-it-works page brings its own full-bleed sections. */
  const page = (element) => <div className="container">{element}</div>

  return (
    <div className="app">
      {/* Deployment posture is stated in the chrome so a simulated demo can
          never be mistaken for live money movement. */}
      <div className="announce">
        <div className="announce-inner">
          <div className="announce-group">
            <span>
              Execution <b>{live ? 'Live Razorpay' : 'Simulated'}</b>
            </span>
            <span className="announce-sep" />
            <span>
              Reasoning <b>{health?.ai_configured ? modelLabel(health.model) : 'Rules only'}</b>
              {health?.llm_provider && <> via {health.llm_provider}</>}
            </span>
            <span className="announce-sep" />
            <span>
              Auto-action cap <b>₹{(health?.policy?.auto_action_limit_rupees ?? 0).toLocaleString('en-IN')}</b>
            </span>
          </div>
          <div className="announce-group">
            <span>
              Queue depth <b>{health?.runtime?.queue_depth ?? 0}</b>
            </span>
            <span className="announce-sep" />
            <span>
              Processed <b>{health?.runtime?.processed ?? 0}</b>
            </span>
          </div>
        </div>
      </div>

      <header className="topbar">
        <div className="topbar-inner">
          <Link className="brand" to="/overview" aria-label="Recoup home">
            <BrandLockup />
          </Link>

          <nav className={`nav${navOpen ? ' open' : ''}`}>
            <NavLink className="nav-link" to="/overview">Overview</NavLink>
            <NavLink className="nav-link" to="/transactions">At-risk queue</NavLink>
            <NavLink className="nav-link" to="/experiment">Baseline vs agent</NavLink>
            <NavMenu open={menuOpen} onToggle={setMenuOpen} onClose={() => setMenuOpen(false)} />
          </nav>

          <div className="topbar-actions">
            <span className="status-pill" title={running ? 'Agent worker running' : 'Agent worker idle'}>
              <span className={`status-dot${running ? '' : ' idle'}`} />
              {running ? 'Agent live' : 'Idle'}
            </span>
            <Link className="btn btn-primary btn-sm" to="/transactions">
              Open queue
              <svg width="12" height="12" viewBox="0 0 12 12" aria-hidden="true">
                <path d="M2 6h7M6 3l3 3-3 3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" />
              </svg>
            </Link>
          </div>

          <button
            className="nav-toggle"
            aria-label="Toggle navigation"
            aria-expanded={navOpen}
            onClick={() => setNavOpen((open) => !open)}
          >
            <svg width="18" height="18" viewBox="0 0 18 18" aria-hidden="true">
              <path d="M2 5h14M2 9h14M2 13h14" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" />
            </svg>
          </button>
        </div>
      </header>

      <main className="main">
        <Routes>
          <Route path="/" element={<Navigate to="/overview" replace />} />
          <Route path="/overview" element={page(<Overview />)} />
          <Route path="/transactions" element={page(<Transactions />)} />
          <Route path="/transactions/:id" element={page(<TransactionDetail />)} />
          <Route path="/experiment" element={page(<Experiment />)} />
          <Route path="/how-it-works" element={<HowItWorks />} />
        </Routes>
      </main>

      <footer className="site-foot">
        <div className="foot-inner">
          <div className="foot-col">
            <span className="brand foot-brand">
              <BrandLockup size={22} />
            </span>
            <p className="foot-note">
              An AI revenue recovery agent for Razorpay merchants. The model reasons about
              every failed payment; deterministic code decides what is allowed to happen next.
            </p>
          </div>
          <div className="foot-col">
            <h4>Dashboard</h4>
            <Link to="/overview">Overview</Link>
            <Link to="/transactions">At-risk queue</Link>
            <Link to="/experiment">Baseline vs agent</Link>
          </div>
          <div className="foot-col">
            <h4>How it works</h4>
            <Link to="/how-it-works#pipeline">The seven stages</Link>
            <Link to="/how-it-works#architecture">Architecture</Link>
            <Link to="/how-it-works#guardrails">Guardrails</Link>
            <Link to="/how-it-works#faq">Questions</Link>
          </div>
        </div>
        <div className="foot-legal">
          <span>Recoup — built for the Razorpay hackathon. Not affiliated with Razorpay.</span>
          <span>
            {live ? 'Executing against live Razorpay APIs.' : 'Running in simulated mode — no money moves.'}
          </span>
        </div>
      </footer>
    </div>
  )
}
