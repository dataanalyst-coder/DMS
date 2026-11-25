import { useState } from 'react'
import { del, get, post, put } from '../api.js'
import { Chip, Err, Ok, useCan, useLoad } from '../components.jsx'

function Revision({ rev, designs, act, can }) {
  const draft = rev.status === 'DRAFT'
  const [line, setLine] = useState({ design_id: '', quantity: 1, quantity_basis: '', allowed_revisions: '' })
  const [alt, setAlt] = useState({})
  const [text, setText] = useState({ instructions: rev.instructions || '', checklist_refs: rev.checklist_refs || '' })
  const edit = draft && can('assembly.edit')
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>Revision {rev.revision} <Chip v={rev.status} /> <small className="muted">{rev.change_reason}</small></h2>
      <table>
        <thead><tr><th>Element design</th><th>Qty</th><th>Revisions accepted</th><th>Approved substitutes</th><th></th></tr></thead>
        <tbody>
          {rev.lines.map((l) => (
            <tr key={l.id}>
              <td><b>{l.design_code}</b> <small className="muted">{l.description} · {l.kind}</small></td>
              <td>{edit ? <input type="number" min="0" step="any" style={{ width: 70, minWidth: 0 }} defaultValue={l.quantity}
                onBlur={(e) => Number(e.target.value) !== l.quantity && act(() => put(`/assembly-lines/${l.id}`, { quantity: Number(e.target.value) }), 'Quantity updated.')()} /> : l.quantity}
                {l.quantity_basis && <small className="muted"> ({l.quantity_basis})</small>}</td>
              <td>{l.allowed_revisions || <span className="muted">current approved only</span>}</td>
              <td>
                {l.alternatives.map((a) => (
                  <span key={a.id} style={{ marginRight: 8 }}>{a.design_code}{a.allowed_revisions ? ` (${a.allowed_revisions})` : ''}
                    {edit && <button className="secondary" style={{ padding: '0 6px', marginLeft: 3 }} onClick={act(() => del(`/assembly-alternatives/${a.id}`), 'Substitute removed.')}>×</button>}</span>
                ))}
                {edit && (
                  <span className="noprint"><select value={alt[l.id] || ''} onChange={(e) => setAlt({ ...alt, [l.id]: e.target.value })}>
                    <option value="">+ substitute…</option>
                    {designs.filter((d) => d.id !== l.design_id && d.kind === l.kind).map((d) => <option key={d.id} value={d.id}>{d.code}</option>)}
                  </select>{' '}
                  <button className="secondary" disabled={!alt[l.id]} onClick={act(() => post(`/assembly-lines/${l.id}/alternatives`, { design_id: Number(alt[l.id]) }), 'Substitute added.')}>Add</button></span>
                )}
              </td>
              <td>{edit && <button className="danger" onClick={act(() => del(`/assembly-lines/${l.id}`), 'Line removed.')}>Remove</button>}</td>
            </tr>
          ))}
          {rev.lines.length === 0 && <tr><td colSpan="5" className="muted">No element lines yet.</td></tr>}
        </tbody>
      </table>
      {edit && (
        <div className="row" style={{ marginTop: 10 }}>
          <label>Add element design<select value={line.design_id} onChange={(e) => setLine({ ...line, design_id: e.target.value })}>
            <option value="">- design -</option>
            {designs.filter((d) => !rev.lines.some((l) => l.design_id === d.id)).map((d) => <option key={d.id} value={d.id}>{d.code} - {d.description}</option>)}
          </select></label>
          <label>Quantity<input type="number" min="0" step="any" value={line.quantity} onChange={(e) => setLine({ ...line, quantity: e.target.value })} /></label>
          <label>Quantity basis (consumables)<input value={line.quantity_basis} onChange={(e) => setLine({ ...line, quantity_basis: e.target.value })} /></label>
          <label>Accepted revisions (blank = current)<input value={line.allowed_revisions} onChange={(e) => setLine({ ...line, allowed_revisions: e.target.value })} placeholder="A,B" /></label>
          <button disabled={!line.design_id} onClick={act(() => post(`/assembly-revisions/${rev.id}/lines`, { ...line, design_id: Number(line.design_id), quantity: Number(line.quantity) }), 'Line added.', () => setLine({ design_id: '', quantity: 1, quantity_basis: '', allowed_revisions: '' }))}>Add line</button>
        </div>
      )}
      <div className="row" style={{ marginTop: 10 }}>
        <label>Assembly instructions{edit ? '' : ' (read-only)'}<textarea rows="2" disabled={!edit} value={text.instructions} onChange={(e) => setText({ ...text, instructions: e.target.value })} style={{ minWidth: 320 }} /></label>
        <label>Checklist references<input disabled={!edit} value={text.checklist_refs} onChange={(e) => setText({ ...text, checklist_refs: e.target.value })} /></label>
        {edit && <button className="secondary" onClick={act(() => put(`/assembly-revisions/${rev.id}`, text), 'Saved.')}>Save text</button>}
        {draft && can('assembly.approve') && <button onClick={act(() => post(`/assembly-revisions/${rev.id}/approve`), 'Approved. The previous approved revision is now obsolete for new builds.')}>Approve revision</button>}
        {rev.status === 'APPROVED' && can('assembly.approve') && <button className="danger" onClick={act(() => post(`/assembly-revisions/${rev.id}/obsolete`, { reason: window.prompt('Reason for making this revision obsolete?') }), 'Marked obsolete.')}>Obsolete</button>}
      </div>
    </div>
  )
}

function Detail({ id }) {
  const can = useCan()
  const [d, loadErr, reload] = useLoad(() => get(`/assemblies/${id}`), [id])
  const [designs] = useLoad(() => get('/designs', { limit: 500, active: true }), [])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [reason, setReason] = useState('')
  if (loadErr) return <Err error={loadErr} />
  if (!d) return <p className="muted">Loading…</p>
  const act = (fn, ok, after) => async () => { try { await fn(); setError(null); setMsg(ok || null); after && after(); reload() } catch (e) { setError(e); setMsg(null) } }
  const hasDraft = d.revisions.some((r) => r.status === 'DRAFT')
  return (
    <>
      <h1>{d.code} <small className="muted">{d.description} · {d.category.code}</small></h1>
      <p className="muted">An assembly definition says what an assembly consists of. Physical builds against it are on the <a href="#/builds">Builds</a> page.
        Mapped to {d.mapping_count} part operation(s) — see <a href="#/mappings">ME mapping</a>.</p>
      <Err error={error} /><Ok>{msg}</Ok>
      {[...d.revisions].reverse().map((r) => <Revision key={r.id} rev={r} designs={designs?.rows || []} act={act} can={can} />)}
      {can('assembly.edit') && !hasDraft && (
        <div className="row card">
          <label>Reason for new revision *<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <button className="secondary" onClick={act(() => post(`/assemblies/${id}/revisions`, { reason }), 'Draft created (lines copied from the latest revision).', () => setReason(''))}>New revision</button>
        </div>
      )}
    </>
  )
}

export default function Assemblies({ selected }) {
  const can = useCan()
  const [q, setQ] = useState('')
  const [list, err, reload] = useLoad(() => get('/assemblies', { q, limit: 200 }), [q])
  const [cats] = useLoad(() => get('/categories'), [])
  const [f, setF] = useState({ code: '', description: '', category_id: '' })
  const [error, setError] = useState(null)
  if (selected) return <><p><a href="#/assemblies">← all assemblies</a></p><Detail id={selected} /></>

  async function create(e) {
    e.preventDefault()
    try { const d = await post('/assemblies', { ...f, category_id: Number(f.category_id) }); setError(null); window.location.hash = `#/assemblies/${d.id}`; reload() }
    catch (er) { setError(er) }
  }
  return (
    <>
      <h1>Assembly definitions</h1>
      <Err error={err} />
      {can('assembly.edit') && (
        <form className="card" onSubmit={create}>
          <b>New assembly</b> <small className="muted">(starts as draft revision A; add element lines, then Engineering approves)</small>
          <div className="row">
            <label>Code *<input value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })} required /></label>
            <label>Description *<input value={f.description} onChange={(e) => setF({ ...f, description: e.target.value })} required /></label>
            <label>Category *<select value={f.category_id} onChange={(e) => setF({ ...f, category_id: e.target.value })} required><option value="">-</option>{(cats || []).filter((c) => c.active).map((c) => <option key={c.id} value={c.id}>{c.code}</option>)}</select></label>
            <button>Create</button>
          </div>
          <Err error={error} />
        </form>
      )}
      <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter assemblies…" />
      <table style={{ marginTop: 10 }}>
        <thead><tr><th>Code</th><th>Description</th><th>Approved revision</th><th>Active</th></tr></thead>
        <tbody>{(list?.rows || []).map((a) => (
          <tr key={a.id}><td><a href={`#/assemblies/${a.id}`}>{a.code}</a></td><td>{a.description}</td>
            <td>{a.approved_revision || <span className="muted">none</span>}</td><td>{a.active ? 'Yes' : 'No'}</td></tr>
        ))}</tbody>
      </table>
    </>
  )
}
