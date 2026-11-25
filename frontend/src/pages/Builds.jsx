import { useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, LabelPrint, LocationSelect, Ok, fmtTime, useCan, useLoad } from '../components.jsx'

function Detail({ id }) {
  const can = useCan()
  const [b, loadErr, reload] = useLoad(() => get(`/builds/${id}`), [id])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [code, setCode] = useState('')
  const [choices, setChoices] = useState([])
  const [dest, setDest] = useState('')
  const [reason, setReason] = useState('')
  const [moveTo, setMoveTo] = useState('')
  const [replacement, setReplacement] = useState({})
  const [dismantle, setDismantle] = useState(null)
  if (loadErr) return <Err error={loadErr} />
  if (!b) return <p className="muted">Loading…</p>
  const act = (fn, ok, after) => async () => { try { await fn(); setError(null); setMsg(ok || null); after && after(); reload() } catch (e) { setError(e); setMsg(null) } }
  const open = b.status !== 'DISMANTLED'

  async function scan() {
    try {
      const hits = await get('/lookup', { code })
      if (hits.length === 0) throw new Error(`Nothing found for "${code}"`)
      if (hits.length > 1) { setChoices(hits); return }
      await post(`/builds/${id}/fit`, { item_id: hits[0].id })
      setCode(''); setChoices([]); setError(null); setMsg(`Fitted ${hits[0].display_id}.`); reload()
    } catch (e) { setError(e); setMsg(null) }
  }
  const fitChosen = (it) => act(() => post(`/builds/${id}/fit`, { item_id: it.id }), `Fitted ${it.display_id}.`, () => { setChoices([]); setCode('') })

  return (
    <>
      <h1>{b.code} <small className="muted"><a href={`#/assemblies/${b.assembly.id}`}>{b.assembly.code}</a> rev {b.revision.revision} <Chip v={b.revision.status} /></small></h1>
      <Err error={error} /><Ok>{msg}</Ok>
      <div className="card">
        <p><Chip v={b.status} /> {b.ready_to_issue && <b style={{ color: 'var(--ok)' }}>Ready to issue</b>} · at <b>{b.location}</b> · assembled by {b.assembler} {b.verified_by ? `· verified by ${b.verified_by} ${fmtTime(b.verified_at)}` : '· not verified'}</p>
        {can('item.label') && <p className="noprint"><LabelPrint path={`/builds/${b.id}/label.svg`}>Print build label</LabelPrint></p>}
        {b.status === 'DISMANTLED' && <p className="muted">Closed {fmtTime(b.closed_at)}: {b.close_reason}</p>}
        {b.problems.length > 0 && <div className="warn"><b>Not ready:</b><ul className="indent">{b.problems.map((p) => <li key={p}>{p}</li>)}</ul></div>}
        <table>
          <thead><tr><th>Required element</th><th>Needed</th><th>Fitted</th></tr></thead>
          <tbody>{b.lines.map((l) => <tr key={l.line_id}><td>{l.design_code}</td><td>{l.required}</td><td style={{ color: l.filled === l.required ? 'var(--ok)' : 'var(--bad)' }}>{l.filled}</td></tr>)}</tbody>
        </table>
      </div>

      {open && can('build.assemble') && (
        <div className="card noprint">
          <b>Scan or type an item to fit</b>
          <div className="row">
            <label>QR / tooling number / display ID<input autoFocus value={code} onChange={(e) => setCode(e.target.value)} onKeyDown={(e) => e.key === 'Enter' && code && scan()} /></label>
            <button disabled={!code} onClick={scan}>Fit</button>
          </div>
          {choices.length > 0 && <div className="warn">Several items match; choose one:<ul>{choices.map((c) => <li key={c.id}><button className="secondary" onClick={fitChosen(c)}>{c.display_id}</button> {c.system_id} · {c.condition} · {c.location}</li>)}</ul></div>}
          <p className="muted">Wrong, held, already fitted or already issued items are rejected with the reason.</p>
        </div>
      )}

      <h2>Components</h2>
      <table>
        <thead><tr><th>Item</th><th>Design</th><th>Condition</th><th>Custody</th><th>Location</th><th>Fitted by</th><th></th></tr></thead>
        <tbody>
          {b.components.map((c) => (
            <tr key={c.component_id}>
              <td className="mono"><a href={`#/item/${c.item_id}`}>{c.display_id}</a></td><td>{c.design_code} <small className="muted">rev {c.design_revision}</small></td>
              <td><Chip v={c.condition} /></td><td><Chip v={c.custody} /></td><td>{c.location}</td><td>{c.fitted_by} {fmtTime(c.fitted_at)}</td>
              <td className="noprint">{open && can('build.assemble') && <>
                <button className="secondary" disabled={!dest || !reason} title="needs a destination and a reason below" onClick={act(() => post(`/builds/${id}/remove`, { item_id: c.item_id, to_location_id: dest, reason }), `${c.display_id} removed.`)}>Remove</button>{' '}
                <input placeholder="replace with (QR/number)" style={{ minWidth: 120, width: 140 }} value={replacement[c.item_id] || ''} onChange={(e) => setReplacement({ ...replacement, [c.item_id]: e.target.value })} />{' '}
                <button className="secondary" disabled={!dest || !reason || !replacement[c.item_id]} onClick={act(async () => {
                  const hits = await get('/lookup', { code: replacement[c.item_id] })
                  if (hits.length !== 1) throw new Error(hits.length ? 'Several items match that code; use a unique display ID' : 'No item found')
                  await post(`/builds/${id}/replace`, { remove_item_id: c.item_id, new_item_id: hits[0].id, to_location_id: dest, reason })
                }, 'Component replaced; verify the build again.')}>Replace</button></>}</td>
            </tr>
          ))}
          {b.components.length === 0 && <tr><td colSpan="7" className="muted">Nothing fitted yet.</td></tr>}
        </tbody>
      </table>

      {open && (
        <div className="card noprint" style={{ marginTop: 14 }}>
          {can('build.assemble') && (
            <div className="row">
              <label>Removed/outgoing items go to<LocationSelect value={dest} onChange={setDest} /></label>
              <label>Reason (remove / replace / dismantle)<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
            </div>
          )}
          <div className="row" style={{ marginTop: 8 }}>
            {can('build.verify') && b.status === 'ASSEMBLING' && <button onClick={act(() => post(`/builds/${id}/verify`), 'Build verified against the approved definition.')}>Verify build</button>}
            {can('build.move') && b.components.length > 0 && <>
              <label>Move whole build to<LocationSelect value={moveTo} onChange={setMoveTo} /></label>
              <button className="secondary" disabled={!moveTo} onClick={act(() => post(`/builds/${id}/move`, { to_location_id: moveTo }), 'Build moved with all its components.')}>Move build</button></>}
            {can('build.dismantle') && <button className="danger" onClick={() => setDismantle(dismantle ? null : Object.fromEntries(b.components.map((c) => [c.item_id, ''])))}>{dismantle ? 'Cancel dismantle' : 'Dismantle…'}</button>}
          </div>
          {dismantle && (
            <div style={{ marginTop: 10 }}>
              <b>Scan each component to its actual destination, then confirm.</b>
              {b.components.map((c) => <div className="row" key={c.item_id}><span style={{ width: 120 }}>{c.display_id}</span><LocationSelect value={dismantle[c.item_id]} onChange={(v) => setDismantle({ ...dismantle, [c.item_id]: v })} /></div>)}
              <button className="danger" disabled={!reason || Object.values(dismantle).some((v) => !v)} onClick={act(() => post(`/builds/${id}/dismantle`, { reason, destinations: Object.entries(dismantle).map(([item_id, location_id]) => ({ item_id: Number(item_id), location_id })) }), 'Build dismantled and closed.', () => setDismantle(null))}>Confirm dismantle</button>
            </div>
          )}
        </div>
      )}

      <h2>Membership history</h2>
      <table>
        <thead><tr><th>Item</th><th>Fitted</th><th>Removed</th><th>Reason</th></tr></thead>
        <tbody>{b.history.map((h) => <tr key={h.component_id}><td className="mono">{h.display_id}</td><td>{h.fitted_by} {fmtTime(h.fitted_at)}</td><td>{h.removed_at ? `${h.removed_by} ${fmtTime(h.removed_at)}` : <span className="muted">fitted</span>}</td><td>{h.removal_reason}</td></tr>)}</tbody>
      </table>
    </>
  )
}

export default function Builds({ selected }) {
  const can = useCan()
  const [status, setStatus] = useState('')
  const [list, err, reload] = useLoad(() => get('/builds', { status, limit: 200 }), [status])
  const [asms] = useLoad(() => get('/assemblies', { limit: 500 }), [])
  const [f, setF] = useState({ definition_id: '', location_id: '' })
  const [error, setError] = useState(null)
  if (selected) return <><p><a href="#/builds">← all builds</a></p><Detail id={selected} /></>
  async function create(e) {
    e.preventDefault()
    try { const b = await post('/builds', { definition_id: Number(f.definition_id), location_id: Number(f.location_id) }); setError(null); window.location.hash = `#/builds/${b.id}`; reload() }
    catch (er) { setError(er) }
  }
  return (
    <>
      <h1>Builds</h1>
      <p className="muted">A build is a physical assembly: the exact items fitted against an approved definition.</p>
      <Err error={err} />
      {can('build.assemble') && (
        <form className="card" onSubmit={create}>
          <b>Start a build</b>
          <div className="row">
            <label>Assembly (approved only) *<select value={f.definition_id} onChange={(e) => setF({ ...f, definition_id: e.target.value })} required>
              <option value="">-</option>{(asms?.rows || []).filter((a) => a.approved_revision && a.active).map((a) => <option key={a.id} value={a.id}>{a.code} rev {a.approved_revision}</option>)}
            </select></label>
            <label>Build location *<LocationSelect value={f.location_id} onChange={(v) => setF({ ...f, location_id: v })} required /></label>
            <button>Start build</button>
          </div>
          <Err error={error} />
        </form>
      )}
      <label>Status<select value={status} onChange={(e) => setStatus(e.target.value)}><option value="">All</option><option>ASSEMBLING</option><option>VERIFIED</option><option>DISMANTLED</option></select></label>
      <table style={{ marginTop: 10 }}>
        <thead><tr><th>Build</th><th>Assembly</th><th>Rev</th><th>Status</th><th>Components</th><th>Location</th></tr></thead>
        <tbody>{(list?.rows || []).map((b) => (
          <tr key={b.id}><td><a href={`#/builds/${b.id}`}>{b.code}</a></td><td>{b.assembly_code}</td><td>{b.revision}</td><td><Chip v={b.status} /></td><td>{b.components}</td><td>{b.location}</td></tr>
        ))}</tbody>
      </table>
    </>
  )
}
