import { useEffect, useState } from 'react'
import { get } from '../api.js'
import { Chip, Err, ItemStatusTable, asmLink, fmtTime, itemLink, useDebounced } from '../components.jsx'

export default function Dashboard() {
  const [q, setQ] = useState('')
  const dq = useDebounced(q)
  const [counts, setCounts] = useState(null)
  const [open, setOpen] = useState([])
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    Promise.all([get('/dashboard'), get('/issues/open')])
      .then(([c, o]) => { setCounts(c); setOpen(o); setError(null) })
      .catch(setError)
  }, [])

  useEffect(() => {
    if (!dq.trim()) { setRes(null); return }
    get('/search', { q: dq }).then((r) => { setRes(r); setError(null) }).catch(setError)
  }, [dq])

  return (
    <>
      <h1>Search / Dashboard</h1>
      <Err error={error} />
      {counts && (
        <div className="counts">
          <div className="count"><Chip status="AVAILABLE" /><b>{counts.AVAILABLE}</b>Available</div>
          <div className="count"><Chip status="IN_USE" /><b>{counts.IN_USE}</b>In use</div>
          <div className="count"><Chip status="UNDER_REPAIR" /><b>{counts.UNDER_REPAIR}</b>Under repair</div>
        </div>
      )}
      <input className="big" autoFocus value={q} onChange={(e) => setQ(e.target.value)}
        placeholder="Type a die, flange, pattern, assembly, WO or batch…" />

      {res && (
        <>
          {res.items.length === 0 && res.assemblies.length === 0 && res.issues.length === 0 && (
            <p className="muted">No matches.</p>
          )}
          {res.items.length > 0 && (<><h2>Items</h2><ItemStatusTable items={res.items} /></>)}
          {res.assemblies.length > 0 && (
            <>
              <h2>Assemblies</h2>
              <table>
                <thead><tr><th>Assembly</th><th>Ready to issue</th><th>Items</th></tr></thead>
                <tbody>
                  {res.assemblies.map((a) => (
                    <tr key={a.assembly_id}>
                      <td className="mono">{asmLink(a.assembly_id)}</td>
                      <td>{a.ready_to_issue ? 'Yes' : 'No'}</td>
                      <td>{a.items.map((s) => (
                        <span key={s.item_id} style={{ marginRight: 10 }}>{itemLink(s.item_id)} <Chip status={s.status} /></span>
                      ))}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          {res.issues.length > 0 && (
            <>
              <h2>Work orders / batches</h2>
              <table>
                <thead><tr><th>WO</th><th>Batch</th><th>Assembly</th><th>Issued to</th><th>Issued at</th><th>Items</th></tr></thead>
                <tbody>
                  {res.issues.map((i) => (
                    <tr key={i.group_id}>
                      <td>{i.wo_id}</td><td>{i.batch_no}</td><td>{asmLink(i.assembly_id)}</td>
                      <td>{i.issued_to}</td><td>{fmtTime(i.issued_at)}</td><td className="mono">{i.item_ids}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}

      {!res && (
        <>
          <h2>Currently issued</h2>
          {open.length === 0 ? <p className="muted">Nothing is issued right now.</p> : (
            <table>
              <thead><tr><th>WO</th><th>Batch</th><th>Assembly</th><th>Issued to</th><th>Since</th><th>Items</th></tr></thead>
              <tbody>
                {open.map((o) => (
                  <tr key={o.group_id}>
                    <td>{o.wo_id}</td><td>{o.batch_no}</td><td>{asmLink(o.assembly_id)}</td>
                    <td>{o.issued_to}</td><td>{fmtTime(o.since)}</td><td className="mono">{o.item_ids}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </>
      )}
    </>
  )
}
