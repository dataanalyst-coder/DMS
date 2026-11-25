import { useState } from 'react'
import { post } from '../api.js'
import { Err, Ok, useSession } from '../components.jsx'

export default function Account() {
  const { user } = useSession()
  const [f, setF] = useState({ old_password: '', new_password: '' })
  const [error, setError] = useState(null)
  const [msg, setMsg] = useState(null)
  async function submit(e) {
    e.preventDefault()
    try {
      await post('/auth/change-password', f)
      setError(null); setMsg('Password changed. Other sessions were signed out; please sign in again.')
      setTimeout(() => window.location.reload(), 1500)
    } catch (err) { setError(err); setMsg(null) }
  }
  return (
    <>
      <h1>My account</h1>
      <p>{user.full_name} ({user.username}) · roles: {user.roles.join(', ') || 'none'}</p>
      <form className="card" onSubmit={submit} style={{ maxWidth: 360, display: 'flex', flexDirection: 'column', gap: 10 }}>
        <b>Change password</b>
        <label>Current password<input type="password" value={f.old_password} onChange={(e) => setF({ ...f, old_password: e.target.value })} autoComplete="current-password" required /></label>
        <label>New password (min 10 characters)<input type="password" value={f.new_password} onChange={(e) => setF({ ...f, new_password: e.target.value })} autoComplete="new-password" required /></label>
        <button>Change password</button>
        <Err error={error} /><Ok>{msg}</Ok>
      </form>
    </>
  )
}
