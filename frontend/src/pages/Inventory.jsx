import { useEffect, useState } from 'react'
import { clean, download, get, post } from '../api.js'
import { Err, LocationSelect, Pager, useCan, useLoad } from '../components.jsx'
import { ItemTable } from './Dashboard.jsx'
import { Chip } from '../components.jsx'

// The four tooling types, each shown on its own with ID and description. Matched to categories by code, ignoring case and punctuation.
const TYPES = [['DIE', 'Die'], ['PATTERN', 'Pattern'], ['TOPFLANGE', 'Top flange'], ['BOTTOMFLANGE', 'Bottom flange']]
const norm = (c) => String(c || '').toUpperCase().replace(/[^A-Z]/g, '')

function TypedTable({ rows }) {
  return (
    <table>
      <thead><tr><th>ID</th><th>Description</th><th>Condition</th><th>Custody</th><th>Location (bin)</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td className="mono"><a href={`#/item/${r.id}`}>{r.display_id}</a></td><td>{r.description} <small className="muted">{r.design_code}</small></td>
            <td><Chip v={r.condition} /></td><td><Chip v={r.custody} /></td><td>{r.location}</td>
          </tr>))}
        {rows.length === 0 && <tr><td colSpan="5" className="muted">No matching items.</td></tr>}
      </tbody>
    </table>
  )
}

const CONDITIONS = ['SERVICEABLE', 'AWAITING_INSPECTION', 'HOLD', 'UNDER_REPAIR', 'SCRAPPED']
const CUSTODIES = ['STORES', 'ASSEMBLY_AREA', 'PENDING_HANDOVER', 'PRODUCTION', 'REPAIR', 'MISSING']
const LIMIT = 25
const BLANK = { design_id: '', legacy_no: '', display_id: '', condition: 'SERVICEABLE', location_id: '', provenance: '', acquisition_date: '', opening_cycles: '', opening_tonnage: '', remarks: '' }

function Register({ designs, onDone }) {
  const [f, setF] = useState(BLANK)
  const [error, setError] = useState(null)
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  async function submit(e) {
    e.preventDefault()
    try {
      const body = clean({ ...f, design_id: Number(f.design_id), location_id: Number(f.location_id),
        opening_cycles: f.opening_cycles === '' ? undefined : Number(f.opening_cycles),
        opening_tonnage: f.opening_tonnage === '' ? undefined : Number(f.opening_tonnage) })
      const it = await post('/items', body)
      setError(null); setF(BLANK); onDone(it)
    } catch (err) { setError(err) }
  }
  return (
    <form className="card" onSubmit={submit}>
      <b>Register a physical item</b>
      <div className="row">
        <label>Design *<select value={f.design_id} onChange={set('design_id')} required>
          <option value="">- design -</option>
          {designs.filter((d) => d.approved_revision && d.kind === 'REUSABLE' && d.active).map((d) => <option key={d.id} value={d.id}>{d.code} - {d.description} (rev {d.approved_revision})</option>)}
        </select></label>
        <label>Existing tooling number<input value={f.legacy_no} onChange={set('legacy_no')} /></label>
        <label>Unique display ID<input value={f.display_id} onChange={set('display_id')} placeholder="blank = tooling number" /></label>
        <label>Condition *<select value={f.condition} onChange={set('condition')}>{CONDITIONS.map((c) => <option key={c}>{c}</option>)}</select></label>
        <label>Location (home and current) *<LocationSelect value={f.location_id} onChange={(v) => setF({ ...f, location_id: v })} required /></label>
        <label>Acquired<input type="date" value={f.acquisition_date} onChange={set('acquisition_date')} /></label>
        <label>Opening cycles<input type="number" min="0" value={f.opening_cycles} onChange={set('opening_cycles')} placeholder="blank = unknown" /></label>
        <label>Opening tonnage<input type="number" min="0" step="any" value={f.opening_tonnage} onChange={set('opening_tonnage')} placeholder="blank = unknown" /></label>
        <label>Provenance<input value={f.provenance} onChange={set('provenance')} /></label>
        <button>Register</button>
      </div>
      <Err error={error} />
    </form>
  )
}

export default function Inventory({ initial }) {
  const can = useCan()
  const [f, setF] = useState({ q: '', design_id: '', condition: '', custody: '', location_id: '', home_mismatch: '', ...initial })
  const [applied, setApplied] = useState(f)
  const [offset, setOffset] = useState(0)
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)
  const [showReg, setShowReg] = useState(false)
  const [designs] = useLoad(() => get('/designs', { limit: 500, active: true }), [])
  const [cats] = useLoad(() => get('/categories'), [])
  const [type, setType] = useState('')
  const catId = type ? (cats || []).find((c) => norm(c.code) === type)?.id : ''

  const load = () => get('/items', { ...applied, category_id: catId || undefined, limit: LIMIT, offset }).then((r) => { setRes(r); setError(null) }).catch(setError)
  useEffect(() => { load() }, [applied, offset, catId]) // eslint-disable-line react-hooks/exhaustive-deps
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  const apply = (e) => { e.preventDefault(); setOffset(0); setApplied(f) }

  return (
    <>
      <h1>Inventory</h1>
      {can('item.register') && <p><button className="secondary" onClick={() => setShowReg(!showReg)}>{showReg ? 'Hide registration' : 'Register item'}</button></p>}
      {showReg && <Register designs={designs?.rows || []} onDone={(it) => { window.location.hash = `#/item/${it.id}` }} />}
      <div className="row noprint" style={{ marginBottom: 8 }}>
        {[['', 'All'], ...TYPES].map(([k, label]) => (
          <button key={k} type="button" className={type === k ? '' : 'secondary'} onClick={() => { setOffset(0); setType(k) }}>{label}</button>))}
        <a href="#/diesets" style={{ alignSelf: 'center' }}>Die mapping →</a>
      </div>
      <form className="card" onSubmit={apply}>
        <div className="row">
          <label>Search<input value={f.q} onChange={set('q')} placeholder="number, QR, design…" /></label>
          <label>Design<select value={f.design_id} onChange={set('design_id')}><option value="">All</option>{(designs?.rows || []).map((d) => <option key={d.id} value={d.id}>{d.code}</option>)}</select></label>
          <label>Condition<select value={f.condition} onChange={set('condition')}><option value="">All</option>{CONDITIONS.map((c) => <option key={c}>{c}</option>)}</select></label>
          <label>Custody<select value={f.custody} onChange={set('custody')}><option value="">All</option>{CUSTODIES.map((c) => <option key={c}>{c}</option>)}</select></label>
          <label>Location (and below)<LocationSelect value={f.location_id} onChange={(v) => setF({ ...f, location_id: v })} placeholder="Anywhere" /></label>
          <label>Home mismatch<select value={f.home_mismatch} onChange={set('home_mismatch')}><option value="">Any</option><option value="true">Away from home</option></select></label>
          <button>Filter</button>
          <button type="button" className="secondary" onClick={() => download('/export/items', 'items.xlsx', clean(applied)).catch(setError)}>Export to Excel</button>
        </div>
      </form>
      <Err error={error} />
      {res && (<>{type ? <TypedTable rows={res.rows} /> : <ItemTable rows={res.rows} />}<Pager total={res.total} limit={LIMIT} offset={offset} onChange={setOffset} /></>)}
    </>
  )
}
