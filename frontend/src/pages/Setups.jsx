import { useState } from 'react'
import { download, get, post, upload } from '../api.js'
import { Chip, Err, Ok, Pager, fmtTime, useCan, useLoad } from '../components.jsx'

const MATCH_HELP = {
  MATCHED: 'tooling found',
  UNMATCHED: 'no tooling assigned yet',
  AMBIGUOUS: 'rules disagree',
}

const count = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`

function Tooling({ t }) {
  return (
    <>
      <h2>Tooling for {t.assembly.code} <small className="muted">{t.assembly.description}</small></h2>
      <h3>Built assemblies</h3>
      <table>
        <thead><tr><th>Build</th><th>Status</th><th>Component</th><th>Type</th><th>Design</th><th>Bin / location</th></tr></thead>
        <tbody>
          {t.builds.flatMap((b) => b.components.map((c, i) => (
            <tr key={`${b.id}-${c.item_id}`}>
              <td>{i === 0 ? <a href={`#/builds/${b.id}`}>{b.code}</a> : ''}</td><td>{i === 0 ? <Chip v={b.status} /> : ''}</td>
              <td className="mono"><a href={`#/item/${c.item_id}`}>{c.display_id}</a></td><td>{c.category}</td><td>{c.design_code}</td><td>{c.location}</td>
            </tr>)))}
          {t.builds.length === 0 && <tr><td colSpan="6" className="muted">No assembly of this type is built yet. The loose parts below can be assembled.</td></tr>}
        </tbody>
      </table>
      <h3>Serviceable parts available, by position</h3>
      <table>
        <thead><tr><th>Position</th><th>Type</th><th>Needed</th><th>Available (die / pattern no. and bin)</th></tr></thead>
        <tbody>
          {t.lines.map((l) => (
            <tr key={l.line_id}>
              <td>{l.designs.join(' / ')}</td><td>{l.category}</td><td>{l.required}</td>
              <td>{l.candidates.length === 0 ? <span style={{ color: 'var(--bad)' }}>none available</span> :
                l.candidates.map((c) => <div key={c.item_id}><a className="mono" href={`#/item/${c.item_id}`}>{c.display_id}</a> <small className="muted">{c.design_code}</small> · {c.location} <Chip v={c.custody} /></div>)}</td>
            </tr>))}
        </tbody>
      </table>
    </>
  )
}

function AddLine({ setupId, onDone, setError }) {
  const empty = { wo_id: '', part_id: '', tool_description: '', family: '', grade: '', fg_qty: '' }
  const [f, setF] = useState(empty)
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }))
  async function add(e) {
    e.preventDefault()
    const body = Object.fromEntries(Object.entries(f).filter(([, v]) => v !== ''))
    if (body.fg_qty !== undefined) body.fg_qty = Number(body.fg_qty)
    try { await post(`/setups/${setupId}/lines`, body); setF(empty); setError(null); onDone() } catch (er) { setError(er) }
  }
  return (
    <form className="row noprint" onSubmit={add}>
      <label>WO NO *<input value={f.wo_id} onChange={set('wo_id')} required /></label>
      <label>Part ID *<input value={f.part_id} onChange={set('part_id')} required /></label>
      <label>Tool description<input value={f.tool_description} onChange={set('tool_description')} /></label>
      <label>Family<input value={f.family} onChange={set('family')} /></label>
      <label>Grade<input value={f.grade} onChange={set('grade')} style={{ width: 80 }} /></label>
      <label>FG qty<input type="number" min="0" value={f.fg_qty} onChange={set('fg_qty')} style={{ width: 80 }} /></label>
      <button className="secondary">Add WO / part</button>
    </form>
  )
}

function Detail({ id }) {
  const can = useCan()
  const [s, err, reload] = useLoad(() => get(`/setups/${id}`), [id])
  const [reason, setReason] = useState('')
  const [error, setError] = useState(null)
  if (err) return <Err error={err} />
  if (!s) return <p className="muted">Loading…</p>
  return (
    <>
      <h1>{s.code}{s.name ? ` · ${s.name}` : ''} <small className="muted">{count(s.wo_count, 'WO')} · {count(s.part_count, 'part')}</small> <Chip v={s.state} /></h1>
      <Err error={error} />
      <p className="muted">{s.plan_ref ? `Plan ${s.plan_ref} · ` : ''}entered by {s.created_by} ({s.source === 'EXCEL' ? 'Excel' : 'typed'}) {fmtTime(s.created_at)}
        {s.issues.length > 0 && <> · issues: {s.issues.map((i) => <span key={i.id}><a href={`#/issues/${i.id}`}>{i.code}</a>{i.open ? ' (out)' : ''} </span>)}</>}
        {s.cancelled && ` · cancelled: ${s.cancel_reason}`}</p>
      <h2>WO and part lines</h2>
      <table>
        <thead><tr><th>WO</th><th>Part ID (internal ID)</th><th>Tool description</th><th>Family</th><th>Grade</th><th>FG qty</th><th>Rcd qty</th><th>Weight</th><th>Tooling</th></tr></thead>
        <tbody>
          {s.lines.map((l) => (
            <tr key={l.id}><td>{l.wo_id}</td><td>{l.part_id}</td><td>{l.tool_description}</td><td>{l.family}</td><td>{l.grade}</td><td>{l.fg_qty}</td><td>{l.rcd_qty}</td><td>{l.weight}</td>
              <td><Chip v={l.match} /> {l.assembly_code}{l.match_reason && <small className="muted"> {l.match_reason}</small>}</td></tr>))}
        </tbody>
      </table>
      {can('setup.plan') && !s.cancelled && <AddLine setupId={id} onDone={reload} setError={setError} />}
      <p>Setup tooling: <Chip v={s.match} /> {s.assembly_code ? <b>{s.assembly_code}</b> : null} {s.match_reason && <span className="muted"> {s.match_reason}</span>}</p>
      {s.tooling && <Tooling t={s.tooling} />}
      {can('setup.plan') && !s.cancelled && (
        <div className="row noprint" style={{ marginTop: 12 }}>
          <label>Reason to cancel this setup *<input value={reason} onChange={(e) => setReason(e.target.value)} /></label>
          <button className="danger" disabled={!reason.trim()} onClick={async () => { try { await post(`/setups/${id}/cancel`, { reason }); setError(null); reload() } catch (e) { setError(e) } }}>Cancel setup</button>
        </div>
      )}
    </>
  )
}

function Importer({ onDone, setMsg, setError }) {
  const [file, setFile] = useState(null)
  const [pre, setPre] = useState(null)
  async function run(commit) {
    try {
      const r = await upload('/setups/import', { file }, commit ? { commit: true } : undefined)
      setPre(r); setError(null)
      if (r.committed) { setMsg(`${count(r.created, 'WO / part line')} added in ${count(r.setups, 'setup')} from sheet "${r.sheet}"; ${r.counts.DUPLICATE} already planned and skipped.`); onDone() }
    } catch (e) { setError(e); setMsg(null) }
  }
  return (
    <div className="card noprint">
      <b>Import Planning's sheet (Excel)</b>
      <div className="row">
        <label>Sheet (.xlsx)<input type="file" accept=".xlsx" onChange={(e) => { setFile(e.target.files[0]); setPre(null) }} /></label>
        <button className="secondary" disabled={!file} onClick={() => run(false)}>Preview</button>
        <button disabled={!file || !pre?.valid || pre.counts.OK === 0 || pre.committed} onClick={() => run(true)}>Add {pre?.counts.OK ?? 0} line(s)</button>
        <button className="secondary" onClick={() => download('/templates/setups', 'setups_template.xlsx').catch(setError)}>Download template</button>
      </div>
      <small className="muted">Needs WO NO and PART ID. Rows with the same <b>SETUP</b> belong to one setup; a row with no SETUP is a setup of its own. Tool description, family and internal grade are used to find the tooling. Other columns are ignored.</small>
      {pre && (
        <>
          <p>Sheet "{pre.sheet}": {pre.counts.OK} new line(s) in {count(pre.setups, 'setup')} · {pre.counts.DUPLICATE} already planned (skipped) · {pre.counts.ERROR} with errors{pre.counts.ERROR > 0 ? ' (fix these first: nothing is added while any row has an error)' : ''}</p>
          {pre.rows.filter((r) => r.status !== 'OK').length > 0 && (
            <table><thead><tr><th>Row</th><th>Result</th><th>Why</th></tr></thead>
              <tbody>{pre.rows.filter((r) => r.status !== 'OK').map((r) => <tr key={r.row}><td>{r.row}</td><td><Chip v={r.status === 'ERROR' ? 'REJECTED' : 'PENDING'} /> {r.status}</td><td>{r.message}</td></tr>)}</tbody></table>)}
        </>
      )}
    </div>
  )
}

const BLANK_LINE = { wo_id: '', part_id: '', tool_description: '', family: '', grade: '', fg_qty: '' }

function Manual({ onDone, setMsg, setError }) {
  const [name, setName] = useState('')
  const [lines, setLines] = useState([{ ...BLANK_LINE }])
  const setL = (i, k) => (e) => setLines((ls) => ls.map((l, n) => (n === i ? { ...l, [k]: e.target.value } : l)))
  async function add(e) {
    e.preventDefault()
    const body = {
      name: name || undefined,
      lines: lines.map((l) => {
        const o = Object.fromEntries(Object.entries(l).filter(([, v]) => v !== ''))
        if (o.fg_qty !== undefined) o.fg_qty = Number(o.fg_qty)
        return o
      }),
    }
    try {
      const s = await post('/setups', body)
      setMsg(`${s.code} added with ${count(s.wo_count, 'WO')} and ${count(s.part_count, 'part')} (${s.match === 'MATCHED' ? `tooling ${s.assembly_code}` : 'no tooling assigned yet'}).`)
      setError(null); setName(''); setLines([{ ...BLANK_LINE }]); onDone()
    } catch (er) { setError(er); setMsg(null) }
  }
  return (
    <form className="card noprint" onSubmit={add}>
      <b>Add a setup by hand</b> <small className="muted">(one setup can run several WOs and parts)</small>
      <div className="row"><label>Setup name / number<input value={name} onChange={(e) => setName(e.target.value)} placeholder="optional" /></label></div>
      {lines.map((l, i) => (
        <div className="row" key={i}>
          <label>WO NO *<input value={l.wo_id} onChange={setL(i, 'wo_id')} required /></label>
          <label>Part ID *<input value={l.part_id} onChange={setL(i, 'part_id')} required /></label>
          <label>Tool description<input value={l.tool_description} onChange={setL(i, 'tool_description')} /></label>
          <label>Family<input value={l.family} onChange={setL(i, 'family')} /></label>
          <label>Grade<input value={l.grade} onChange={setL(i, 'grade')} style={{ width: 80 }} /></label>
          <label>FG qty<input type="number" min="0" value={l.fg_qty} onChange={setL(i, 'fg_qty')} style={{ width: 80 }} /></label>
          {lines.length > 1 && <button type="button" className="secondary" onClick={() => setLines((ls) => ls.filter((_, n) => n !== i))}>Remove</button>}
        </div>
      ))}
      <div className="row">
        <button type="button" className="secondary" onClick={() => setLines((ls) => [...ls, { ...BLANK_LINE }])}>+ Another WO / part</button>
        <button>Add setup</button>
      </div>
    </form>
  )
}

function Rules({ onChange, setMsg, setError }) {
  const can = useCan()
  const [rules, , reload] = useLoad(() => get('/setup-maps'), [])
  const [asm] = useLoad(() => get('/assemblies', { limit: 500 }), [])
  const [f, setF] = useState({ definition_id: '', part_id: '', description: '', family: '' })
  const set = (k) => (e) => setF((p) => ({ ...p, [k]: e.target.value }))
  const run = (fn, ok) => async () => { try { await fn(); setError(null); setMsg(ok); reload(); onChange() } catch (e) { setError(e); setMsg(null) } }
  return (
    <>
      <h2>Tool mapping rules <small className="muted">(Engineering: which assembly serves a part / description / family; the most specific rule wins)</small></h2>
      <table>
        <thead><tr><th>Part ID</th><th>Tool description</th><th>Family</th><th>Assembly</th><th>Note</th>{can('setup.map') && <th></th>}</tr></thead>
        <tbody>
          {(rules || []).map((r) => (
            <tr key={r.id}><td>{r.part_id || <span className="muted">any</span>}</td><td>{r.description || <span className="muted">any</span>}</td><td>{r.family || <span className="muted">any</span>}</td>
              <td><b>{r.assembly_code}</b> <small className="muted">{r.assembly_description}</small></td><td>{r.note}</td>
              {can('setup.map') && <td><button className="secondary" onClick={() => { const why = window.prompt('Reason for removing this rule?'); if (why) run(() => post(`/setup-maps/${r.id}/remove`, { reason: why }), 'Rule removed.')() }}>Remove</button></td>}</tr>))}
          {rules?.length === 0 && <tr><td colSpan="6" className="muted">No rules yet. Setups stay "no tooling assigned" until Engineering adds one.</td></tr>}
        </tbody>
      </table>
      {can('setup.map') && (
        <div className="row noprint" style={{ marginTop: 8 }}>
          <label>Part ID<input value={f.part_id} onChange={set('part_id')} /></label>
          <label>Tool description<input value={f.description} onChange={set('description')} /></label>
          <label>Family<input value={f.family} onChange={set('family')} /></label>
          <label>Assembly *<select value={f.definition_id} onChange={set('definition_id')}><option value="">- assembly -</option>{(asm?.rows || []).map((a) => <option key={a.id} value={a.id}>{a.code} {a.description}</option>)}</select></label>
          <button disabled={!f.definition_id || !(f.part_id || f.description || f.family)} onClick={run(async () => {
            await post('/setup-maps', { definition_id: Number(f.definition_id), part_id: f.part_id || undefined, description: f.description || undefined, family: f.family || undefined })
            setF({ definition_id: '', part_id: '', description: '', family: '' })
          }, 'Rule added. Setups it covers now show their tooling.')}>Add rule</button>
        </div>)}
    </>
  )
}

export default function Setups({ selected }) {
  const can = useCan()
  const [q, setQ] = useState('')
  const [match, setMatch] = useState('')
  const [state, setState] = useState('')
  const [offset, setOffset] = useState(0)
  const [list, err, reload] = useLoad(() => get('/setups', { q, match, state, limit: 50, offset }), [q, match, state, offset])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  if (selected) return <><p><a href="#/setups">← all setups</a></p><Detail id={selected} /></>
  return (
    <>
      <h1>Planned setups</h1>
      <p className="muted">Planning's setups. A setup is one tooling set that runs one or more WOs and parts. Each is matched to a tool assembly by Engineering's rules, and every issue to production is made for one setup.</p>
      <Err error={error || err} /><Ok>{msg}</Ok>
      {list && <div className="counts"><div className="count"><b>{list.setups_planned}</b>Planned</div><div className="count"><b>{list.setups_unmatched}</b>Without tooling</div></div>}
      {can('setup.plan') && <Importer onDone={reload} setMsg={setMsg} setError={setError} />}
      {can('setup.plan') && <Manual onDone={reload} setMsg={setMsg} setError={setError} />}
      <div className="row" style={{ marginBottom: 8 }}>
        <label>Search<input value={q} onChange={(e) => { setOffset(0); setQ(e.target.value) }} placeholder="setup, WO, part, description, family" /></label>
        <label>Tooling<select value={match} onChange={(e) => { setOffset(0); setMatch(e.target.value) }}><option value="">any</option>{Object.keys(MATCH_HELP).map((m) => <option key={m} value={m}>{m} - {MATCH_HELP[m]}</option>)}</select></label>
        <label>State<select value={state} onChange={(e) => { setOffset(0); setState(e.target.value) }}><option value="">any</option>{['NOT_ISSUED', 'ISSUED', 'DONE', 'CANCELLED'].map((m) => <option key={m}>{m}</option>)}</select></label>
      </div>
      <table>
        <thead><tr><th>Setup</th><th>WOs</th><th>Parts</th><th>Tool description</th><th>Family</th><th>Grade</th><th>FG qty</th><th>Tooling</th><th>State</th></tr></thead>
        <tbody>
          {(list?.rows || []).map((s) => (
            <tr key={s.id}>
              <td><a href={`#/setups/${s.id}`}>{s.code}</a>{s.name && <><br /><small className="muted">{s.name}</small></>}</td>
              <td><b>{s.wo_count}</b> <small className="muted">{s.wo_ids.slice(0, 3).join(', ')}{s.wo_ids.length > 3 ? '…' : ''}</small></td>
              <td><b>{s.part_count}</b> <small className="muted">{s.part_ids.slice(0, 3).join(', ')}{s.part_ids.length > 3 ? '…' : ''}</small></td>
              <td>{s.tool_description}{s.line_count > 1 ? <small className="muted"> +{s.line_count - 1} more</small> : null}</td><td>{s.family}</td><td>{s.grade}</td><td>{s.fg_qty ?? ''}</td>
              <td><Chip v={s.match} /> {s.assembly_code}</td><td><Chip v={s.state} /></td>
            </tr>))}
          {list?.rows.length === 0 && <tr><td colSpan="9" className="muted">No setups yet.</td></tr>}
        </tbody>
      </table>
      {list && <Pager total={list.total} limit={50} offset={offset} onChange={setOffset} />}
      <Rules onChange={reload} setMsg={setMsg} setError={setError} />
    </>
  )
}
