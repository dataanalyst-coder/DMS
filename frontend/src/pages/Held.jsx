import { useState } from 'react'
import { get, post } from '../api.js'
import { Err, Ok, fmtTime, useCan, useLoad } from '../components.jsx'

function ReworkForm({ repair, onDone, setMsg, setError }) {
  const [design] = useLoad(() => get(`/designs/${repair.design_id}`), [repair.design_id])
  const [rev, setRev] = useState('')
  const [reason, setReason] = useState('')
  const choices = (design?.revisions || []).filter((r) => r.status === 'APPROVED' && r.id !== repair.design_revision_id)
  if (!choices.length) return <small className="muted">No newer approved design revision to rework to.</small>
  async function go() {
    try {
      const r = await post(`/items/${repair.item_id}/rework-revision`, { new_revision_id: Number(rev), reason, repair_id: repair.id })
      setMsg(`Reworked ${r.old_revision} → ${r.new_revision}. Accepted by approved assemblies: ${r.accepted_by_assemblies.join(', ') || 'none'}. Open WOs that counted on it are flagged for recheck.`)
      setError(null); onDone()
    } catch (e) { setError(e); setMsg(null) }
  }
  return (
    <span className="row" style={{ alignItems: 'center' }}>
      <select value={rev} onChange={(e) => setRev(e.target.value)}><option value="">rework to revision…</option>{choices.map((r) => <option key={r.id} value={r.id}>{r.revision}</option>)}</select>
      <input placeholder="reason (required)" value={reason} onChange={(e) => setReason(e.target.value)} />
      <button className="secondary" disabled={!rev || !reason} onClick={go}>Approve revision change</button>
    </span>
  )
}

export default function Held() {
  const can = useCan()
  const [q, err, reload] = useLoad(() => get('/held'), [])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [f, setF] = useState({})
  const [w, setW] = useState({})
  const [note, setNote] = useState({})
  if (!q) return <><h1>Repair and disposition</h1><Err error={err} /></>
  const act = (fn, ok) => async () => { try { await fn(); setError(null); setMsg(ok); reload() } catch (e) { setError(e); setMsg(null) } }
  const upd = (id, patch) => setF((p) => ({ ...p, [id]: { decision: 'REPAIR', ...p[id], ...patch } }))

  return (
    <>
      <h1>Repair and disposition</h1>
      <p className="muted">Held items wait here for a decision. Repair ends in re-inspection; nothing becomes serviceable without a passing inspection and Engineering's release. Scrap needs the designated approver.</p>
      <Err error={error} /><Ok>{msg}</Ok>

      <h2>Held items</h2>
      <table>
        <thead><tr><th>Item</th><th>Design</th><th>Held since</th><th>Decision</th></tr></thead>
        <tbody>
          {q.holds.map((h) => (
            <tr key={h.id}>
              <td className="mono"><a href={`#/item/${h.id}`}>{h.display_id}</a></td><td>{h.design_code}</td><td>{fmtTime(h.since)}</td>
              <td>{h.pending_scrap_proposal_id ? <span className="muted">scrap proposed; waiting for the approver</span> : can('disposition.decide') ? (
                <span className="row" style={{ alignItems: 'center' }}>
                  <select value={f[h.id]?.decision || 'REPAIR'} onChange={(e) => upd(h.id, { decision: e.target.value })}><option value="REPAIR">Repair</option><option value="FURTHER_INSPECTION">Further inspection</option><option value="PROPOSE_SCRAP">Propose scrap</option></select>
                  <input placeholder="findings *" value={f[h.id]?.findings || ''} onChange={(e) => upd(h.id, { findings: e.target.value })} />
                  <input placeholder="responsible *" value={f[h.id]?.responsible || ''} onChange={(e) => upd(h.id, { responsible: e.target.value })} />
                  <button disabled={!f[h.id]?.findings || !f[h.id]?.responsible} onClick={act(() => post(`/items/${h.id}/disposition`, { decision: f[h.id].decision || 'REPAIR', findings: f[h.id].findings, responsible: f[h.id].responsible }), 'Decision recorded.')}>Record</button>
                </span>) : <span className="muted">{h.last_decision ? `last: ${h.last_decision}` : 'awaiting a decision from Tooling'}</span>}</td>
            </tr>
          ))}
          {q.holds.length === 0 && <tr><td colSpan="4" className="muted">No items are on hold.</td></tr>}
        </tbody>
      </table>

      <h2>Open repairs</h2>
      <table>
        <thead><tr><th>Item</th><th>Findings</th><th>Responsible</th><th>Opened</th><th>Complete / rework</th></tr></thead>
        <tbody>
          {q.open_repairs.map((r) => (
            <tr key={r.id}>
              <td className="mono"><a href={`#/item/${r.item_id}`}>{r.display_id}</a></td><td>{r.findings}</td><td>{r.responsible}</td><td>{fmtTime(r.opened_at)}</td>
              <td>
                {can('rework.revision') && <div><ReworkForm repair={r} onDone={reload} setMsg={setMsg} setError={setError} /></div>}
                {can('repair.complete') && (
                  <span className="row" style={{ alignItems: 'center', marginTop: 6 }}>
                    <input placeholder="work done *" value={w[r.id]?.work || ''} onChange={(e) => setW((p) => ({ ...p, [r.id]: { ...p[r.id], work: e.target.value } }))} />
                    <input placeholder="measurements" value={w[r.id]?.meas || ''} onChange={(e) => setW((p) => ({ ...p, [r.id]: { ...p[r.id], meas: e.target.value } }))} />
                    <button disabled={!w[r.id]?.work} onClick={act(() => post(`/repairs/${r.id}/complete`, { work_done: w[r.id].work, measurements: w[r.id].meas }), 'Repair recorded: the item now waits for inspection.')}>Complete repair</button>
                  </span>)}
              </td>
            </tr>
          ))}
          {q.open_repairs.length === 0 && <tr><td colSpan="5" className="muted">No repairs in progress.</td></tr>}
        </tbody>
      </table>

      <h2>Scrap proposals</h2>
      <table>
        <thead><tr><th>Item</th><th>Findings</th><th>Proposed by</th><th>When</th><th>Decision</th></tr></thead>
        <tbody>
          {q.pending_scrap.map((p) => (
            <tr key={p.id}>
              <td className="mono"><a href={`#/item/${p.item_id}`}>{p.display_id}</a></td><td>{p.findings}</td><td>{p.proposed_by}</td><td>{fmtTime(p.proposed_at)}</td>
              <td>{can('condition.scrap') ? (
                <span className="row" style={{ alignItems: 'center' }}>
                  <input placeholder="note (required to refuse)" value={note[p.id] || ''} onChange={(e) => setNote((n) => ({ ...n, [p.id]: e.target.value }))} />
                  <button className="danger" onClick={() => window.confirm(`Authorise scrapping ${p.display_id}?`) && act(() => post(`/scrap-proposals/${p.id}/decide`, { approve: true, note: note[p.id] }), 'Scrap approved.')()}>Approve scrap</button>
                  <button className="secondary" onClick={act(() => post(`/scrap-proposals/${p.id}/decide`, { approve: false, note: note[p.id] }), 'Scrap refused; the item stays on hold.')}>Refuse</button>
                </span>) : <span className="muted">only the designated scrap approver decides</span>}</td>
            </tr>
          ))}
          {q.pending_scrap.length === 0 && <tr><td colSpan="5" className="muted">No scrap proposals waiting.</td></tr>}
        </tbody>
      </table>
    </>
  )
}
