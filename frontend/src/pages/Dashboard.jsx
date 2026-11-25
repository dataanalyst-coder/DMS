import { useEffect, useState } from 'react'
import { get } from '../api.js'
import { Chip, Err, itemLink, useDebounced, useLoad } from '../components.jsx'

export function ItemTable({ rows }) {
  return (
    <table>
      <thead><tr><th>Item</th><th>QR / System ID</th><th>Design</th><th>Condition</th><th>Custody</th><th>Location</th><th>Home</th></tr></thead>
      <tbody>
        {rows.map((r) => (
          <tr key={r.id}>
            <td className="mono">{itemLink(r)}{r.legacy_no && r.legacy_no.toUpperCase() !== r.display_id ? <small className="muted"> ({r.legacy_no})</small> : null}{!r.active && <small className="muted"> retired</small>}</td>
            <td className="mono">{r.system_id}</td>
            <td>{r.design_code} <small className="muted">rev {r.revision}</small></td>
            <td><Chip v={r.condition} /></td><td><Chip v={r.custody} /></td>
            <td>{r.location}</td>
            <td>{r.home_mismatch ? <span title="Not in its home location" style={{ color: 'var(--use)' }}>{r.home_location} (away)</span> : r.home_location}</td>
          </tr>
        ))}
        {rows.length === 0 && <tr><td colSpan="7" className="muted">No matching items.</td></tr>}
      </tbody>
    </table>
  )
}

export default function Dashboard() {
  const [q, setQ] = useState('')
  const dq = useDebounced(q)
  const [counts, cErr] = useLoad(() => get('/dashboard'), [])
  const [res, setRes] = useState(null)
  const [error, setError] = useState(null)

  useEffect(() => {
    if (!dq.trim()) { setRes(null); return }
    get('/items', { q: dq, limit: 25, active: '' }).then((r) => { setRes(r); setError(null) }).catch(setError)
  }, [dq])

  // Scanner input (or Enter after typing a QR / legacy number) jumps straight to a unique match.
  async function scan(e) {
    if (e.key !== 'Enter' || !q.trim()) return
    try {
      const hits = await get('/lookup', { code: q })
      if (hits.length === 1) window.location.hash = `#/item/${hits[0].id}`
    } catch (err) { setError(err) }
  }

  return (
    <>
      <h1>Search / Dashboard</h1>
      <Err error={error || cErr} />
      {counts && (
        <div className="counts">
          {['SERVICEABLE', 'AWAITING_INSPECTION', 'HOLD', 'UNDER_REPAIR', 'SCRAPPED'].map((c) => (
            <a key={c} className="count" href={`#/inventory?condition=${c}`} style={{ textDecoration: 'none', color: 'inherit' }}>
              <Chip v={c} /><b>{counts.condition[c]}</b>
            </a>
          ))}
        </div>
      )}
      {counts && (
        <div className="counts">
          <a className="count" href="#/issues" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.outstanding_returns}</b>Outstanding returns</a>
          <a className="count" href="#/issues" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.pending_handovers}</b>Pending hand-overs</a>
          <a className="count" href="#/inventory?condition=HOLD" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.holds}</b>Items on hold</a>
          <a className="count" href="#/readiness" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.wos_needing_recheck}</b>WOs to recheck</a>
        </div>
      )}
      {counts && (
        <div className="counts">
          <a className="count" href="#/inspections" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.condition.AWAITING_INSPECTION}</b>Awaiting inspection</a>
          <a className="count" href="#/held" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.open_repairs}</b>Repairs in progress</a>
          <a className="count" href="#/held" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.pending_scrap_proposals}</b>Scrap awaiting approval</a>
          <a className="count" href="#/inspections" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.open_inspections}</b>Inspections open</a>
        </div>
      )}
      {counts && (
        <div className="counts">
          <a className="count" href="#/inventory?custody=MISSING" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.custody?.MISSING ?? 0}</b>Missing items</a>
          <a className="count" href="#/usage" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.integration_exceptions}</b>OMS exceptions</a>
          <a className="count" href="#/counts" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.open_counts}</b>Counts in progress</a>
          <a className="count" href="#/setups" style={{ textDecoration: 'none', color: 'inherit' }}><b>{counts.setups_unmatched}</b>Setups without tooling</a>
        </div>
      )}
      {counts?.pending_import_review > 0 && (
        <div className="warn">{counts.pending_import_review} imported row(s) wait in the <a href="#/import">duplicate review queue</a>.</div>
      )}
      <input className="big" autoFocus value={q} onChange={(e) => setQ(e.target.value)} onKeyDown={scan}
        placeholder="Scan a QR, or type a legacy number, display ID, design, alias…" />
      {res && (
        <>
          <p className="muted">{res.total} match(es){res.total > res.rows.length ? ' (first 25 shown; use Inventory for all)' : ''}</p>
          <ItemTable rows={res.rows} />
        </>
      )}
      {!res && <p className="muted">Counts are of active items. Click a condition to list those items.</p>}
    </>
  )
}
