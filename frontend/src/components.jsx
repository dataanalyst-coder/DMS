import { createContext, useContext, useEffect, useRef, useState } from 'react'
import { blob, get } from './api.js'

export const Session = createContext({ user: null })
export const useSession = () => useContext(Session)
// Buttons are shown from the server-supplied permission list; the server re-checks every action.
export const useCan = () => { const { user } = useSession(); return (p) => !!user?.permissions?.includes(p) }

export const Chip = ({ v }) => (v ? <span className={`chip ${v}`}>{String(v).replaceAll('_', ' ')}</span> : <span className="muted">-</span>)

export const Err = ({ error }) => (error ? <div className="msg err"><b>{error.message}</b></div> : null)
export const Ok = ({ children }) => (children ? <div className="msg ok">{children}</div> : null)

export const fmtTime = (t) => (t ? String(t).replace('T', ' ').slice(0, 16) : '')
export const itemLink = (it) => <a href={`#/item/${it.id}`}>{it.display_id}</a>

export function useDebounced(value, ms = 250) {
  const [v, setV] = useState(value)
  useEffect(() => { const t = setTimeout(() => setV(value), ms); return () => clearTimeout(t) }, [value, ms])
  return v
}

export function useLoad(fn, deps) {
  const [data, setData] = useState(null)
  const [error, setError] = useState(null)
  const reload = () => fn().then((d) => { setData(d); setError(null) }).catch(setError)
  useEffect(() => { reload() }, deps) // eslint-disable-line react-hooks/exhaustive-deps
  return [data, error, reload]
}

export function useLocations() {
  const [locs] = useLoad(() => get('/locations'), [])
  return locs || []
}

export function LocationSelect({ value, onChange, level, required, placeholder = '- location -' }) {
  const locs = useLocations()
  const opts = locs.filter((l) => l.active && (!level || l.level === level))
  return (
    <select value={value ?? ''} onChange={(e) => onChange(e.target.value ? Number(e.target.value) : '')} required={required}>
      <option value="">{placeholder}</option>
      {opts.map((l) => <option key={l.id} value={l.id}>{l.path}{l.name ? ` (${l.name})` : ''}</option>)}
    </select>
  )
}

export function Pager({ total, limit, offset, onChange }) {
  const from = total ? offset + 1 : 0
  return (
    <div className="pager">
      <button className="secondary" disabled={offset <= 0} onClick={() => onChange(Math.max(0, offset - limit))}>Prev</button>
      <span>{from}-{Math.min(offset + limit, total)} of {total}</span>
      <button className="secondary" disabled={offset + limit >= total} onClick={() => onChange(offset + limit)}>Next</button>
    </div>
  )
}

// Protected image (photo): fetched with the token, shown from a blob URL.
export function ProtectedImg({ path, alt }) {
  const [src, setSrc] = useState(null)
  useEffect(() => {
    let url
    blob(path).then((b) => { url = URL.createObjectURL(b); setSrc(url) }).catch(() => setSrc(null))
    return () => url && URL.revokeObjectURL(url)
  }, [path])
  return src ? <img src={src} alt={alt} /> : <span className="muted">loading…</span>
}

// ---- scanning: keyboard-wedge scanner (types the code + Enter), typed lookup, or phone/tablet camera
function CameraScanner({ onCode, onClose }) {
  const video = useRef(null)
  const [err, setErr] = useState(null)
  useEffect(() => {
    let stream, timer, done = false
    ;(async () => {
      try {
        stream = await navigator.mediaDevices.getUserMedia({ video: { facingMode: 'environment' } })
        video.current.srcObject = stream
        await video.current.play()
        const detector = new window.BarcodeDetector({ formats: ['qr_code'] })
        timer = setInterval(async () => {
          if (done || !video.current) return
          const found = await detector.detect(video.current).catch(() => [])
          if (found.length) { done = true; onCode(found[0].rawValue); onClose() }
        }, 300)
      } catch (e) { setErr(e.message || 'Camera not available') }
    })()
    return () => { done = true; clearInterval(timer); stream && stream.getTracks().forEach((t) => t.stop()) }
  }, [])  // eslint-disable-line react-hooks/exhaustive-deps
  return (
    <div className="card">
      <video ref={video} style={{ width: '100%', maxWidth: 360, background: '#000' }} muted playsInline />
      {err && <div className="msg err">{err}</div>}
      <p><button type="button" className="secondary" onClick={onClose}>Close camera</button></p>
    </div>
  )
}

export function ScanInput({ onCode, label = 'Scan or type a code', autoFocus, buttonText = 'Look up' }) {
  const [text, setText] = useState('')
  const [cam, setCam] = useState(false)
  const camOk = typeof window !== 'undefined' && 'BarcodeDetector' in window && !!navigator.mediaDevices?.getUserMedia
  const secure = typeof window !== 'undefined' && window.isSecureContext
  const submit = () => { if (text.trim()) { onCode(text.trim()); setText('') } }
  return (
    <div className="noprint">
      <div className="row">
        <label>{label}
          <input autoFocus={autoFocus} value={text} onChange={(e) => setText(e.target.value)}
            onKeyDown={(e) => { if (e.key === 'Enter') { e.preventDefault(); submit() } }} />
        </label>
        <button type="button" disabled={!text.trim()} onClick={submit}>{buttonText}</button>
        {camOk && secure && <button type="button" className="secondary" onClick={() => setCam(true)}>Use camera</button>}
      </div>
      {!camOk && <small className="muted">Camera scanning needs a browser with QR support (Chrome/Edge on a phone or tablet). A barcode scanner or typing always works.</small>}
      {camOk && !secure && <small className="muted">Camera scanning needs https (or localhost). A barcode scanner or typing always works.</small>}
      {cam && <CameraScanner onCode={onCode} onClose={() => setCam(false)} />}
    </div>
  )
}

// ---- labels: the QR carries the stable ID, so reprinting never creates a new identity
export function LabelPrint({ path, children = 'Print / reprint label' }) {
  const [shown, setShown] = useState(null)
  const [error, setError] = useState(null)
  async function open() {
    try {
      await blob(`${path}?reprint=true`) // records the print in the audit trail (Stores only)
      setShown(path); setError(null)
      setTimeout(() => window.print(), 350)
    } catch (e) { setError(e) }
  }
  return (
    <span>
      <button type="button" className="secondary noprint" onClick={open}>{children}</button>
      <Err error={error} />
      {shown && <span className="label-print" style={{ display: "block" }}><ProtectedImg path={shown} alt="label" /></span>}
    </span>
  )
}
