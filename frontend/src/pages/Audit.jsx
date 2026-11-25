import { useEffect, useState } from 'react'
import { clean, get } from '../api.js'
import { Err, Pager, fmtTime } from '../components.jsx'

const LIMIT = 50

export default function Audit() {
  const [f, setF] = useState({ entity_type: '', user_name: '', action: '' })
  const [applied, setApplied] = useState(f)
  const [offset, setOffset] = useState(0)
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => {
    get('/audit', { ...clean(applied), limit: LIMIT, offset }).then((r) => { setRes(r); setError(null) }).catch(setError)
  }, [applied, offset])
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })
  return (
    <>
      <h1>Audit trail</h1>
      <form className="card" onSubmit={(e) => { e.preventDefault(); setOffset(0); setApplied(f) }}>
        <div className="row">
          <label>Entity<input value={f.entity_type} onChange={set('entity_type')} placeholder="item, design, user…" /></label>
          <label>User<input value={f.user_name} onChange={set('user_name')} /></label>
          <label>Action<input value={f.action} onChange={set('action')} placeholder="APPROVE, SET_CONDITION…" /></label>
          <button>Filter</button>
        </div>
      </form>
      <Err error={error} />
      {res && (
        <>
          <table>
            <thead><tr><th>Time</th><th>User</th><th>Action</th><th>Entity</th><th>Before</th><th>After</th><th>Reason</th></tr></thead>
            <tbody>{res.rows.map((r) => (
              <tr key={r.id}><td>{fmtTime(r.at)}</td><td>{r.username}</td><td>{r.action}</td><td>{r.entity_type} {r.entity_id}</td>
                <td className="mono" style={{ fontSize: 11 }}>{r.before}</td><td className="mono" style={{ fontSize: 11 }}>{r.after}</td><td>{r.reason}</td></tr>
            ))}</tbody>
          </table>
          <Pager total={res.total} limit={LIMIT} offset={offset} onChange={setOffset} />
        </>
      )}
    </>
  )
}
