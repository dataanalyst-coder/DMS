import { useState } from 'react'
import { download, get, post, upload } from '../api.js'
import { Err, Ok, useCan, useLoad } from '../components.jsx'

function Review() {
  const can = useCan()
  const [rows, err, reload] = useLoad(() => get('/import/review'), [])
  const [ids, setIds] = useState({})
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const act = (fn, ok) => async () => { try { await fn(); setError(null); setMsg(ok); reload() } catch (e) { setError(e); setMsg(null) } }
  return (
    <>
      <h2>Duplicate review queue</h2>
      <p className="muted">Rows whose legacy number already exists are never overwritten. Give each a unique display ID (it becomes a new, separate item), or reject it.</p>
      <Err error={err || error} /><Ok>{msg}</Ok>
      <table>
        <thead><tr><th>Row</th><th>Legacy no.</th><th>Design</th><th>Home</th><th>Why held</th><th></th></tr></thead>
        <tbody>
          {(rows || []).map((r) => (
            <tr key={r.id}>
              <td>{r.row_number}<br /><small className="muted">{r.source_file}</small></td>
              <td className="mono">{r.data.legacy_no}</td><td>{r.data.design_code}</td><td>{r.data.home_location}</td><td>{r.reason}</td>
              <td>{can('review.resolve') && <>
                <input placeholder="unique display ID" value={ids[r.id] || ''} onChange={(e) => setIds({ ...ids, [r.id]: e.target.value })} />{' '}
                <button disabled={!ids[r.id]} onClick={act(() => post(`/import/review/${r.id}/resolve`, { display_id: ids[r.id] }), 'Item created.')}>Create item</button>{' '}
                <button className="danger" onClick={act(() => post(`/import/review/${r.id}/reject`, { note: window.prompt('Reason for rejecting this row?') }), 'Row rejected.')}>Reject</button></>}</td>
            </tr>
          ))}
          {rows?.length === 0 && <tr><td colSpan="6" className="muted">Nothing waiting for review.</td></tr>}
        </tbody>
      </table>
    </>
  )
}

export default function ImportPage() {
  const can = useCan()
  const [file, setFile] = useState(null)
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function send(commit) {
    setBusy(true); setError(null)
    try { setRes(await upload('/import/items', { file }, { commit })) } catch (e) { setRes(null); setError(e) } finally { setBusy(false) }
  }
  return (
    <>
      <h1>Import items</h1>
      <div className="card">
        <div className="row">
          <label>Excel file (.xlsx)<input key={file ? 'f' : 'n'} type="file" accept=".xlsx" onChange={(e) => { setFile(e.target.files[0] || null); setRes(null) }} /></label>
          <button disabled={!file || busy || !can('item.import')} onClick={() => send(false)}>Preview</button>
          <button disabled={!res?.valid || res.committed || busy} onClick={() => window.confirm(`Import ${res.counts.OK} item(s) and hold ${res.counts.DUPLICATE} duplicate row(s) for review?`) && send(true)}>Commit import</button>
          <button className="secondary" onClick={() => download('/templates/items', 'items_template.xlsx').catch(setError)}>Download template</button>
        </div>
        <p className="muted">Nothing is saved until you commit. Any error rejects the whole file. Blank opening usage is stored as unknown, never zero.{!can('item.import') && ' Your role cannot import (Stores only).'}</p>
      </div>
      <Err error={error} />
      {res && (
        <div className="card">
          {res.committed && <Ok>Imported {res.created} item(s); {res.queued_for_review} row(s) held for review.</Ok>}
          {!res.committed && res.valid && <Ok>{res.counts.OK} row(s) will be created; {res.counts.DUPLICATE} duplicate legacy number(s) will be held for review. Press Commit import to save.</Ok>}
          {res.duplicates?.length > 0 && <div className="warn"><b>Duplicates (held for review, not overwritten)</b><ul>{res.duplicates.map((d) => <li key={d.row}>Row {d.row}: {d.message}</li>)}</ul></div>}
          {res.errors.length > 0 && (
            <>
              <div className="msg err"><b>{res.errors.length} problem(s); nothing was imported.</b> Fix the file and preview again.</div>
              <table><thead><tr><th>Excel row</th><th>Problem</th></tr></thead>
                <tbody>{res.errors.map((e, i) => <tr key={i}><td>{e.row}</td><td>{e.message}</td></tr>)}</tbody></table>
            </>
          )}
        </div>
      )}
      <Review />
    </>
  )
}
