import { useEffect, useState } from 'react'
import { clean, get, post, put } from '../api.js'
import { Chip, Err, asmLink, itemLink } from '../components.jsx'

const TYPES = ['DIE', 'TOP_FLANGE', 'BOTTOM_FLANGE', 'PATTERN']
const num = (v) => (v === '' || v == null ? undefined : Number(v))

function ItemsTab() {
  const [items, setItems] = useState([])
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(null) // existing item_id or null for new
  const [f, setF] = useState({ item_id: '', item_type: 'DIE', description: '', od: '', id_dim: '', length: '', home_location: '', remarks: '', active: true })

  const load = () => get('/items').then(setItems).catch(setError)
  useEffect(() => { load() }, [])
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  function edit(i) {
    setEditing(i.item_id)
    setF({ ...i, od: i.od ?? '', id_dim: i.id_dim ?? '', length: i.length ?? '', description: i.description ?? '', home_location: i.home_location ?? '', remarks: i.remarks ?? '' })
  }
  function reset() {
    setEditing(null)
    setF({ item_id: '', item_type: 'DIE', description: '', od: '', id_dim: '', length: '', home_location: '', remarks: '', active: true })
  }

  async function save(e) {
    e.preventDefault()
    try {
      const body = { description: f.description, od: num(f.od), id_dim: num(f.id_dim), length: num(f.length), home_location: f.home_location, remarks: f.remarks }
      if (editing) await put(`/items/${encodeURIComponent(editing)}`, { ...body, active: f.active })
      else await post('/items', clean({ item_id: f.item_id, item_type: f.item_type, ...body }))
      setError(null); reset(); load()
    } catch (err) { setError(err) }
  }

  return (
    <>
      <form className="card" onSubmit={save}>
        <b>{editing ? `Edit ${editing}` : 'Add item'}</b>
        <div className="row">
          {!editing && <label>Item ID *<input value={f.item_id} onChange={set('item_id')} required /></label>}
          {!editing && <label>Type *<select value={f.item_type} onChange={set('item_type')}>{TYPES.map((t) => <option key={t}>{t}</option>)}</select></label>}
          <label>Description<input value={f.description} onChange={set('description')} /></label>
          <label>OD<input type="number" step="any" value={f.od} onChange={set('od')} /></label>
          <label>ID<input type="number" step="any" value={f.id_dim} onChange={set('id_dim')} /></label>
          <label>Length<input type="number" step="any" value={f.length} onChange={set('length')} /></label>
          <label>Home location<input value={f.home_location} onChange={set('home_location')} /></label>
          <label>Remarks<input value={f.remarks} onChange={set('remarks')} /></label>
          {editing && <label>Active<input type="checkbox" checked={f.active} onChange={set('active')} style={{ minWidth: 0 }} /></label>}
          <button>{editing ? 'Save' : 'Add'}</button>
          {editing && <button type="button" className="secondary" onClick={reset}>Cancel</button>}
        </div>
        <p className="muted">Status and location are derived from the ledger and cannot be edited. A new item gets a RECEIVE entry at its home location.</p>
      </form>
      <Err error={error} />
      <table>
        <thead><tr><th>Item</th><th>Type</th><th>Description</th><th>OD</th><th>ID</th><th>Len</th><th>Home</th><th>Status</th><th>Location</th><th>Active</th><th></th></tr></thead>
        <tbody>
          {items.map((i) => (
            <tr key={i.item_id}>
              <td className="mono">{itemLink(i.item_id)}</td><td>{i.item_type}</td><td>{i.description}</td>
              <td>{i.od}</td><td>{i.id_dim}</td><td>{i.length}</td><td>{i.home_location}</td>
              <td><Chip status={i.status} /></td><td>{i.current_location}</td><td>{i.active ? 'Yes' : 'No'}</td>
              <td><button className="secondary" onClick={() => edit(i)}>Edit</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

const SLOTS = [['die_id', 'DIE', true], ['pattern_id', 'PATTERN', true], ['top_flange_id', 'TOP_FLANGE', false], ['bottom_flange_id', 'BOTTOM_FLANGE', false]]
const BLANK_A = { assembly_id: '', description: '', od: '', length: '', die_id: '', pattern_id: '', top_flange_id: '', bottom_flange_id: '', pouring_wt: '', gating_wt: '', rcd_wt: '', active: true }

function AssembliesTab() {
  const [asms, setAsms] = useState([])
  const [items, setItems] = useState([])
  const [error, setError] = useState(null)
  const [editing, setEditing] = useState(null)
  const [f, setF] = useState(BLANK_A)

  const load = () => Promise.all([get('/assemblies'), get('/items')]).then(([a, i]) => { setAsms(a); setItems(i) }).catch(setError)
  useEffect(() => { load() }, [])
  const set = (k) => (e) => setF({ ...f, [k]: e.target.type === 'checkbox' ? e.target.checked : e.target.value })

  function edit(a) {
    setEditing(a.assembly_id)
    setF(Object.fromEntries(Object.entries({ ...BLANK_A, ...a }).map(([k, v]) => [k, v ?? ''])))
  }
  const reset = () => { setEditing(null); setF(BLANK_A) }

  async function save(e) {
    e.preventDefault()
    try {
      const body = {
        description: f.description, od: num(f.od), length: num(f.length),
        die_id: f.die_id, pattern_id: f.pattern_id,
        top_flange_id: f.top_flange_id, bottom_flange_id: f.bottom_flange_id,
        pouring_wt: num(f.pouring_wt), gating_wt: num(f.gating_wt), rcd_wt: num(f.rcd_wt),
      }
      if (editing) {
        // empty string clears an optional flange; PUT treats it as "no flange"
        await put(`/assemblies/${encodeURIComponent(editing)}`, { ...body, active: f.active })
      } else {
        await post('/assemblies', clean({ assembly_id: f.assembly_id, ...body }))
      }
      setError(null); reset(); load()
    } catch (err) { setError(err) }
  }

  return (
    <>
      <form className="card" onSubmit={save}>
        <b>{editing ? `Edit ${editing}` : 'Add assembly'}</b>
        <div className="row">
          {!editing && <label>Assembly ID *<input value={f.assembly_id} onChange={set('assembly_id')} required /></label>}
          <label>Description<input value={f.description} onChange={set('description')} /></label>
          <label>OD<input type="number" step="any" value={f.od} onChange={set('od')} /></label>
          <label>Length<input type="number" step="any" value={f.length} onChange={set('length')} /></label>
          {SLOTS.map(([slot, type, req]) => (
            <label key={slot}>{type.replace('_', ' ')}{req ? ' *' : ' (optional)'}
              <select value={f[slot]} onChange={set(slot)} required={req}>
                <option value="">{req ? '- select -' : '- none -'}</option>
                {items.filter((i) => i.item_type === type).map((i) => <option key={i.item_id}>{i.item_id}</option>)}
              </select>
            </label>
          ))}
          <label>Pouring wt<input type="number" step="any" value={f.pouring_wt} onChange={set('pouring_wt')} /></label>
          <label>Gating wt<input type="number" step="any" value={f.gating_wt} onChange={set('gating_wt')} /></label>
          <label>RCD wt<input type="number" step="any" value={f.rcd_wt} onChange={set('rcd_wt')} /></label>
          {editing && <label>Active<input type="checkbox" checked={f.active} onChange={set('active')} style={{ minWidth: 0 }} /></label>}
          <button>{editing ? 'Save' : 'Add'}</button>
          {editing && <button type="button" className="secondary" onClick={reset}>Cancel</button>}
        </div>
      </form>
      <Err error={error} />
      <table>
        <thead><tr><th>Assembly</th><th>Description</th><th>Die</th><th>Pattern</th><th>Top flange</th><th>Bottom flange</th><th>Pouring wt</th><th>Active</th><th></th></tr></thead>
        <tbody>
          {asms.map((a) => (
            <tr key={a.assembly_id}>
              <td className="mono">{asmLink(a.assembly_id)}</td><td>{a.description}</td>
              <td>{itemLink(a.die_id)}</td><td>{itemLink(a.pattern_id)}</td>
              <td>{a.top_flange_id ? itemLink(a.top_flange_id) : '-'}</td><td>{a.bottom_flange_id ? itemLink(a.bottom_flange_id) : '-'}</td>
              <td>{a.pouring_wt}</td><td>{a.active ? 'Yes' : 'No'}</td>
              <td><button className="secondary" onClick={() => edit(a)}>Edit</button></td>
            </tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

export default function Masters() {
  const [tab, setTab] = useState('items')
  return (
    <>
      <h1>Masters</h1>
      <div className="tabs">
        <button className={tab === 'items' ? 'on' : ''} onClick={() => setTab('items')}>Items</button>
        <button className={tab === 'assemblies' ? 'on' : ''} onClick={() => setTab('assemblies')}>Assemblies (mapping)</button>
      </div>
      {tab === 'items' ? <ItemsTab /> : <AssembliesTab />}
    </>
  )
}
