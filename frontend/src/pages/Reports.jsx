import { useState } from 'react'
import { download, get } from '../api.js'
import { Err, LocationSelect, useLoad } from '../components.jsx'

const TITLES = {
  inventory: 'Inventory by location and condition',
  'assembly-readiness': 'Assembly readiness',
  'custody-by-wo': 'Tooling in production custody, by WO',
  'missing-overdue': 'Missing and overdue tooling',
  repairs: 'Repairs',
  'item-usage': 'Cycles and tonnage by item',
  '5s-mismatches': '5S: items away from their home location',
  'batch-stock': 'Consumable batch stock',
  audit: 'Audit trail',
}
// the filters each report understands (the server ignores anything else)
const FILTERS = {
  inventory: ['condition', 'custody', 'location_id'],
  'missing-overdue': ['days'],
  audit: ['action', 'user'],
}
const CONDITIONS = ['SERVICEABLE', 'AWAITING_INSPECTION', 'HOLD', 'UNDER_REPAIR', 'SCRAPPED']
const CUSTODIES = ['STORES', 'ASSEMBLY_AREA', 'PENDING_HANDOVER', 'PRODUCTION', 'REPAIR', 'MISSING']

const cell = (v) => (v === null || v === undefined ? '' : Array.isArray(v) ? v.join(', ') : typeof v === 'object' ? JSON.stringify(v) : String(v))

export default function Reports() {
  const [names, nErr] = useLoad(() => get('/reports'), [])
  const [name, setName] = useState('inventory')
  const [params, setParams] = useState({})
  const [rep, setRep] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)
  const set = (k, v) => setParams((p) => ({ ...p, [k]: v }))
  const filters = FILTERS[name] || []
  const label = (n) => TITLES[n] || n

  async function run() {
    setBusy(true)
    try { setRep(await get(`/reports/${name}`, params)); setError(null) } catch (e) { setError(e); setRep(null) } finally { setBusy(false) }
  }
  async function excel() {
    try { await download(`/reports/${name}`, `${name}.xlsx`, { ...params, format: 'xlsx' }); setError(null) } catch (e) { setError(e) }
  }

  return (
    <>
      <h1>Reports</h1>
      <Err error={error || nErr} />
      <div className="row noprint">
        <label>Report<select value={name} onChange={(e) => { setName(e.target.value); setParams({}); setRep(null) }}>{(names || Object.keys(TITLES)).map((n) => <option key={n} value={n}>{label(n)}</option>)}</select></label>
        {filters.includes('condition') && <label>Condition<select value={params.condition || ''} onChange={(e) => set('condition', e.target.value)}><option value="">any</option>{CONDITIONS.map((c) => <option key={c}>{c}</option>)}</select></label>}
        {filters.includes('custody') && <label>Custody<select value={params.custody || ''} onChange={(e) => set('custody', e.target.value)}><option value="">any</option>{CUSTODIES.map((c) => <option key={c}>{c}</option>)}</select></label>}
        {filters.includes('location_id') && <label>Location<LocationSelect value={params.location_id} onChange={(v) => set('location_id', v)} /></label>}
        {filters.includes('days') && <label>Out longer than (days)<input type="number" min="0" value={params.days ?? ''} onChange={(e) => set('days', e.target.value)} style={{ width: 90 }} /></label>}
        {filters.includes('action') && <label>Action<input value={params.action || ''} onChange={(e) => set('action', e.target.value)} placeholder="e.g. DENIED" /></label>}
        {filters.includes('user') && <label>User<input value={params.user || ''} onChange={(e) => set('user', e.target.value)} /></label>}
        <button onClick={run} disabled={busy}>Show</button>
        <button className="secondary" onClick={excel}>Export to Excel</button>
      </div>
      {rep && (
        <>
          <h2>{label(name)}</h2>
          <p className="muted">{Object.entries(rep.summary || {}).filter(([, v]) => typeof v !== 'object').map(([k, v]) => `${k.replaceAll('_', ' ')}: ${v}`).join(' · ')}</p>
          <div style={{ overflowX: 'auto' }}>
            <table>
              <thead><tr>{rep.columns.map((c) => <th key={c}>{c.replaceAll('_', ' ')}</th>)}</tr></thead>
              <tbody>
                {rep.rows.map((r, i) => <tr key={i}>{rep.columns.map((c) => <td key={c}>{cell(r[c])}</td>)}</tr>)}
                {rep.rows.length === 0 && <tr><td colSpan={rep.columns.length} className="muted">Nothing to report.</td></tr>}
              </tbody>
            </table>
          </div>
        </>
      )}
      {!rep && <p className="muted">Choose a report and press Show. The Excel file has the same columns and rows as the table.</p>}
    </>
  )
}
