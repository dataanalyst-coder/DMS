import { useState } from 'react'
import { get, post, put, upload } from '../api.js'
import { Chip, Err, Ok, ProtectedImg, fmtTime, useCan, useLoad } from '../components.jsx'

function CheckRow({ c, inspectionId, open, onSaved, can }) {
  const [res, setRes] = useState(c.result || '')
  const [val, setVal] = useState(c.value ?? '')
  const [text, setText] = useState(c.text || '')
  const [reason, setReason] = useState(c.reason || '')
  const [error, setError] = useState(null)
  const editable = open && can('inspection.perform')
  const needsPhoto = c.evidence === 'ALWAYS' || c.evidence === 'ON_FAIL'

  async function save() {
    try {
      const body = { check_id: c.id }
      if (c.check_type === 'MEASUREMENT' && res !== 'NA') body.value = Number(val)
      else if (c.check_type === 'TEXT' && res !== 'NA') body.text = text
      else body.result = res
      if (reason) body.reason = reason
      await put(`/inspections/${inspectionId}/results`, body)
      setError(null); onSaved()
    } catch (e) { setError(e) }
  }
  async function photo(file) {
    if (!file) return
    try { await upload(`/inspections/${inspectionId}/photos`, { check_id: c.id, file }); setError(null); onSaved() } catch (e) { setError(e) }
  }
  return (
    <tr>
      <td><b>{c.title}</b> {c.required && <small className="muted">required</small>}<br /><small className="muted">{c.guidance} {c.scope === 'design' ? '· design-specific' : ''}</small></td>
      <td>
        {c.check_type === 'MEASUREMENT' && <><input type="number" step="any" style={{ width: 90, minWidth: 0 }} disabled={!editable} value={val} onChange={(e) => setVal(e.target.value)} /> {c.unit} <small className="muted">[{c.min_value ?? '–'}, {c.max_value ?? '–'}]</small> </>}
        {c.check_type === 'TEXT' && <input disabled={!editable} value={text} onChange={(e) => setText(e.target.value)} />}
        {c.check_type === 'PASS_FAIL' && <select disabled={!editable} value={res} onChange={(e) => setRes(e.target.value)}><option value="">-</option><option>PASS</option><option>FAIL</option>{c.na_allowed && <option>NA</option>}</select>}
        {c.check_type !== 'PASS_FAIL' && c.na_allowed && editable && <label style={{ display: 'inline-flex', flexDirection: 'row', gap: 3 }}><input type="checkbox" style={{ minWidth: 0 }} checked={res === 'NA'} onChange={(e) => setRes(e.target.checked ? 'NA' : '')} />n/a</label>}
        {(res === 'FAIL' || res === 'NA' || c.result === 'FAIL') && <> <input placeholder="reason / finding" disabled={!editable} value={reason} onChange={(e) => setReason(e.target.value)} /></>}
      </td>
      <td>{c.result && <Chip v={c.result} />}{c.result === 'FAIL' && c.reason && <small className="muted"> {c.reason}</small>}</td>
      <td>
        {c.photos.map((p) => <span key={p.id} style={{ marginRight: 4 }}><ProtectedImg path={`/inspection-photos/${p.id}`} alt={p.file_name} /></span>)}
        {editable && (needsPhoto || c.photos.length > 0) && <label className="noprint">photo ({c.evidence === 'ALWAYS' ? 'always' : 'on fail'})<input type="file" accept=".png,.jpg,.jpeg" onChange={(e) => { photo(e.target.files[0]); e.target.value = '' }} /></label>}
        <Err error={error} />
      </td>
      <td>{editable && <button className="secondary" disabled={!(res || val !== '' || text)} onClick={save}>Save</button>}</td>
    </tr>
  )
}

function Detail({ id }) {
  const can = useCan()
  const [d, loadErr, reload] = useLoad(() => get(`/inspections/${id}`), [id])
  const [summary, setSummary] = useState('')
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  if (loadErr) return <Err error={loadErr} />
  if (!d) return <p className="muted">Loading…</p>
  const open = d.status === 'OPEN'
  async function complete() {
    try {
      const r = await post(`/inspections/${id}/complete`, { summary })
      setMsg(r.outcome === 'HOLD' ? 'Inspection failed a required check: the item is now on HOLD.' : 'Inspection passed. The item stays "awaiting inspection" until Engineering approves its release.')
      setError(null); reload()
    } catch (e) { setError(e); setMsg(null) }
  }
  return (
    <>
      <h1>Inspection #{d.id} <small className="muted"><a href={`#/item/${d.item.id}`}>{d.item.display_id}</a></small> <Chip v={d.status} /> {d.outcome && <Chip v={d.outcome === 'PASS' ? 'PASS' : 'HOLD'} />}</h1>
      <p className="muted">Started by {d.started_by} {fmtTime(d.started_at)}{d.completed_at ? ` · completed by ${d.completed_by} ${fmtTime(d.completed_at)}` : ''}.
        Checklist: {Object.entries(d.checklist_versions).map(([k, v]) => `${v.template} v${v.version}`).join(' + ')} (the versions in force when it was started).</p>
      <Err error={error} /><Ok>{msg}</Ok>
      <table>
        <thead><tr><th>Check</th><th>Result</th><th>Recorded</th><th>Photos</th><th></th></tr></thead>
        <tbody>{d.checks.map((c) => <CheckRow key={c.id} c={c} inspectionId={id} open={open} onSaved={reload} can={can} />)}</tbody>
      </table>
      {open && can('inspection.perform') && (
        <div className="card noprint" style={{ marginTop: 12 }}>
          <div className="row">
            <label>Summary<input value={summary} onChange={(e) => setSummary(e.target.value)} style={{ minWidth: 320 }} /></label>
            <button onClick={complete}>Complete inspection</button>
          </div>
          <p className="muted">Every check needs an answer and mandatory photos must be attached. A failed required check puts the item on hold; nothing is released by inspecting.</p>
        </div>
      )}
      {d.summary && <p>Summary: {d.summary}</p>}
    </>
  )
}

export default function Inspections({ selected }) {
  const can = useCan()
  const [queue, qerr, reloadQ] = useLoad(() => get('/inspections/queue'), [])
  const [list, lerr, reloadL] = useLoad(() => get('/inspections', { limit: 50 }), [])
  const [error, setError] = useState(null)
  if (selected) return <><p><a href="#/inspections">← all inspections</a></p><Detail id={selected} /></>
  async function start(itemId) {
    try { const i = await post('/inspections', { item_id: itemId }); window.location.hash = `#/inspections/${i.id}` }
    catch (e) { setError(e) }
  }
  return (
    <>
      <h1>Inspection</h1>
      <Err error={error || qerr || lerr} />
      <h2>Waiting for inspection</h2>
      <table>
        <thead><tr><th>Item</th><th>Design</th><th>Waiting since</th><th></th></tr></thead>
        <tbody>
          {(queue || []).map((q) => (
            <tr key={q.id}><td className="mono"><a href={`#/item/${q.id}`}>{q.display_id}</a></td><td>{q.design_code}</td><td>{fmtTime(q.since)}</td>
              <td>{q.open_inspection_id ? <a href={`#/inspections/${q.open_inspection_id}`}>open inspection #{q.open_inspection_id}</a>
                : can('inspection.perform') && <button onClick={() => start(q.id)}>Start inspection</button>}</td></tr>
          ))}
          {queue?.length === 0 && <tr><td colSpan="4" className="muted">Nothing is waiting for inspection.</td></tr>}
        </tbody>
      </table>
      <h2>Recent inspections</h2>
      <table>
        <thead><tr><th>#</th><th>Item</th><th>Status</th><th>Outcome</th><th>Inspector</th><th>Started</th></tr></thead>
        <tbody>{(list?.rows || []).map((i) => (
          <tr key={i.id}><td><a href={`#/inspections/${i.id}`}>{i.id}</a></td><td className="mono">{i.display_id}</td><td><Chip v={i.status} /></td>
            <td>{i.outcome && <Chip v={i.outcome === 'PASS' ? 'PASS' : 'HOLD'} />}</td><td>{i.started_by}</td><td>{fmtTime(i.started_at)}</td></tr>
        ))}</tbody>
      </table>
    </>
  )
}
