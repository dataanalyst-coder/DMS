import { useEffect, useState } from 'react'
import Dashboard from './pages/Dashboard.jsx'
import AssemblyView from './pages/AssemblyView.jsx'
import ItemDetail from './pages/ItemDetail.jsx'
import Issue from './pages/Issue.jsx'
import Return from './pages/Return.jsx'
import Masters from './pages/Masters.jsx'
import Ledger from './pages/Ledger.jsx'
import Import from './pages/Import.jsx'

// Tiny hash router (avoids adding a routing library): #/issue?assembly=T1
function parseHash() {
  const raw = window.location.hash.replace(/^#/, '') || '/'
  const [path, query = ''] = raw.split('?')
  return { parts: path.split('/').filter(Boolean).map(decodeURIComponent), query: new URLSearchParams(query) }
}

const NAV = [
  ['', 'Search'],
  ['issue', 'Issue'],
  ['return', 'Return'],
  ['masters', 'Masters'],
  ['ledger', 'Ledger'],
  ['import', 'Import'],
]

export default function App() {
  const [route, setRoute] = useState(parseHash())
  useEffect(() => {
    const on = () => setRoute(parseHash())
    window.addEventListener('hashchange', on)
    return () => window.removeEventListener('hashchange', on)
  }, [])

  const [page, arg] = route.parts
  let view
  if (!page) view = <Dashboard />
  else if (page === 'assembly') view = <AssemblyView key={arg} id={arg} />
  else if (page === 'item') view = <ItemDetail key={arg} id={arg} />
  else if (page === 'issue') view = <Issue key={route.query.get('assembly') || ''} initial={route.query.get('assembly') || ''} />
  else if (page === 'return') view = <Return />
  else if (page === 'masters') view = <Masters />
  else if (page === 'ledger') view = <Ledger initial={Object.fromEntries(route.query)} />
  else if (page === 'import') view = <Import />
  else view = <p>Page not found.</p>

  return (
    <>
      <nav>
        <span className="brand">VSPL DIE Mgmt</span>
        {NAV.map(([p, label]) => (
          <a key={p} href={`#/${p}`} className={(page || '') === p ? 'active' : ''}>{label}</a>
        ))}
      </nav>
      <main>{view}</main>
    </>
  )
}
