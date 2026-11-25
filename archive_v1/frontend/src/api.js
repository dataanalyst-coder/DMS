// Thin fetch wrapper. No business rules here: every decision comes from the backend.
export const API = import.meta.env.VITE_API_URL || 'http://localhost:8000'

export class ApiError extends Error {
  constructor(status, data) {
    super(typeof data?.detail === 'string' ? data.detail : `Request failed (${status})`)
    this.status = status
    this.data = data
  }
}

async function request(method, path, { params, body } = {}) {
  const url = new URL(API + path)
  Object.entries(params || {}).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') url.searchParams.set(k, v)
  })
  let res
  try {
    res = await fetch(url, {
      method,
      headers: body ? { 'Content-Type': 'application/json' } : undefined,
      body: body ? JSON.stringify(body) : undefined,
    })
  } catch {
    throw new ApiError(0, { detail: `Cannot reach the API at ${API}. Is the backend running?` })
  }
  const data = await res.json().catch(() => null)
  if (!res.ok) {
    if (Array.isArray(data?.detail)) {
      // FastAPI request-validation errors
      throw new ApiError(res.status, { detail: data.detail.map((d) => d.msg).join('; ') })
    }
    throw new ApiError(res.status, data)
  }
  return data
}

export const get = (path, params) => request('GET', path, { params })
export const post = (path, body) => request('POST', path, { body: body || {} })
export const put = (path, body) => request('PUT', path, { body })

// Remove empty strings so the backend sees "not provided".
export const clean = (o) =>
  Object.fromEntries(Object.entries(o).filter(([, v]) => v !== '' && v !== undefined))
