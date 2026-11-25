import { useEffect, useState } from 'react'

export const Chip = ({ status }) =>
  status ? <span className={`chip ${status}`}>{status.replace('_', ' ')}</span> : <span className="muted">-</span>

export const Err = ({ error }) => {
  if (!error) return null
  const d = error.data
  return (
    <div className="msg err">
      <b>{error.message}</b>
      {d?.blockers?.length > 1 && (
        <ul>
          {d.blockers.map((b) => (
            <li key={b.item_id}>{b.item_id}: {b.status}{b.wo_id ? `, WO ${b.wo_id}` : ''}{b.batch_no ? `, batch ${b.batch_no}` : ''}</li>
          ))}
        </ul>
      )}
    </div>
  )
}

export const itemLink = (id) => <a href={`#/item/${encodeURIComponent(id)}`}>{id}</a>
export const asmLink = (id) => (id ? <a href={`#/assembly/${encodeURIComponent(id)}`}>{id}</a> : '-')

// Who/where/which job an item is tied up with (all derived by the backend).
export const holder = (s) =>
  s?.status === 'IN_USE' ? `${s.issued_to || ''} · WO ${s.wo_id || '-'} · batch ${s.batch_no || '-'}` : ''

export function ItemStatusTable({ items }) {
  return (
    <table>
      <thead><tr><th>Item</th><th>Type</th><th>Status</th><th>Location</th><th>Held by / WO / batch</th></tr></thead>
      <tbody>
        {items.map((s) => (
          <tr key={s.item_id}>
            <td className="mono">{itemLink(s.item_id)}</td>
            <td>{s.item_type}</td>
            <td><Chip status={s.status} /></td>
            <td>{s.current_location || '-'}</td>
            <td>{holder(s)}</td>
          </tr>
        ))}
      </tbody>
    </table>
  )
}

export function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value)
  useEffect(() => {
    const t = setTimeout(() => setV(value), ms)
    return () => clearTimeout(t)
  }, [value, ms])
  return v
}

export const fmtTime = (t) => (t ? t.replace('T', ' ').slice(0, 19) : '')
