import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import { computed, reactive } from 'vue'

const source = readFileSync(new URL('../src/components/AccountProxyBinding.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
function binding(overrides = {}) {
    const props = reactive({ account: { id: 7, email: 'demo@example.test', user_paygate_tier: 'PAYGATE_TIER_ONE', credits: 120,
        refresh_status: 'ready' }, draft: {}, proxies: [], activeTab: 'account', ...overrides })
    const code = compileScript(descriptor, { id: 'account-proxy' }).content
        .replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"];?\s*$/gm, '').replace('export default', 'const component =') + '\ncomponent'
    const events = []
    const state = runInNewContext(code, { computed, URL }).setup(props, { expose() {}, emit: (...args) => events.push(args) })
    return { props, events, state }
}

test('expanded account tab directly presents cookies without repeated summaries or another disclosure', () => {
    assert.match(descriptor.template.content, /role="tablist"/)
    assert.match(descriptor.template.content, /role="tabpanel"/)
    assert.doesNotMatch(descriptor.template.content, /draft\.(remark|image_enabled|video_enabled|auto_refresh_enabled|refresh_interval_minutes)/)
    assert.doesNotMatch(descriptor.template.content, /保存配置|常用配置|el-input-number|quick-account-fields|<details|v-html|Flow 套餐|Flow 积分|最近维护/)
    assert.match(descriptor.template.content, /Cookie JSON（只读）/)
    assert.match(descriptor.template.content, /@click="copyCookies"/)
})

test('Cookie JSON retains scoped duplicate cookies and only recorded import metadata', () => {
    const cookies = [
        { name: 'SID', value: 'synthetic-flow', domain: 'flow.google.com', path: '/', secure: true,
            httpOnly: true, hostOnly: true, expires: 1893456000, sameSite: 'Lax', scopeSource: 'browser' },
        { name: 'SID', value: 'synthetic-accounts', domain: 'accounts.google.com', path: '/signin', secure: true },
    ]
    const { state, props, events } = binding({ account: { email: 'demo@example.test',
        google_cookies: JSON.stringify(cookies), current_project_id: 'synthetic-project', user_paygate_tier: 'PAYGATE_TIER_ONE',
        st: 'profile-session:not-a-google-session-id', at: 'synthetic-access', login_password: 'synthetic-password' } })
    const saved = JSON.parse(state.cookieDocument.value.text)
    assert.deepEqual(saved, { cookies, email: 'demo@example.test', projectId: 'synthetic-project', userPaygateTier: 'PAYGATE_TIER_ONE' })
    assert.equal(state.cookieDocument.value.format, 'json')
    assert.ok(state.cookieDocument.value.text.includes('\n'))
    assert.ok(!/profile-session:|synthetic-access|synthetic-password|"sid"/.test(state.cookieDocument.value.text))
    state.copyCookies()
    assert.deepEqual(events, [['copy', state.cookieDocument.value.text]])
    props.account.google_cookies = '{"SID":"rotated-synthetic-cookie"}'
    assert.equal(JSON.parse(state.cookieDocument.value.text).cookies.SID, 'rotated-synthetic-cookie')
})

test('name-value and wrapped cookie JSON preserve values and supported properties', () => {
    const { state, props } = binding({ account: { google_cookies: { SID: 'cookie with = and ; characters',
        '__Host-1PLSID': 'synthetic-secure-cookie' } } })
    assert.deepEqual(JSON.parse(state.cookieDocument.value.text), { cookies: {
        SID: 'cookie with = and ; characters', '__Host-1PLSID': 'synthetic-secure-cookie',
    } })
    props.account.google_cookies = JSON.stringify({ cookies: [{ name: 'SID', value: 'preserved', domain: '.google.com' }],
        email: 'saved@example.test', sid: 'recorded-id', extra: { source: 'synthetic-import' } })
    assert.deepEqual(JSON.parse(state.cookieDocument.value.text), { cookies: [{ name: 'SID', value: 'preserved', domain: '.google.com' }],
        email: 'saved@example.test', sid: 'recorded-id', extra: { source: 'synthetic-import' } })
})

test('missing cookies expose an empty state without invented credentials or copy events', () => {
    for (const google_cookies of [undefined, null, '', '   ', '[]', '{}', 'null', '{"cookies":{}}', '{"cookies":[]}', '{"refreshCookie":{}}']) {
        const { state, events } = binding({ account: { google_cookies, email: 'empty@example.test', st: 'profile-session:marker' } })
        assert.equal(state.cookieDocument.value.format, 'empty', String(google_cookies))
        assert.equal(state.cookieDocument.value.text, '')
        state.copyCookies()
        assert.deepEqual(events, [])
    }
})

test('historical refreshCookie wrappers produce the same importable payload as saved jars', () => {
    const cookies = [{ name: 'SID', value: 'synthetic-scoped-cookie', domain: '.google.com', path: '/', httpOnly: true }]
    for (const raw of [{ refreshCookie: cookies }, { refreshCookie: { cookies } }, { cookies: JSON.stringify(cookies) }]) {
        const { state } = binding({ account: { email: 'legacy@example.test', google_cookies: JSON.stringify(raw) } })
        const document = JSON.parse(state.cookieDocument.value.text)
        assert.deepEqual(document, { cookies, email: 'legacy@example.test' })
    }
})

test('legacy non-JSON credentials display and copy the exact original text', () => {
    const raw = '  SID=synthetic=payload; __Secure-1PSID=another\r\n'
    const { state, events } = binding({ account: { google_cookies: raw, email: 'legacy@example.test' } })
    assert.equal(state.cookieDocument.value.format, 'raw')
    assert.equal(state.cookieDocument.value.text, raw)
    state.copyCookies()
    assert.deepEqual(events, [['copy', raw]])
})

test('syntax tokens retain literal markup and escaped strings as plain text', () => {
    const hostile = '</script><img src=x onerror="alert(1)">\\\n雪'
    const { state } = binding({ account: { google_cookies: JSON.stringify({ SID: hostile }) } })
    const displayed = state.cookieLines.value.map(line => line.map(token => token.text).join('')).join('\n')
    assert.equal(displayed, state.cookieDocument.value.text)
    assert.equal(JSON.parse(displayed).cookies.SID, hostile)
    assert.match(descriptor.template.content, /{{ token.text }}/)
    assert.doesNotMatch(descriptor.template.content, /v-html|innerHTML/)
})

test('arrow, home and end keys select tabs and move focus without saving', () => {
    const { state, events } = binding()
    const focus = []
    let prevented = 0
    const event = key => ({ key, preventDefault() { prevented++ }, currentTarget: {
        parentElement: { querySelectorAll: () => [0, 1].map(id => ({ focus: () => focus.push(id) })) },
    } })
    state.navigateTabs(event('ArrowRight'), 0)
    state.navigateTabs(event('ArrowLeft'), 1)
    state.navigateTabs(event('End'), 0)
    state.navigateTabs(event('Home'), 1)
    assert.deepEqual(events, [['update:activeTab', 'proxy'], ['update:activeTab', 'account'], ['update:activeTab', 'proxy'], ['update:activeTab', 'account']])
    assert.deepEqual(focus, [1, 0, 1, 0])
    assert.equal(prevented, 4)
    assert.equal(events.some(([type]) => type === 'save'), false)
})
