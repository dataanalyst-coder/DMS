import { useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, Ok, Pager, fmtTime, useCan, useLoad } from '../components.jsx'

const REASON = {
  MISSING_BUILD: 'No build/assembly number was sent',
  UNKNOWN_BUILD: 'The build number is not known here',
  MISSING_CASTINGS: 'Castings quantity missing',
  MISSING_CYCLES: 'Cycles missing',
  MISSING_TIME: 'Production time missing',
  NO_ISSUE: 'The build was not issued to this WO at that time',
  SPLIT_REQUIRED: 'More than one issue applies; the split must be confirmed',
  INVALID_NUMBER: 'A quantity was not a valid whole number',
  FUTURE_TIME: 'The production time is in the future',
  INVALID_TIME: 'The production time is not a valid date/time',
}

function Exceptions({ ex, reload, setMsg, setError }) {
  const can = useCan()
  const [fix, setFix] = useState({})
  const upd = (id, patch) => setFix((p) => ({ ...p, [id]: { ...p[id], ...patch } }))
  const run = (fn, ok) => async () => { try { await fn(); setError(null); setMsg(ok); reload() } catch (e) { setError(e); setMsg(null) } }
  return (
    <>
      <div className="counts">
        <div className="count"><b>{ex.pending}</b>Waiting for reconciliation</div>
        <div className="count"><b>{ex.rejected}</b>Rejected</div>
        <div className="count"><b>{ex.last_successful_sync ? fmtTime(ex.last_successful_sync) : 'never'}</b>Last successful sync</div>
      </div>
      <table>
        <thead><tr><th>OMS event</th><th>WO / op</th><th>Build</th><th>Cycles / castings</th><th>Status</th><th>Why</th><th>Tries</th><th>Last attempt</th>{can('usage.reconcile') && <th>Reconcile</th>}</tr></thead>
        <tbody>
          {ex.events.map((e) => (
            <tr key={e.id}>
              <td className="mono">{e.source_event_id} <small className="muted">v{e.version}</small></td><td>{e.wo_id} {e.operation_id}</td><td>{e.build || ''}</td>
              <td>{e.cycles ?? '?'} / {e.castings ?? '?'}</td><td><Chip v={e.status} /></td>
              <td>{REASON[e.reason_code] || e.reason_code}<br /><small className="muted">{e.reason}</small></td>
              <td>{e.retry_count}</td><td>{fmtTime(e.last_attempt_at)}</td>
              {can('usage.reconcile') && <td>{e.status === 'PENDING' && (
                <div>
                  <button className="secondary" onClick={run(() => post(`/integration/usage/${e.id}/retry`), 'Retried.')}>Retry</button>
                  <div className="row" style={{ marginTop: 4, alignItems: 'center' }}>
                    {(e.reason_code === 'MISSING_BUILD' || e.reason_code === 'UNKNOWN_BUILD' || e.reason_code === 'NO_ISSUE' || e.reason_code === 'SPLIT_REQUIRED') && <input placeholder="build e.g. BLD-000012" value={fix[e.id]?.build || ''} onChange={(ev) => upd(e.id, { build: ev.target.value })} />}
                    {e.reason_code === 'MISSING_CASTINGS' && <input type="number" placeholder="castings" value={fix[e.id]?.castings || ''} onChange={(ev) => upd(e.id, { castings: ev.target.value })} />}
                    {e.reason_code === 'MISSING_CYCLES' && <input type="number" placeholder="cycles" value={fix[e.id]?.cycles || ''} onChange={(ev) => upd(e.id, { cycles: ev.target.value })} />}
                    <input placeholder="reason / who confirmed *" value={fix[e.id]?.reason || ''} onChange={(ev) => upd(e.id, { reason: ev.target.value })} />
                    <button disabled={!fix[e.id]?.reason} onClick={run(() => {
                      const f = fix[e.id]
                      return post(`/integration/usage/${e.id}/resolve`, { reason: f.reason, build: f.build || undefined, castings: f.castings ? Number(f.castings) : undefined, cycles: f.cycles ? Number(f.cycles) : undefined })
                    }, 'Reconciled and posted. The original message is kept as received.')}>Resolve</button>
                  </div>
                </div>)}</td>}
            </tr>
          ))}
          {ex.events.length === 0 && <tr><td colSpan="9" className="muted">No integration exceptions. Everything received has posted.</td></tr>}
        </tbody>
      </table>
    </>
  )
}

function Submit({ onDone, setMsg, setError }) {
  const [f, setF] = useState({ source_event_id: '', version: 1, wo_id: '', operation_id: '', build: '', production_end: '', cycles: '', castings: '' })
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }))
  async function go(e) {
    e.preventDefault()
    const body = Object.fromEntries(Object.entries(f).filter(([, v]) => v !== ''))
    ;['version', 'cycles', 'castings'].forEach((k) => { if (body[k] !== undefined) body[k] = Number(body[k]) })
    try {
      const r = await post('/integration/usage', body)
      setMsg(`${r.result}${r.event?.tonnage != null ? ` · ${r.event.tonnage} t` : ''}${r.event?.reason ? ` · ${r.event.reason}` : ''}`); setError(null); onDone()
    } catch (err) { setError(err); setMsg(null); onDone() }
  }
  return (
    <form className="card" onSubmit={go}>
      <b>Submit a usage message by hand</b> <small className="muted">(stand-in for the OMS feed; the same rules apply as to the automatic feed)</small>
      <div className="row">
        <label>OMS event ID *<input value={f.source_event_id} onChange={set('source_event_id')} required /></label>
        <label>Version<input type="number" min="1" value={f.version} onChange={set('version')} style={{ width: 70 }} /></label>
        <label>WO *<input value={f.wo_id} onChange={set('wo_id')} required /></label>
        <label>Operation<input value={f.operation_id} onChange={set('operation_id')} /></label>
        <label>Build / assembly<input value={f.build} onChange={set('build')} placeholder="BLD-000001" /></label>
        <label>Production end (UTC)<input type="datetime-local" value={f.production_end} onChange={set('production_end')} /></label>
        <label>Cycles<input type="number" min="0" value={f.cycles} onChange={set('cycles')} style={{ width: 90 }} /></label>
        <label>Castings<input type="number" min="0" value={f.castings} onChange={set('castings')} style={{ width: 90 }} /></label>
        <button>Submit</button>
      </div>
    </form>
  )
}

export default function Usage() {
  const can = useCan()
  const [ex, exErr, reloadEx] = useLoad(() => get('/integration/exceptions'), [])
  const [status, setStatus] = useState('')
  const [wo, setWo] = useState('')
  const [offset, setOffset] = useState(0)
  const [list, listErr, reloadList] = useLoad(() => get('/integration/usage', { status, wo_id: wo, limit: 25, offset }), [status, wo, offset])
  const [out] = useLoad(() => get('/usage/output'), [])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const reload = () => { reloadEx(); reloadList() }
  return (
    <>
      <h1>Usage and integration</h1>
      <p className="muted">Actual production usage from OMS. Cycles and castings are kept separate; tonnage = castings × pouring weight ÷ 1000, using the weight in force when the event was posted. Nothing is guessed: an event that cannot be tied to an issued build waits here.</p>
      <Err error={error || exErr || listErr} /><Ok>{msg}</Ok>
      {out && <div className="counts">
        <div className="count"><b>{out.events}</b>Posted events</div><div className="count"><b>{out.cycles}</b>Cycles</div>
        <div className="count"><b>{out.castings}</b>Castings</div><div className="count"><b>{out.tonnage}</b>Tonnes poured</div>
        <div className="count"><b>{out.events_with_unknown_tonnage}</b>Events with unknown weight</div>
      </div>}
      <h2>Exceptions</h2>
      {ex && <Exceptions ex={ex} reload={reload} setMsg={setMsg} setError={setError} />}
      {can('usage.ingest') && <Submit onDone={reload} setMsg={setMsg} setError={setError} />}
      <h2>All received events</h2>
      <div className="row">
        <label>Status<select value={status} onChange={(e) => { setOffset(0); setStatus(e.target.value) }}><option value="">any</option>{['POSTED', 'PENDING', 'SUPERSEDED', 'CANCELLED', 'REJECTED'].map((s) => <option key={s}>{s}</option>)}</select></label>
        <label>WO<input value={wo} onChange={(e) => { setOffset(0); setWo(e.target.value) }} /></label>
      </div>
      <table>
        <thead><tr><th>OMS event</th><th>WO / op</th><th>Build</th><th>Production end</th><th>Cycles</th><th>Castings</th><th>Weight</th><th>Tonnes</th><th>Status</th></tr></thead>
        <tbody>
          {(list?.rows || []).map((e) => (
            <tr key={e.id}>
              <td className="mono">{e.source_event_id} <small className="muted">v{e.version}</small></td><td>{e.wo_id} {e.operation_id}</td><td>{e.build || ''}</td><td>{fmtTime(e.production_end)}</td>
              <td>{e.cycles ?? ''}</td><td>{e.castings ?? ''}</td><td>{e.weight_kg != null ? `${e.weight_kg} kg${e.weight_revision ? ` (${e.weight_revision})` : ''}` : <span className="muted">unknown</span>}</td>
              <td>{e.tonnage ?? <span className="muted">unknown</span>}</td><td><Chip v={e.status} /></td>
            </tr>
          ))}
          {list?.rows.length === 0 && <tr><td colSpan="9" className="muted">No events.</td></tr>}
        </tbody>
      </table>
      {list && <Pager total={list.total} limit={25} offset={offset} onChange={setOffset} />}
    </>
  )
}
