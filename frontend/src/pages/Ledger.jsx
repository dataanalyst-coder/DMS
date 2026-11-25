import { useEffect, useState } from 'react'
import { clean, download, get } from '../api.js'
import { Err, Pager, fmtTime } from '../components.jsx'

const ACTIONS = ['', 'RECEIVE', 'MOVE', 'SET_CONDITION', 'REVERSAL']
const EMPTY = { user_name: '', action: '', date_from: '', date_to: '' }
const LIMIT = 50

export default function Ledger() {
  const [f, setF] = useState(EMPTY)
  const [applied, setApplied] = useState(EMPTY)
  const [offset, setOffset] = useState(0)
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)
  const params = (x) => clean({ ...x, date_to: x.date_to ? `${x.date_to}T23:59:59` : '' })

  useEffect(() => {
    get('/ledger', { ...params(applied), limit: LIMIT, offset }).then((r) => { setRes(r); setError(null) }).catch(setError)
  }, [applied, offset])
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  return (
    <>
      <h1>Ledger</h1>
      <form className="card" onSubmit={(e) => { e.preventDefault(); setOffset(0); setApplied(f) }}>
        <div className="row">
          <label>User<input value={f.user_name} onChange={set('user_name')} /></label>
          <label>Action<select value={f.action} onChange={set('action')}>{ACTIONS.map((a) => <option key={a} value={a}>{a || 'All'}</option>)}</select></label>
          <label>From<input type="date" value={f.date_from} onChange={set('date_from')} /></label>
          <label>To<input type="date" value={f.date_to} onChange={set('date_to')} /></label>
          <button>Filter</button>
          <button type="button" className="secondary" onClick={() => { setF(EMPTY); setOffset(0); setApplied(EMPTY) }}>Clear</button>
          <button type="button" className="secondary" onClick={() => download('/export/ledger', 'ledger.xlsx', params(applied)).catch(setError)}>Export to Excel</button>
        </div>
      </form>
      <Err error={error} />
      <p className="muted">Entries are never edited or deleted; corrections appear as REVERSAL rows. Rows with the same event id came from one action.</p>
      {res && (
        <>
          <table>
            <thead><tr><th>#</th><th>Time</th><th>Item</th><th>Action</th><th>Condition</th><th>Custody</th><th>From → To</th><th>By</th><th>Note</th><th>Event</th></tr></thead>
            <tbody>
              {res.rows.map((r) => (
                <tr key={r.txn_id}>
                  <td>{r.txn_id}</td><td>{fmtTime(r.txn_time)}</td><td className="mono"><a href={`#/item/${r.item_id}`}>{r.display_id}</a></td>
                  <td>{r.action}{r.ref_txn_id ? ` of #${r.ref_txn_id}` : ''}</td><td>{r.to_condition}</td><td>{r.to_custody}</td>
                  <td>{r.from_location || '-'} → {r.to_location || '-'}</td><td>{r.user}</td><td>{r.remarks}</td><td className="mono">{r.event_id.slice(0, 6)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <Pager total={res.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </>
      )}
    </>
  )
}
