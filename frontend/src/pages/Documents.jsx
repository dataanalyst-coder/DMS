import { useState } from 'react'
import { del, download, get, post, upload } from '../api.js'
import { Chip, Err, Ok, fmtTime, useCan, useLoad } from '../components.jsx'

const TYPES = ['DIE_DRAWING', 'TOOL_DRAWING', 'WORK_INSTRUCTION', 'INSPECTION_REPORT', 'REPAIR_RECORD', 'OTHER']

function DocDetail({ id }) {
  const can = useCan()
  const [d, loadErr, reload] = useLoad(() => get(`/documents/${id}`), [id])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [file, setFile] = useState(null)
  const [reason, setReason] = useState('')
  const [link, setLink] = useState({ target_type: 'DESIGN', target_id: '' })
  if (loadErr) return <Err error={loadErr} />
  if (!d) return <p className="muted">Loading…</p>
  const act = (fn, ok) => async () => {
    try { await fn(); setError(null); setMsg(ok || null); reload() } catch (e) { setError(e); setMsg(null) }
  }
  return (
    <div className="card">
      <h2 style={{ marginTop: 0 }}>{d.doc_key} <small className="muted">{d.title} · {d.doc_type}</small></h2>
      <Err error={error} /><Ok>{msg}</Ok>
      <p className="muted">New work uses the current approved version{d.current_version_id ? '' : ' (none approved yet)'}. Older versions stay available for past builds and inspections. Uploading never approves anything.</p>
      <table>
        <thead><tr><th>Rev</th><th>Status</th><th>File</th><th>Uploaded</th><th>Approved</th><th>Reason</th><th></th></tr></thead>
        <tbody>
          {d.versions.map((v) => (
            <tr key={v.id}>
              <td>{v.revision}{v.id === d.current_version_id && <small className="muted"> (current)</small>}</td><td><Chip v={v.status} /></td>
              <td><a href="#" onClick={(e) => { e.preventDefault(); download(`/document-versions/${v.id}/file`, v.file_name).catch(setError) }}>{v.file_name}</a> <small className="muted">{Math.round(v.size / 1024)} KB</small></td>
              <td>{v.author} {fmtTime(v.created_at)}</td><td>{v.approved_by ? `${v.approved_by} ${fmtTime(v.approved_at)}` : ''}</td><td>{v.change_reason}</td>
              <td>
                {v.status === 'DRAFT' && can('document.approve') && <button onClick={act(() => post(`/document-versions/${v.id}/approve`), 'Approved; the previous approved version is now obsolete.')}>Approve</button>}{' '}
                {v.status !== 'OBSOLETE' && can('document.approve') && <button className="danger" onClick={act(() => post(`/document-versions/${v.id}/obsolete`, { reason: window.prompt('Reason?') }), 'Marked obsolete.')}>Obsolete</button>}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      {can('document.upload') && (
        <div className="row" style={{ marginTop: 10 }}>
          <label>Upload new version<input type="file" onChange={(e) => setFile(e.target.files[0] || null)} /></label>
          <label>Change reason{d.versions.length ? ' *' : ''}<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <button disabled={!file} onClick={act(async () => { await upload(`/documents/${id}/versions`, { file, change_reason: reason }); setFile(null); setReason('') }, 'Uploaded as a draft; Engineering must approve it.')}>Upload</button>
        </div>
      )}
      <h2>Applies to</h2>
      <ul>
        {d.links.map((l) => (
          <li key={`${l.target_type}${l.target_id}`}>
            <a href={l.target_type === 'DESIGN' ? `#/designs/${l.target_id}` : `#/item/${l.target_id}`}>{l.target_type} #{l.target_id}</a>{' '}
            {can('document.upload') && <button className="secondary" onClick={act(() => del(`/documents/${id}/links/${l.target_type}/${l.target_id}`), 'Unlinked.')}>Unlink</button>}
          </li>
        ))}
        {d.links.length === 0 && <li className="muted">Not linked to any design or item.</li>}
      </ul>
      {can('document.upload') && (
        <div className="row">
          <label>Link to<select value={link.target_type} onChange={(e) => setLink({ ...link, target_type: e.target.value })}><option value="DESIGN">Design</option><option value="ITEM">Item</option></select></label>
          <label>Internal ID (see the design/item page URL)<input type="number" value={link.target_id} onChange={(e) => setLink({ ...link, target_id: e.target.value })} /></label>
          <button className="secondary" disabled={!link.target_id} onClick={act(() => post(`/documents/${id}/links`, { ...link, target_id: Number(link.target_id) }), 'Linked.')}>Link</button>
        </div>
      )}
    </div>
  )
}

export default function Documents({ selected }) {
  const can = useCan()
  const [q, setQ] = useState('')
  const [list, err, reload] = useLoad(() => get('/documents', { q, limit: 200 }), [q])
  const [f, setF] = useState({ doc_key: '', doc_type: 'DIE_DRAWING', title: '' })
  const [error, setError] = useState(null)

  async function create(e) {
    e.preventDefault()
    try { const d = await post('/documents', f); setError(null); setF({ ...f, doc_key: '', title: '' }); window.location.hash = `#/documents/${d.id}`; reload() }
    catch (er) { setError(er) }
  }
  return (
    <>
      <h1>Documents</h1>
      <Err error={err} />
      {selected ? <><p><a href="#/documents">← all documents</a></p><DocDetail id={selected} /></> : (
        <>
          {can('document.upload') && (
            <form className="card" onSubmit={create}>
              <b>New controlled document</b>
              <div className="row">
                <label>Document no. *<input value={f.doc_key} onChange={(e) => setF({ ...f, doc_key: e.target.value })} required /></label>
                <label>Type<select value={f.doc_type} onChange={(e) => setF({ ...f, doc_type: e.target.value })}>{TYPES.map((t) => <option key={t}>{t}</option>)}</select></label>
                <label>Title *<input value={f.title} onChange={(e) => setF({ ...f, title: e.target.value })} required /></label>
                <button>Create</button>
              </div>
              <Err error={error} />
            </form>
          )}
          <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Filter documents…" />
          <table style={{ marginTop: 10 }}>
            <thead><tr><th>Document</th><th>Type</th><th>Title</th><th>Approved revision</th></tr></thead>
            <tbody>
              {(list?.rows || []).map((d) => (
                <tr key={d.id}><td><a href={`#/documents/${d.id}`}>{d.doc_key}</a></td><td>{d.doc_type}</td><td>{d.title}</td>
                  <td>{d.current_revision || <span className="muted">none approved</span>}</td></tr>
              ))}
            </tbody>
          </table>
        </>
      )}
    </>
  )
}
