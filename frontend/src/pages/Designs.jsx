import { useState } from 'react'
import { get, post, put } from '../api.js'
import { Chip, Err, Ok, useCan, useLoad } from '../components.jsx'

function DesignDetail({ id }) {
  const can = useCan()
  const [d, loadErr, reload] = useLoad(() => get(`/designs/${id}`), [id])
  const [docs, , reloadDocs] = useLoad(() => get('/documents', { target_type: 'DESIGN', target_id: id }), [id])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [reason, setReason] = useState('')
  const [edit, setEdit] = useState({})
  if (loadErr) return <Err error={loadErr} />
  if (!d) return <p className="muted">Loading…</p>
  const draft = d.revisions.find((r) => r.status === 'DRAFT')
  const act = (fn, ok) => async () => {
    try { await fn(); setError(null); setMsg(ok || null); reload(); reloadDocs() } catch (e) { setError(e); setMsg(null) }
  }

  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>{d.code} <small className="muted">{d.description} · {d.category.code} · {d.kind} · {d.base_uom} · {d.item_count} item(s)</small></h2>
      <Err error={error} /><Ok>{msg}</Ok>
      <table>
        <thead><tr><th>Rev</th><th>Status</th><th>Specifications</th><th>Storage instructions</th><th>Reason</th><th></th></tr></thead>
        <tbody>
          {d.revisions.map((r) => (
            <tr key={r.id}>
              <td>{r.revision}</td><td><Chip v={r.status} /></td>
              <td>{r.status === 'DRAFT' && can('design.edit')
                ? <input value={edit[r.id]?.specifications ?? r.specifications ?? ''} onChange={(e) => setEdit({ ...edit, [r.id]: { ...edit[r.id], specifications: e.target.value } })} />
                : r.specifications}</td>
              <td>{r.status === 'DRAFT' && can('design.edit')
                ? <input value={edit[r.id]?.storage_instructions ?? r.storage_instructions ?? ''} onChange={(e) => setEdit({ ...edit, [r.id]: { ...edit[r.id], storage_instructions: e.target.value } })} />
                : r.storage_instructions}</td>
              <td>{r.change_reason}</td>
              <td>
                {r.status === 'DRAFT' && can('design.edit') && edit[r.id] && <button className="secondary" onClick={act(() => put(`/design-revisions/${r.id}`, edit[r.id]), 'Draft saved.')}>Save draft</button>}{' '}
                {r.status === 'DRAFT' && can('design.approve') && <button onClick={act(() => post(`/design-revisions/${r.id}/approve`), 'Approved. Earlier approved revision is now obsolete for new work.')}>Approve</button>}
                {r.status === 'APPROVED' && can('design.approve') && <button className="danger" onClick={act(() => post(`/design-revisions/${r.id}/obsolete`, { reason: window.prompt('Reason for making this revision obsolete?') }), 'Marked obsolete.')}>Obsolete</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {can('design.edit') && !draft && (
        <div className="row" style={{ marginTop: 8 }}>
          <label>Reason for new revision *<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <button className="secondary" onClick={act(async () => { await post(`/designs/${id}/revisions`, { reason }); setReason('') }, 'Draft revision created.')}>New revision</button>
        </div>
      )}
      <h2>Documents</h2>
      {docs?.rows.length ? <ul>{docs.rows.map((x) => <li key={x.id}><a href={`#/documents/${x.id}`}>{x.doc_key}</a> {x.title} · {x.current_revision ? `approved rev ${x.current_revision}` : 'no approved version'}</li>)}</ul> : <p className="muted">No documents linked. Link them from the Documents page.</p>}
    </div>
  )
}

export default function Designs({ selected }) {
  const can = useCan()
  const [q, setQ] = useState('')
  const [list, err, reload] = useLoad(() => get('/designs', { q, limit: 200 }), [q])
  const [cats] = useLoad(() => get('/categories'), [])
  const [uoms] = useLoad(() => get('/uoms'), [])
  const [f, setF] = useState({ code: '', description: '', category_id: '', base_uom: 'EA', kind: 'REUSABLE', specifications: '', storage_instructions: '' })
  const [error, setError] = useState(null)
  const set = (k) => (e) => setF({ ...f, [k]: e.target.value })

  async function create(e) {
    e.preventDefault()
    try {
      const d = await post('/designs', { ...f, category_id: Number(f.category_id) })
      setError(null); window.location.hash = `#/designs/${d.id}`; reload()
    } catch (er) { setError(er) }
  }

  return (
    <>
      <h1>Element designs</h1>
      <Err error={err} />
      {selected ? <p><a href="#/designs">← all designs</a></p> : null}
      {selected && <DesignDetail id={selected} />}
      {!selected && (
        <>
          {can('design.edit') && (
            <form className="card" onSubmit={create}>
              <b>New design</b> <small className="muted">(starts as a draft revision A; Engineering approves it before items can be registered)</small>
              <div className="row">
                <label>Code *<input value={f.code} onChange={set('code')} required /></label>
                <label>Description *<input value={f.description} onChange={set('description')} required /></label>
                <label>Category *<select value={f.category_id} onChange={set('category_id')} required><option value="">-</option>{(cats || []).filter((c) => c.active).map((c) => <option key={c.id} value={c.id}>{c.code}</option>)}</select></label>
                <label>Kind<select value={f.kind} onChange={set('kind')}><option>REUSABLE</option><option>CONSUMABLE</option></select></label>
                <label>UOM<select value={f.base_uom} onChange={set('base_uom')}>{(uoms || []).map((u) => <option key={u.code}>{u.code}</option>)}</select></label>
                <label>Specifications<input value={f.specifications} onChange={set('specifications')} /></label>
                <label>Storage instructions<input value={f.storage_instructions} onChange={set('storage_instructions')} /></label>
                <button>Create</button>
              </div>
              <Err error={error} />
            </form>
          )}
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter designs…" />
          <table style={{ marginTop: 10 }}>
            <thead><tr><th>Code</th><th>Description</th><th>Kind</th><th>UOM</th><th>Approved rev</th><th>Active</th></tr></thead>
            <tbody>
              {(list?.rows || []).map((d) => (
                <tr key={d.id}><td><a href={`#/designs/${d.id}`}>{d.code}</a></td><td>{d.description}</td><td>{d.kind}</td><td>{d.base_uom}</td>
                  <td>{d.approved_revision || <span className="muted">none</span>}</td><td>{d.active ? 'Yes' : 'No'}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  )
}
