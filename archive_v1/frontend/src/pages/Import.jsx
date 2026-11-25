import { useState } from 'react'
import { API, ApiError } from '../api.js'
import { Err } from '../components.jsx'

export default function Import() {
  const [kind, setKind] = useState('items')
  const [file, setFile] = useState(null)
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)
  const [busy, setBusy] = useState(false)

  async function send(commit) {
    setBusy(true); setError(null)
    try {
      const fd = new FormData()
      fd.append('file', file)
      const r = await fetch(`${API}/import/${kind}?commit=${commit}`, { method: 'POST', body: fd })
      const data = await r.json().catch(() => null)
      if (!r.ok) throw new ApiError(r.status, data)
      setRes(data)
    } catch (e) {
      setRes(null)
      setError(e instanceof ApiError ? e : new ApiError(0, { detail: `Cannot reach the API at ${API}.` }))
    } finally { setBusy(false) }
  }

  const reset = () => { setFile(null); setRes(null); setError(null) }

  return (
    <>
      <h1>Import</h1>
      <div className="card">
        <div className="row">
          <label>Type
            <select value={kind} onChange={(e) => { setKind(e.target.value); reset() }}>
              <option value="items">Items</option>
              <option value="assemblies">Assemblies</option>
            </select>
          </label>
          <label>Excel file (.xlsx)
            <input key={kind + (file ? 'f' : 'n')} type="file" accept=".xlsx" onChange={(e) => { setFile(e.target.files[0] || null); setRes(null); setError(null) }} />
          </label>
          <button disabled={!file || busy} onClick={() => send(false)}>Preview / validate</button>
          <button disabled={!res?.valid || res.committed || busy}
            onClick={() => window.confirm(`Import ${res.rows} ${kind}? This cannot be undone (corrections are made with new entries).`) && send(true)}>
            Commit import
          </button>
        </div>
        <p className="muted">
          Download template: <a href={`${API}/templates/items`}>items_template.xlsx</a> · <a href={`${API}/templates/assemblies`}>assemblies_template.xlsx</a>.
          {' '}Import items before assemblies. Nothing is written until you commit, and a file with any error is rejected as a whole.
        </p>
      </div>

      <Err error={error} />

      {res && (
        <div className="card">
          {res.committed && <div className="msg ok">Imported {res.created} {res.kind}. Each new item has a RECEIVE ledger entry.</div>}
          {!res.committed && res.valid && <div className="msg ok">{res.rows} row(s) valid. Review, then press Commit import.</div>}
          {!res.valid && (
            <>
              <div className="msg err"><b>{res.errors.length} problem(s) found; nothing was imported.</b> Fix the file and preview again.</div>
              <table>
                <thead><tr><th>Excel row</th><th>Column</th><th>Problem</th></tr></thead>
                <tbody>
                  {res.errors.map((e, i) => <tr key={i}><td>{e.row}</td><td className="mono">{e.field}</td><td>{e.message}</td></tr>)}
                </tbody>
              </table>
            </>
          )}
          {res.committed && <p><button className="secondary" onClick={reset}>Import another file</button></p>}
        </div>
      )}
    </>
  )
}
