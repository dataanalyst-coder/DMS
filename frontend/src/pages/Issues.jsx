import { useEffect, useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, LocationSelect, Ok, ScanInput, fmtTime, useCan, useLoad } from '../components.jsx'

function ReplaceForm({ issue, onDone, setMsg, setError }) {
  const [out, setOut] = useState('')
  const [code, setCode] = useState('')
  const [reason, setReason] = useState('')
  const lines = issue.lines.filter((l) => l.state === 'PENDING_HANDOVER' || l.state === 'IN_PRODUCTION')
  async function go() {
    try {
      const hits = await get('/lookup', { code })
      if (hits.length !== 1) throw new Error(hits.length ? 'Several items match; scan the QR label or use the display ID' : `Nothing found for ${code}`)
      const r = await post(`/issues/${issue.id}/replace-component`, { outgoing_item_id: Number(out), incoming_item_id: hits[0].id, reason })
      setMsg(`${r.outgoing} taken out, ${r.incoming} fitted. Usage before now stays with ${r.outgoing}; usage from now on goes to ${r.incoming}. The build is back to ASSEMBLING and must be verified again.`)
      setError(null); setOut(''); setCode(''); setReason(''); onDone()
    } catch (e) { setError(e); setMsg(null) }
  }
  return (
    <details className="noprint" style={{ marginTop: 12 }}>
      <summary className="muted" style={{ cursor: 'pointer' }}>Replace one part while the set is in production</summary>
      <div className="card">
        <small className="muted">(the replaced part stays outstanding on this issue until it is returned)</small>
        <div className="row">
          <label>Part to replace *<select value={out} onChange={(e) => setOut(e.target.value)}><option value="">- choose -</option>{lines.map((l) => <option key={l.item_id} value={l.item_id}>{l.display_id} ({l.design_code})</option>)}</select></label>
          <label>Replacement (scan or type) *<input value={code} onChange={(e) => setCode(e.target.value)} /></label>
          <label>Reason *<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <button disabled={!out || !code.trim() || !reason.trim()} onClick={go}>Replace</button>
        </div>
      </div>
    </details>
  )
}

// One panel to take tooling back: everything that is out is ticked, parts go back to their home bin, only the unused coating is typed.
function ReturnPanel({ d, reload, setMsg, setError }) {
  const out = d.lines.filter((l) => l.state === 'PENDING_HANDOVER' || l.state === 'IN_PRODUCTION')
  const outBatches = d.consumables.filter((b) => b.state === 'ISSUED')
  const [sel, setSel] = useState({})
  const [dmg, setDmg] = useState({})
  const [unused, setUnused] = useState({})
  const [loc, setLoc] = useState('')
  const key = out.map((l) => l.item_id).join(',')
  useEffect(() => { setSel(Object.fromEntries(out.map((l) => [l.item_id, true]))) }, [key]) // eslint-disable-line react-hooks/exhaustive-deps
  const chosen = out.filter((l) => sel[l.item_id])
  const unusedOk = outBatches.every((b) => unused[b.batch_id] !== undefined && unused[b.batch_id] !== '')
  const damageOk = chosen.every((l) => !dmg[l.item_id]?.on || (dmg[l.item_id]?.text || '').trim())
  const upd = (i, patch) => setDmg((p) => ({ ...p, [i]: { ...p[i], ...patch } }))

  async function go() {
    try {
      const lines = chosen.map((l) => ({ item_id: l.item_id, to_location_id: loc ? Number(loc) : undefined, damaged: !!dmg[l.item_id]?.on, remarks: dmg[l.item_id]?.on ? dmg[l.item_id].text : undefined }))
      const consumables = outBatches.map((b) => ({ batch_id: b.batch_id, unused_quantity: Number(unused[b.batch_id]) }))
      const r = await post(`/issues/${d.id}/return`, { lines, consumables })
      setMsg(`Returned ${r.returned} part(s)${r.consumables_settled ? ` and settled ${r.consumables_settled} coating batch(es)` : ''}. ${r.outstanding.length ? `Still out: ${r.outstanding.join(', ')}.` : 'Everything is back.'} Returned parts now wait for inspection.`)
      setError(null); setUnused({}); setDmg({}); reload()
    } catch (e) { setError(e); setMsg(null) }
  }

  return (
    <div className="card noprint" style={{ borderColor: 'var(--accent)' }}>
      <b>Return to stores</b> <small className="muted">(everything that is out is ticked; untick what is not back yet)</small>
      <table>
        <thead><tr><th></th><th>ID</th><th>Part</th><th>Goes back to</th><th></th></tr></thead>
        <tbody>{out.map((l) => (
          <tr key={l.item_id}>
            <td><input type="checkbox" style={{ minWidth: 0 }} checked={!!sel[l.item_id]} onChange={(e) => setSel((p) => ({ ...p, [l.item_id]: e.target.checked }))} /></td>
            <td className="mono"><a href={`#/item/${l.item_id}`}>{l.display_id}</a></td><td>{l.design_code}</td>
            <td>{loc ? 'the bin chosen below' : 'its home bin'}</td>
            <td>{sel[l.item_id] && (dmg[l.item_id]?.on
              ? <><label style={{ display: 'inline-flex', flexDirection: 'row', gap: 4 }}><input type="checkbox" style={{ minWidth: 0 }} checked onChange={() => upd(l.item_id, { on: false })} />damaged</label> <input placeholder="describe the damage *" value={dmg[l.item_id]?.text || ''} onChange={(e) => upd(l.item_id, { text: e.target.value })} /></>
              : <a href="#/issues" onClick={(e) => { e.preventDefault(); upd(l.item_id, { on: true }) }}>report damage</a>)}</td>
          </tr>))}</tbody>
      </table>
      {outBatches.length > 0 && (
        <div style={{ marginTop: 8 }}>
          <b>Coating left</b> <small className="muted">(the verified unused quantity, 0 if none; the rest is recorded as consumed)</small>
          {outBatches.map((b) => (
            <div className="row" key={b.batch_id} style={{ alignItems: 'center' }}>
              <span>{b.code} · {b.design_code} · {b.quantity} {b.uom} issued</span>
              <input type="number" min="0" max={b.quantity} step="any" placeholder="unused" style={{ width: 100, minWidth: 0 }} value={unused[b.batch_id] ?? ''} onChange={(e) => setUnused((p) => ({ ...p, [b.batch_id]: e.target.value }))} /> {b.uom}
            </div>))}
        </div>)}
      <details style={{ margin: '8px 0' }}>
        <summary className="muted" style={{ cursor: 'pointer' }}>Put them somewhere else than their home bin</summary>
        <label>All returned parts go to<LocationSelect value={loc} onChange={setLoc} placeholder="each part's home bin" /></label>
      </details>
      <button style={{ fontSize: 16, padding: '10px 22px' }} disabled={(!chosen.length && !outBatches.length) || !unusedOk || !damageOk} onClick={go}>
        Return {chosen.length} part{chosen.length === 1 ? '' : 's'}{outBatches.length ? ' and settle the coating' : ''}
      </button>
      {!unusedOk && <small className="muted" style={{ marginLeft: 10 }}>Enter the unused coating (0 if none).</small>}
      <p className="muted" style={{ marginBottom: 0 }}>Returned parts become <b>Awaiting inspection</b> (or Hold if damaged) and cannot be reused until cleared.</p>
    </div>
  )
}

function Detail({ id }) {
  const can = useCan()
  const [d, loadErr, reload] = useLoad(() => get(`/issues/${id}`), [id])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  if (loadErr) return <Err error={loadErr} />
  if (!d) return <p className="muted">Loading…</p>
  const out = d.lines.filter((l) => l.state === 'PENDING_HANDOVER' || l.state === 'IN_PRODUCTION')
  const outBatches = d.consumables.filter((b) => b.state === 'ISSUED')
  const awaiting = d.lines.filter((l) => l.state === 'RETURNED' && l.condition === 'AWAITING_INSPECTION')

  return (
    <>
      <h1>{d.code} <small className="muted">WO {d.wo_id}</small> <Chip v={d.status} /></h1>
      <Err error={error} /><Ok>{msg}</Ok>
      {can('return.receive') && (out.length > 0 || outBatches.length > 0) && <ReturnPanel d={d} reload={reload} setMsg={setMsg} setError={setError} />}
      <div className="card">
        {d.handed_over_to && <p className="msg ok">The tooling was handed on to the next setup: <a href={`#/issues/${d.handed_over_to.id}`}>{d.handed_over_to.code}</a> {fmtTime(d.handed_over_at)}. It did not go back to stores.</p>}
        {d.setup && <p>Setup <a href={`#/setups/${d.setup.id}`}>{d.setup.code}</a>{d.setup.name ? ` · ${d.setup.name}` : ''}{d.setup.tool_description ? ` · ${d.setup.tool_description}` : ''}{d.setup.family ? ` · ${d.setup.family}` : ''}{d.setup.grade ? ` · grade ${d.setup.grade}` : ''}</p>}
        {d.setup_lines?.length > 0 && (
          <table><thead><tr><th>WO</th><th>Part</th><th>Tool description</th><th>Routing</th><th>Operation</th></tr></thead>
            <tbody>{d.setup_lines.map((l) => <tr key={l.line_id}><td>{l.wo_id}</td><td>{l.part_id}</td><td>{l.tool_description}</td><td>{l.routing_revision}</td><td>{l.operation_id}</td></tr>)}</tbody></table>
        )}
        <p>Build <a href={`#/builds/${d.build.id}`}>{d.build.code}</a> · issued by <b>{d.issued_by}</b> {fmtTime(d.issued_at)}{d.destination ? ` to ${d.destination}` : ''}
          {d.received_by.length ? <> · received by <b>{d.received_by.join(', ')}</b></> : ' · not yet acknowledged by Production'}</p>
        {d.remarks && <p>{d.remarks}</p>}
        {d.lines.some((l) => l.state === 'PENDING_HANDOVER') && can('issue.acknowledge') && (
          <button onClick={async () => { try { await post(`/issues/${id}/acknowledge`); setMsg('Receipt acknowledged under your account.'); setError(null); reload() } catch (e) { setError(e); setMsg(null) } }}>Acknowledge receipt</button>
        )}
        {d.lines.some((l) => l.state === 'PENDING_HANDOVER') && !can('issue.acknowledge') && <p className="muted">Waiting for Production to acknowledge receipt. Until then the parts are not free stock.</p>}
        {awaiting.length > 0 && can('inspection.perform') && (
          <p><button className="secondary" onClick={async () => { try { const r = await post(`/issues/${id}/inspections`); setMsg(`Started ${r.started.length} inspection(s), one per returned item. Open them from the Inspection page.`); setError(null); reload() } catch (e) { setError(e); setMsg(null) } }}>Start inspections for the {awaiting.length} returned item(s)</button></p>
        )}
      </div>
      <table>
        <thead><tr><th>Item</th><th>Design</th><th>State on this issue</th><th>Since</th><th>By</th><th>Condition now</th><th>Custody</th><th>Location</th></tr></thead>
        <tbody>
          {d.lines.map((l) => (
            <tr key={l.item_id}>
              <td className="mono"><a href={`#/item/${l.item_id}`}>{l.display_id}</a></td><td>{l.design_code} rev {l.design_revision}</td>
              <td><Chip v={l.state} /></td><td>{fmtTime(l.since)}</td><td>{l.by}</td><td><Chip v={l.condition} /></td><td><Chip v={l.custody} /></td><td>{l.location}</td>
            </tr>
          ))}
        </tbody>
      </table>

      {d.documents?.length > 0 && (
        <>
          <h2>Documents in force when this was issued</h2>
          <table>
            <thead><tr><th>Document</th><th>Revision used</th><th>Approved now</th></tr></thead>
            <tbody>{d.documents.map((x) => (
              <tr key={x.version_id}><td>{x.doc_key} <small className="muted">{x.title}</small></td><td>{x.revision_used}</td>
                <td>{x.superseded ? <span style={{ color: 'var(--use)' }}>revision {x.current_revision} (newer; applies to new work only)</span> : 'same revision'}</td></tr>
            ))}</tbody>
          </table>
        </>
      )}

      {d.consumables.length > 0 && (
        <>
          <h2>Coating</h2>
          <table>
            <thead><tr><th>Batch</th><th>Material</th><th>Issued</th><th>Basis</th><th>State</th><th>Unused returned</th><th>Consumed</th></tr></thead>
            <tbody>{d.consumables.map((b) => (
              <tr key={b.batch_id}>
                <td><a href={`#/batches/${b.batch_id}`}>{b.code}</a></td><td>{b.design_code}</td><td>{b.quantity} {b.uom}</td>
                <td>{b.basis}{b.override_reason && <><br /><small className="muted">authorised entry: {b.override_reason}</small></>}</td>
                <td><Chip v={b.state === 'ISSUED' ? 'IN_PRODUCTION' : b.state} /></td>
                <td>{b.unused ?? ''}</td><td>{b.consumed ?? ''}</td>
              </tr>
            ))}</tbody>
          </table>
        </>
      )}

      {can('issue.create') && out.length > 0 && <ReplaceForm issue={d} onDone={reload} setMsg={setMsg} setError={setError} />}
    </>
  )
}

// Find what to return: pick it from the list, or scan / type a die set, part, WO or setup.
function QuickFind({ rows }) {
  const [error, setError] = useState(null)
  async function find(code) {
    try {
      const c = code.trim()
      let hit = null
      try {
        const r = await get('/scan', { code: c })
        if (r.type === 'BUILD') hit = rows.find((i) => i.build.id === r.build.id)
        else if (r.type === 'ITEM' && r.items.length === 1 && r.items[0].issue) hit = rows.find((i) => i.id === r.items[0].issue.id)
      } catch { /* not a scannable code: try it as a WO or setup */ }
      if (!hit) hit = rows.find((i) => i.wo_id.split(',').map((x) => x.trim().toUpperCase()).includes(c.toUpperCase()) || (i.setup && i.setup.code.toUpperCase() === c.toUpperCase()))
      if (!hit) throw new Error(`Nothing is out for "${c}". Check the die set, part, WO or setup number.`)
      window.location.hash = `#/issues/${hit.id}`
    } catch (e) { setError(e) }
  }
  return (
    <div className="card noprint">
      <b>Return tooling</b> <small className="muted">(pick what is back, or scan / type the die set, a part, the WO or the setup)</small>
      <Err error={error} />
      <div className="row">
        <label>Out in production
          <select value="" onChange={(e) => { if (e.target.value) window.location.hash = `#/issues/${e.target.value}` }} style={{ minWidth: 520 }}>
            <option value="">- choose what to return -</option>
            {rows.map((i) => <option key={i.id} value={i.id}>{i.code} · {i.setup?.code || ''} · WO {i.wo_id} · {i.outstanding.join(', ')}</option>)}
          </select>
        </label>
      </div>
      <ScanInput onCode={find} label="or scan / type" buttonText="Find" />
    </div>
  )
}

export default function Issues({ selected }) {
  const can = useCan()
  const [status, setStatus] = useState('open')
  const [wo, setWo] = useState('')
  const [list, err] = useLoad(() => get('/issues', { status: status === 'all' ? '' : status, wo_id: wo, limit: 200 }), [status, wo])
  const [open] = useLoad(() => get('/issues', { status: 'open', limit: 200 }), [])
  if (selected) return <><p><a href="#/issues">← all issues</a></p><Detail id={selected} /></>
  return (
    <>
      <h1>Returns and issues</h1>
      <Err error={err} />
      {can('return.receive') && open && <QuickFind rows={open.rows} />}
      <div className="row" style={{ marginBottom: 10 }}>
        <label>Show<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="open">Outstanding</option><option value="closed">Closed</option><option value="all">All</option></select></label>
        <label>WO<input value={wo} onChange={(e) => setWo(e.target.value)} /></label>
        <a href="#/issue"><button type="button">New issue</button></a>
      </div>
      <table>
        <thead><tr><th>Issue</th><th>Setup</th><th>WO</th><th>Part / op</th><th>Build</th><th>Issued by</th><th>When</th><th>Status</th><th>Outstanding</th></tr></thead>
        <tbody>
          {(list?.rows || []).map((i) => (
            <tr key={i.id}><td><a href={`#/issues/${i.id}`}>{i.code}</a></td><td>{i.setup ? <a href={`#/setups/${i.setup.id}`}>{i.setup.code}</a> : ''}</td><td>{i.wo_id}</td><td>{i.part_id} / {i.operation_id}</td><td><a href={`#/builds/${i.build.id}`}>{i.build.code}</a></td>
              <td>{i.issued_by}</td><td>{fmtTime(i.issued_at)}</td><td><Chip v={i.status} /></td><td>{i.outstanding.join(', ')}</td></tr>
          ))}
          {list?.rows.length === 0 && <tr><td colSpan="9" className="muted">Nothing here.</td></tr>}
        </tbody>
      </table>
    </>
  )
}
