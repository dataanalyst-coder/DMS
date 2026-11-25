import { useState } from 'react'
import { get, post, put } from '../api.js'
import { Err, Ok, useCan, useLoad } from '../components.jsx'

export default function Mappings() {
  const can = useCan()
  const [flt, setFlt] = useState({ part_id: '', operation_id: '' })
  const [applied, setApplied] = useState(flt)
  const [res, err, reload] = useLoad(() => get('/mappings', { ...applied, active: true }), [applied])
  const [asms] = useLoad(() => get('/assemblies', { limit: 500 }), [])
  const [f, setF] = useState({ part_id: '', routing_revision: '', operation_id: '', definition_id: '', tooling_status: 'READY', pouring_weight_kg: '', pouring_weight_revision: '' })
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const act = (fn, ok) => async () => { try { await fn(); setError(null); setMsg(ok); reload() } catch (e) { setError(e); setMsg(null) } }

  return (
    <>
      <h1>ME operation mapping</h1>
      <div className="warn">Manufacturing Engineering owns this mapping. Until the ME integration is agreed, Engineering enters it here as a
        stand-in (source MANUAL). Only options marked <b>selectable</b> can be chosen; the others show why not.</div>
      <Err error={err || error} /><Ok>{msg}</Ok>
      {can('mapping.manage') && (
        <div className="card">
          <b>Link an assembly to a part operation</b>
          <div className="row">
            <label>Part ID *<input value={f.part_id} onChange={(e) => setF({ ...f, part_id: e.target.value })} /></label>
            <label>Routing revision *<input value={f.routing_revision} onChange={(e) => setF({ ...f, routing_revision: e.target.value })} /></label>
            <label>Operation *<input value={f.operation_id} onChange={(e) => setF({ ...f, operation_id: e.target.value })} /></label>
            <label>Assembly *<select value={f.definition_id} onChange={(e) => setF({ ...f, definition_id: e.target.value })}><option value="">-</option>{(asms?.rows || []).map((a) => <option key={a.id} value={a.id}>{a.code}</option>)}</select></label>
            <label>Tooling<select value={f.tooling_status} onChange={(e) => setF({ ...f, tooling_status: e.target.value })}><option>READY</option><option>TO_BE_DEVELOPED</option></select></label>
            <label>Pouring wt (kg)<input type="number" min="0" step="any" value={f.pouring_weight_kg} onChange={(e) => setF({ ...f, pouring_weight_kg: e.target.value })} placeholder="blank = unknown" /></label>
            <label>Weight revision<input value={f.pouring_weight_revision} onChange={(e) => setF({ ...f, pouring_weight_revision: e.target.value })} /></label>
            <button disabled={!f.part_id || !f.routing_revision || !f.operation_id || !f.definition_id}
              onClick={act(() => post('/mappings', { ...f, definition_id: Number(f.definition_id), pouring_weight_kg: f.pouring_weight_kg === '' ? null : Number(f.pouring_weight_kg), pouring_weight_revision: f.pouring_weight_revision || null }), 'Mapping added.')}>Add</button>
          </div>
        </div>
      )}
      <form className="row" onSubmit={(e) => { e.preventDefault(); setApplied(flt) }} style={{ marginBottom: 10 }}>
        <label>Part ID<input value={flt.part_id} onChange={(e) => setFlt({ ...flt, part_id: e.target.value })} /></label>
        <label>Operation<input value={flt.operation_id} onChange={(e) => setFlt({ ...flt, operation_id: e.target.value })} /></label>
        <button>Filter</button>
      </form>
      <table>
        <thead><tr><th>Part</th><th>Routing</th><th>Op</th><th>Assembly</th><th>Rev used</th><th>Selected</th><th>Tooling</th><th>Pouring wt</th><th>Selectable</th><th></th></tr></thead>
        <tbody>
          {(res?.rows || []).map((m) => (
            <tr key={m.id}>
              <td className="mono">{m.part_id}</td><td>{m.routing_revision}</td><td>{m.operation_id}</td>
              <td><a href={`#/assemblies/${m.definition_id}`}>{m.assembly_code}</a></td>
              <td>{m.effective_revision || '-'}{m.pinned_revision_id ? ' (pinned)' : ''}</td>
              <td>{m.selected ? 'Yes' : ''}</td><td>{m.tooling_status.replaceAll('_', ' ')}</td>
              <td>{m.pouring_weight_kg ?? <span className="muted">unknown</span>}{m.pouring_weight_revision ? ` (${m.pouring_weight_revision})` : ''}</td>
              <td>{m.selectable ? <span style={{ color: 'var(--ok)' }}>Yes</span> : <span style={{ color: 'var(--bad)' }}>No: {m.not_selectable_because.join('; ')}</span>}</td>
              <td>{can('mapping.manage') && <>
                {!m.selected && <button className="secondary" onClick={act(() => put(`/mappings/${m.id}`, { selected: true }), 'Selected.')}>Select</button>}{' '}
                <button className="secondary" onClick={act(() => put(`/mappings/${m.id}`, { tooling_status: m.tooling_status === 'READY' ? 'TO_BE_DEVELOPED' : 'READY' }), 'Updated.')}>{m.tooling_status === 'READY' ? 'Mark to develop' : 'Mark ready'}</button>{' '}
                <button className="danger" onClick={act(() => put(`/mappings/${m.id}`, { active: false, reason: window.prompt('Reason for deactivating this mapping?') }), 'Deactivated.')}>Deactivate</button></>}</td>
            </tr>
          ))}
          {res?.rows.length === 0 && <tr><td colSpan="10" className="muted">No mappings.</td></tr>}
        </tbody>
      </table>
    </>
  )
}
