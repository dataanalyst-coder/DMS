import { useState } from 'react'
import { download, post, put, get, upload } from '../api.js'
import { Chip, Err, LabelPrint, LocationSelect, Ok, ProtectedImg, fmtTime, useCan, useLoad } from '../components.jsx'

const CONDITIONS = ['SERVICEABLE', 'AWAITING_INSPECTION', 'HOLD', 'UNDER_REPAIR', 'SCRAPPED']

function DocList({ docs, title }) {
  if (!docs?.length) return null
  return (
    <>
      <h2>{title}</h2>
      <table><tbody>
        {docs.map((d) => (
          <tr key={d.id}>
            <td><a href={`#/documents/${d.id}`}>{d.doc_key}</a></td><td>{d.title}</td>
            <td>{d.current_revision ? <>approved rev {d.current_revision}</> : <span className="muted">no approved version</span>}</td>
            <td>{d.current_version_id && <a href="#" onClick={(e) => { e.preventDefault(); download(`/document-versions/${d.current_version_id}/file`, `${d.doc_key}.${'file'}`) }}>download</a>}</td>
          </tr>
        ))}
      </tbody></table>
    </>
  )
}

export default function ItemDetail({ id }) {
  const can = useCan()
  const [it, loadErr, reload] = useLoad(() => get(`/items/${id}`), [id])
  const [qh, , reloadQ] = useLoad(() => get(`/items/${id}/quality-history`), [id])
  const [use] = useLoad(() => get(`/usage/items/${id}`), [id])
  const [dset] = useLoad(() => get(`/items/${id}/die-set`), [id])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [toLoc, setToLoc] = useState('')
  const [moveNote, setMoveNote] = useState('')
  const [toCond, setToCond] = useState('')
  const [reason, setReason] = useState('')
  const [alias, setAlias] = useState({ alias: '', reason: '' })
  const [edit, setEdit] = useState(null)

  const run = (fn, ok) => async () => {
    try { await fn(); setError(null); setMsg(ok || null); reload(); reloadQ() } catch (e) { setError(e); setMsg(null) }
  }
  if (loadErr) return <Err error={loadErr} />
  if (!it) return <p className="muted">Loading…</p>

  const current = it.state_txn_id
  async function reverse(h) {
    const why = window.prompt(`Reverse the whole event that recorded entry #${h.txn_id} (all items in it)? Enter the reason:`)
    if (why === null) return
    try { const r = await post(`/ledger/${h.txn_id}/reverse`, { reason: why }); setMsg(`Reversed ${r.reversed_txn_ids.length} entr${r.reversed_txn_ids.length === 1 ? 'y' : 'ies'}.`); setError(null); reload() }
    catch (e) { setError(e); setMsg(null) }
  }
  async function addPhoto(kind, file) {
    if (!file) return
    try { await upload(`/items/${id}/photos`, { kind, file }); setError(null); reload() } catch (e) { setError(e) }
  }

  return (
    <>
      <h1>{it.display_id} <small className="muted">{it.design_code} - {it.description}</small></h1>
      {!it.active && <div className="msg err">Retired: {it.retired_reason}</div>}
      {it.custody === 'MISSING' && <div className="msg err"><b>This item is marked missing.</b> It cannot be fitted, issued or moved, and it cannot pass a readiness check until it is found and recovered in a stock count.</div>}
      <Err error={error} /><Ok>{msg}</Ok>
      <div className="card">
        <p>
          <Chip v={it.condition} /> <Chip v={it.custody} /> at <b>{it.location}</b>
          {it.home_mismatch && <span style={{ color: 'var(--use)' }}> (home: {it.home_location})</span>}
        </p>
        {it.issue && <p>Issued under WO <b>{it.issue.wo_id}</b> (<a href={`#/issues/${it.issue.id}`}>{it.issue.code}</a>, {it.issue.state.replaceAll('_', ' ').toLowerCase()}).</p>}
        {it.build && <p>Fitted in build <a href={`#/builds/${it.build.id}`}>{it.build.code}</a> (not individually available).</p>}
        <p className="muted">
          System ID / QR <span className="mono">{it.system_id}</span> · tooling number {it.legacy_no || '-'} ·
          design <a href={`#/designs/${it.design_id}`}>{it.design_code}</a> rev {it.revision} ·
          acquired {it.acquisition_date || 'unknown'} · opening cycles {it.opening_cycles ?? 'unknown'} · opening tonnage {it.opening_tonnage ?? 'unknown'}
        </p>
        {it.provenance && <p>Provenance: {it.provenance}</p>}
        {it.remarks && <p>Remarks: {it.remarks}</p>}
        <p className="noprint">
          {can('item.label') && <LabelPrint path={`/items/${id}/label.svg`} />}{' '}
          {can('item.edit') && <button className="secondary" onClick={() => setEdit(edit ? null : { remarks: it.remarks || '', provenance: it.provenance || '', home_location_id: it.home_location_id })}>{edit ? 'Cancel edit' : 'Edit details'}</button>}
        </p>
        {edit && (
          <div className="row noprint">
            <label>Remarks<input value={edit.remarks} onChange={(e) => setEdit({ ...edit, remarks: e.target.value })} /></label>
            <label>Provenance<input value={edit.provenance} onChange={(e) => setEdit({ ...edit, provenance: e.target.value })} /></label>
            <label>Home location<LocationSelect value={edit.home_location_id} onChange={(v) => setEdit({ ...edit, home_location_id: v })} /></label>
            <button onClick={run(async () => { await put(`/items/${id}`, { ...edit, home_location_id: edit.home_location_id || undefined }); setEdit(null) }, 'Saved.')}>Save</button>
          </div>
        )}
      </div>

      {it.active && (can('item.move') || can('condition.hold') || can('item.retire')) && (
        <div className="card noprint">
          <b>Actions</b>
          {can('item.move') && (
            <div className="row" style={{ marginTop: 8 }}>
              <label>Move to<LocationSelect value={toLoc} onChange={setToLoc} /></label>
              <label>Note<input value={moveNote} onChange={(e) => setMoveNote(e.target.value)} /></label>
              <button disabled={!toLoc} onClick={run(() => post('/items/move', { item_ids: [id], to_location_id: toLoc, remarks: moveNote }), 'Moved. Condition is unchanged.')}>Move</button>
            </div>
          )}
          <div className="row" style={{ marginTop: 8 }}>
            <label>Change condition to<select value={toCond} onChange={(e) => setToCond(e.target.value)}>
              <option value="">- condition -</option>{CONDITIONS.filter((c) => c !== it.condition).map((c) => <option key={c}>{c}</option>)}
            </select></label>
            <label>Reason / findings *<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
            <button disabled={!toCond} onClick={run(() => post('/items/condition', { item_ids: [id], to_condition: toCond, reason }), 'Condition changed.')}>Apply</button>
            {can('item.retire') && <button className="danger" onClick={run(() => post(`/items/${id}/retire`, { reason }), 'Retired.')}>Retire item</button>}
          </div>
          <p className="muted">The server decides which transitions your role may make (for example only Engineering releases a hold; only the scrap approver scraps).</p>
        </div>
      )}

      <h2>Photos</h2>
      <div className="thumbs">
        {it.photos.map((p) => <figure key={p.id} style={{ margin: 0 }}><ProtectedImg path={`/photos/${p.id}`} alt={p.file_name} /><figcaption className="muted">{p.kind === 'STORAGE' ? 'correct storage' : 'item'}</figcaption></figure>)}
        {it.photos.length === 0 && <span className="muted">No photos yet.</span>}
      </div>
      {can('item.photo') && (
        <p className="noprint">
          <label>Add item photo (png/jpg)<input type="file" accept=".png,.jpg,.jpeg" onChange={(e) => { addPhoto('ITEM', e.target.files[0]); e.target.value = '' }} /></label>{' '}
          <label>Add correct-storage photo<input type="file" accept=".png,.jpg,.jpeg" onChange={(e) => { addPhoto('STORAGE', e.target.files[0]); e.target.value = '' }} /></label>
        </p>
      )}

      <h2>Aliases</h2>
      <table><tbody>
        {it.aliases.map((a) => (
          <tr key={a.id}><td className="mono">{a.alias}</td><td><Chip v={a.status} /></td><td>{a.reason}</td>
            <td>{a.status === 'PENDING' && can('alias.approve') && <>
              <button className="secondary" onClick={run(() => post(`/aliases/${a.id}/approve`, {}), 'Alias approved.')}>Approve</button>{' '}
              <button className="danger" onClick={run(() => post(`/aliases/${a.id}/reject`, { note: window.prompt('Reason for rejecting?') }), 'Alias rejected.')}>Reject</button></>}</td></tr>
        ))}
        {it.aliases.length === 0 && <tr><td className="muted">No aliases.</td></tr>}
      </tbody></table>
      {can('alias.request') && (
        <div className="row noprint" style={{ marginTop: 8 }}>
          <label>New alias<input value={alias.alias} onChange={(e) => setAlias({ ...alias, alias: e.target.value })} /></label>
          <label>Why<input value={alias.reason} onChange={(e) => setAlias({ ...alias, reason: e.target.value })} /></label>
          <button className="secondary" onClick={run(async () => { await post(`/items/${id}/aliases`, alias); setAlias({ alias: '', reason: '' }) }, 'Alias requested; Engineering must approve it.')}>Request alias</button>
        </div>
      )}

      <DocList docs={it.documents} title="Documents for this item" />
      <DocList docs={it.design_documents} title="Documents for the design" />

      <h2>Quality history</h2>
      {it.condition === 'AWAITING_INSPECTION' && can('inspection.perform') && !qh?.inspections.some((i) => i.status === 'OPEN') && (
        <p className="noprint"><button onClick={async () => { try { const i = await post('/inspections', { item_id: id }); window.location.hash = `#/inspections/${i.id}` } catch (e) { setError(e) } }}>Start inspection</button></p>
      )}
      {it.condition === 'AWAITING_INSPECTION' && <p className="muted">Waiting for inspection. A passing inspection does not release the item: Engineering approves the technical release afterwards (use "Change condition to Serviceable").</p>}
      {qh && (
        <table>
          <thead><tr><th>Kind</th><th>When</th><th>Detail</th></tr></thead>
          <tbody>
            {qh.inspections.map((i) => <tr key={`i${i.id}`}><td>Inspection</td><td>{fmtTime(i.completed_at || i.started_at)}</td><td><a href={`#/inspections/${i.id}`}>#{i.id}</a> {i.status} {i.outcome && <Chip v={i.outcome === 'PASS' ? 'PASS' : 'HOLD'} />} by {i.started_by}</td></tr>)}
            {qh.dispositions.map((d) => <tr key={`d${d.id}`}><td>Decision</td><td>{fmtTime(d.at)}</td><td><b>{d.decision}</b>: {d.findings} (responsible: {d.responsible}, by {d.by})</td></tr>)}
            {qh.repairs.map((r) => <tr key={`r${r.id}`}><td>Repair</td><td>{fmtTime(r.completed_at || r.opened_at)}</td><td>{r.status}{r.work_done ? `: ${r.work_done}` : ''}{r.measurements ? ` (${r.measurements})` : ''}</td></tr>)}
            {qh.revision_changes.map((c) => <tr key={`c${c.id}`}><td>Revision change</td><td>{fmtTime(c.at)}</td><td>{c.old} → {c.new}: {c.reason} (approved by {c.approved_by})</td></tr>)}
            {qh.scrap_proposals.map((p) => <tr key={`s${p.id}`}><td>Scrap proposal</td><td></td><td><Chip v={p.status} /> {p.findings}{p.note ? ` — ${p.note}` : ''}</td></tr>)}
            {!qh.inspections.length && !qh.dispositions.length && !qh.repairs.length && !qh.revision_changes.length && !qh.scrap_proposals.length && <tr><td colSpan="3" className="muted">No inspections, decisions or repairs recorded.</td></tr>}
          </tbody>
        </table>
      )}

      {dset && (
        <>
          <h2>Die mapping</h2>
          <table><thead><tr><th></th><th>ID</th><th>Description</th><th>Bin / location</th></tr></thead><tbody>
            {[['die', 'Die'], ['pattern', 'Pattern'], ['top_flange', 'Top flange'], ['bottom_flange', 'Bottom flange']].map(([k, label]) => dset[k] && (
              <tr key={k} style={dset.slot === k ? { background: '#eef3ff' } : undefined}><td>{label}</td><td className="mono"><a href={`#/item/${dset[k].id}`}>{dset[k].display_id}</a></td><td>{dset[k].description}</td><td>{dset[k].location}</td></tr>))}
          </tbody></table>
        </>
      )}

      <h2>Usage</h2>
      {use && (
        <>
          <p>Recorded since registration: <b>{use.recorded_cycles}</b> cycles, <b>{use.recorded_tonnage}</b> t{use.events_with_unknown_tonnage > 0 ? <span className="muted"> ({use.events_with_unknown_tonnage} event(s) with unknown weight not counted)</span> : null}.
            {' '}Opening usage: {use.opening_cycles ?? <span className="muted">unknown</span>} cycles / {use.opening_tonnage ?? <span className="muted">unknown</span>} t.
            {' '}Lifetime: {use.total_cycles ?? <span className="muted">unknown</span>} cycles / {use.total_tonnage ?? <span className="muted">unknown</span>} t.</p>
          <table>
            <thead><tr><th>When</th><th>WO</th><th>Build</th><th>Cycles</th><th>Tonnes</th></tr></thead>
            <tbody>
              {use.events.map((e) => <tr key={e.event_id}><td>{fmtTime(e.at)}</td><td>{e.wo_id}</td><td>{e.build}</td><td>{e.cycles}</td><td>{e.tonnage ?? <span className="muted">unknown</span>}</td></tr>)}
              {use.events.length === 0 && <tr><td colSpan="5" className="muted">No production usage recorded for this item.</td></tr>}
            </tbody>
          </table>
        </>
      )}

      <h2>Ledger history</h2>
      <table>
        <thead><tr><th>#</th><th>Time</th><th>Action</th><th>Condition</th><th>Custody</th><th>Location</th><th>By</th><th>Note</th><th></th></tr></thead>
        <tbody>
          {it.history.map((h) => (
            <tr key={h.txn_id}>
              <td>{h.txn_id}</td><td>{fmtTime(h.txn_time)}</td>
              <td>{h.action}{h.ref_txn_id ? ` of #${h.ref_txn_id}` : ''}</td>
              <td>{h.from_condition && h.from_condition !== h.to_condition ? `${h.from_condition} → ` : ''}{h.to_condition}</td>
              <td>{h.to_custody}</td><td>{h.to_location}</td><td>{h.user}</td><td>{h.remarks}</td>
              <td>{h.txn_id === current && h.action !== 'RECEIVE' && <button className="secondary noprint" onClick={() => reverse(h)}>Reverse event</button>}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}
