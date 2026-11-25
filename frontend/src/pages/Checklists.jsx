import { useState } from 'react'
import { del, get, post, put } from '../api.js'
import { Chip, Err, Ok, useCan, useLoad } from '../components.jsx'

const BLANK = { code: '', title: '', check_type: 'PASS_FAIL', required: true, na_allowed: false, evidence: 'NONE', unit: '', min_value: '', max_value: '', guidance: '' }

function Detail({ id }) {
  const can = useCan()
  const [t, loadErr, reload] = useLoad(() => get(`/checklists/${id}`), [id])
  const [f, setF] = useState(BLANK)
  const [reason, setReason] = useState('')
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  if (loadErr) return <Err error={loadErr} />
  if (!t) return <p className="muted">Loading…</p>
  const act = (fn, ok, after) => async () => { try { await fn(); setError(null); setMsg(ok); after && after(); reload() } catch (e) { setError(e); setMsg(null) } }
  const hasDraft = t.versions.some((v) => v.status === 'DRAFT')
  const num = (v) => (v === '' ? null : Number(v))
  return (
    <>
      <h1>{t.name} <small className="muted">{t.scope_type === 'CATEGORY' ? 'tooling category' : 'design-specific additions'}</small></h1>
      <Err error={error} /><Ok>{msg}</Ok>
      {[...t.versions].reverse().map((v) => {
        const draft = v.status === 'DRAFT' && can('checklist.edit')
        return (
          <div className="card" key={v.id}>
            <h2 style={{ marginTop: 0 }}>Version {v.version} <Chip v={v.status} /> <small className="muted">{v.change_reason}</small></h2>
            <table>
              <thead><tr><th>#</th><th>Code</th><th>Check</th><th>Type / limits</th><th>Required (fail = hold)</th><th>N/A</th><th>Photo</th><th></th></tr></thead>
              <tbody>{v.checks.map((c) => (
                <tr key={c.id}><td>{c.position}</td><td className="mono">{c.code}</td><td>{c.title}<br /><small className="muted">{c.guidance}</small></td>
                  <td>{c.check_type}{c.check_type === 'MEASUREMENT' && ` ${c.min_value ?? '–'}..${c.max_value ?? '–'} ${c.unit}`}</td>
                  <td>{draft ? <input type="checkbox" style={{ minWidth: 0 }} checked={c.required} onChange={act(() => put(`/checklist-checks/${c.id}`, { required: !c.required }), 'Updated.')} /> : (c.required ? 'Yes' : 'No')}</td>
                  <td>{c.na_allowed ? 'Yes' : ''}</td>
                  <td>{draft ? <select value={c.evidence} onChange={(e) => act(() => put(`/checklist-checks/${c.id}`, { evidence: e.target.value }), 'Updated.')()}><option>NONE</option><option>ON_FAIL</option><option>ALWAYS</option></select> : c.evidence}</td>
                  <td>{draft && <button className="danger" onClick={act(() => del(`/checklist-checks/${c.id}`), 'Check removed.')}>Remove</button>}</td></tr>
              ))}</tbody>
            </table>
            {draft && (
              <div className="row" style={{ marginTop: 10 }}>
                <label>Code *<input value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })} /></label>
                <label>Title *<input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} /></label>
                <label>Type<select value={f.check_type} onChange={(e) => setF({ ...f, check_type: e.target.value })}><option>PASS_FAIL</option><option>MEASUREMENT</option><option>TEXT</option></select></label>
                {f.check_type === 'MEASUREMENT' && <>
                  <label>Unit *<input style={{ minWidth: 60, width: 70 }} value={f.unit} onChange={(e) => setF({ ...f, unit: e.target.value })} /></label>
                  <label>Min<input type="number" step="any" style={{ width: 90, minWidth: 0 }} value={f.min_value} onChange={(e) => setF({ ...f, min_value: e.target.value })} /></label>
                  <label>Max<input type="number" step="any" style={{ width: 90, minWidth: 0 }} value={f.max_value} onChange={(e) => setF({ ...f, max_value: e.target.value })} /></label></>}
                <label>Photo<select value={f.evidence} onChange={(e) => setF({ ...f, evidence: e.target.value })}><option>NONE</option><option>ON_FAIL</option><option>ALWAYS</option></select></label>
                <label style={{ flexDirection: 'row', gap: 4, alignItems: 'center' }}><input type="checkbox" style={{ minWidth: 0 }} checked={f.required} onChange={(e) => setF({ ...f, required: e.target.checked })} />required</label>
                <label style={{ flexDirection: 'row', gap: 4, alignItems: 'center' }}><input type="checkbox" style={{ minWidth: 0 }} checked={f.na_allowed} onChange={(e) => setF({ ...f, na_allowed: e.target.checked })} />n/a allowed</label>
                <button disabled={!f.code || !f.title} onClick={act(() => post(`/checklist-versions/${v.id}/checks`, { ...f, unit: f.unit || null, min_value: num(f.min_value), max_value: num(f.max_value), guidance: f.guidance || null }), 'Check added.', () => setF(BLANK))}>Add check</button>
              </div>
            )}
            {v.status === 'DRAFT' && can('checklist.approve') && <p><button onClick={act(() => post(`/checklist-versions/${v.id}/approve`), 'Approved. New inspections use this version; past inspections keep theirs.')}>Approve version</button></p>}
          </div>
        )
      })}
      {can('checklist.edit') && !hasDraft && (
        <div className="row card">
          <label>Reason for new version *<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <button className="secondary" onClick={act(() => post(`/checklists/${id}/versions`, { reason }), 'Draft created (checks copied).', () => setReason(''))}>New version</button>
        </div>
      )}
    </>
  )
}

export default function Checklists({ selected }) {
  const can = useCan()
  const [list, err, reload] = useLoad(() => get('/checklists'), [])
  const [cats] = useLoad(() => get('/categories'), [])
  const [designs] = useLoad(() => get('/designs', { limit: 500 }), [])
  const [f, setF] = useState({ scope_type: 'CATEGORY', scope_id: '', starter: true })
  const [error, setError] = useState(null)
  if (selected) return <><p><a href="#/checklists">← all checklists</a></p><Detail id={selected} /></>
  async function create(e) {
    e.preventDefault()
    try { const t = await post('/checklists', { scope_type: f.scope_type, scope_id: Number(f.scope_id), starter: f.starter }); setError(null); window.location.hash = `#/checklists/${t.id}`; reload() }
    catch (er) { setError(er) }
  }
  const options = f.scope_type === 'CATEGORY' ? (cats || []).map((c) => [c.id, c.code]) : (designs?.rows || []).map((d) => [d.id, d.code])
  return (
    <>
      <h1>Inspection checklists</h1>
      <p className="muted">One checklist per tooling category, plus optional design-specific additions. Engineering decides which checks, limits and photos are mandatory; versions are approved and past inspections keep the version they used.</p>
      <Err error={err} />
      {can('checklist.edit') && (
        <form className="card" onSubmit={create}>
          <div className="row">
            <label>For<select value={f.scope_type} onChange={(e) => setF({ ...f, scope_type: e.target.value, scope_id: '' })}><option value="CATEGORY">Tooling category</option><option value="DESIGN">Design (additions)</option></select></label>
            <label>Which<select value={f.scope_id} onChange={(e) => setF({ ...f, scope_id: e.target.value })} required><option value="">-</option>{options.map(([id, code]) => <option key={id} value={id}>{code}</option>)}</select></label>
            <label style={{ flexDirection: 'row', gap: 4, alignItems: 'center' }}><input type="checkbox" style={{ minWidth: 0 }} checked={f.starter} onChange={(e) => setF({ ...f, starter: e.target.checked })} />start from the standard checks</label>
            <button>Create checklist</button>
          </div>
          <Err error={error} />
        </form>
      )}
      <table>
        <thead><tr><th>Checklist</th><th>Applies to</th><th>Approved version</th></tr></thead>
        <tbody>{(list || []).map((t) => <tr key={t.id}><td><a href={`#/checklists/${t.id}`}>{t.name}</a></td><td>{t.scope_type}</td><td>{t.approved_version || <span className="muted">none approved: items cannot be inspected</span>}</td></tr>)}</tbody>
      </table>
    </>
  )
}
