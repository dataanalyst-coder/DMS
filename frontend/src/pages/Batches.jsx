import { useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, LabelPrint, LocationSelect, Ok, fmtTime, useCan, useLoad } from '../components.jsx'

function Detail({ id }) {
  const can = useCan()
  const [b, loadErr, reload] = useLoad(() => get(`/batches/${id}`), [id])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [qty, setQty] = useState('')
  const [reason, setReason] = useState('')
  const [delta, setDelta] = useState('')
  const [loc, setLoc] = useState('')
  if (loadErr) return <Err error={loadErr} />
  if (!b) return <p className="muted">Loading…</p>
  const act = (fn, ok) => async () => { try { await fn(); setError(null); setMsg(ok); reload() } catch (e) { setError(e); setMsg(null) } }
  const reverse = (h) => async () => {
    const why = window.prompt(`Reverse the whole event of entry #${h.txn_id}? Enter the reason:`)
    if (why === null) return
    await act(() => post(`/batch-ledger/${h.txn_id}/reverse`, { reason: why }), 'Reversed.')()
  }
  return (
    <>
      <h1>{b.code} <small className="muted">{b.design_code} · {b.description}</small> <Chip v={b.status} /></h1>
      <Err error={error} /><Ok>{msg}</Ok>
      <div className="counts">
        {[['Usable', b.usable], ['On hold', b.held], ['Issued, not settled', b.outstanding], ['Consumed', b.consumed]].map(([l, v]) => (
          <div className="count" key={l}><b>{v}</b>{l} ({b.uom})</div>
        ))}
      </div>
      <p className="muted">Received {b.received_qty} {b.uom}{b.supplier_lot ? ` · supplier lot ${b.supplier_lot}` : ''}{b.expiry_date ? ` · expires ${b.expiry_date}` : ''} · at <b>{b.location || '-'}</b>.
        Balances are the sum of the ledger below; they can never go negative.</p>
      {can('item.label') && <p className="noprint"><LabelPrint path={`/batches/${b.id}/label.svg`}>Print batch label</LabelPrint></p>}
      <div className="card noprint">
        <div className="row">
          <label>Quantity<input type="number" min="0" step="any" value={qty} onChange={(e) => setQty(e.target.value)} /></label>
          <label>Reason<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          {can('batch.hold') && <button className="secondary" disabled={!qty} onClick={act(() => post(`/batches/${id}/hold`, { quantity: Number(qty), reason }), 'Quantity put on hold.')}>Put on hold</button>}
          {can('batch.release') && <button className="secondary" disabled={!qty} onClick={act(() => post(`/batches/${id}/release`, { quantity: Number(qty), reason }), 'Hold released.')}>Release hold</button>}
        </div>
        <div className="row" style={{ marginTop: 8 }}>
          {can('batch.adjust') && <>
            <label>Adjustment (+/-) after a count<input type="number" step="any" value={delta} onChange={(e) => setDelta(e.target.value)} /></label>
            <button className="secondary" disabled={!delta || !reason} onClick={act(() => post(`/batches/${id}/adjust`, { delta: Number(delta), reason }), 'Adjusted (history kept).')}>Adjust usable stock</button></>}
          {can('batch.move') && <>
            <label>Move to<LocationSelect value={loc} onChange={setLoc} /></label>
            <button className="secondary" disabled={!loc} onClick={act(() => post(`/batches/${id}/move`, { to_location_id: loc, remarks: reason }), 'Moved.')}>Move batch</button></>}
        </div>
      </div>
      <h2>Ledger (append-only)</h2>
      <table>
        <thead><tr><th>#</th><th>Time</th><th>Action</th><th>Usable</th><th>Held</th><th>Out</th><th>Consumed</th><th>Location</th><th>By</th><th>Note</th><th></th></tr></thead>
        <tbody>{b.history.map((h) => (
          <tr key={h.txn_id}><td>{h.txn_id}</td><td>{fmtTime(h.txn_time)}</td><td>{h.action}{h.ref_txn_id ? ` of #${h.ref_txn_id}` : ''}</td>
            {['d_usable', 'd_held', 'd_outstanding', 'd_consumed'].map((k) => <td key={k}>{h[k] ? (h[k] > 0 ? `+${h[k]}` : h[k]) : ''}</td>)}
            <td>{h.location}</td><td>{h.user}</td><td>{h.remarks}</td>
            <td>{h.action !== 'REVERSAL' && <button className="secondary noprint" onClick={reverse(h)}>Reverse</button>}</td></tr>
        ))}</tbody>
      </table>
    </>
  )
}

export default function Batches({ selected }) {
  const can = useCan()
  const [q, setQ] = useState('')
  const [status, setStatus] = useState('')
  const [list, err, reload] = useLoad(() => get('/batches', { q, status, limit: 200 }), [q, status])
  const [designs] = useLoad(() => get('/designs', { kind: 'CONSUMABLE', active: true, limit: 500 }), [])
  const [f, setF] = useState({ design_id: '', quantity: '', location_id: '', supplier_lot: '', expiry_date: '' })
  const [error, setError] = useState(null)
  if (selected) return <><p><a href="#/batches">← all batches</a></p><Detail id={selected} /></>
  async function receive(e) {
    e.preventDefault()
    try {
      const b = await post('/batches', { design_id: Number(f.design_id), quantity: Number(f.quantity), location_id: Number(f.location_id),
        supplier_lot: f.supplier_lot || undefined, expiry_date: f.expiry_date || undefined })
      setError(null); window.location.hash = `#/batches/${b.id}`; reload()
    } catch (er) { setError(er) }
  }
  return (
    <>
      <h1>Consumable batches</h1>
      <Err error={err} />
      {can('batch.receive') && (
        <form className="card" onSubmit={receive}>
          <b>Receive a batch</b>
          <div className="row">
            <label>Material (consumable design) *<select value={f.design_id} onChange={(e) => setF({ ...f, design_id: e.target.value })} required>
              <option value="">-</option>{(designs?.rows || []).filter((d) => d.approved_revision).map((d) => <option key={d.id} value={d.id}>{d.code} ({d.base_uom})</option>)}</select></label>
            <label>Quantity *<input type="number" min="0" step="any" value={f.quantity} onChange={(e) => setF({ ...f, quantity: e.target.value })} required /></label>
            <label>Location *<LocationSelect value={f.location_id} onChange={(v) => setF({ ...f, location_id: v })} required /></label>
            <label>Supplier lot<input value={f.supplier_lot} onChange={(e) => setF({ ...f, supplier_lot: e.target.value })} /></label>
            <label>Expiry (if applicable)<input type="date" value={f.expiry_date} onChange={(e) => setF({ ...f, expiry_date: e.target.value })} /></label>
            <button>Receive</button>
          </div>
          <Err error={error} />
        </form>
      )}
      <div className="row" style={{ marginBottom: 10 }}>
        <label>Search<input value={q} onChange={(e) => setQ(e.target.value)} placeholder="batch, lot, material…" /></label>
        <label>Status<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">All</option>
          {['AVAILABLE', 'HELD', 'EXPIRED', 'ISSUED_OUT', 'EXHAUSTED'].map((s) => <option key={s}>{s}</option>)}</select></label>
      </div>
      <table>
        <thead><tr><th>Batch</th><th>Material</th><th>Lot</th><th>Usable</th><th>Held</th><th>Out</th><th>Consumed</th><th>Expiry</th><th>Location</th><th>Status</th></tr></thead>
        <tbody>{(list?.rows || []).map((b) => (
          <tr key={b.id}><td><a href={`#/batches/${b.id}`}>{b.code}</a></td><td>{b.design_code}</td><td>{b.supplier_lot}</td>
            <td>{b.usable} {b.uom}</td><td>{b.held}</td><td>{b.outstanding}</td><td>{b.consumed}</td><td>{b.expiry_date}</td><td>{b.location}</td><td><Chip v={b.status} /></td></tr>
        ))}</tbody>
      </table>
    </>
  )
}
