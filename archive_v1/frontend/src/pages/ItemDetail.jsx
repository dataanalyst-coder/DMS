import { useEffect, useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, asmLink, fmtTime, holder } from '../components.jsx'

export default function ItemDetail({ id }) {
  const [d, setD] = useState(null)
  const [error, setError] = useState(null)
  const [loc, setLoc] = useState('')
  const [notes, setNotes] = useState('')

  const load = () => get(`/items/${encodeURIComponent(id)}`).then((r) => { setD(r); setError(null) }).catch(setError)
  useEffect(() => { load() }, [id])

  const run = (path, body) => async () => {
    try { await post(path, { item_ids: [id], to_location: loc, remarks: notes, ...body }); setLoc(''); load() }
    catch (e) { setError(e) }
  }
  const reverse = (h) => async () => {
    const txn = h.txn_id
    const msg = h.event_id
      ? `Entry ${txn} was recorded together with other entries in one action (e.g. one assembly issue). Every entry from that same action will be reversed, or the reversal is rejected. Continue?`
      : `Append a reversal for entry ${txn}?`
    if (!window.confirm(msg)) return
    try { await post(`/reverse/${txn}`, { remarks: notes }); load() } catch (e) { setError(e) }
  }

  if (!d) return error ? <Err error={error} /> : <p className="muted">Loading…</p>
  const { item, status, assemblies, history } = d
  const st = status?.status
  return (
    <>
      <h1>{item.item_id} <small className="muted">{item.item_type}</small></h1>
      <div className="card">
        <Chip status={st} /> at <b>{status?.current_location || '-'}</b> {holder(status) && <>· {holder(status)}</>}
        <p className="muted">{item.description} {item.od ? `· OD ${item.od}` : ''} {item.id_dim ? `· ID ${item.id_dim}` : ''} {item.length ? `· L ${item.length}` : ''} · home {item.home_location || '-'}</p>
        <p>Used in: {assemblies.length ? assemblies.map((a) => <span key={a.assembly_id} style={{ marginRight: 8 }}>{asmLink(a.assembly_id)}</span>) : <span className="muted">no assembly</span>}</p>
      </div>

      <div className="card row">
        <label>Location / remarks for the action
          <input value={loc} onChange={(e) => setLoc(e.target.value)} placeholder="Target location" />
        </label>
        <label>Remarks<input value={notes} onChange={(e) => setNotes(e.target.value)} /></label>
        <button className="secondary" disabled={st !== 'AVAILABLE' || !loc} onClick={run('/transfer')}>Transfer</button>
        <button className="secondary" disabled={st !== 'AVAILABLE'} onClick={run('/repair/send', { to_location: loc || undefined })}>Send to repair</button>
        <button className="secondary" disabled={st !== 'UNDER_REPAIR' || !loc} onClick={run('/repair/back')}>Back from repair</button>
        <button className="danger" disabled={!['AVAILABLE', 'UNDER_REPAIR'].includes(st)}
          onClick={() => window.confirm(`Scrap ${item.item_id}?`) && run('/scrap')()}>Scrap</button>
      </div>
      <Err error={error} />

      <h2>Ledger history</h2>
      <table>
        <thead><tr><th>#</th><th>Time</th><th>Action</th><th>From → To</th><th>WO</th><th>Batch</th><th>By</th><th>To</th><th>Remarks</th><th></th></tr></thead>
        <tbody>
          {history.map((h) => (
            <tr key={h.txn_id}>
              <td>{h.txn_id}</td><td>{fmtTime(h.txn_time)}</td>
              <td>{h.action}{h.ref_txn_id ? ` of #${h.ref_txn_id}` : ''}</td>
              <td>{h.from_location || '-'} → {h.to_location || '-'}</td>
              <td>{h.wo_id}</td><td>{h.batch_no}</td><td>{h.issued_by}</td><td>{h.issued_to}</td><td>{h.remarks}</td>
              <td>{status?.txn_id === h.txn_id && history.length > 1 && <button className="secondary" onClick={reverse(h)}>Reverse</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
