export class ApiError extends Error {
  constructor(message, status, payload, isAdminSessionExpired = false) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.payload = payload
    this.isAdminSessionExpired = isAdminSessionExpired
  }
}

export function createApi({
  fetcher = (...args) => fetch(...args),
  getToken = () => localStorage.getItem('token'),
  onExpired = () => window.dispatchEvent(new Event('session-expired')),
} = {}) {
  async function raw(path, options = {}) {
    const headers = new Headers(options.headers)
    const explicitAuth = headers.has('Authorization')
    const token = getToken()
    if (token && !explicitAuth && path.startsWith('/api/')) headers.set('Authorization', `Bearer ${token}`)
    const response = await fetcher(path, { credentials: 'include', ...options, headers })
    if (
      response.status === 401 &&
      response.headers.get('X-Admin-Session') === 'expired' &&
      path.startsWith('/api/') &&
      !path.endsWith('/login') &&
      !explicitAuth
    ) onExpired()
    return response
  }

  async function request(path, method = 'GET', body, options = {}) {
    const headers = new Headers(options.headers)
    const form = typeof FormData !== 'undefined' && body instanceof FormData
    if (body !== undefined && !form) headers.set('Content-Type', 'application/json')
    const response = await raw(path, {
      ...options,
      method,
      headers,
      body: body === undefined ? undefined : form ? body : JSON.stringify(body),
    })
    if (response.status === 204) return null
    const payload = await response.json().catch(() => null)
    if (!response.ok || payload?.success === false) {
      const detail = payload?.detail
      const message =
        typeof detail === 'string'
          ? detail
          : Array.isArray(detail)
            ? detail.map((item) => item.msg).join('；')
            : detail?.message || payload?.message || payload?.error?.message || `请求未完成（HTTP ${response.status}）`
      throw new ApiError(
        message,
        response.status,
        payload,
        response.status === 401 && response.headers.get('X-Admin-Session') === 'expired',
      )
    }
    return payload
  }
  return {
    raw,
    request,
    get: (path, options) => request(path, 'GET', undefined, options),
    post: (path, body = {}, options) => request(path, 'POST', body, options),
    put: (path, body, options) => request(path, 'PUT', body, options),
    delete: (path, options) => request(path, 'DELETE', undefined, options),
    upload: (path, body, options) => request(path, 'POST', body, options),
  }
}
