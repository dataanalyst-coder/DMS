import { useEffect, useState } from 'react'
import { API, get } from '../api.js'
import { Err, asmLink, fmtTime, itemLink } from '../components.jsx'

const ACTIONS = ['', 'RECEIVE', 'ISSUE', 'RETURN', 'TRANSFER', 'SEND_REPAIR', 'BACK_FROM_REPAIR', 'SCRAP', 'REVERSAL']
const EMPTY = { item: '', assembly: '', wo: '', batch: '', person: '', action: '', date_from: '', date_to: '' }

export default function Ledger({ initial }) {
  const [f, setF] = useState({ ...EMPTY, ...initial })
  const [rows, setRows] = useState([])
  const [error, setError] = useState(null)

  // date inputs give YYYY-MM-DD; make date_to inclusive of that whole day
  const params = (x) => ({ ...x, date_to: x.date_to ? `${x.date_to}T23:59:59` : '' })
  const load = (x = f) => { setApplied(x); return get('/ledger', { ...params(x), limit: 1000 }).then((r) => { setRows(r); setError(null) }).catch(setError) }
  useEffect(() => { load() }, [])
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  // Export uses the filters as last applied to the table, not unsaved edits in the form.
  const [applied, setApplied] = useState({ ...EMPTY, ...initial })
  const exportUrl = `${API}/export/ledger?` + new URLSearchParams(
    Object.entries(params(applied)).filter(([, v]) => v)).toString()

  return (
    <>
      <h1>Ledger</h1>
      <form className="card" onSubmit={(e) => { e.preventDefault(); load() }}>
        <div className="row">
          <label>Item<input value={f.item} onChange={set('item')} /></label>
          <label>Assembly<input value={f.assembly} onChange={set('assembly')} /></label>
          <label>WO<input value={f.wo} onChange={set('wo')} /></label>
          <label>Batch<input value={f.batch} onChange={set('batch')} /></label>
          <label>Person<input value={f.person} onChange={set('person')} /></label>
          <label>Action<select value={f.action} onChange={set('action')}>{ACTIONS.map((a) => <option key={a} value={a}>{a || 'All'}</option>)}</select></label>
          <label>From<input type="date" value={f.date_from} onChange={set('date_from')} /></label>
          <label>To<input type="date" value={f.date_to} onChange={set('date_to')} /></label>
          <button>Filter</button>
          <button type="button" className="secondary" onClick={() => { setF(EMPTY); load(EMPTY) }}>Clear</button>
          <a href={exportUrl}><button type="button" className="secondary">Export to Excel</button></a>
          <a href={`${API}/export/status`}><button type="button" className="secondary">Export item status</button></a>
        </div>
      </form>
      <Err error={error} />
      <p className="muted">{rows.length} entries (newest first, max 1000 shown). Entries are never edited or deleted; corrections appear as REVERSAL rows.</p>
      <table>
        <thead><tr><th>#</th><th>Time</th><th>Item</th><th>Assembly</th><th>Action</th><th>From → To</th><th>WO</th><th>Batch</th><th>By</th><th>To person</th><th>Machine</th><th>Remarks</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.txn_id}>
              <td>{r.txn_id}</td><td>{fmtTime(r.txn_time)}</td><td className="mono">{itemLink(r.item_id)}</td><td>{asmLink(r.assembly_id)}</td>
              <td>{r.action}{r.ref_txn_id ? ` of #${r.ref_txn_id}` : ''}</td><td>{r.from_location || '-'} → {r.to_location || '-'}</td>
              <td>{r.wo_id}</td><td>{r.batch_no}</td><td>{r.issued_by}</td><td>{r.issued_to}</td><td>{r.machine}</td><td>{r.remarks}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
