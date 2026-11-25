import { useEffect, useState } from 'react'
import { get, post } from '../api.js'
import { Chip, Err, Ok, useCan, useLoad } from '../components.jsx'

const norm = (c) => String(c || '').toUpperCase().replace(/[^A-Z]/g, '')
const SLOTS = [['die_item_id', 'Die', 'DIE', 'die'], ['pattern_item_id', 'Pattern', 'PATTERN', 'pattern'],
  ['top_flange_item_id', 'Top flange', 'TOPFLANGE', 'top_flange'], ['bottom_flange_item_id', 'Bottom flange', 'BOTTOMFLANGE', 'bottom_flange']]

const Part = ({ p }) => p ? (
  <div><a className="mono" href={`#/item/${p.id}`}>{p.display_id}</a> <Chip v={p.condition} /><br /><small>{p.description}</small><br /><small className="muted">{p.location}</small></div>
) : <span className="muted">-</span>

function Editor({ cats, sets, initial, onDone, setMsg, setError }) {
  const [f, setF] = useState(initial)
  const [opts, setOpts] = useState({})
  useEffect(() => setF(initial), [initial])
  useEffect(() => {
    SLOTS.forEach(([, , type]) => {
      const c = cats.find((x) => norm(x.code) === type)
      if (c) get('/items', { category_id: c.id, limit: 500 }).then((r) => setOpts((o) => ({ ...o, [type]: r.rows }))).catch(setError)
    })
  }, [cats]) // eslint-disable-line react-hooks/exhaustive-deps
  const usedBy = {}
  sets.forEach((s) => ['die', 'pattern', 'top_flange', 'bottom_flange'].forEach((k) => { if (s[k] && s.die.id !== Number(f.die_item_id)) usedBy[s[k].id] = s.die.display_id }))
  async function save() {
    try {
      const body = { note: f.note || undefined }
      SLOTS.forEach(([k]) => { if (f[k]) body[k] = Number(f[k]) })
      await post('/die-sets', body)
      setMsg('Die mapping saved.'); setError(null); setF(initial); onDone()
    } catch (e) { setError(e); setMsg(null) }
  }
  return (
    <div className="card noprint">
      <b>Map a die to its pattern and flanges</b> <small className="muted">(choosing a die that is already mapped replaces its set)</small>
      <div className="row">
        {SLOTS.map(([k, label, type]) => (
          <label key={k}>{label}{k === 'die_item_id' ? ' *' : ''}
            <select value={f[k] || ''} onChange={(e) => setF((p) => ({ ...p, [k]: e.target.value }))}>
              <option value="">{k === 'die_item_id' ? '- die -' : '- none -'}</option>
              {(opts[type] || []).map((i) => <option key={i.id} value={i.id} disabled={type !== 'DIE' && !!usedBy[i.id]}>{i.display_id} - {i.description}{type !== 'DIE' && usedBy[i.id] ? ` (with ${usedBy[i.id]})` : ''}</option>)}
            </select>
          </label>))}
        <label>Note<input value={f.note || ''} onChange={(e) => setF((p) => ({ ...p, note: e.target.value }))} /></label>
        <button disabled={!f.die_item_id} onClick={save}>Save mapping</button>
      </div>
    </div>
  )
}

export default function DieSets() {
  const can = useCan()
  const [q, setQ] = useState('')
  const [res, err, reload] = useLoad(() => get('/die-sets', { q, limit: 500 }), [q])
  const [all] = useLoad(() => get('/die-sets', { limit: 500 }), [res])
  const [cats] = useLoad(() => get('/categories'), [])
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const [edit, setEdit] = useState({})
  const rows = res?.rows || []
  const editRow = (s) => { setEdit({ die_item_id: s.die.id, pattern_item_id: s.pattern?.id, top_flange_item_id: s.top_flange?.id, bottom_flange_item_id: s.bottom_flange?.id, note: s.note }); window.scrollTo(0, 0) }
  async function remove(s) {
    const why = window.prompt(`Reason for removing the mapping of die ${s.die.display_id}?`)
    if (!why) return
    try { await post(`/die-sets/${s.die.id}/remove`, { reason: why }); setMsg('Mapping removed; its parts are free to map again.'); setError(null); reload() } catch (e) { setError(e); setMsg(null) }
  }
  return (
    <>
      <h1>Die mapping</h1>
      <p className="muted">Each physical die and the pattern, top flange and bottom flange that belong to it, with their IDs, descriptions and bins. A part belongs to only one die. This is a reference for finding the right parts; issuing still goes through builds and setups.</p>
      <Err error={error || err} /><Ok>{msg}</Ok>
      {can('dieset.manage') && cats && <Editor cats={cats} sets={all?.rows || []} initial={edit} onDone={reload} setMsg={setMsg} setError={setError} />}
      <div className="row"><label>Search<input value={q} onChange={(e) => setQ(e.target.value)} placeholder="ID or description of any part" /></label></div>
      <table>
        <thead><tr><th>Die</th><th>Pattern</th><th>Top flange</th><th>Bottom flange</th><th>Note</th>{can('dieset.manage') && <th></th>}</tr></thead>
        <tbody>
          {rows.map((s) => (
            <tr key={s.id}><td><Part p={s.die} /></td><td><Part p={s.pattern} /></td><td><Part p={s.top_flange} /></td><td><Part p={s.bottom_flange} /></td><td>{s.note}</td>
              {can('dieset.manage') && <td><button className="secondary" onClick={() => editRow(s)}>Edit</button> <button className="secondary" onClick={() => remove(s)}>Remove</button></td>}</tr>))}
          {rows.length === 0 && <tr><td colSpan="6" className="muted">No die is mapped yet.</td></tr>}
        </tbody>
      </table>
    </>
  )
}
