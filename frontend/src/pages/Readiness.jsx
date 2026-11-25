import { useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, fmtTime, itemLink, useCan, useLoad } from '../components.jsx'

const BLANK = { wo_id: '', part_id: '', routing_revision: '', operation_id: '' }

function Result({ r, onUseOption }) {
  return (
    <div className="card">
      <p><span className={`big-result ${r.result}`}>{r.result.replaceAll('_', ' ')}</span>{' '}
        {r.can_release ? <b>WO may be released against this option.</b> : <b>Do not release the WO against this option.</b>}
        <small className="muted"> checked {fmtTime(r.checked_at)} by {r.checked_by} · assembly {r.assembly || '-'} · check #{r.id}</small></p>
      {r.note && <p className="muted">{r.note}</p>}
      {r.reasons.length > 0 && <ul>{r.reasons.map((x, i) => <li key={i}><b className="mono">{x.code}</b>: {x.message}</li>)}</ul>}
      {r.shortages.length > 0 && (
        <table>
          <thead><tr><th>Required element</th><th>Needed</th><th>Missing</th><th>Not usable</th></tr></thead>
          <tbody>{r.shortages.map((s, i) => (
            <tr key={i}><td>{s.line}</td><td>{s.needed}</td><td style={{ color: 'var(--bad)' }}>{s.missing}</td>
              <td>{s.blocked_items.length ? s.blocked_items.map((b) => `${b.display_id} (${b.condition})`).join(', ') : <span className="muted">none registered</span>}</td></tr>
          ))}</tbody>
        </table>
      )}
      {r.custody_warnings.length > 0 && (
        <div className="warn"><b>Custody warnings</b> (release is NOT blocked; nothing is reserved)<ul>{r.custody_warnings.map((w) => <li key={w.item_id}>{w.message}</li>)}</ul></div>
      )}
      {r.warnings.length > 0 && <div className="warn"><ul>{r.warnings.map((w, i) => <li key={i}>{w.message}</li>)}</ul></div>}
      {r.alternatives.length > 0 && (
        <div className="warn"><b>Another approved option is ready.</b> It is not selected for you: choose it and rerun the check.
          <ul>{r.alternatives.map((a) => <li key={a.mapping_id}>{a.assembly_code} rev {a.revision} {onUseOption && <button className="secondary" onClick={() => onUseOption(a.mapping_id)}>Use this option and rerun</button>}</li>)}</ul></div>
      )}
      {r.candidate_item_ids.length > 0 && <p className="muted">Evidence only (not an allocation): {r.candidate_item_ids.length} candidate items. The items actually issued may differ.</p>}
    </div>
  )
}

export default function Readiness() {
  const can = useCan()
  const [f, setF] = useState(BLANK)
  const [options, setOptions] = useState(null)
  const [mappingId, setMappingId] = useState('')
  const [result, setResult] = useState(null)
  const [error, setError] = useState(null)
  const [flagged, , reloadFlagged] = useLoad(() => get('/readiness/recheck'), [])
  const [hist, , reloadHist] = useLoad(() => get('/readiness/checks', { limit: 25 }), [])
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  async function find() {
    try {
      const o = await get('/mappings/options', { part_id: f.part_id, routing_revision: f.routing_revision, operation_id: f.operation_id })
      setOptions(o); setError(null)
      const sel = o.find((x) => x.selected)
      setMappingId(sel ? String(sel.id) : '')
    } catch (e) { setError(e) }
  }
  async function run(mid = mappingId) {
    try {
      setResult(await post('/readiness/checks', { ...f, mapping_id: mid ? Number(mid) : undefined }))
      setError(null); reloadFlagged(); reloadHist()
    } catch (e) { setError(e) }
  }
  const rerun = (c) => { setF({ wo_id: c.wo_id, part_id: c.part_id, routing_revision: c.routing_revision, operation_id: c.operation_id }); setOptions(null); setMappingId(c.mapping_id ? String(c.mapping_id) : '') }

  return (
    <>
      <h1>WO readiness</h1>
      <div className="warn">This screen stands in for OMS, which will call the same check when a WO is released. <b>Ready</b> means the tooling exists in
        serviceable condition (even if stored, fitted or issued). It is not a reservation and not a promise of physical availability: issue re-checks everything.</div>
      <Err error={error} />
      {flagged?.length > 0 && (
        <div className="card">
          <b>Open WOs to recheck</b> <small className="muted">(tooling they were cleared on has changed; nothing was cancelled)</small>
          <table><tbody>{flagged.map((c) => <tr key={c.id}><td><b>{c.wo_id}</b></td><td>{c.part_id} / {c.operation_id}</td><td>{c.recheck_reason}</td>
            <td>{can('readiness.check') && <button className="secondary" onClick={() => rerun(c)}>Load to rerun</button>}</td></tr>)}</tbody></table>
        </div>
      )}
      <div className="card">
        <div className="row">
          <label>WO ID *<input value={f.wo_id} onChange={set('wo_id')} /></label>
          <label>Part ID *<input value={f.part_id} onChange={set('part_id')} /></label>
          <label>Routing revision *<input value={f.routing_revision} onChange={set('routing_revision')} /></label>
          <label>Operation *<input value={f.operation_id} onChange={set('operation_id')} /></label>
          <button className="secondary" disabled={!f.part_id || !f.routing_revision || !f.operation_id} onClick={find}>Show assembly options</button>
        </div>
        {options && (
          <div style={{ marginTop: 10 }}>
            {options.length === 0 ? <p className="muted">No assembly option is mapped to this operation: the check will answer UNABLE TO VERIFY.</p> : (
              <table>
                <thead><tr><th></th><th>Option</th><th>Revision</th><th>ME selected</th><th>Selectable</th></tr></thead>
                <tbody>{options.map((o) => (
                  <tr key={o.id}><td><input type="radio" name="opt" style={{ minWidth: 0 }} checked={mappingId === String(o.id)} onChange={() => setMappingId(String(o.id))} /></td>
                    <td>{o.assembly_code}</td><td>{o.effective_revision || '-'}</td><td>{o.selected ? 'Yes' : ''}</td>
                    <td>{o.selectable ? 'Yes' : <span style={{ color: 'var(--bad)' }}>{o.not_selectable_because.join('; ')}</span>}</td></tr>
                ))}</tbody>
              </table>
            )}
          </div>
        )}
        <p><button disabled={!f.wo_id || !f.part_id || !f.routing_revision || !f.operation_id || !can('readiness.check')} onClick={() => run()}>Run readiness check</button>{' '}
          <span className="muted">{mappingId ? 'for the chosen option' : "uses ME's selected option"}</span></p>
      </div>
      {result && <Result r={result} onUseOption={(mid) => { setMappingId(String(mid)); run(String(mid)) }} />}

      <h2>Recent checks</h2>
      <table>
        <thead><tr><th>#</th><th>When</th><th>WO</th><th>Part / op</th><th>Assembly</th><th>Result</th><th>Recheck</th></tr></thead>
        <tbody>{(hist?.rows || []).map((c) => (
          <tr key={c.id}><td>{c.id}</td><td>{fmtTime(c.checked_at)}</td><td>{c.wo_id}</td><td>{c.part_id} / {c.operation_id}</td><td>{c.assembly || '-'}</td>
            <td><Chip v={c.result} /></td><td>{c.needs_recheck ? <span style={{ color: 'var(--use)' }} title={c.recheck_reason}>needed</span> : ''}</td></tr>
        ))}</tbody>
      </table>
    </>
  )
}
