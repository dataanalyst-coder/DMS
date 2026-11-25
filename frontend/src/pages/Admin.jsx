import { useState } from 'react'
import { get, post, put } from '../api.js'
import { Err, LabelPrint, Ok, useCan, useLoad } from '../components.jsx'

const ROLES = ['ADMIN', 'ENGINEERING', 'STORES', 'PRODUCTION', 'TOOLING', 'SCRAP_APPROVER', 'MANAGER']
const CHILD = { PLANT: 'AREA', AREA: 'RACK', RACK: 'SHELF', SHELF: 'BIN' }

function useAct() {
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  const act = (fn, ok, after) => async () => {
    try { await fn(); setError(null); setMsg(ok || null); after && after() } catch (e) { setError(e); setMsg(null) }
  }
  return { error, msg, act, banner: <><Err error={error} /><Ok>{msg}</Ok></> }
}

function Locations() {
  const [locs, , reload] = useLoad(() => get('/locations'), [])
  const [f, setF] = useState({ code: '', name: '', parent_id: '' })
  const { act, banner } = useAct()
  return (
    <>
      {banner}
      <div className="row card">
        <label>Parent (blank = new plant)<select value={f.parent_id} onChange={(e) => setF({ ...f, parent_id: e.target.value })}>
          <option value="">- top level (plant) -</option>
          {(locs || []).filter((l) => CHILD[l.level]).map((l) => <option key={l.id} value={l.id}>{l.path} → adds a {CHILD[l.level]}</option>)}
        </select></label>
        <label>Code *<input value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })} /></label>
        <label>Name<input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <button disabled={!f.code} onClick={act(() => post('/locations', { code: f.code, name: f.name || null, parent_id: f.parent_id ? Number(f.parent_id) : null }), 'Location added.', () => { setF({ ...f, code: '', name: '' }); reload() })}>Add location</button>
      </div>
      <table>
        <thead><tr><th>Path</th><th>Level</th><th>Name</th><th>Active</th><th></th></tr></thead>
        <tbody>
          {(locs || []).map((l) => (
            <tr key={l.id}><td className="mono" style={{ paddingLeft: 8 + 14 * (l.path.split('/').length - 1) }}>{l.path}</td><td>{l.level}</td><td>{l.name}</td><td>{l.active ? 'Yes' : 'No'}</td>
              <td><button className="secondary" onClick={act(() => put(`/locations/${l.id}`, { active: !l.active }), l.active ? 'Deactivated.' : 'Activated.', reload)}>{l.active ? 'Deactivate' : 'Activate'}</button>{' '}
                {l.level !== 'PLANT' && <LabelPrint path={`/locations/${l.id}/label.svg`}>Label</LabelPrint>}</td></tr>
          ))}
        </tbody>
      </table>
    </>
  )
}

function Simple({ title, path, keyField, hasActive }) {
  const [rows, , reload] = useLoad(() => get(path), [])
  const [f, setF] = useState({ code: '', name: '' })
  const { act, banner } = useAct()
  return (
    <>
      {banner}
      <div className="row card">
        <label>Code *<input value={f.code} onChange={(e) => setF({ ...f, code: e.target.value })} /></label>
        <label>Name *<input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
        <button disabled={!f.code || !f.name} onClick={act(() => post(path, f), `${title} added.`, () => { setF({ code: '', name: '' }); reload() })}>Add</button>
      </div>
      <table>
        <thead><tr><th>Code</th><th>Name</th>{hasActive && <><th>Active</th><th></th></>}</tr></thead>
        <tbody>{(rows || []).map((r) => (
          <tr key={r[keyField]}><td className="mono">{r.code}</td><td>{r.name}</td>
            {hasActive && <><td>{r.active ? 'Yes' : 'No'}</td><td><button className="secondary" onClick={act(() => put(`${path}/${r.id}`, { active: !r.active }), 'Updated.', reload)}>{r.active ? 'Deactivate' : 'Activate'}</button></td></>}</tr>
        ))}</tbody>
      </table>
    </>
  )
}

function Users() {
  const [users, , reload] = useLoad(() => get('/users'), [])
  const [f, setF] = useState({ username: '', full_name: '', password: '', roles: [] })
  const { act, banner } = useAct()
  const toggle = (roles, r) => (roles.includes(r) ? roles.filter((x) => x !== r) : [...roles, r])
  return (
    <>
      {banner}
      <div className="card">
        <b>New user</b>
        <div className="row">
          <label>Username *<input value={f.username} onChange={(e) => setF({ ...f, username: e.target.value })} /></label>
          <label>Full name *<input value={f.full_name} onChange={(e) => setF({ ...f, full_name: e.target.value })} /></label>
          <label>Initial password (min 10) *<input type="password" value={f.password} onChange={(e) => setF({ ...f, password: e.target.value })} autoComplete="new-password" /></label>
          <label>Roles<span>{ROLES.map((r) => <label key={r} style={{ flexDirection: 'row', gap: 4, display: 'inline-flex', marginRight: 8 }}><input type="checkbox" style={{ minWidth: 0 }} checked={f.roles.includes(r)} onChange={() => setF({ ...f, roles: toggle(f.roles, r) })} />{r}</label>)}</span></label>
          <button onClick={act(() => post('/users', f), 'User created.', () => { setF({ username: '', full_name: '', password: '', roles: [] }); reload() })}>Create</button>
        </div>
      </div>
      <table>
        <thead><tr><th>User</th><th>Roles</th><th>Active</th><th></th></tr></thead>
        <tbody>{(users || []).map((u) => (
          <tr key={u.id}>
            <td>{u.full_name} <small className="muted">{u.username}</small></td>
            <td>{ROLES.map((r) => <label key={r} style={{ flexDirection: 'row', gap: 3, display: 'inline-flex', marginRight: 8 }}><input type="checkbox" style={{ minWidth: 0 }} checked={u.roles.includes(r)} onChange={act(() => put(`/users/${u.id}/roles`, { roles: toggle(u.roles, r) }), 'Roles updated.', reload)} />{r}</label>)}</td>
            <td>{u.active ? 'Yes' : 'No'}</td>
            <td>
              <button className="secondary" onClick={act(() => put(`/users/${u.id}/active`, { active: !u.active }), 'Updated.', reload)}>{u.active ? 'Deactivate' : 'Activate'}</button>{' '}
              <button className="secondary" onClick={() => { const p = window.prompt(`New password for ${u.username} (min 10 characters):`); if (p) act(() => post(`/users/${u.id}/reset-password`, { new_password: p }), 'Password reset; their sessions are signed out.')() }}>Reset password</button>
            </td>
          </tr>
        ))}</tbody>
      </table>
    </>
  )
}

export default function Admin() {
  const can = useCan()
  const [tab, setTab] = useState(can('config.manage') ? 'locations' : 'users')
  return (
    <>
      <h1>Administration</h1>
      <div className="tabs">
        {can('config.manage') && ['locations', 'categories', 'uoms'].map((t) => <button key={t} className={tab === t ? 'on' : ''} onClick={() => setTab(t)}>{t === 'uoms' ? 'Units of measure' : t[0].toUpperCase() + t.slice(1)}</button>)}
        {can('users.manage') && <button className={tab === 'users' ? 'on' : ''} onClick={() => setTab('users')}>Users &amp; roles</button>}
      </div>
      {tab === 'locations' && <Locations />}
      {tab === 'categories' && <Simple title="Category" path="/categories" keyField="id" hasActive />}
      {tab === 'uoms' && <Simple title="Unit" path="/uoms" keyField="code" />}
      {tab === 'users' && <Users />}
    </>
  )
}
