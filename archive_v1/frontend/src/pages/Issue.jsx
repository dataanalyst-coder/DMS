import { useEffect, useState } from 'react'
import { get, post } from '../api.js'
import { Err, ItemStatusTable } from '../components.jsx'

const EMPTY = { wo_id: '', batch_no: '', issued_by: '', issued_to: '', machine: '', remarks: '' }

export default function Issue({ initial }) {
  const [assemblies, setAssemblies] = useState([])
  const [aid, setAid] = useState(initial)
  const [view, setView] = useState(null)
  const [f, setF] = useState(EMPTY)
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)

  useEffect(() => { get('/assemblies').then(setAssemblies).catch(setError) }, [])

  // Auto-fill the mapped items whenever a known assembly is chosen.
  useEffect(() => {
    setView(null)
    if (!aid.trim()) return
    get(`/assemblies/${encodeURIComponent(aid.trim())}`).then((v) => { setView(v); setError(null) }).catch(() => {})
  }, [aid])

  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  async function submit(e) {
    e.preventDefault()
    setError(null); setDone(null)
    try {
      const r = await post('/issue', { assembly_id: view.assembly_id, ...f })
      setDone(r)
      setF(EMPTY)
      setView(await get(`/assemblies/${encodeURIComponent(r.assembly_id)}`))
    } catch (err) { setError(err) }
  }

  return (
    <>
      <h1>Issue assembly</h1>
      <form className="card" onSubmit={submit}>
        <div className="row">
          <label>Assembly
            <input list="asm" value={aid} onChange={(e) => setAid(e.target.value)} placeholder="e.g. T211" required />
            <datalist id="asm">{assemblies.map((a) => <option key={a.assembly_id} value={a.assembly_id}>{a.description}</option>)}</datalist>
          </label>
          <label>WO ID *<input value={f.wo_id} onChange={set('wo_id')} required /></label>
          <label>Batch no *<input value={f.batch_no} onChange={set('batch_no')} required /></label>
          <label>Issued by *<input value={f.issued_by} onChange={set('issued_by')} required /></label>
          <label>Issued to *<input value={f.issued_to} onChange={set('issued_to')} required /></label>
          <label>Machine<input value={f.machine} onChange={set('machine')} /></label>
          <label>Remarks<input value={f.remarks} onChange={set('remarks')} /></label>
        </div>
        <p><button disabled={!view}>Issue all items</button>{' '}
          {view && !view.ready_to_issue && <span className="muted">Not all items are available; the server will name the blocking item.</span>}
        </p>
      </form>
      <Err error={error} />
      {done && (
        <div className="msg ok">
          Issued {done.item_ids.join(', ')} · group <span className="mono">{done.group_id.slice(0, 8)}</span>.{' '}
          <a href="#/return">Go to Return</a>
        </div>
      )}
      {view && (<><h2>Items in {view.assembly_id}</h2><ItemStatusTable items={view.items} /></>)}
    </>
  )
}
