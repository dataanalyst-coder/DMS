import { useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, LocationSelect, Ok, ScanInput, fmtTime, useCan, useLoad } from '../components.jsx'

const WHY = {
  EXPECTED_ELSEWHERE: 'recorded at another location',
  WITH_PRODUCTION: 'recorded as with Production',
  MARKED_MISSING: 'was marked missing',
  RETIRED: 'retired item',
}

function Detail({ id }) {
  const can = useCan()
  const [c, loadErr, reload] = useLoad(() => get(`/counts/${id}`), [id])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [batchQty, setBatchQty] = useState('')
  const [reason, setReason] = useState({})
  const [note, setNote] = useState('')
  if (loadErr) return <Err error={loadErr} />
  if (!c) return <p className="muted">Loading…</p>
  const r = c.report
  const open = c.status === 'OPEN'
  const done = c.status === 'COMPLETED'
  const act = (fn, ok) => async () => { try { const x = await fn(); setError(null); setMsg(typeof ok === 'function' ? ok(x) : ok); reload() } catch (e) { setError(e); setMsg(null) } }
  const setR = (k, v) => setReason((p) => ({ ...p, [k]: v }))
  const adjust = (body, ok) => act(() => post(`/counts/${id}/apply`, body), ok)

  async function scan(code) {
    try {
      const res = await post(`/counts/${id}/scan`, { code, quantity: batchQty === '' ? undefined : Number(batchQty) })
      setError(null); setMsg(`Recorded ${code}.`); reload(); return res
    } catch (e) { setError(e); setMsg(null) }
  }

  return (
    <>
      <h1>Count #{c.id} <small className="muted">{c.location}</small> <Chip v={c.status} /></h1>
      <Err error={error} /><Ok>{msg}</Ok>
      <p className="muted">Started by {c.started_by} {fmtTime(c.started_at)}{c.completed_at ? ` · closed ${fmtTime(c.completed_at)} by ${c.completed_by}` : ''}. A count only observes: nothing changes in stock until an authorised person adjusts it, with a reason.</p>

      {open && can('count.perform') && (
        <div className="card noprint">
          <ScanInput autoFocus buttonText="Count" label="Scan each item or build label here (consumable batches need a quantity)" onCode={scan} />
          <label style={{ marginTop: 8 }}>Quantity found (consumable batch scans only)
            <input type="number" min="0" step="any" value={batchQty} onChange={(e) => setBatchQty(e.target.value)} style={{ width: 120 }} />
          </label>
          <p className="row">
            <input placeholder="note (optional)" value={note} onChange={(e) => setNote(e.target.value)} />
            <button onClick={act(() => post(`/counts/${id}/complete`, { note: note || undefined }), 'Count closed. The figures below are now fixed.')}>Complete count</button>
            <button className="secondary" onClick={() => window.confirm('Cancel this count? Scans are kept for audit but no result is produced.') && act(() => post(`/counts/${id}/cancel`, { reason: 'cancelled by user' }), 'Count cancelled.')()}>Cancel</button>
          </p>
        </div>
      )}

      <div className="counts">
        <div className="count"><b>{r.summary.expected}</b>Expected here</div>
        <div className="count"><b>{r.summary.found}</b>Found</div>
        <div className="count"><b>{r.summary.missing}</b>{open ? 'Not yet seen' : 'Missing'}</div>
        <div className="count"><b>{r.summary.unexpected}</b>Unexpected</div>
        <div className="count"><b>{r.summary.batch_variances}</b>Batch variances</div>
      </div>

      <h2>{open ? 'Not yet seen' : 'Missing'} <small className="muted">(expected here, not scanned)</small></h2>
      <table>
        <thead><tr><th>Item</th><th>Design</th><th>Condition</th><th>Custody</th><th>Recorded at</th>{done && can('stock.adjust') && <th>Mark missing</th>}</tr></thead>
        <tbody>
          {r.missing.map((m) => (
            <tr key={m.id}>
              <td className="mono"><a href={`#/item/${m.id}`}>{m.display_id}</a></td><td>{m.design_code}</td><td><Chip v={m.condition} /></td><td><Chip v={m.custody} /></td><td>{m.location}</td>
              {done && can('stock.adjust') && <td className="row" style={{ alignItems: 'center' }}>
                <input placeholder="reason *" value={reason[`m${m.id}`] || ''} onChange={(e) => setR(`m${m.id}`, e.target.value)} />
                <button className="danger" disabled={!reason[`m${m.id}`]} onClick={adjust({ item_actions: [{ item_id: m.id, action: 'MARK_MISSING', reason: reason[`m${m.id}`] }] }, `${m.display_id} marked missing; it can no longer pass a readiness check.`)}>Mark missing</button>
              </td>}
            </tr>
          ))}
          {r.missing.length === 0 && <tr><td colSpan="6" className="muted">Nothing missing.</td></tr>}
        </tbody>
      </table>

      <h2>Unexpected <small className="muted">(scanned here but not expected here)</small></h2>
      <table>
        <thead><tr><th>Item</th><th>Why</th><th>Custody</th><th>Recorded at</th>{done && can('stock.adjust') && <th>Adjust</th>}</tr></thead>
        <tbody>
          {r.unexpected.map((u) => (
            <tr key={u.id}>
              <td className="mono"><a href={`#/item/${u.id}`}>{u.display_id}</a></td><td>{WHY[u.reason] || u.reason}</td><td><Chip v={u.custody} /></td><td>{u.recorded_location}</td>
              {done && can('stock.adjust') && <td>{(u.reason === 'EXPECTED_ELSEWHERE' || u.reason === 'MARKED_MISSING') ? (
                <span className="row" style={{ alignItems: 'center' }}>
                  <input placeholder="reason *" value={reason[`u${u.id}`] || ''} onChange={(e) => setR(`u${u.id}`, e.target.value)} />
                  <button disabled={!reason[`u${u.id}`]} onClick={adjust({ item_actions: [{ item_id: u.id, action: u.reason === 'MARKED_MISSING' ? 'RECOVER' : 'MOVE_HERE', reason: reason[`u${u.id}`] }] }, u.reason === 'MARKED_MISSING' ? `${u.display_id} recovered.` : `${u.display_id} recorded here.`)}>{u.reason === 'MARKED_MISSING' ? 'Recover here' : 'Record it here'}</button>
                </span>) : <span className="muted">no adjustment possible</span>}</td>}
            </tr>
          ))}
          {r.unexpected.length === 0 && <tr><td colSpan="5" className="muted">None.</td></tr>}
        </tbody>
      </table>

      <h2>Consumable batches</h2>
      <table>
        <thead><tr><th>Batch</th><th>Expected</th><th>Counted</th><th>Variance</th><th>State</th>{done && can('stock.adjust') && <th>Correct to counted</th>}</tr></thead>
        <tbody>
          {r.batches.map((b) => (
            <tr key={b.batch_id}>
              <td><a href={`#/batches/${b.batch_id}`}>{b.code}</a></td><td>{b.expected} {b.uom}</td><td>{b.counted ?? ''}</td><td>{b.variance ?? ''}</td><td><Chip v={b.state} /></td>
              {done && can('stock.adjust') && <td>{(b.state === 'SHORT' || b.state === 'OVER') && (
                <span className="row" style={{ alignItems: 'center' }}>
                  <input placeholder="reason *" value={reason[`b${b.batch_id}`] || ''} onChange={(e) => setR(`b${b.batch_id}`, e.target.value)} />
                  <button disabled={!reason[`b${b.batch_id}`]} onClick={adjust({ batch_adjustments: [{ batch_id: b.batch_id, reason: reason[`b${b.batch_id}`] }] }, `${b.code} corrected to ${b.counted} ${b.uom}.`)}>Adjust</button>
                </span>)}</td>}
            </tr>
          ))}
          {r.batches.length === 0 && <tr><td colSpan="6" className="muted">No consumable batches stored here.</td></tr>}
        </tbody>
      </table>

      <h2>Found</h2>
      <table>
        <thead><tr><th>Item</th><th>Design</th><th>Condition</th><th>Custody</th></tr></thead>
        <tbody>
          {r.found.map((f) => <tr key={f.id}><td className="mono"><a href={`#/item/${f.id}`}>{f.display_id}</a></td><td>{f.design_code}</td><td><Chip v={f.condition} /></td><td><Chip v={f.custody} /></td></tr>)}
          {r.found.length === 0 && <tr><td colSpan="4" className="muted">Nothing scanned yet.</td></tr>}
        </tbody>
      </table>
    </>
  )
}

export default function Counts({ selected }) {
  const can = useCan()
  const [list, err, reload] = useLoad(() => get('/counts'), [])
  const [loc, setLoc] = useState('')
  const [error, setError] = useState(null)
  if (selected) return <><p><a href="#/counts">← all counts</a></p><Detail id={selected} /></>
  async function start() {
    try { const c = await post('/counts', { location_id: Number(loc) }); window.location.hash = `#/counts/${c.id}` } catch (e) { setError(e) }
  }
  return (
    <>
      <h1>Stock counts</h1>
      <p className="muted">Count one location at a time: scan what is physically there and the system shows what is found, missing and unexpected. Missing items cannot be used until resolved.</p>
      <Err error={err || error} />
      {can('count.perform') && (
        <div className="row" style={{ marginBottom: 12 }}>
          <label>Location to count *<LocationSelect value={loc} onChange={setLoc} /></label>
          <button disabled={!loc} onClick={start}>Start count</button>
        </div>
      )}
      <table>
        <thead><tr><th>Count</th><th>Location</th><th>Status</th><th>Started</th><th>By</th><th>Closed</th></tr></thead>
        <tbody>
          {(list || []).map((c) => (
            <tr key={c.id}><td><a href={`#/counts/${c.id}`}>#{c.id}</a></td><td>{c.location}</td><td><Chip v={c.status} /></td><td>{fmtTime(c.started_at)}</td><td>{c.started_by}</td><td>{fmtTime(c.completed_at)}</td></tr>
          ))}
          {list?.length === 0 && <tr><td colSpan="6" className="muted">No counts yet.</td></tr>}
        </tbody>
      </table>
    </>
  )
}
