// Thin fetch wrapper. No business rules here: every decision (and every permission) is the server's.
export const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'

const KEY = 'die_mgmt_token'
let token = null
try { token = sessionStorage.getItem(KEY) } catch { /* private mode: stay in memory */ }

export const getToken = () => token
export function setToken(t) {
  token = t
  try { t ? sessionStorage.setItem(KEY, t) : sessionStorage.removeItem(KEY) } catch { /* ignore */ }
}

export class ApiError extends Error {
  constructor(status, data) {
    super(typeof data?.detail === 'string' ? data.detail : `Request failed (${status})`)
    this.status = status
    this.data = data
  }
}

let onUnauthorized = () => {}
export const setUnauthorizedHandler = (fn) => { onUnauthorized = fn }

async function raw(method, path, { params, body, form } = {}) {
  const url = new URL(API + path)
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v)
  })
  const headers = {}
  if (token) headers.Authorization = `Bearer ${token}`
  if (body) headers['Content-Type'] = 'application/json'
  let res
  try {
    res = await fetch(url, { method, headers, body: form || (body ? JSON.stringify(body) : undefined) })
  } catch {
    throw new ApiError(0, { detail: `Cannot reach the API at ${API}. Is the backend running?` })
  }
  if (res.status === 401 && token && !path.startsWith('/auth/login')) onUnauthorized()
  if (!res.ok) {
    const data = await res.json().catch(() => null)
    if (Array.isArray(data?.detail)) throw new ApiError(res.status, { detail: data.detail.map((d) => d.msg).join('; ') })
    throw new ApiError(res.status, data)
  }
  return res
}

const json = async (p) => { const r = await p; return r.status === 204 ? null : r.json() }
export const get = (path, params) => json(raw('GET', path, { params }))
export const post = (path, body) => json(raw('POST', path, { body: body || {} }))
export const put = (path, body) => json(raw('PUT', path, { body }))
export const del = (path) => json(raw('DELETE', path))
export const upload = (path, fields, params) => {
  const form = new FormData()
  Object.entries(fields).forEach(([k, v]) => { if (v !== undefined && v !== null && v !== '') form.append(k, v) })
  return json(raw('POST', path, { form, params }))
}

// Files are protected, so they are fetched with the token and turned into a blob.
export async function blob(path, params) {
  const res = await raw('GET', path, { params })
  return res.blob()
}
export async function download(path, fallbackName, params) {
  const res = await raw('GET', path, { params })
  // prefer the server's file name (Content-Disposition), else the caller's
  const m = /filename="?([^";]+)"?/.exec(res.headers.get('Content-Disposition') || '')
  const url = URL.createObjectURL(await res.blob())
  const a = document.createElement('a')
  a.href = url; a.download = m ? m[1] : fallbackName; a.click()
  setTimeout(() => URL.revokeObjectURL(url), 5000)
}

export const clean = (o) => Object.fromEntries(Object.entries(o).filter(([, v]) => v !== '' && v !== undefined))
