import { useEffect, useState } from 'react'
import { get } from '../api.js'
import { Err, ItemStatusTable } from '../components.jsx'

export default function AssemblyView({ id }) {
  const [v, setV] = useState(null)
  const [error, setError] = useState(null)
  useEffect(() => { get(`/assemblies/${encodeURIComponent(id)}`).then(setV).catch(setError) }, [id])

  if (error) return <Err error={error} />
  if (!v) return <p className="muted">Loading…</p>
  const a = v.assembly
  return (
    <>
      <h1>Assembly {v.assembly_id}</h1>
      <p className="muted">
        {a.description || ''} {a.od ? `· OD ${a.od}` : ''} {a.length ? `· Length ${a.length}` : ''}
        {a.pouring_wt ? ` · Pouring wt ${a.pouring_wt}` : ''}
      </p>
      <ItemStatusTable items={v.items} />
      <p>
        <button disabled={!v.ready_to_issue} onClick={() => { window.location.hash = `#/issue?assembly=${encodeURIComponent(v.assembly_id)}` }}>
          Issue
        </button>{' '}
        {!v.ready_to_issue && <span className="muted">Enabled only when every item is Available.</span>}
      </p>
      <p className="muted">{v.items.length} item(s) mapped{v.items.length < 3 ? ' (no flanges mapped)' : ''}.</p>
    </>
  )
}
