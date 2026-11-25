import { useEffect, useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, LocationSelect, ScanInput, useCan, useLoad } from '../components.jsx'

const TYPE = { DIE: 'die', PATTERN: 'pattern', TOP_FLANGE: 'top flange', BOTTOM_FLANGE: 'bottom flange' }
const short = (b) => b.components.map((c) => `${TYPE[c.category] || c.design_code} ${c.display_id}`).join(' · ')

function buildLabel(b) {
  if (b.open_issue) return `${b.code} · ${short(b)} — in production for ${b.open_issue.setup || b.open_issue.code} (WO ${b.open_issue.wo_id})${b.open_issue.can_hand_over ? ' · can be handed over' : ' · waiting for Production to acknowledge'}`
  if (b.status !== 'VERIFIED') return `${b.code} · ${short(b)} — ${b.status.toLowerCase()}, not ready to issue`
  return `${b.code} · ${short(b)} · bin ${b.components[0]?.location || ''} — available`
}

export default function Issue() {
  const can = useCan()
  const [setupId, setSetupId] = useState('')
  const [lineSel, setLineSel] = useState({}) // setup line id -> ticked
  const [lineOps, setLineOps] = useState({}) // setup line id -> ME mapping id (operation)
  const [buildId, setBuildId] = useState('')
  const [preview, setPreview] = useState(null)
  const [oldIssue, setOldIssue] = useState(null)
  const [scanned, setScanned] = useState(null)
  const [picks, setPicks] = useState({}) // consumable line id -> {batch_id, quantity}
  const [unused, setUnused] = useState({}) // previous setup's batch id -> unused quantity (hand-over)
  const [override, setOverride] = useState('')
  const [dest, setDest] = useState('')
  const [remarks, setRemarks] = useState('')
  const [error, setError] = useState(null)
  const [done, setDone] = useState(null)

  const [list, listErr, reloadList] = useLoad(() => get('/setups', { match: 'MATCHED', state: 'NOT_ISSUED', limit: 500 }), [])
  const [setup, setupErr] = useLoad(() => (setupId ? get(`/setups/${setupId}`) : Promise.resolve(null)), [setupId])
  const [ops] = useLoad(() => (setup ? get('/mappings', { definition_id: setup.definition_id, active: true, limit: 1000 }) : Promise.resolve(null)), [setup?.id])
  const opsFor = (part) => (ops?.rows || []).filter((m) => m.selectable && m.part_id === part)

  // every line starts ticked; its operation is the only one for the part, else the one ME marked as selected
  useEffect(() => {
    if (!setup || !ops) return
    const sel = {}, chosen = {}
    setup.lines.forEach((l) => {
      sel[l.id] = true
      const o = opsFor(l.part_id)
      const pick = o.length === 1 ? o[0] : o.find((m) => m.selected)
      chosen[l.id] = pick ? String(pick.id) : ''
    })
    setLineSel(sel); setLineOps(chosen)
  }, [setup?.id, ops]) // eslint-disable-line react-hooks/exhaustive-deps

  const builds = setup?.tooling?.builds || []
  const build = builds.find((b) => String(b.id) === buildId)
  const handOver = !!build?.open_issue
  const ticked = (setup?.lines || []).filter((l) => lineSel[l.id])
  const opsOk = ticked.length > 0 && ticked.every((l) => lineOps[l.id])

  // the die set is picked for you: the first one that is free, else the first one that can be handed on
  useEffect(() => {
    if (!setup || buildId) return
    const free = builds.find((b) => !b.open_issue && b.status === 'VERIFIED')
    const hand = builds.find((b) => b.open_issue?.can_hand_over)
    const pick = free || hand
    if (pick) chooseBuild(String(pick.id))
  }, [setup?.id]) // eslint-disable-line react-hooks/exhaustive-deps

  function reset(keepSetup) {
    if (!keepSetup) { setSetupId(''); setLineSel({}); setLineOps({}) }
    setBuildId(''); setPreview(null); setOldIssue(null); setScanned(null); setPicks({}); setUnused({}); setOverride(''); setDest(''); setRemarks('')
  }

  async function chooseBuild(id, scannedItem, list_) {
    setBuildId(id); setPreview(null); setOldIssue(null); setDone(null); setUnused({}); setOverride('')
    setScanned(scannedItem || null)
    if (!id) return
    try {
      const b = (list_ || builds).find((x) => String(x.id) === String(id))
      const p = await get(`/builds/${id}/issue-preview`)
      setPreview(p)
      const init = {}
      p.consumables.forEach((c) => {
        const bt = c.batches.find((x) => x.usable >= c.quantity) || c.batches[0]
        init[c.line_id] = { batch_id: bt ? String(bt.id) : '', quantity: String(c.quantity) }
      })
      setPicks(init)
      if (b?.open_issue) setOldIssue(await get(`/issues/${b.open_issue.id}`))
      setError(null)
    } catch (e) { setError(e); setPreview(null) }
  }

  async function onScan(code) {
    try {
      const r = await get('/scan', { code })
      let id = null, item = null
      if (r.type === 'BUILD') id = r.build.id
      else if (r.type === 'ITEM') {
        if (r.items.length > 1) throw new Error('Several items match that code; scan the QR label or use a unique display ID')
        item = r.items[0]
        if (!item.build) throw new Error(`${item.display_id} is not fitted in a build`)
        id = item.build.id
      } else throw new Error(`Nothing found for "${code}"`)
      if (!builds.some((b) => b.id === id)) throw new Error('That build is not for the assembly this setup needs. Pick one from the list.')
      await chooseBuild(String(id), item ? item.id : null)
    } catch (e) { setError(e) }
  }

  const needsOverride = preview?.consumables.some((c) => Number(picks[c.line_id]?.quantity) !== c.quantity)
  const picksOk = !preview || preview.consumables.every((c) => picks[c.line_id]?.batch_id && Number(picks[c.line_id]?.quantity) > 0)
  const oldBatches = (oldIssue?.consumables || []).filter((c) => c.state === 'ISSUED')
  const unusedOk = oldBatches.every((c) => unused[c.batch_id] !== undefined && unused[c.batch_id] !== '')
  const lineMaps = () => Object.fromEntries(ticked.map((l) => [l.id, Number(lineOps[l.id])]))
  const consumables = () => preview.consumables.map((c) => ({ line_id: c.line_id, batch_id: Number(picks[c.line_id].batch_id), quantity: Number(picks[c.line_id].quantity) }))
  const ready = setupId && opsOk && build && preview && picksOk && (!needsOverride || override) && can('issue.create')
    && (handOver ? build.open_issue.can_hand_over && unusedOk : preview.can_issue)
  const coating = preview?.consumables.map((c) => {
    const b = c.batches.find((x) => String(x.id) === picks[c.line_id]?.batch_id)
    return `${picks[c.line_id]?.quantity || c.quantity} ${c.uom} of ${c.design_code}${b ? ` (${b.code})` : ''}`
  }).join(', ')

  async function submit() {
    try {
      let d
      if (handOver) {
        const r = await post(`/issues/${build.open_issue.id}/hand-over`, {
          setup_id: Number(setupId), line_ids: ticked.map((l) => l.id), line_mappings: lineMaps(), consumables: consumables(),
          consumable_returns: oldBatches.map((c) => ({ batch_id: c.batch_id, unused_quantity: Number(unused[c.batch_id]) })),
          override_reason: needsOverride ? override : undefined, remarks: remarks || undefined })
        d = { ...r.to, from: r.from }
      } else {
        d = await post('/issues', {
          setup_id: Number(setupId), line_ids: ticked.map((l) => l.id), line_mappings: lineMaps(), build_id: preview.build.id,
          confirmed_item_ids: preview.components.map((c) => c.item_id), scanned_item_ids: scanned ? [scanned] : undefined,
          destination_location_id: dest || undefined, remarks: remarks || undefined, consumables: consumables(),
          override_reason: needsOverride ? override : undefined })
      }
      setDone(d); reset(false); reloadList(); setError(null)
    } catch (e) { setError(e) }
  }

  return (
    <>
      <h1>Issue to production</h1>
      {!can('issue.create') && <div className="warn">Only Stores issues tooling. You can look, but the Issue button is disabled.</div>}
      <Err error={error || listErr || setupErr} />
      {done && (
        <div className="msg ok">
          {done.from ? <>Handed on from {done.from.code} to {done.code}: </> : <>Issued {done.code}: </>}
          WO {done.wo_id}, setup {done.setup?.code}. {done.lines.length} items are now <b>pending hand-over</b>
          {done.consumables.length > 0 && <> and {done.consumables.map((c) => `${c.quantity} ${c.uom} of ${c.code}`).join(', ')} was deducted from stock</>}.
          Production must acknowledge receipt. <a href={`#/issues/${done.id}`}>Open the issue</a>
        </div>
      )}

      <div className="card">
        <b>1. Which setup?</b>
        <div className="row">
          <label>Setup
            <select value={setupId} onChange={(e) => { reset(true); setSetupId(e.target.value) }} style={{ minWidth: 460 }}>
              <option value="">- choose a setup -</option>
              {(list?.rows || []).map((s) => <option key={s.id} value={s.id}>{s.code}{s.name ? ` · ${s.name}` : ''} · {s.wo_count} WO · {s.part_count} part{s.part_count === 1 ? '' : 's'} · {s.wo_id}{s.tool_description ? ` · ${s.tool_description}` : ''}</option>)}
            </select>
          </label>
        </div>
        {list && list.rows.length === 0 && <p className="muted">No planned setup is waiting. Planning adds setups on the <a href="#/setups">Setups</a> page.</p>}
        {setup && <p className="muted" style={{ margin: 0 }}>{setup.wo_count} WO ({setup.wo_ids.join(', ')}) · {setup.part_count} part{setup.part_count === 1 ? '' : 's'} · tooling {setup.assembly_code}</p>}
      </div>

      {setup && builds.length === 0 && <div className="msg err">No assembly of this type is built yet. Build and verify one on the <a href="#/builds">Builds</a> page.</div>}

      {setup && builds.length > 0 && (
        <div className="card">
          <b>2. {handOver ? 'Hand over this die set' : 'Issue this die set'}</b>
          {builds.length > 1 && (
            <div className="row">
              <label>Die set (change if needed)
                <select value={buildId} onChange={(e) => chooseBuild(e.target.value)} style={{ minWidth: 520 }}>
                  <option value="">- choose -</option>
                  {builds.map((b) => <option key={b.id} value={b.id} disabled={!b.open_issue && b.status !== 'VERIFIED'}>{buildLabel(b)}</option>)}
                </select>
              </label>
            </div>
          )}
          {!build && <p className="muted">No die set is free. Choose one above{builds.some((b) => b.open_issue) ? ' (one that is in production can be handed on once Production has acknowledged it)' : ''}.</p>}

          {build && preview && (
            <>
              <p style={{ margin: '8px 0 4px' }}><b>{preview.build.code}</b> <Chip v={handOver ? 'IN_PRODUCTION' : preview.build.status} /> <small className="muted">({preview.build.assembly} rev {preview.build.revision})</small></p>
              {handOver && <div className="msg ok">This set is in production for <b>{build.open_issue.setup || build.open_issue.code}</b> (WO {build.open_issue.wo_id}). It moves straight to this setup, with no return to stores; Production confirms receipt again.</div>}
              {!handOver && preview.problems.length > 0 && <div className="msg err"><b>This build cannot be issued:</b><ul>{preview.problems.map((p) => <li key={p}>{p}</li>)}</ul></div>}
              {handOver && !build.open_issue.can_hand_over && <div className="msg err">Production has not acknowledged {build.open_issue.code} yet, so it cannot be handed on.</div>}
              <table>
                <thead><tr><th>Part</th><th>ID</th><th>Description</th><th>Bin</th></tr></thead>
                <tbody>{build.components.map((c) => <tr key={c.item_id}><td>{TYPE[c.category] || c.category}</td><td className="mono"><a href={`#/item/${c.item_id}`}>{c.display_id}</a></td><td>{c.design_code}</td><td>{c.location}</td></tr>)}</tbody>
              </table>
              <p>For <b>{ticked.length} WO / part line{ticked.length === 1 ? '' : 's'}</b> ({ticked.map((l) => `${l.wo_id} ${l.part_id}`).join(', ') || 'none ticked'}){coating ? <> · coating <b>{coating}</b></> : null}</p>

              {handOver && oldBatches.length > 0 && (
                <div className="card" style={{ background: '#fff9ec' }}>
                  <b>Coating left from the previous setup</b> <small className="muted">(the verified unused quantity, 0 if none)</small>
                  {oldBatches.map((c) => (
                    <div className="row" key={c.batch_id} style={{ alignItems: 'center' }}>
                      <span>{c.code} · {c.design_code} · {c.quantity} {c.uom} issued</span>
                      <input type="number" min="0" max={c.quantity} step="any" placeholder="unused" style={{ width: 100, minWidth: 0 }} value={unused[c.batch_id] ?? ''} onChange={(e) => setUnused((p) => ({ ...p, [c.batch_id]: e.target.value }))} /> {c.uom}
                    </div>))}
                </div>
              )}

              <details style={{ margin: '8px 0' }}>
                <summary className="muted" style={{ cursor: 'pointer' }}>Change lines, operations, coating or scan a different set</summary>
                <table>
                  <thead><tr><th></th><th>WO</th><th>Part ID</th><th>Tool description</th><th>FG qty</th><th>Operation *</th></tr></thead>
                  <tbody>{setup.lines.map((l) => (
                    <tr key={l.id}>
                      <td><input type="checkbox" style={{ minWidth: 0 }} checked={!!lineSel[l.id]} onChange={(e) => setLineSel((p) => ({ ...p, [l.id]: e.target.checked }))} /></td>
                      <td>{l.wo_id}</td><td>{l.part_id}</td><td>{l.tool_description}</td><td>{l.fg_qty ?? ''}</td>
                      <td>{opsFor(l.part_id).length === 0 ? <span style={{ color: 'var(--bad)' }}>no ME mapping for this part</span> : (
                        <select value={lineOps[l.id] || ''} onChange={(e) => setLineOps((p) => ({ ...p, [l.id]: e.target.value }))} disabled={!lineSel[l.id]}>
                          <option value="">- operation -</option>
                          {opsFor(l.part_id).map((m) => <option key={m.id} value={m.id}>routing {m.routing_revision} · {m.operation_id}{m.selected ? ' (ME selected)' : ''}</option>)}
                        </select>)}</td>
                    </tr>))}</tbody>
                </table>
                {preview.consumables.length > 0 && (
                  <table>
                    <thead><tr><th>Coating</th><th>Required</th><th>Basis</th><th>Batch</th><th>Quantity</th></tr></thead>
                    <tbody>{preview.consumables.map((c) => (
                      <tr key={c.line_id}>
                        <td>{c.design_code}</td><td>{c.quantity} {c.uom}</td><td>{c.basis || <span className="muted">authorised requirement</span>}</td>
                        <td>{c.batches.length === 0 ? <span style={{ color: 'var(--bad)' }}>no usable stock</span> : (
                          <select value={picks[c.line_id]?.batch_id || ''} onChange={(e) => setPicks((p) => ({ ...p, [c.line_id]: { ...p[c.line_id], batch_id: e.target.value } }))}>
                            <option value="">- batch -</option>
                            {c.batches.map((b) => <option key={b.id} value={b.id}>{b.code} · {b.usable} {b.uom}{b.expiry_date ? ` · exp ${b.expiry_date}` : ''}</option>)}
                          </select>)}</td>
                        <td><input type="number" min="0" step="any" style={{ width: 90, minWidth: 0 }} value={picks[c.line_id]?.quantity || ''} onChange={(e) => setPicks((p) => ({ ...p, [c.line_id]: { ...p[c.line_id], quantity: e.target.value } }))} /> {c.uom}</td>
                      </tr>))}</tbody>
                  </table>)}
                {needsOverride && <label>Documented reason for issuing a different coating quantity (authorised entry) *<input style={{ width: '100%' }} value={override} onChange={(e) => setOverride(e.target.value)} /></label>}
                <div className="row">
                  {!handOver && <label>Destination (optional)<LocationSelect value={dest} onChange={setDest} placeholder="keep current location" /></label>}
                  <label>Remarks<input value={remarks} onChange={(e) => setRemarks(e.target.value)} /></label>
                </div>
                <ScanInput onCode={onScan} label="Scan the build label / any part QR" buttonText="Select" />
              </details>

              <button style={{ fontSize: 16, padding: '10px 22px' }} disabled={!ready} onClick={submit}>{handOver ? 'Hand over to' : 'Issue to'} {setup.code}</button>
              {!ready && build && preview && (
                <small className="muted" style={{ marginLeft: 10 }}>
                  {!opsOk ? 'Tick at least one line and choose its operation (open "Change…").' : !picksOk ? 'Choose a coating batch (open "Change…").' : needsOverride && !override ? 'Give the reason for the different coating quantity.'
                    : handOver && !build.open_issue.can_hand_over ? 'Waiting for Production to acknowledge the first issue.' : handOver && !unusedOk ? 'Enter the unused coating above.' : !handOver && !preview.can_issue ? 'This set cannot be issued (see above).' : ''}
                </small>)}
            </>
          )}
        </div>
      )}
    </>
  )
}
