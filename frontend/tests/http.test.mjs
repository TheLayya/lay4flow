import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { createApi } from '../src/lib/http.js'

test('cookies are included and public key overrides the admin token', async () => {
  const requests = []
  const api = createApi({ getToken: () => 'synthetic-admin', fetcher: async (url, options) => { requests.push({ url, options }); return new Response('{}') } })
  await api.get('/api/stats')
  await api.raw('/v1/models', { headers: { Authorization: 'Bearer synthetic-api' } })
  assert.equal(requests[0].options.credentials, 'include')
  assert.equal(requests[0].options.headers.get('Authorization'), 'Bearer synthetic-admin')
  assert.equal(requests[1].options.headers.get('Authorization'), 'Bearer synthetic-api')
})

test('marked admin expiry triggers sign-in but an invalid public key does not', async () => {
  let expired = 0
  const api = createApi({ getToken: () => null, onExpired: () => expired++, fetcher: async () => new Response('{"detail":"expired"}', { status: 401, headers: { 'X-Admin-Session': 'expired' } }) })
  await assert.rejects(api.get('/api/stats'), { status: 401, message: 'expired', isAdminSessionExpired: true })
  await api.raw('/v1/models', { headers: { Authorization: 'Bearer invalid-public' } })
  await assert.rejects(api.post('/api/login', {}))
  assert.equal(expired, 1)
})

test('an upstream account 401 keeps the admin session and preserves the error', async () => {
  let expired = 0
  const payload = { error: { code: 'session_authentication_failed', message: 'Account session expired' } }
  const api = createApi({ getToken: () => null, onExpired: () => expired++, fetcher: async () => new Response(JSON.stringify(payload), { status: 401 }) })
  await assert.rejects(api.post('/api/admin/vids/accounts/3/quota-refresh'), error => {
    assert.equal(error.status, 401)
    assert.equal(error.message, 'Account session expired')
    assert.equal(error.isAdminSessionExpired, false)
    assert.deepEqual(error.payload, payload)
    return true
  })
  const response = await api.raw('/api/admin/studio/chat/completions', { method: 'POST' })
  assert.equal(response.status, 401)
  assert.deepEqual(await response.json(), payload)
  assert.equal(expired, 0)
})

test('account 401 errors remain visible while admin expiry is handled by sign-in', () => {
  const notices = []
  const source = readFileSync(new URL('../src/lib/api.js', import.meta.url), 'utf8')
  const body = source.match(/export function notifyError\(error\) \{([\s\S]*?)\n\}/)[1]
  const notifyError = new Function('ElMessage', `return function(error) {${body}}`)({ error: message => notices.push(message) })
  notifyError({ status: 401, message: 'Account session expired', isAdminSessionExpired: false })
  notifyError({ status: 401, message: 'Admin session expired', isAdminSessionExpired: true })
  assert.deepEqual(notices, ['Account session expired'])
})

test('validation and business errors preserve readable details, and form uploads remain multipart', async () => {
  const api = createApi({ getToken: () => null, fetcher: async () => new Response('{"detail":[{"msg":"field required"}]}', { status: 422 }) })
  await assert.rejects(api.post('/api/tokens', {}), { message: 'field required' })
  const business = createApi({ getToken: () => null, fetcher: async () => new Response('{"success":false,"message":"identity mismatch"}') })
  await assert.rejects(business.post('/api/tokens', {}), { message: 'identity mismatch' })
  const transfer = createApi({ getToken: () => null, fetcher: async () => new Response(JSON.stringify({ detail: { error_code: 'profile_seed_conflict', message: 'Profile seed already belongs to another account' } }), { status: 409 }) })
  await assert.rejects(transfer.post('/api/profile-transfer/import', {}), error => {
    assert.equal(error.message, 'Profile seed already belongs to another account')
    assert.equal(error.status, 409)
    assert.equal(error.payload.detail.error_code, 'profile_seed_conflict')
    return true
  })
  const form = new FormData(); form.append('file', 'synthetic')
  const upload = createApi({ getToken: () => null, fetcher: async (_, options) => { assert.equal(options.body, form); assert.equal(options.headers.has('Content-Type'), false); return new Response('{}') } })
  await upload.upload('/api/import', form)
})
