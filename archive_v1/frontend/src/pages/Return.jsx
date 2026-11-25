import { useEffect, useState } from 'react'
import { get, post } from '../api.js'
import { Err, asmLink, fmtTime } from '../components.jsx'

export default function Return() {
  const [open, setOpen] = useState([])
  const [sel, setSel] = useState('')
  const [loc, setLoc] = useState('')
  const [by, setBy] = useState('')
  const [notes, setNotes] = useState('')
  const [filter, setFilter] = useState('')
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)

  const load = () => get('/issues/open').then(setOpen).catch(setError)
  useEffect(() => { load() }, [])

  const shown = open.filter((o) =>
    `${o.wo_id} ${o.batch_no} ${o.assembly_id || ''} ${o.item_ids}`.toLowerCase().includes(filter.toLowerCase()))

  async function submit(e) {
    e.preventDefault()
    setError(null); setDone(null)
    try {
      const r = await post('/return', { group_id: sel, to_location: loc, condition_remarks: notes, returned_by: by })
      setDone(r); setSel(''); setNotes('')
      load()
    } catch (err) { setError(err) }
  }

  return (
    <>
      <h1>Return</h1>
      <input value={filter} onChange={(e) => setFilter(e.target.value)} placeholder="Filter by WO, batch, assembly or item…" />
      <table style={{ marginTop: 10 }}>
        <thead><tr><th></th><th>WO</th><th>Batch</th><th>Assembly</th><th>Issued to</th><th>Since</th><th>Items</th></tr></thead>
        <tbody>
          {shown.map((o) => (
            <tr key={o.group_id}>
              <td><input type="radio" name="g" checked={sel === o.group_id} onChange={() => setSel(o.group_id)} style={{ minWidth: 0 }} /></td>
              <td>{o.wo_id}</td><td>{o.batch_no}</td><td>{asmLink(o.assembly_id)}</td>
              <td>{o.issued_to}</td><td>{fmtTime(o.since)}</td><td className="mono">{o.item_ids}</td>
            </tr>
          ))}
          {shown.length === 0 && <tr><td colSpan="7" className="muted">No open issues.</td></tr>}
        </tbody>
      </table>
      <form className="card" onSubmit={submit} style={{ marginTop: 14 }}>
        <div className="row">
          <label>Return location *<input value={loc} onChange={(e) => setLoc(e.target.value)} required /></label>
          <label>Returned by<input value={by} onChange={(e) => setBy(e.target.value)} /></label>
          <label>Condition remarks<input value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
          <button disabled={!sel}>Return all items</button>
        </div>
      </form>
      <Err error={error} />
      {done && <div className="msg ok">Returned {done.item_ids.join(', ')} to {done.to_location}.</div>}
    </>
  )
}
