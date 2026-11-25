import { useEffect, useState } from 'react'
import { get, post, setToken, getToken, setUnauthorizedHandler } from './api.js'
import { Err, Session } from './components.jsx'
import Dashboard from './pages/Dashboard.jsx'
import Inventory from './pages/Inventory.jsx'
import ItemDetail from './pages/ItemDetail.jsx'
import Designs from './pages/Designs.jsx'
import Assemblies from './pages/Assemblies.jsx'
import Mappings from './pages/Mappings.jsx'
import Builds from './pages/Builds.jsx'
import Readiness from './pages/Readiness.jsx'
import Issue from './pages/Issue.jsx'
import Issues from './pages/Issues.jsx'
import Batches from './pages/Batches.jsx'
import Inspections from './pages/Inspections.jsx'
import Held from './pages/Held.jsx'
import Checklists from './pages/Checklists.jsx'
import Documents from './pages/Documents.jsx'
import ImportPage from './pages/Import.jsx'
import Ledger from './pages/Ledger.jsx'
import Admin from './pages/Admin.jsx'
import Audit from './pages/Audit.jsx'
import Account from './pages/Account.jsx'
import Counts from './pages/Counts.jsx'
import Usage from './pages/Usage.jsx'
import Reports from './pages/Reports.jsx'
import Setups from './pages/Setups.jsx'
import DieSets from './pages/DieSets.jsx'

// Tiny hash router: #/item/12, #/inventory?q=b32
function parseHash() {
  const raw = window.location.hash.replace(/^#/, '') || '/'
  const [path, query = ''] = raw.split('?')
  return { parts: path.split('/').filter(Boolean).map(decodeURIComponent), query: new URLSearchParams(query) }
}

function Login({ onLogin }) {
  const [username, setU] = useState('')
  const [password, setP] = useState('')
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  async function submit(e) {
    e.preventDefault(); setBusy(true); setError(null)
    try {
      const r = await post('/auth/login', { username, password })
      setToken(r.token); onLogin(r.user)
    } catch (err) { setError(err) } finally { setBusy(false) }
  }
  return (
    <div className="login">
      <h1>VSPL Tool and Die Management</h1>
      <form className="card" onSubmit={submit}>
        <label>Username<input autoFocus value={username} onChange={(e) => setU(e.target.value)} autoComplete="username" required /></label>
        <label>Password<input type="password" value={password} onChange={(e) => setP(e.target.value)} autoComplete="current-password" required /></label>
        <button disabled={busy}>Sign in</button>
        <Err error={error} />
      </form>
    </div>
  )
}

export default function App() {
  const [user, setUser] = useState(null)
  const [ready, setReady] = useState(!getToken())
  const [route, setRoute] = useState(parseHash())

  const logout = () => { setToken(null); setUser(null) }
  useEffect(() => { setUnauthorizedHandler(logout) }, [])
  useEffect(() => {
    if (getToken()) get('/auth/me').then(setUser).catch(() => setToken(null)).finally(() => setReady(true))
  }, [])
  useEffect(() => {
    const on = () => setRoute(parseHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  if (!ready) return <p style={{ padding: 20 }}>Loading…</p>
  if (!user) return <Login onLogin={setUser} />

  const can = (p) => user.permissions.includes(p)
  const [page, arg] = route.parts
  const NAV = [
    ['', 'Search'], ['inventory', 'Inventory'], ['designs', 'Designs'], ['assemblies', 'Assemblies'], ['builds', 'Builds'],
    ['diesets', 'Die mapping'], ['setups', 'Setups'], ['mappings', 'ME mapping'], ['readiness', 'Readiness'], ['issue', 'Issue'], ['issues', 'Returns'], ['inspections', 'Inspection'], ['held', 'Repair & hold'],
    ['batches', 'Batches'], ['checklists', 'Checklists'], ['counts', 'Stock counts'], ['usage', 'Usage / OMS'], ['reports', 'Reports'],
    ['documents', 'Documents'],
    ['import', 'Import'], ['ledger', 'Ledger'],
    ...(can('config.manage') || can('users.manage') ? [['admin', 'Admin']] : []),
    ...(can('audit.view') ? [['audit', 'Audit']] : []),
  ]
  let view
  if (!page) view = <Dashboard />
  else if (page === 'inventory') view = <Inventory key={route.query.toString()} initial={Object.fromEntries(route.query)} />
  else if (page === 'item') view = <ItemDetail key={arg} id={Number(arg)} />
  else if (page === 'designs') view = <Designs key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'assemblies') view = <Assemblies key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'builds') view = <Builds key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'mappings') view = <Mappings />
  else if (page === 'readiness') view = <Readiness />
  else if (page === 'inspections') view = <Inspections key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'held') view = <Held />
  else if (page === 'batches') view = <Batches key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'checklists') view = <Checklists key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'counts') view = <Counts key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'usage') view = <Usage />
  else if (page === 'diesets') view = <DieSets />
  else if (page === 'setups') view = <Setups key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'reports') view = <Reports />
  else if (page === 'issue') view = <Issue />
  else if (page === 'issues') view = <Issues key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'documents') view = <Documents key={arg || ''} selected={arg ? Number(arg) : null} />
  else if (page === 'import') view = <ImportPage />
  else if (page === 'ledger') view = <Ledger />
  else if (page === 'admin') view = <Admin />
  else if (page === 'audit') view = <Audit />
  else if (page === 'account') view = <Account />
  else view = <p>Page not found.</p>

  return (
    <Session.Provider value={{ user }}>
      <nav>
        <span className="brand">VSPL Tool &amp; Die</span>
        {NAV.map(([p, label]) => <a key={p} href={`#/${p}`} className={(page || '') === p ? 'active' : ''}>{label}</a>)}
        <span className="right">
          <a href="#/account" style={{ padding: 0 }}>{user.full_name} ({user.roles.join(', ') || 'no role'})</a>
          <button className="secondary" onClick={logout}>Sign out</button>
        </span>
      </nav>
      <main>{view}</main>
    </Session.Provider>
  )
}
