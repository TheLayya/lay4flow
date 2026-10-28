import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import { computed, reactive, ref, watch, nextTick } from 'vue'
import { CAPTCHA_SERVICE_URL } from '../src/lib/captcha-service.js'

const parseDateSource = readFileSync(new URL('../src/lib/api.js', import.meta.url), 'utf8').split(
    'export function parseDate(value)',
)[1]
const parseDate = new Function(
    'value',
    parseDateSource.slice(parseDateSource.indexOf('{') + 1, parseDateSource.lastIndexOf('}')),
)
const plain = (value) => JSON.parse(JSON.stringify(value))
const deferred = () => {
    let resolve
    let reject
    const promise = new Promise((ok, fail) => {
        resolve = ok
        reject = fail
    })
    return { promise, resolve, reject }
}

function view(name, overrides = {}) {
    const filename = new URL(`../src/views/${name}View.vue`, import.meta.url)
    const { descriptor } = parse(readFileSync(filename, 'utf8'))
    const compiled = compileScript(descriptor, { id: name }).content
    const imports = []
    for (const match of descriptor.scriptSetup.content.matchAll(/import\s+([\s\S]*?)\s+from\s+['"][^'"]+['"]/g)) {
        const clause = match[1]
        const named = clause.match(/\{([\s\S]*?)\}/)?.[1]
        if (named)
            imports.push(
                ...named
                    .split(',')
                    .map((item) =>
                        item
                            .trim()
                            .split(/\s+as\s+/)
                            .pop(),
                    )
                    .filter(Boolean),
            )
        const defaultName = clause
            .replace(/\{[\s\S]*?\}/, '')
            .replace(/,\s*$/, '')
            .trim()
        if (defaultName) imports.push(defaultName)
    }
    const requests = []
    const notices = []
    const errors = []
    const rejectNetwork = async (path, body) => {
        requests.push({ path, body })
        throw new Error('Unexpected fixture request: ' + path)
    }
    const api = {
        get: rejectNetwork,
        post: rejectNetwork,
        put: rejectNetwork,
        delete: rejectNetwork,
        raw: rejectNetwork,
        ...overrides.api,
    }
    const message = (value) => notices.push(value)
    for (const kind of ['success', 'warning', 'info', 'error']) message[kind] = (value) => notices.push({ kind, value })
    const cleanup = []
    const mounted = []
    const storage = new Map()
    const context = Object.fromEntries(imports.map((name) => [name, {}]))
    Object.assign(context, {
        computed,
        reactive,
        ref,
        watch,
        nextTick,
        CAPTCHA_SERVICE_URL,
        Event,
        CustomEvent: class extends Event {
            constructor(type, options) {
                super(type)
                this.detail = options?.detail
            }
        },
        crypto: { randomUUID: () => 'request-fixture-id' },
        onMounted: (callback) => mounted.push(callback),
        onUnmounted: (callback) => cleanup.push(callback),
        useRoute: () => ({ query: {} }),
        api,
        notifyError: (error) => errors.push(error),
        parseDate,
        ElMessage: message,
        ElMessageBox: { confirm: async () => {}, alert: async () => {} },
        URL,
        Blob,
        Headers,
        Response,
        Date,
        console,
        setTimeout: () => 1,
        clearTimeout: () => {},
        setInterval: () => 1,
        clearInterval: () => {},
        localStorage: { removeItem: () => {} },
        sessionStorage: {
            getItem: (key) => storage.get(key),
            setItem: (key, value) => storage.set(key, value),
            removeItem: (key) => storage.delete(key),
        },
        location: { origin: 'http://fixture.local' },
        document: {
            hidden: false,
            addEventListener: () => {},
            removeEventListener: () => {},
            querySelector: () => null,
            querySelectorAll: () => [],
        },
        navigator: { clipboard: { writeText: async () => {} } },
        window: { removeEventListener: () => {}, dispatchEvent: () => {} },
        ...overrides.context,
    })
    const executable =
        compiled
            .replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"];?\s*$/gm, '')
            .replace('export default', 'const component =') + '\ncomponent'
    const component = runInNewContext(executable, context, { filename: filename.pathname })
    return {
        state: component.setup({}, { expose() {} }),
        requests,
        notices,
        errors,
        cleanup,
        mounted,
        storage,
        api,
        document: context.document,
    }
}
function accountReads(accounts) {
    return async (path) =>
        path === '/api/tokens'
            ? accounts
            : path === '/api/token-refresh/config'
              ? { config: { protocol_refresh_enabled: false } }
              : { accounts: [] }
}

test('orphan Vids-only rows can be deleted individually and in a mixed bulk delete', async () => {
    const deletes = []
    const fixture = view('Accounts', {
        api: {
            get: accountReads([]),
            delete: async (path) => {
                deletes.push(path)
                return { success: true }
            },
        },
    })
    const s = fixture.state
    const orphan = {
        id: 4,
        email: '',
        vidsOnly: true,
        vids: { token_id: 4, identity_verified: false },
        is_active: false,
    }
    const flow = { id: 2, email: 'flow@fixture.local', is_active: true }
    s.accounts.value = [flow]
    s.vids.value = [orphan.vids]
    await s.action(orphan, 'delete')
    assert.deepEqual(deletes, ['/api/tokens/4'])
    s.selected.value = [orphan, flow]
    await s.bulk('delete')
    assert.deepEqual(deletes, ['/api/tokens/4', '/api/tokens/4', '/api/tokens/2'])
})

test('login avoids returning to the root login redirect and preserves a protected destination', () => {
    for (const next of ['/', '/?from=expired', '/login', '/login?next=/accounts', '//elsewhere.test']) {
        const { state } = view('Login', { context: { useRouter: () => ({}), useRoute: () => ({ query: { next } }) } })
        assert.equal(state.nextPath(), '/dashboard', next)
    }
    const { state } = view('Login', {
        context: { useRouter: () => ({}), useRoute: () => ({ query: { next: '/accounts?platform=vids' } }) },
    })
    assert.equal(state.nextPath(), '/accounts?platform=vids')
})

test('settings overview searches field labels without changing drafts and counts each dirty group once', () => {
    const { state: s } = view('Settings')
    for (const source of s.configSources) s.markSnapshot(source.id, source.target)
    s.markSnapshot('debug', { enabled: s.admin.debug_enabled })
    s.settingsQuery.value = '图片生成超时'
    assert.deepEqual(plain(s.matchingCards.value.map((card) => card.id)), ['timeouts'])
    assert.equal(s.sectionVisible('generation'), true)
    assert.equal(s.sectionVisible('general'), false)
    s.generation.image_timeout = 480
    s.callLogic.call_mode = 'polling'
    s.newKey.value = 'fixture-new-key'
    assert.equal(s.dirtyCount.value, 3)
    s.settingsQuery.value = '调试日志'
    assert.equal(s.cardVisible('debug'), true)
    assert.equal(s.cardVisible('plugin'), false)
    assert.equal(s.generation.image_timeout, 480)
    s.settingsQuery.value = ''
    assert.equal(s.matchingCards.value.length, 10)
    assert.equal(s.dirtyCount.value, 3)
    assert.equal(s.sectionVisible('general'), true)
    assert.equal(s.sectionVisible('generation'), true)
    s.jumpToSection('generation')
    assert.equal(s.sectionVisible('general'), false)
    assert.equal(s.sectionVisible('generation'), true)
    assert.equal(s.generation.image_timeout, 480)
    assert.equal(s.newKey.value, 'fixture-new-key')
    s.jumpToSection('all')
    assert.equal(s.sectionVisible('general'), true)
    assert.equal(s.sectionVisible('generation'), true)
})

test('release information is local on load and checks online versions only on request', async () => {
    const paths = []
    const local = {
        version: '1.2.0',
        release_date: '2026-10-21',
        entries: [{ version: '1.2.0', date: '2026-10-21', items: ['合成更新记录'] }],
    }
    const { state: s } = view('Settings', {
        api: {
            get: async (path) => {
                paths.push(path)
                if (path === '/api/admin/release') return local
                if (path === '/api/admin/release/check')
                    return { status: 'unpublished', message: 'GitHub 尚未发布稳定版' }
                throw new Error('Unexpected release path')
            },
        },
    })
    await s.loadRelease()
    assert.equal(s.release.version, '1.2.0')
    assert.deepEqual(paths, ['/api/admin/release'])
    await s.checkRelease()
    assert.deepEqual(paths, ['/api/admin/release', '/api/admin/release/check'])
    assert.equal(s.releaseCheck.status, 'unpublished')
    assert.equal(s.releaseCheck.loading, false)
    assert.equal(s.release.entries[0].items[0], '合成更新记录')
    s.settingsQuery.value = 'Windows'
    assert.deepEqual(plain(s.matchingCards.value.map((card) => card.id)), ['release'])
})

test('release check failures retain the local version and changelog', async () => {
    const { state: s } = view('Settings', {
        api: {
            get: async () => {
                throw new Error('Synthetic offline network')
            },
        },
    })
    s.release.version = '1.2.0'
    s.release.entries = [{ version: '1.2.0', items: ['已知修复'] }]
    await s.checkRelease()
    assert.equal(s.release.version, '1.2.0')
    assert.equal(s.release.entries[0].items[0], '已知修复')
    assert.equal(s.releaseCheck.loading, false)
    assert.match(s.releaseCheck.message, /已保留本地更新记录/)
})

test('captcha settings always use the fixed service and refresh global health after saving', async () => {
    const writes = []
    const events = []
    const fixture = view('Settings', {
        context: { window: { dispatchEvent: (event) => events.push(event.type) } },
        api: {
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return { success: true }
            },
        },
    })
    const s = fixture.state
    const source = s.configSources.find((item) => item.id === 'captcha')
    const remote = source.map({
        captcha_method: 'obsolete',
        remote_browser_base_url: 'https://captcha.example.test',
        remote_browser_api_key: 'synthetic-service-key',
        remote_browser_timeout: 120,
        unrelated_legacy_field: 'ignored',
    })
    Object.assign(s.captcha, remote)
    s.loaded.add('captcha')
    assert.equal(remote.remote_browser_base_url, CAPTCHA_SERVICE_URL)
    s.markSnapshot('captcha', s.captcha)
    s.captcha.remote_browser_api_key = '  updated-synthetic-key  '
    assert.equal(s.dirty('captcha', s.captcha), true)
    await s.saveCaptcha()
    assert.deepEqual(writes, [
        {
            path: '/api/captcha/config',
            body: {
                captcha_method: 'remote_browser',
                remote_browser_base_url: CAPTCHA_SERVICE_URL,
                remote_browser_api_key: 'updated-synthetic-key',
                remote_browser_timeout: 120,
            },
        },
    ])
    assert.deepEqual(events, ['captcha-config-saved'])
    assert.equal(s.dirty('captcha', s.captcha), false)
})

test('captcha save requires a key and a failed save retains the draft without publishing an event', async () => {
    const events = []
    const fixture = view('Settings', {
        context: { window: { dispatchEvent: (event) => events.push(event.type) } },
        api: {
            post: async () => {
                throw new Error('fixture save failure')
            },
        },
    })
    const s = fixture.state
    s.loaded.add('captcha')
    s.markSnapshot('captcha', s.captcha)
    s.captcha.remote_browser_api_key = '  '
    await s.saveCaptcha()
    assert.equal(fixture.notices.at(-1).kind, 'warning')
    assert.equal(fixture.errors.length, 0)
    s.captcha.remote_browser_api_key = 'fixture-key'
    await s.saveCaptcha()
    assert.equal(fixture.errors.length, 1)
    assert.equal(s.captcha.remote_browser_api_key, 'fixture-key')
    assert.equal(s.dirty('captcha', s.captcha), true)
    assert.deepEqual(events, [])
})

test('settings captcha deep link reveals the section and focuses its key without losing other drafts', async () => {
    const route = reactive({ query: { section: 'captcha' } })
    const visits = []
    let focuses = 0
    const fixture = view('Settings', {
        context: {
            useRoute: () => route,
            document: { getElementById: (id) => ({ scrollIntoView: () => visits.push(id) }) },
        },
    })
    const s = fixture.state
    s.captchaKeyInput.value = {
        focus: () => {
            focuses++
        },
    }
    s.generation.image_timeout = 420
    s.settingsQuery.value = '视频'
    s.loading.value = false
    await nextTick()
    await new Promise(setImmediate)
    assert.equal(s.settingsQuery.value, '')
    assert.equal(s.group.value, 'captcha')
    assert.equal(s.generation.image_timeout, 420)
    assert.equal(focuses, 1)
    assert.deepEqual(visits, ['settings-captcha'])
    route.query.section = 'general'
    await nextTick()
    route.query.section = 'captcha'
    await nextTick()
    await new Promise(setImmediate)
    assert.equal(focuses, 2)
})

test('proxy pool editing retains row identity and failed edits keep the submitted form open', async () => {
    const writes = []
    let fail = true
    const fixture = view('ProxyPool', {
        api: {
            put: async (path, body) => {
                writes.push({ path, body: plain(body) })
                if (fail) throw new Error('fixture edit failed')
                return { success: true, id: 7 }
            },
            get: async () => ({ success: true, entries: [] }),
        },
    })
    const s = fixture.state
    s.openProxy({ id: 7, proxy_url: 'socks5://fixture.proxy:1080', note: 'original' })
    s.poolForm.note = 'edited'
    await s.addProxy()
    assert.equal(s.inlineEditing.value, true)
    assert.equal(s.poolDialog.value, false)
    assert.equal(s.poolForm.note, 'edited')
    assert.equal(writes[0].path, '/api/proxy/pool/7')
    fail = false
    await s.addProxy()
    assert.equal(s.poolDialog.value, false)
    assert.equal(writes[1].body.note, 'edited')
    s.openProxy()
    s.poolForm.host = 'not a proxy'
    await s.addProxy()
    assert.equal(writes.length, 2)
})

test('proxy fields round-trip encoded credentials, IPv6 and protocol default ports', () => {
    const { state: s } = view('ProxyPool')
    const fields = s.parseProxyFields('socks5h://old%40user:p%3Aa%2Fss%25@[2001:db8::1]:1080')
    assert.deepEqual(plain(fields), {
        protocol: 'socks5h',
        host: '2001:db8::1',
        port: 1080,
        username: 'old@user',
        password: 'p:a/ss%',
    })
    assert.equal(s.proxyURLFromFields(fields), 'socks5h://old%40user:p%3Aa%2Fss%25@[2001:db8::1]:1080')
    assert.equal(s.parseProxyFields('https://example.test:443').port, 443)
    assert.equal(s.parseProxyFields('socks5://user:raw%value@example.test:1080').password, 'raw%value')
    assert.equal(
        s.proxyURLFromFields({
            protocol: 'http',
            host: '203.0.113.42',
            port: 7123,
            username: 'fresh@user',
            password: 'new:p/a@ss',
        }),
        'http://fresh%40user:new%3Ap%2Fa%40ss@203.0.113.42:7123',
    )
    for (const invalid of [
        { host: '', port: 80 },
        { host: 'bad host', port: 80 },
        { host: 'example.test', port: 0 },
        { host: 'example.test', port: 65536 },
        { host: 'example.test', port: 2.5 },
        { host: 'example.test', port: 80, password: 'password' },
    ])
        assert.throws(() => s.proxyURLFromFields({ protocol: 'http', ...invalid }))
})

test('bound proxy metadata edits preserve the original connection string', async () => {
    const writes = []
    const fixture = view('ProxyPool', {
        api: {
            put: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return { success: true }
            },
            get: async () => ({ entries: [] }),
        },
    })
    fixture.state.openProxy({ id: 8, proxy_url: 'socks5://user:p%raw@fixture.test:1080', bound_account_count: 2 })
    fixture.state.poolForm.note = 'stable exit'
    await fixture.state.addProxy()
    assert.deepEqual(writes, [{ path: '/api/proxy/pool/8', body: { note: 'stable exit' } }])
})

test('Flow cookie combo copies the saved value without changing cookie fields or browser state', async () => {
    const copied = [],
        reads = []
    let hasTimestamp = false
    const fixture = view('Accounts', {
        context: { navigator: { clipboard: { writeText: async (value) => copied.push(value) } } },
        api: {
            get: async (path, options) => {
                reads.push({ path, options })
                return {
                    success: true,
                    cookie: hasTimestamp ? 'psid::sid=value::osid%2Fvalue::timestamp' : 'psid::sid=value::osid%2Fvalue',
                    has_1psidts: hasTimestamp,
                }
            },
        },
    })
    const s = fixture.state
    const account = { id: 7, email: 'fixture@example.invalid', google_cookies: 'saved original fixture' }
    s.accounts.value = [account]
    s.openEditor(account)
    s.form.google_cookies = 'unsaved fixture draft'
    await s.copyFlowCookieCombo(account)
    hasTimestamp = true
    await s.copyFlowCookieCombo(account)
    assert.deepEqual(copied, ['psid::sid=value::osid%2Fvalue', 'psid::sid=value::osid%2Fvalue::timestamp'])
    assert.deepEqual(
        reads.map(({ path, options }) => ({ path, options: { cache: options.cache } })),
        [
            { path: '/api/tokens/7/flow-cookie-combo', options: { cache: 'no-store' } },
            { path: '/api/tokens/7/flow-cookie-combo', options: { cache: 'no-store' } },
        ],
    )
    assert.equal(account.google_cookies, 'saved original fixture')
    assert.equal(s.form.google_cookies, 'unsaved fixture draft')
    assert.equal(fixture.requests.length, 0)
    assert.equal(s.busy.has('7:copy-cookie-combo'), false)
    assert.match(fixture.notices.at(-1).value, /4 段/)
})

test('Flow cookie combo failures keep the clipboard untouched and release the pending copy', async () => {
    const pending = deferred(),
        copied = []
    let reads = 0
    const fixture = view('Accounts', {
        context: { navigator: { clipboard: { writeText: async (value) => copied.push(value) } } },
        api: {
            get: async () => {
                reads++
                return pending.promise
            },
        },
    })
    const row = { id: 7 },
        s = fixture.state
    const first = s.copyFlowCookieCombo(row)
    await s.copyFlowCookieCombo(row)
    await s.copyFlowCookieCombo({ id: 8, vidsOnly: true })
    assert.equal(reads, 1)
    assert.equal(s.busy.has('7:copy-cookie-combo'), true)
    pending.reject(
        Object.assign(new Error('Missing saved credentials'), {
            payload: { detail: { missing: ['__Secure-1PSID', 'OSID'] } },
        }),
    )
    await first
    assert.deepEqual(copied, [])
    assert.match(fixture.errors[0].message, /__Secure-1PSID、OSID/)
    assert.equal(s.busy.has('7:copy-cookie-combo'), false)
    fixture.api.get = async () => ({ success: true, cookie: 'psid::sid::osid' })
    await s.copyFlowCookieCombo(row)
    assert.deepEqual(copied, ['psid::sid::osid'])
})

test('inline proxy drafts survive row switches and exact credentials copy', async () => {
    const copied = []
    const fixture = view('ProxyPool', {
        context: { navigator: { clipboard: { writeText: async (value) => copied.push(value) } } },
        api: { get: async () => ({ entries: [] }) },
    })
    const s = fixture.state
    const first = { id: 1, proxy_url: 'socks5h://user%40test:p%25%2Fa%3Ass@[2001:db8::1]:1080', note: 'first' }
    s.openProxy(first)
    s.poolForm.note = 'unsaved'
    s.expandProxy(first, false)
    s.openProxy({ id: 2, proxy_url: 'http://second.test:80' })
    s.expandProxy(first, true)
    assert.equal(s.poolForm.note, 'unsaved')
    await s.loadPool()
    assert.equal(s.poolForm.note, 'unsaved')
    assert.equal(s.poolDialog.value, false)
    assert.equal(s.hostPort.value, '[2001:db8::1]:1080')
    for (const value of [first.proxy_url, s.poolForm.username, s.poolForm.password]) await s.copyValue(value, 'fixture')
    assert.deepEqual(copied, [first.proxy_url, 'user@test', 'p%/a:ss'])
    s.busy.add('poolAdd')
    s.openProxy({ id: 3, proxy_url: 'http://third.test:80' })
    s.expandProxy(first, false)
    assert.equal(s.expandedId.value, 1)
})

test('proxy pool batch preserves per-entry failures and blocks overlapping batches', async () => {
    const response = deferred(),
        writes = []
    const fixture = view('ProxyPool', {
        api: {
            raw: async (path, options) => {
                writes.push({ path, body: JSON.parse(options.body) })
                return response.promise
            },
            get: async () => ({ success: true, entries: [] }),
        },
    })
    const s = fixture.state
    const operation = s.batchPool({ action: 'disable', ids: [1, 2] })
    await s.batchPool({ action: 'enable', ids: [1] })
    assert.equal(writes.length, 1)
    response.resolve(
        new Response(
            JSON.stringify({
                success: true,
                succeeded: 1,
                results: [
                    { id: 1, success: true },
                    { id: 2, success: false, message: 'fixture failure' },
                ],
            }),
        ),
    )
    await operation
    assert.equal(s.poolBulk.running, false)
    assert.equal(s.poolBulkResult.value.results[1].message, 'fixture failure')
    assert.deepEqual(writes[0].body, { action: 'disable', ids: [1, 2] })
})

test('proxy import keeps failed original lines for correction and refreshes the saved pool', async () => {
    const writes = []
    const fixture = view('ProxyPool', {
        api: {
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return {
                    success: true,
                    total: 3,
                    added: 1,
                    skipped: 1,
                    failed: 1,
                    results: [
                        { line: 2, status: 'added', id: 11 },
                        { line: 4, status: 'skipped', id: 12 },
                        { line: 5, status: 'failed', error_code: 'invalid_format', message: 'secret input' },
                    ],
                }
            },
            get: async () => ({ success: true, entries: [{ id: 11 }] }),
        },
    })
    const s = fixture.state
    const text = '\n192.0.2.1:6525:demo:demo-pass\r\n \r\nsocks5://demo:demo-pass@192.0.2.2:1080\r\nbad-row'
    s.poolImportText.value = text
    s.openPoolImport()
    assert.equal(s.poolImportDialog.value, true)
    assert.equal(s.poolImportLines.value, 3)
    await s.importPool()
    assert.deepEqual(writes, [{ path: '/api/proxy/pool/import', body: { text } }])
    assert.equal(s.poolImportText.value, 'bad-row')
    assert.equal(s.pool.value[0].id, 11)
    assert.equal(s.poolImportResult.value.added, 1)
    assert.equal(s.poolImportResult.value.skipped, 1)
    assert.equal(s.poolImportResult.value.failed, 1)
    assert.equal(s.poolImportResult.value.results[2].line, 5)
    assert.match(s.poolImportResult.value.results[2].message, /格式有误/)
    assert.doesNotMatch(JSON.stringify(s.poolImportResult.value), /secret input/)
    assert.equal(s.poolImporting.value, false)
    assert.equal(fixture.storage.size, 0)
})

test('proxy import rejects empty input and size limits before sending any requests', async () => {
    const fixture = view('ProxyPool')
    const s = fixture.state
    for (const [text, reason] of [
        [' \n\r\n', /请填写/],
        [Array(501).fill('192.0.2.1:80:demo:pass').join('\n'), /500 行/],
        ['x'.repeat(4097), /第 1 行超过 4096/],
        ['192.0.2.1:80:demo:pass\n' + ' '.repeat(4097), /第 2 行超过 4096/],
        ['x'.repeat(2100001), /2100000/],
    ]) {
        s.poolImportText.value = text
        await s.importPool()
        assert.match(s.poolImportError.value, reason)
        assert.equal(s.poolImportText.value, text)
    }
    assert.equal(fixture.requests.length, 0)
    assert.equal(s.poolImporting.value, false)
})

test('proxy import guards overlapping import and batch mutations in either direction', async () => {
    const response = deferred()
    const writes = []
    const fixture = view('ProxyPool', {
        api: {
            post: async (path) => {
                writes.push(path)
                return response.promise
            },
            get: async () => ({ entries: [] }),
        },
    })
    const s = fixture.state
    s.poolImportText.value = '192.0.2.1:80:demo:pass'
    s.poolBulk.running = true
    s.openPoolImport()
    await s.importPool()
    assert.equal(s.poolImportDialog.value, false)
    assert.equal(writes.length, 0)
    s.poolBulk.running = false
    const importing = s.importPool()
    await s.importPool()
    await s.batchPool({ action: 'disable', ids: [1] })
    await s.poolAction({ id: 1 }, 'toggle')
    await s.addProxy()
    assert.equal(writes.length, 1)
    assert.equal(s.poolImporting.value, true)
    response.resolve({
        success: true,
        total: 1,
        added: 1,
        skipped: 0,
        failed: 0,
        results: [{ line: 1, status: 'added', id: 1 }],
    })
    await importing
    assert.equal(s.poolImporting.value, false)
    assert.equal(s.poolImportText.value, '')
    assert.equal(s.poolImportResult.value.added, 1)
})

test('proxy import request failures keep all text and hide raw credential errors', async () => {
    for (const response of [
        () => {
            throw new Error('upstream http://demo:private-pass@192.0.2.1:80 failed')
        },
        () => ({ success: false, message: 'rejected http://demo:private-pass@192.0.2.1:80' }),
    ]) {
        const fixture = view('ProxyPool', { api: { post: async () => response() } })
        const s = fixture.state
        const text = '\n192.0.2.1:80:demo:private-pass\n'
        s.poolImportText.value = text
        await s.importPool()
        assert.equal(s.poolImportText.value, text)
        assert.equal(s.poolImportResult.value, null)
        assert.equal(s.poolImporting.value, false)
        assert.match(s.poolImportError.value, /输入已保留/)
        assert.doesNotMatch(s.poolImportError.value, /private-pass|http:/)
        assert.equal(fixture.errors.length, 0)
    }
})

test('successful proxy import keeps its result when pool refresh fails and hides unknown row errors', async () => {
    const fixture = view('ProxyPool', {
        api: {
            post: async () => ({
                success: true,
                total: 2,
                added: 1,
                skipped: 0,
                failed: 1,
                results: [
                    { line: 1, status: 'added', id: 1 },
                    { line: 2, status: 'failed', message: 'demo:private-pass@192.0.2.1' },
                ],
            }),
            get: async () => {
                throw new Error('refresh failed with private data')
            },
        },
    })
    const s = fixture.state
    s.poolImportText.value = '192.0.2.1:80:demo:pass\nbad-row'
    await s.importPool()
    assert.equal(s.poolImportText.value, 'bad-row')
    assert.equal(s.poolImportResult.value.added, 1)
    assert.equal(s.poolImportError.value, '')
    assert.equal(s.poolImportRefreshWarning.value, true)
    assert.equal(s.poolImporting.value, false)
    assert.doesNotMatch(s.poolImportResult.value.results[1].message, /private-pass|192\.0/)
    assert.equal(fixture.errors.length, 0)
})

test('failed settings saves preserve dirty drafts and unloaded groups make no request', async () => {
    const fixture = view('Settings', {
        api: {
            post: async () => {
                throw new Error('fixture rejected')
            },
        },
    })
    const s = fixture.state
    s.loaded.add('cache')
    s.markSnapshot('cache', s.cache)
    const before = s.snapshots.cache
    s.cache.timeout = 9000
    await s.saveCache()
    assert.equal(s.snapshots.cache, before)
    assert.equal(s.dirty('cache', s.cache), true)
    assert.equal(fixture.errors[0].message, 'fixture rejected')
    s.loaded.clear()
    await s.save('generation', '/api/config/generation', { ...s.generation })
    assert.equal(fixture.notices.at(-1).kind, 'warning')
    assert.equal(s.busy.size, 0)
})

test('a settings save snapshots the submitted value while later edits remain dirty', async () => {
    const response = deferred()
    let received
    const { state: s } = view('Settings', {
        api: {
            post: async (_, body) => {
                received = plain(body)
                return response.promise
            },
        },
    })
    s.loaded.add('generation')
    s.markSnapshot('generation', s.generation)
    s.generation.image_timeout = 400
    const saving = s.save('generation', '/api/config/generation', { ...s.generation }, s.generation)
    s.generation.image_timeout = 500
    response.resolve({ success: true })
    await saving
    assert.equal(received.image_timeout, 400)
    assert.equal(JSON.parse(s.snapshots.generation).image_timeout, 400)
    assert.equal(s.dirty('generation', s.generation), true)
    s.markSnapshot('admin', s.admin)
    s.admin.error_ban_threshold = 7
    s.loaded.add('debug')
    await s.save('debug', '/api/admin/debug', { enabled: true })
    assert.equal(s.dirty('admin', s.admin), true)
})

test('editing an account preserves unchanged ST and blank password, and keeps rejection visible', async () => {
    const writes = []
    const account = {
        id: 4,
        email: 'member@fixture.local',
        st: 'synthetic-st',
        login_password: 'synthetic-secret',
        current_project_id: 'project-4',
    }
    const fixture = view('Accounts', {
        api: {
            get: accountReads([account]),
            put: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return { success: true }
            },
        },
    })
    const s = fixture.state
    s.openEditor(account)
    s.formRef.value = { validate: async () => true }
    s.form.remark = 'Updated note'
    await s.saveAccount()
    assert.equal(writes[0].path, '/api/tokens/4')
    assert.equal('st' in writes[0].body, false)
    assert.equal('login_password' in writes[0].body, false)
    assert.equal(writes[0].body.project_id, 'project-4')
    s.openEditor(account)
    s.form.st = 'different-identity-st'
    fixture.api.put = async () => {
        throw new Error('account identity mismatch')
    }
    await s.saveAccount()
    assert.equal(s.editorOpen.value, true)
    assert.equal(s.saving.value, false)
    assert.equal(fixture.errors.at(-1).message, 'account identity mismatch')
})

test('account quick binding saves only a pool ID, serializes writes and updates both pool counts without folding', async () => {
    const response = deferred()
    const writes = []
    const fixture = view('Accounts', {
        api: {
            put: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return response.promise
            },
        },
    })
    const s = fixture.state
    const first = { id: 4, email: 'first@example.test', proxy_pool_id: 1, proxy_url: 'socks5://old:pass@old.test:1080' }
    const second = { id: 5, email: 'second@example.test', proxy_pool_id: null }
    s.accounts.value = [first, second]
    s.proxyPool.value = [
        { id: 1, enabled: true, proxy_url: first.proxy_url, bound_account_count: 2 },
        {
            id: 2,
            enabled: true,
            last_check_ok: false,
            proxy_url: 'socks5h://new:pass@new.test:1080',
            bound_account_count: 3,
        },
    ]
    s.toggleQuickAccount(first)
    s.quickDraft(first).proxy_pool_id = 2
    s.quickDraft(second).proxy_pool_id = 2
    const operation = s.saveQuickAccount(first)
    await s.saveQuickAccount(first)
    await s.saveQuickAccount(second)
    assert.deepEqual(writes, [{ path: '/api/tokens/4', body: { proxy_pool_id: 2 } }])
    assert.equal(s.accounts.value[0].proxy_pool_id, 1)
    response.resolve({ success: true })
    await operation
    assert.equal(s.accounts.value[0].proxy_pool_id, 2)
    assert.equal(s.accounts.value[0].proxy_url, s.proxyPool.value[1].proxy_url)
    assert.deepEqual(plain(s.proxyPool.value.map((proxy) => proxy.bound_account_count)), [1, 4])
    assert.deepEqual(plain(s.expandedAccountIds.value), [4])
    assert.equal(s.quickSavingId.value, null)
    assert.equal(s.quickErrors[4], '')
    assert.match(s.quickSaved[4], /#2/)
    assert.equal(fixture.requests.length, 0)
    assert.equal(fixture.storage.size, 0)
})

test('quick binding retains Cloak rejection, pending choice and other account drafts for an in-place retry', async () => {
    let rejected = true
    const writes = []
    const fixture = view('Accounts', {
        api: {
            put: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return rejected ? { success: false, message: 'Cloak Profile 固定使用原账号出口' } : { success: true }
            },
        },
    })
    const s = fixture.state
    const row = { id: 7, browser_profile_source: 'cloak', proxy_pool_id: 1, proxy_url: 'socks5://old.test:1080' }
    const other = { id: 8, remark: 'original' }
    s.accounts.value = [row, other]
    s.proxyPool.value = [
        { id: 1, enabled: true, proxy_url: row.proxy_url, bound_account_count: 1 },
        { id: 2, enabled: true, proxy_url: 'socks5://new.test:1080', bound_account_count: 0 },
        { id: 3, enabled: false, proxy_url: 'socks5://disabled.test:1080' },
    ]
    s.toggleQuickAccount(row)
    s.quickDraft(row).proxy_pool_id = 2
    s.quickDraft(other).remark = 'keep this draft'
    await s.saveQuickAccount(row)
    assert.equal(s.accounts.value[0].proxy_pool_id, 1)
    assert.equal(s.proxyPool.value[0].bound_account_count, 1)
    assert.equal(s.proxyPool.value[1].bound_account_count, 0)
    assert.equal(s.quickDraft(row).proxy_pool_id, 2)
    assert.match(s.quickErrors[7], /Cloak/)
    assert.equal(fixture.notices.length, 0)
    assert.equal(s.quickDraft(other).remark, 'keep this draft')
    s.quickDraft(row).proxy_pool_id = 3
    await s.saveQuickAccount(row)
    assert.equal(writes.length, 1, 'Disabled exits must fail before making a request')
    s.quickDraft(row).proxy_pool_id = 2
    rejected = false
    await s.saveQuickAccount(row)
    assert.equal(s.accounts.value[0].proxy_pool_id, 2)
    assert.equal(s.quickErrors[7], '')
    assert.deepEqual(plain(s.expandedAccountIds.value), [7])
})

test('quick Cloak switching confirms before writing and cancellation keeps the chosen draft', async () => {
    const confirmations = []
    const fixture = view('Accounts', {
        context: {
            ElMessageBox: {
                confirm: async (...args) => {
                    confirmations.push(args)
                    throw new Error('cancel')
                },
            },
        },
    })
    const s = fixture.state
    const row = {
        id: 7,
        email: 'cloak@fixture.local',
        browser_profile_source: 'cloak',
        proxy_pool_id: 1,
        proxy_url: 'socks5://old.fixture:1080',
    }
    s.accounts.value = [row]
    s.proxyPool.value = [{ id: 2, enabled: true, proxy_url: 'socks5://new.fixture:1080' }]
    s.quickDraft(row).proxy_pool_id = 2
    await s.saveQuickAccount(row)
    assert.equal(confirmations.length, 1)
    assert.match(confirmations[0][0], /Cookie.*seed.*Profile/)
    assert.equal(fixture.requests.length, 0)
    assert.equal(s.accounts.value[0].proxy_pool_id, 1)
    assert.equal(s.quickDraft(row).proxy_pool_id, 2)
    assert.equal(s.quickSavingId.value, null)
})

test('inline account settings write only changed common fields and preserve edits on failure', async () => {
    let rejected = true
    const writes = []
    const fixture = view('Accounts', {
        api: {
            put: async (path, body) => {
                writes.push({ path, body: plain(body) })
                if (rejected) throw new Error('保存暂时失败')
                return { success: true }
            },
        },
    })
    const s = fixture.state
    const row = {
        id: 12,
        remark: 'original',
        image_enabled: true,
        video_enabled: true,
        auto_refresh_enabled: true,
        refresh_interval_minutes: 120,
        st: 'leave-st-alone',
    }
    s.accounts.value = [row]
    const draft = s.quickDraft(row)
    draft.remark = 'new note'
    draft.video_enabled = false
    await s.saveQuickAccount(row, 'settings')
    assert.deepEqual(writes[0], { path: '/api/tokens/12', body: { remark: 'new note', video_enabled: false } })
    assert.equal(s.accounts.value[0].remark, 'original')
    assert.equal(draft.remark, 'new note')
    assert.equal(s.quickErrors[12], '保存暂时失败')
    rejected = false
    await s.saveQuickAccount(row, 'settings')
    assert.equal(s.accounts.value[0].remark, 'new note')
    assert.equal(s.accounts.value[0].st, 'leave-st-alone')
    assert.equal(s.accounts.value[0].video_enabled, false)
})

test('account proxy labels keep credentials and copies intact while quick drafts survive folding', async () => {
    const copied = []
    const { state: s } = view('Accounts', {
        context: { navigator: { clipboard: { writeText: async (value) => copied.push(value) } } },
    })
    const proxy = {
        id: 8,
        note: 'fixture',
        proxy_url: 'socks5://user%40test:p%25%2Fa%3Ass@[2001:db8::1]:1080',
        bound_account_count: 1,
    }
    const row = { id: 4, proxy_pool_id: 8, proxy_url: proxy.proxy_url }
    s.proxyPool.value = [proxy]
    s.toggleQuickAccount(row)
    s.quickDraft(row).remark = 'unfinished edit'
    s.toggleQuickAccount(row)
    s.toggleQuickAccount(row)
    assert.equal(s.quickDraft(row).remark, 'unfinished edit')
    assert.match(s.proxyOptionLabel(proxy), /user%40test:p%25%2Fa%3Ass/)
    assert.match(s.accountProxySummary(row), /SOCKS5 \[2001:db8::1\]:1080/)
    await s.copy(proxy.proxy_url)
    await s.copy('user@test')
    await s.copy('p%/a:ss')
    assert.deepEqual(copied, [proxy.proxy_url, 'user@test', 'p%/a:ss'])
})

test('list capability toggles save only the clicked field, suppress duplicate writes and preserve other drafts', async () => {
    const response = deferred()
    const writes = []
    const fixture = view('Accounts', {
        api: {
            put: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return response.promise
            },
        },
    })
    const s = fixture.state
    const row = { id: 12, is_active: true, image_enabled: true, video_enabled: true, remark: 'original' }
    s.accounts.value = [row]
    s.vids.value = [{ token_id: 12, enabled: true, image_enabled: true, video_enabled: true }]
    s.quickDraft(row).remark = 'keep unsaved note'
    const operation = s.toggleAccountSetting(row, 'image_enabled', false)
    await s.toggleAccountSetting(row, 'image_enabled', false)
    await s.toggleAccountSetting(row, 'video_enabled', false)
    assert.deepEqual(writes, [{ path: '/api/tokens/12', body: { image_enabled: false } }])
    assert.equal(s.accountSettingBusy(row), true)
    assert.equal(s.accounts.value[0].image_enabled, true, 'Pending writes keep the last saved state')
    response.resolve({ success: true })
    await operation
    assert.equal(s.accounts.value[0].image_enabled, false)
    assert.equal(s.vids.value[0].image_enabled, false)
    assert.equal(s.quickDraft(row).image_enabled, false)
    assert.equal(s.quickDraft(row).remark, 'keep unsaved note')
    assert.equal(s.accounts.value[0].video_enabled, true)
    assert.equal(s.accountSettingBusy(row), false)
})

test('list status toggle uses validated enable and disable routes and retains state after rejection', async () => {
    const writes = []
    let rejectEnable = true
    const fixture = view('Accounts', {
        api: {
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return path.endsWith('/enable') && rejectEnable
                    ? { success: false, message: '请重新登录后再启用' }
                    : { success: true }
            },
        },
    })
    const s = fixture.state
    const row = { id: 4, is_active: true }
    s.accounts.value = [row]
    s.vids.value = [{ token_id: 4, enabled: true }]
    await s.toggleAccountSetting(row, 'is_active', false)
    assert.equal(s.accounts.value[0].is_active, false)
    assert.equal(s.vids.value[0].enabled, false)
    await s.toggleAccountSetting(s.accounts.value[0], 'is_active', true)
    assert.equal(s.accounts.value[0].is_active, false)
    assert.match(fixture.errors.at(-1).message, /重新登录/)
    assert.equal(s.accountSettingBusy(row), false)
    rejectEnable = false
    await s.toggleAccountSetting(s.accounts.value[0], 'is_active', true)
    assert.equal(s.accounts.value[0].is_active, true)
    assert.deepEqual(
        writes.map((write) => write.path),
        ['/api/tokens/4/disable', '/api/tokens/4/enable', '/api/tokens/4/enable'],
    )
    await s.toggleAccountSetting({ ...row, vidsOnly: true }, 'is_active', false)
    await s.toggleAccountSetting(row, 'proxy_url', false)
    assert.equal(writes.length, 3, 'Vids-only records and unrelated settings do not write through inline toggles')
})

test('list toggle waits out old polling and invalidates concurrent stale account reads', async () => {
    const oldRead = deferred()
    const write = deferred()
    const fixture = view('Accounts', {
        api: { get: accountReads([]), put: async () => write.promise },
    })
    const s = fixture.state
    const row = { id: 19, image_enabled: true }
    s.accounts.value = [row]
    fixture.api.get = async (path) =>
        path === '/api/tokens'
            ? oldRead.promise
            : path.includes('/vids/')
              ? { accounts: [] }
              : path.includes('/proxy/')
                ? { entries: [] }
                : { config: {} }
    const polling = s.load(true)
    const operation = s.toggleAccountSetting(row, 'image_enabled', false)
    oldRead.resolve([row])
    await polling
    await nextTick()
    write.resolve({ success: true })
    await operation
    assert.equal(s.accounts.value[0].image_enabled, false)
    assert.equal(s.accountSettingBusy(row), false)
})

test('account polling updates untouched quick fields while retaining pending edits', async () => {
    const { state: s } = view('Accounts')
    const account = { id: 17, proxy_pool_id: 1, remark: 'original', video_enabled: true }
    s.accounts.value = [account]
    await nextTick()
    const draft = s.quickDraft(account)
    draft.remark = 'pending edit'
    s.accounts.value = [{ ...account, proxy_pool_id: 2, remark: 'server remark', video_enabled: false }]
    await nextTick()
    assert.equal(draft.remark, 'pending edit')
    assert.equal(draft.proxy_pool_id, 2)
    assert.equal(draft.video_enabled, false)
    s.toggleQuickAccount(s.accounts.value[0])
    s.toggleQuickAccount(s.accounts.value[0])
    assert.equal(s.quickDraft(s.accounts.value[0]).remark, 'pending edit')
})

test('cookie account submission strategy saves independently of the login binding and retains failed choices', async () => {
    let fail = true
    const writes = [],
        confirmations = []
    const { state: s } = view('Accounts', {
        api: {
            put: async (path, body) => {
                writes.push({ path, body: plain(body) })
                if (fail) throw new Error('策略保存失败')
                return { success: true }
            },
        },
        context: { ElMessageBox: { confirm: async (...args) => confirmations.push(args) } },
    })
    const row = {
        id: 13,
        browser_profile_source: 'cookies',
        proxy_pool_id: 2,
        proxy_url: 'socks5h://fixed:password@login.fixture:1080',
    }
    s.accounts.value = [row]
    s.proxyPool.value = [{ id: 2, enabled: true, proxy_url: row.proxy_url, bound_account_count: 1 }]
    const draft = s.quickDraft(row)
    assert.equal(draft.submission_proxy_mode, 'fixed')
    draft.submission_proxy_mode = 'pool_random'
    await s.saveQuickAccount(row, 'strategy')
    assert.equal(s.quickErrors[13], '策略保存失败')
    assert.equal(draft.submission_proxy_mode, 'pool_random')
    assert.equal(s.accounts.value[0].submission_proxy_mode, undefined)
    fail = false
    await s.saveQuickAccount(row, 'strategy')
    assert.deepEqual(writes, [
        { path: '/api/tokens/13', body: { submission_proxy_mode: 'pool_random' } },
        { path: '/api/tokens/13', body: { submission_proxy_mode: 'pool_random' } },
    ])
    assert.equal(s.accounts.value[0].proxy_pool_id, 2)
    assert.equal(s.accounts.value[0].proxy_url, row.proxy_url)
    assert.equal(s.proxyPool.value[0].bound_account_count, 1)
    assert.equal(confirmations.length, 0)
    assert.match(s.accountProxySummary(s.accounts.value[0]), /生成 · 代理池随机 · 登录 #2/)
    assert.match(s.quickSaved[13], /代理池随机/)
})

test('polling synchronizes an untouched strategy without discarding an unsaved strategy choice', async () => {
    const { state: s } = view('Accounts')
    const row = { id: 22, submission_proxy_mode: 'fixed' }
    s.accounts.value = [row]
    const draft = s.quickDraft(row)
    s.accounts.value = [{ ...row, submission_proxy_mode: 'pool_random' }]
    await nextTick()
    assert.equal(draft.submission_proxy_mode, 'pool_random')
    draft.submission_proxy_mode = 'fixed'
    s.accounts.value = [{ ...row, submission_proxy_mode: 'pool_random', remark: 'updated' }]
    await nextTick()
    assert.equal(draft.submission_proxy_mode, 'fixed')
})

test('account binding permits canonical equivalents of historical Cloak proxy snapshots', () => {
    const { descriptor } = parse(
        readFileSync(new URL('../src/components/AccountProxyBinding.vue', import.meta.url), 'utf8'),
    )
    const executable =
        compileScript(descriptor, { id: 'account-proxy' })
            .content.replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"];?\s*$/gm, '')
            .replace('export default', 'const component =') + '\ncomponent'
    const component = runInNewContext(executable, { computed, URL })
    const account = { browser_profile_source: 'cloak', proxy_url: 'st5 Example.TEST:1080:user@test:p%/a:ss' }
    const s = component.setup({ account, draft: {}, proxies: [] }, { expose() {}, emit() {} })
    const target = { proxy_url: 'socks5://user%40test:p%25%2Fa%3Ass@Example.TEST:1080' }
    assert.equal(s.fixedExitMismatch(target), false)
    assert.equal(s.sameProxy('example.test:80:user:test', 'http://user:test@example.test:80'), true)
    assert.equal(s.sameProxy('https://user:p%3ass@EXAMPLE.test:443', 'https://user:p%3Ass@example.test:443'), false)
    assert.equal(s.sameProxy('socks5://user:pass@example.test', 'socks5://user:pass@example.test:1080'), false)
    assert.equal(s.fixedExitMismatch({ proxy_url: 'socks5://other:pass@example.test:1080' }), true)
    assert.equal(s.fixedExitMismatch({ proxy_url: 'invalid proxy' }), true)
    assert.equal(s.sameProxy('http://proxy.test:80', 'http://PROXY.test:80'), false)
    assert.equal(s.sameProxy('http://u:p%3ass@proxy.test:80', 'http://u:p%3Ass@proxy.test:80'), false)
})

test('a late proxy reload does not overwrite counts updated by quick binding', async () => {
    const write = deferred()
    const read = deferred()
    const { state: s } = view('Accounts', {
        api: { put: async () => write.promise, get: async () => read.promise },
    })
    const account = { id: 18, proxy_pool_id: 1 }
    const proxies = [
        { id: 1, enabled: true, proxy_url: 'socks5://first.test:1080', bound_account_count: 1 },
        { id: 2, enabled: true, proxy_url: 'socks5://second.test:1080', bound_account_count: 0 },
    ]
    s.accounts.value = [account]
    s.proxyPool.value = proxies.map((row) => ({ ...row }))
    s.quickDraft(account).proxy_pool_id = 2
    const binding = s.saveQuickAccount(account)
    const reload = s.reloadQuickProxies()
    write.resolve({ success: true })
    await binding
    read.resolve({ entries: proxies })
    await reload
    assert.deepEqual(plain(s.proxyPool.value.map((row) => row.bound_account_count)), [0, 1])
    assert.equal(s.accounts.value[0].proxy_pool_id, 2)
})

test('accounts sort the full filtered pool before slicing pages and bulk ignores Vids-only rows', async () => {
    const writes = []
    const accounts = [1, 2, 3, 4, 5, 6].map((id) => ({
        id,
        email: `member${id}@fixture.local`,
        credits: 10 * id,
        is_active: true,
    }))
    const { state: s } = view('Accounts', {
        api: {
            get: accountReads(accounts),
            post: async (path, body) => {
                writes.push({ path, body })
                return { success: true }
            },
        },
    })
    s.accounts.value = accounts
    s.pageSize.value = 2
    await nextTick()
    s.tableSort.order = 'descending'
    s.page.value = 2
    assert.deepEqual(plain(s.paginated.value.map((row) => row.id)), [4, 3])
    s.selected.value = [accounts[0], { id: 99, vidsOnly: true }, accounts[5]]
    await s.bulk('disable')
    assert.deepEqual(
        writes.map((row) => row.path),
        ['/api/tokens/1/disable', '/api/tokens/6/disable'],
    )
})

test('overlapping account polls share requests and a mutation reload waits for fresh state', async () => {
    const old = deferred()
    const requests = []
    let tokenReads = 0
    const { state: s } = view('Accounts', {
        api: {
            get: async (path) => {
                requests.push(path)
                if (path === '/api/tokens') return ++tokenReads === 1 ? old.promise : [{ id: 1, credits: 900 }]
                return path === '/api/token-refresh/config'
                    ? { config: { protocol_refresh_enabled: true } }
                    : { accounts: [] }
            },
        },
    })
    const first = s.load(true)
    const overlapping = s.load(true)
    assert.equal(requests.length, 5)
    const fresh = s.load(true, true)
    const otherFresh = s.load(true, true)
    old.resolve([{ id: 1, credits: 100 }])
    await Promise.all([first, overlapping, fresh, otherFresh])
    assert.equal(tokenReads, 2)
    assert.equal(s.accounts.value[0].credits, 900)
})

test('account quota polling paints cached balances first, merges requests and retains values on failure', async () => {
    const response = deferred()
    const reads = []
    let now = Date.now()
    const cached = [{ token_id: 1, enabled: true, ready: true, last_quota: { image: { available: 10, total: 100 } } }]
    const {
        state: s,
        api,
        notices,
    } = view('Accounts', {
        context: {
            Date: class extends Date {
                static now() {
                    return now
                }
            },
        },
        api: {
            get: async (path) => {
                reads.push(path)
                if (path.includes('?refresh=true')) return response.promise
                if (path === '/api/admin/vids/accounts/detail') return { accounts: cached }
                return accountReads([{ id: 1, credits: 900 }])(path)
            },
        },
    })
    await s.load()
    assert.equal(s.loading.value, false)
    assert.equal(s.rows.value[0].credits, 900)
    assert.equal(s.rows.value[0].vids.last_quota.image.available, 10)
    assert.equal(s.quotaRefreshing.value, true)
    const first = s.refreshVidsQuota()
    const second = s.refreshVidsQuota()
    assert.equal(reads.filter((path) => path.includes('?refresh=true')).length, 1)
    response.resolve({ accounts: [{ ...cached[0], last_quota: { image: { available: 8, total: 100 } } }] })
    await Promise.all([first, second])
    assert.equal(s.rows.value[0].vids.last_quota.image.available, 8)
    await s.load(true)
    const lastKnown = s.rows.value[0].vids.last_quota.image.available
    await s.refreshVidsQuota()
    assert.equal(reads.filter((path) => path.includes('?refresh=true')).length, 1)
    now += 15 * 60 * 1000
    api.get = async () => {
        throw new Error('额度连接暂时中断')
    }
    await s.refreshVidsQuota()
    assert.equal(s.rows.value[0].vids.last_quota.image.available, lastKnown)
    assert.equal(s.quotaInfo(s.rows.value[0]).label, '更新失败 · 保留上次额度')
    assert.match(s.quotaInfo(s.rows.value[0]).error, /额度连接/)
    assert.equal(s.quotaRefreshing.value, false)
    assert.equal(notices.length, 0)
})

test('dashboard quota requests share the 15 minute display refresh interval and pause when hidden', async () => {
    let now = Date.now()
    const reads = []
    const fixture = view('Dashboard', {
        context: {
            Date: class extends Date {
                static now() {
                    return now
                }
            },
            useRouter: () => ({ push() {} }),
        },
        api: {
            get: async (path) => {
                reads.push(path)
                return { configured_count: 0, quota: {} }
            },
        },
    })
    await fixture.state.refreshVidsQuota()
    now += 30 * 1000
    await fixture.state.refreshVidsQuota()
    assert.equal(reads.length, 1)
    now += 15 * 60 * 1000
    fixture.document.hidden = true
    await fixture.state.refreshVidsQuota()
    assert.equal(reads.length, 1)
    fixture.document.hidden = false
    await fixture.state.refreshVidsQuota()
    assert.equal(reads.length, 2)
    assert.ok(reads.every((path) => path === '/api/admin/vids/status?refresh=true'))
})

test('account quotas distinguish zero, unknown, stale and partial totals while skipping paused pages', async () => {
    let reads = 0
    const {
        state: s,
        cleanup,
        document,
    } = view('Accounts', {
        api: {
            get: async () => {
                reads++
                return { accounts: [] }
            },
        },
    })
    document.hidden = true
    await s.refreshVidsQuota()
    assert.equal(reads, 0)
    document.hidden = false
    s.editorOpen.value = true
    await s.refreshVidsQuota()
    assert.equal(reads, 0)
    s.editorOpen.value = false
    s.vids.value = [
        {
            token_id: 1,
            enabled: true,
            ready: true,
            last_quota: {
                image: { available: 0, total: 100, queried_at: '2026-10-15T09:00:00Z' },
                video: { available: 5, total: 10, queried_at: '2026-10-15T09:01:00Z', stale: true },
            },
        },
        { token_id: 2, enabled: true, ready: false },
        { token_id: 3, enabled: false, ready: true, last_quota: { image: { available: 500 } } },
    ]
    assert.equal(s.quotaValue(0), '0')
    for (const value of [null, undefined, '', ' ', -1, false, 'oops']) assert.equal(s.quotaValue(value), '—')
    assert.equal(s.quotaSummary('image').value, '0')
    assert.equal(s.quotaSummary('image').partial, true)
    assert.equal(s.quotaSummary('video').stale, true)
    s.vids.value[1].image_enabled = false
    assert.equal(s.quotaSummary('image').partial, false)
    assert.equal(s.quotaInfo(s.rows.value[0]).updated, '2026-10-15T09:00:00Z')
    assert.equal(s.quotaInfo(s.rows.value[0]).label, '历史额度 · 等待更新')
    assert.equal(s.quotaInfo(s.rows.value[2]).label, '已暂停自动更新')
    s.vids.value[0].last_quota.video.stale = false
    assert.equal(s.quotaInfo(s.rows.value[0]).label, '已自动同步')
    s.selectMobile(s.rows.value[0], true)
    s.selectMobile(s.rows.value[1], true)
    s.selectMobile(s.rows.value[0], false)
    assert.deepEqual(plain(s.selected.value.map((row) => row.id)), [2])
    for (const dispose of cleanup) dispose()
    await s.refreshVidsQuota()
    assert.equal(reads, 0)
})

test('account selection follows IDs across refreshes and uses current rows while pruning removed accounts', async () => {
    const { state: s } = view('Accounts')
    s.accounts.value = [{ id: 1, email: 'selected@example.test', image_enabled: true }, { id: 2 }]
    s.vids.value = [{ token_id: 3, email: 'vids-only@example.test', enabled: true }]
    s.updateAccountSelection([s.rows.value[0], s.rows.value[2]])
    await nextTick()
    const previous = [...s.selected.value]
    const removed = []
    s.accountsTable.value = {
        getSelectionRows: () => previous,
        toggleRowSelection: (row, checked) => removed.push([row.id, checked]),
    }
    s.accounts.value = [{ id: 1, email: 'selected@example.test', image_enabled: false }, { id: 2 }]
    s.vids.value = [{ token_id: 3, email: 'vids-only@example.test', enabled: false }]
    await nextTick()
    assert.deepEqual(plain(s.selected.value.map((row) => row.id)), [1, 3])
    assert.equal(s.selected.value[0].image_enabled, false)
    assert.equal(s.selected.value[1].is_active, false)
    s.accounts.value = [{ id: 2 }]
    await nextTick()
    assert.deepEqual(plain(s.selected.value.map((row) => row.id)), [3])
    assert.deepEqual(removed, [[1, false]])
    s.updateAccountSelection([])
    s.vids.value = [{ token_id: 3, enabled: true }]
    await nextTick()
    assert.equal(s.selected.value.length, 0)
})

test('account overview gives priority to paused, identity and Profile failures instead of a cached ready state', () => {
    const { state: s } = view('Accounts')
    assert.equal(s.vidsState(), '未接入')
    assert.equal(
        s.vidsState({ enabled: false, ready: true, identity_verified: false, session_state: 'ready' }),
        '已暂停',
    )
    assert.equal(
        s.vidsState({ enabled: true, ready: true, identity_verified: false, session_state: 'ready' }),
        '身份待核对',
    )
    assert.equal(s.vidsAttention({ enabled: false, ready: false }), false)
    assert.equal(s.vidsAttention({ enabled: true, ready: true, identity_verified: false }), true)
    assert.equal(s.sessionLabel({ browser_profile_error: 'Profile needs attention' }), '需要关注')
    assert.equal(s.profileLabel('ready'), '已就绪')
})

test('manual Vids quota refresh immediately preserves failed observations and reports the reason', async () => {
    const account = {
        token_id: 1,
        enabled: true,
        ready: true,
        last_quota: { image: { available: 19, total: 20, error: 'transport_failed', stale: true } },
    }
    const fixture = view('Accounts', { api: { post: async () => ({ account }) } })
    fixture.state.vids.value = [{ ...account, last_quota: {} }]
    await fixture.state.action({ id: 1 }, 'quota')
    assert.equal(fixture.state.vids.value[0].last_quota.image.available, 19)
    assert.equal(fixture.errors[0].message, '额度连接暂时中断')
    assert.equal(fixture.notices.length, 0)
    assert.equal(fixture.state.busy.size, 0)
})

test('legacy account imports queue their complete input without applying email-hinted settings in the browser', async () => {
    const writes = []
    const entries = [
        {
            email: 'hint@fixture.local',
            session_token: 'st-a',
            project_id: 'owned-project',
            remark: 'note',
            is_active: false,
        },
        { email: 'hint@fixture.local', session_token: 'rejected-st', project_id: 'other-project' },
    ]
    const { state: s } = view('Accounts', {
        api: {
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return { success: true, job: { id: 'legacy-job', state: 'queued', total: 2, items: [] } }
            },
        },
    })
    s.importText.value = JSON.stringify(entries)
    await s.importAccounts()
    assert.equal(writes.length, 1)
    assert.equal(writes[0].path, '/api/profile-transfer/jobs')
    assert.deepEqual(writes[0].body.bundles, entries)
    assert.equal(s.importJobs.value[0].id, 'legacy-job')
})

test('logs normalize SQLite UTC dates before range filters and sort globally', async () => {
    const { state: s } = view('Logs')
    s.logs.value = [
        { id: 1, created_at: '2026-10-14 02:00:00', duration: 10 },
        { id: 2, created_at: '2026-10-14T10:30:00+08:00', duration: 30 },
        { id: 3, created_at: '2026-10-14 03:00:00', duration: 20 },
    ]
    s.timeRange.value = [new Date('2026-10-14T02:00:00Z'), new Date('2026-10-14T02:45:00Z')]
    assert.deepEqual(plain(s.filtered.value.map((row) => row.id)), [1, 2])
    s.timeRange.value = null
    s.pageSize.value = 1
    await nextTick()
    s.tableSort.order = 'descending'
    s.page.value = 2
    assert.equal(s.paginated.value[0].id, 3)
})

test('late log details and pre-clear polls never overwrite the current selection or cleared history', async () => {
    const first = deferred()
    const second = deferred()
    const list = deferred()
    const { state: s } = view('Logs', {
        api: {
            get: (path) =>
                path === '/api/logs/1' ? first.promise : path === '/api/logs/2' ? second.promise : list.promise,
            delete: async () => ({ success: true }),
        },
    })
    const one = s.openDetail({ id: 1, updated_at: 'old' })
    const two = s.openDetail({ id: 2, updated_at: 'new' })
    second.resolve({ id: 2, updated_at: 'new' })
    await two
    first.resolve({ id: 1, updated_at: 'old' })
    await one
    assert.equal(s.detail.value.id, 2)
    const reading = s.load(true)
    await s.clearLogs()
    list.resolve([{ id: 3 }])
    await reading
    assert.equal(s.logs.value.length, 0)
    assert.equal(s.detailOpen.value, false)
})

test('invalid account input stops before API requests and rejected imports do not mutate local accounts', async () => {
    const fixture = view('Accounts')
    const s = fixture.state
    // Account creation is handled by Cloak login; the editor only updates an
    // existing member. Keep the validation check on that supported path.
    const account = { id: 7, email: 'member@fixture.local', st: 'synthetic-st' }
    s.openEditor(account)
    s.formRef.value = {
        validate: async () => {
            throw new Error('field required')
        },
    }
    await s.saveAccount()
    assert.equal(fixture.requests.length, 0)
    assert.equal(s.editorOpen.value, true)
    assert.equal(s.saving.value, false)
    s.accounts.value = [{ id: 1, email: 'owner@fixture.local', st: 'synthetic' }]
    s.importText.value = '[{"session_token":"rejected-synthetic"}]'
    await s.importAccounts()
    assert.equal(fixture.requests.length, 2)
    assert.equal(fixture.requests[0].path, '/api/profile-transfer/jobs')
    assert.equal(s.accounts.value.length, 1)
    assert.equal(s.importing.value, false)
    assert.match(fixture.errors.at(-1).message, /Unexpected fixture request/)
})

test('adding an account lets the user choose a proxy before creating Cloak', async () => {
    const fixture = view('Accounts', {
        context: { window: { open: () => assert.fail('The picker must not open Cloak') } },
        api: {
            get: async (path) => {
                assert.equal(path, '/api/proxy/pool')
                return {
                    entries: [
                        { id: 17, enabled: true, note: '住宅线路', proxy_url: 'socks5://residential.fixture:1080' },
                    ],
                }
            },
        },
    })
    await fixture.state.openCloakLogin()
    assert.equal(fixture.state.loginOpen.value, true)
    assert.equal(fixture.state.loginStarting.value, false)
    assert.equal(fixture.state.loginSession.value, null)
    assert.equal(fixture.state.availableProxies.value[0].id, 17)
    assert.equal(fixture.requests.length, 0)
})

for (const runtime of ['native', 'docker']) {
    test(`${runtime} desktop sends extension login to its actual local management address without a WebView handshake`, async () => {
        const bridge = []
        const clipboard = []
        const browserWindow = {
            lay4flowDesktop: { runtime, version: '1.5.4' },
            postMessage: (message) => bridge.push(message),
        }
        const fixture = view('Accounts', {
            context: {
                window: browserWindow,
                location: { origin: 'http://127.0.0.1:48127' },
                navigator: { clipboard: { writeText: async (text) => clipboard.push(text) } },
            },
        })
        const s = fixture.state
        assert.equal(s.desktopShell.value, true)
        assert.equal(s.nativeDesktop.value, runtime === 'native')
        assert.equal(s.desktopManagementUrl.value, 'http://127.0.0.1:48127/#/accounts')
        s.openExtensionLogin()
        s.detectExtension()
        s.extensionMessage('import.hello')
        s.extensionReady.value = true
        await s.startExtensionLogin()
        assert.equal(s.extensionOpen.value, true)
        assert.equal(s.extensionDetecting.value, false)
        assert.equal(s.extensionSession.value, null)
        assert.deepEqual(bridge, [])
        assert.deepEqual(fixture.requests, [])
        await s.copy(s.desktopManagementUrl.value)
        assert.deepEqual(clipboard, ['http://127.0.0.1:48127/#/accounts'])
        s.extensionReady.value = false
        s.receiveExtensionMessage({
            source: browserWindow,
            origin: 'http://127.0.0.1:48127',
            data: {
                source: 'lay4flow-import-extension',
                type: 'import.ready',
                extension_id: 'unexpected-in-webview',
                version: '1.8.0',
            },
        })
        assert.equal(s.extensionReady.value, false)
    })
}

test('extension detection reports silence and releases its timeout when the view closes', () => {
    const timers = new Map()
    let nextTimer = 0
    const fixture = view('Accounts', {
        context: {
            setTimeout: (callback) => {
                timers.set(++nextTimer, callback)
                return nextTimer
            },
            clearTimeout: (id) => timers.delete(id),
            window: { postMessage() {}, removeEventListener() {} },
        },
    })
    const s = fixture.state
    s.detectExtension()
    assert.equal(s.extensionDetecting.value, true)
    assert.match(s.extensionDetectionStatus.value, /正在检测/)
    assert.equal(timers.size, 1)
    const callback = [...timers.values()][0]
    timers.clear()
    callback()
    assert.equal(s.extensionDetecting.value, false)
    assert.equal(s.extensionReady.value, false)
    assert.match(s.extensionDetectionStatus.value, /未检测到扩展/)
    assert.match(s.extensionDetectionStatus.value, /1\.7\.3/)
    assert.match(s.extensionDetectionStatus.value, /刷新当前管理页/)
    s.detectExtension()
    assert.equal(timers.size, 1)
    for (const cleanup of fixture.cleanup) cleanup()
    assert.equal(timers.size, 0)
})

test('extension detection identifies an incompatible bridge and accepts a compatible extension installed alongside it', () => {
    const browserWindow = { postMessage() {} }
    const fixture = view('Accounts', { context: { window: browserWindow } })
    const s = fixture.state
    const ready = (extension_id, version) =>
        s.receiveExtensionMessage({
            source: browserWindow,
            origin: 'http://fixture.local',
            data: { source: 'lay4flow-import-extension', type: 'import.ready', extension_id, version },
        })
    s.detectExtension()
    ready('old', '1.7.3')
    assert.equal(s.extensionReady.value, false)
    assert.equal(s.extensionId.value, '')
    assert.match(s.extensionDetectionStatus.value, /旧扩展 1\.7\.3/)
    assert.match(s.extensionDetectionStatus.value, /停用旧扩展/)
    ready('current', '1.8.1')
    assert.equal(s.extensionReady.value, true)
    assert.equal(s.extensionVersion.value, '1.8.1')
    assert.equal(s.extensionId.value, 'current')
    assert.equal(s.extensionDetectionStatus.value, '')
    ready('old', '1.7.3')
    assert.equal(s.extensionReady.value, true)
    assert.equal(s.extensionId.value, 'current')
    s.extensionSession.value = { session_id: 'owned-session' }
    ready('another', '1.9.0')
    assert.equal(s.extensionId.value, 'current', 'an active login remains bound to its extension')
})

test('extension login uses the fingerprint browser network and keeps only a scoped receipt in browser storage', async () => {
    const posts = []
    const bridge = []
    const browserWindow = { postMessage: (message, origin) => bridge.push({ message: plain(message), origin }) }
    const fixture = view('Accounts', {
        context: { window: browserWindow },
        api: {
            post: async (path, body) => {
                posts.push({ path, body: plain(body) })
                return {
                    success: true,
                    session_id: 'extension-session',
                    nonce: 'never-persist-this-nonce',
                    expires_at: Date.now() / 1000 + 300,
                    complete_url: 'http://fixture.local/api/plugin/import/complete',
                    return_url: 'http://fixture.local/manage#/accounts',
                }
            },
        },
    })
    const s = fixture.state
    await s.openExtensionLogin()
    assert.equal(s.extensionOpen.value, true)
    assert.equal(fixture.requests.length, 0, 'extension login does not load the server proxy pool')
    bridge.length = 0
    s.receiveExtensionMessage({
        source: browserWindow,
        origin: 'http://fixture.local',
        data: {
            source: 'lay4flow-import-extension',
            type: 'import.ready',
            extension_id: 'fixture-extension',
            version: '1.8.0',
        },
    })
    await s.startExtensionLogin()
    assert.deepEqual(posts, [{ path: '/api/plugin/import/start', body: {} }])
    assert.equal(bridge[0].origin, 'http://fixture.local')
    assert.equal(bridge[0].message.type, 'import.start.v2')
    assert.equal(bridge[0].message.payload.login_context, 'profile')
    assert.equal(bridge[0].message.payload.nonce, 'never-persist-this-nonce')
    const stored = JSON.parse(fixture.storage.get('lay4flow.extensionLogin'))
    assert.equal(stored.session_id, 'extension-session')
    assert.equal(stored.extension_id, 'fixture-extension')
    assert.equal('nonce' in stored, false)
    assert.equal('cookies' in stored, false)
    s.cancelExtensionLogin()
    assert.equal(bridge[1].message.type, 'import.cancel.v2')
    assert.equal(s.extensionSession.value.session_id, 'extension-session')
    s.receiveExtensionMessage({
        source: browserWindow,
        origin: 'http://fixture.local',
        data: {
            source: 'lay4flow-import-extension',
            extension_id: 'fixture-extension',
            type: 'import.error',
            session_id: 'extension-session',
            error_code: 'cancelled',
            error_summary: '已取消扩展登录',
        },
    })
    assert.equal(s.extensionSession.value, null)
    assert.equal(fixture.storage.has('lay4flow.extensionLogin'), false)
})

test('extension bridge ignores foreign origins, extensions and stale sessions before accepting account completion', async () => {
    const browserWindow = {}
    const fixture = view('Accounts', { context: { window: browserWindow }, api: { get: accountReads([]) } })
    const s = fixture.state
    const ready = { source: 'lay4flow-import-extension', type: 'import.ready', extension_id: 'owned', version: '1.8.0' }
    s.receiveExtensionMessage({ source: browserWindow, origin: 'https://foreign.fixture', data: ready })
    assert.equal(s.extensionReady.value, false)
    s.receiveExtensionMessage({ source: browserWindow, origin: 'http://fixture.local', data: ready })
    s.extensionSession.value = { session_id: 'current' }
    const completion = {
        ...ready,
        type: 'import.result',
        session_id: 'current',
        success: true,
        result: { email: 'verified@fixture.local' },
    }
    s.receiveExtensionMessage({
        source: browserWindow,
        origin: 'http://fixture.local',
        data: { ...completion, extension_id: 'foreign' },
    })
    s.receiveExtensionMessage({
        source: browserWindow,
        origin: 'http://fixture.local',
        data: { ...completion, session_id: 'stale' },
    })
    assert.equal(s.extensionSession.value.session_id, 'current')
    assert.equal(fixture.notices.length, 0)
    s.receiveExtensionMessage({ source: browserWindow, origin: 'http://fixture.local', data: completion })
    assert.equal(s.extensionSession.value, null)
    assert.match(s.extensionStatus.value, /verified@fixture.local/)
    assert.equal(fixture.notices.length, 1)
})

test('Cloak login opens without an email and automatically completes from its scoped status', async () => {
    const writes = []
    const reads = []
    const viewer = {
        document: { body: {} },
        location: {},
        close() {
            this.closed = true
        },
    }
    const receipt = {
        success: true,
        session_id: 'fixture-session',
        token_id: 71,
        viewer_id: 'fixture-viewer',
        viewer_url: '/api/profile-browser/71',
        expires_at: '2099-01-01T00:00:00Z',
    }
    let status = { success: true, status: 'waiting', message: '等待登录' }
    const fixture = view('Accounts', {
        context: { window: { open: () => viewer } },
        api: {
            get: async (path) => {
                reads.push(path)
                if (path.startsWith('/api/profile-login/')) return status
                return accountReads([{ id: 71, email: 'direct@fixture.local', browser_profile_source: 'cloak' }])(path)
            },
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return receipt
            },
        },
    })
    const s = fixture.state
    s.loginForm.proxy_pool_id = 17
    await s.startLogin()
    await s.pollLoginStatus()
    assert.deepEqual(writes, [{ path: '/api/profile-login', body: { proxy_pool_id: 17 } }])
    assert.equal(
        viewer.location.href,
        'http://fixture.local/api/profile-browser/71?viewer_id=fixture-viewer&login_session=fixture-session',
    )
    assert.equal(viewer.opener, null)
    const stored = JSON.parse(fixture.storage.get('lay4flow.profileLogin'))
    assert.equal(stored.session_id, 'fixture-session')
    assert.equal(stored.email, '')
    assert.equal('cookies' in stored, false)
    assert.equal('proxy_url' in stored, false)
    assert.equal(s.loginSession.value.session_id, 'fixture-session')
    status = {
        success: true,
        status: 'ready',
        result: { token_id: 71, email: 'direct@fixture.local', is_active: true },
    }
    await s.pollLoginStatus()
    assert.equal(s.loginSession.value, null)
    assert.equal(s.loginOpen.value, false)
    assert.equal(fixture.storage.has('lay4flow.profileLogin'), false)
    assert.equal(s.accounts.value[0].id, 71)
    assert.ok(reads.includes('/api/profile-login/fixture-session?viewer_id=fixture-viewer'))
    assert.equal(
        writes.some(({ path }) => path.includes('/complete')),
        false,
    )
})

test('cancelled login ignores a late ready response and clears its automatic poll', async () => {
    const pending = deferred()
    const fixture = view('Accounts', {
        api: { get: async () => pending.promise, delete: async () => ({ success: true }) },
    })
    const s = fixture.state
    s.loginSession.value = { session_id: 'cancelled-login', viewer_id: 'owned', token_id: 71 }
    const check = s.pollLoginStatus()
    await s.cancelLogin()
    pending.resolve({ success: true, status: 'ready', result: { email: 'late@fixture.local' } })
    await check
    assert.equal(s.loginSession.value, null)
    assert.equal(s.loginStatus.value, '')
    assert.equal(fixture.notices.length, 0)
})

test('Cloak login rejects foreign viewers and cancellation preserves receipt until server confirms', async () => {
    const fixture = view('Accounts')
    const s = fixture.state
    assert.throws(() =>
        s.loginViewerUrl({ token_id: 1, viewer_id: 'v', viewer_url: 'https://other.test/api/profile-browser/1' }),
    )
    assert.throws(() => s.loginViewerUrl({ token_id: 1, viewer_id: 'v', viewer_url: '/api/profile-browser/2' }))
    s.loginSession.value = { session_id: 's/1', viewer_id: 'v 1', token_id: 1 }
    await s.cancelLogin()
    assert.equal(s.loginSession.value.session_id, 's/1')
    assert.equal(fixture.requests[0].path, '/api/profile-login/s%2F1?viewer_id=v%201')
    fixture.api.delete = async () => ({ success: true })
    await s.cancelLogin()
    assert.equal(s.loginSession.value, null)
})

test('automatic login errors stay inline and a closed viewer resumes the same scoped session', async () => {
    const writes = []
    const failure = {
        success: true,
        status: 'error',
        reason: 'transient_error',
        message: '登录态验证暂时失败，请继续登录',
        browser_open: false,
        retryable: true,
    }
    const fixture = view('Accounts', {
        context: { window: { lay4flowDesktop: { runtime: 'native' } } },
        api: {
            get: async (path) => (path.startsWith('/api/profile-login/') ? failure : accountReads([])(path)),
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return {
                    success: true,
                    session_id: 'retry-login',
                    viewer_id: 'new-owner',
                    token_id: 71,
                    presentation: 'native',
                }
            },
        },
    })
    const s = fixture.state
    const receipt = { session_id: 'retry-login', viewer_id: 'old-owner', token_id: 71, presentation: 'native' }
    s.loginSession.value = receipt
    await s.pollLoginStatus()
    assert.equal(s.loginFailure.value.browser_open, false)
    assert.match(s.loginStatus.value, /验证暂时失败/)
    assert.equal(fixture.errors.length, 0)
    assert.deepEqual(JSON.parse(fixture.storage.get('lay4flow.profileLogin')), { ...receipt, browser_open: false })
    await s.retryLogin()
    assert.deepEqual(writes[0], { path: '/api/profile-login/retry-login/retry', body: { viewer_id: 'old-owner' } })
    assert.equal(s.loginSession.value.session_id, 'retry-login')
    assert.equal(s.loginSession.value.viewer_id, 'new-owner')
    assert.equal(
        writes.some(({ path }) => path === '/api/profile-login'),
        false,
    )
})

test('native Cloak login opens no web popup and resumes the owner-bound local window', async () => {
    const writes = []
    const fixture = view('Accounts', {
        context: {
            window: {
                lay4flowDesktop: { runtime: 'native' },
                open: () => assert.fail('Native login opened a web popup'),
            },
        },
        api: {
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return path === '/api/profile-login'
                    ? {
                          success: true,
                          session_id: 'native-session',
                          token_id: 71,
                          viewer_id: 'native viewer',
                          presentation: 'native',
                      }
                    : { success: true, status: 'ready', presentation: 'native' }
            },
        },
    })
    const s = fixture.state
    s.loginForm.email = 'native@fixture.local'
    await s.startLogin()
    assert.match(s.loginStatus.value, /本地 Cloak/)
    assert.equal(s.loginSession.value.presentation, 'native')
    s.resumeLogin()
    await Promise.resolve()
    assert.equal(writes[1].path, '/api/tokens/71/browser/focus?viewer_id=native%20viewer')
    const stored = JSON.parse(fixture.storage.get('lay4flow.profileLogin'))
    assert.equal(stored.presentation, 'native')
    assert.equal('cookies' in stored, false)
})

test('native account browser controls stay inline and serialize focus, refresh and close', async () => {
    const writes = []
    const pending = deferred()
    let closeFails = true
    const fixture = view('Accounts', {
        context: {
            window: {
                lay4flowDesktop: { runtime: 'native' },
                open: () => assert.fail('Native viewer opened a web popup'),
            },
        },
        api: {
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                if (path.endsWith('/browser'))
                    return { success: true, viewer_id: 'owned id', status: 'ready', presentation: 'native' }
                if (path.includes('/focus')) return pending.promise
                return { success: true, status: 'ready' }
            },
            delete: async (path) => {
                writes.push({ path })
                if (closeFails) throw new Error('Synthetic close failed')
                return { success: true, status: 'closed' }
            },
        },
    })
    const s = fixture.state
    await s.openBrowser({ id: 71, email: 'native@fixture.local' })
    assert.equal(s.nativeBrowser.value.tokenId, 71)
    const focusing = s.focusNativeBrowser()
    await s.refreshNativeBrowser()
    await s.closeNativeBrowser(true)
    assert.equal(writes.length, 2)
    pending.resolve({ success: true, status: 'ready' })
    await focusing
    await s.refreshNativeBrowser()
    assert.equal(writes[2].path, '/api/tokens/71/browser/refresh?viewer_id=owned%20id')
    await s.closeNativeBrowser(true)
    assert.equal(s.nativeBrowser.value.tokenId, 71, 'Failed close retains inline controls for retry')
    assert.equal(writes.filter(({ path }) => path.endsWith('/refresh-at')).length, 0)
    closeFails = false
    await s.closeNativeBrowser(true)
    assert.equal(s.nativeBrowser.value, null)
    assert.equal(writes.at(-1).path, '/api/tokens/71/refresh-at')
})

test('accounts detect a native backend when opened in a normal browser', async () => {
    const fixture = view('Accounts', {
        api: { get: async (path) => ({ viewer_presentation: path === '/health' ? 'native' : 'vnc' }) },
    })
    await fixture.state.detectNativePresentation()
    assert.equal(fixture.state.nativePresentation.value, true)
})

test('Cloak login resumes a future Unix-seconds receipt after reloading without a fresh login request', async () => {
    const fixture = view('Accounts', {
        context: {
            document: { hidden: false, addEventListener() {}, removeEventListener() {} },
        },
        api: { get: accountReads([]) },
    })
    fixture.storage.set(
        'lay4flow.profileLogin',
        JSON.stringify({
            session_id: 'resumable',
            token_id: 71,
            viewer_id: 'owned',
            viewer_url: '/api/profile-browser/71',
            email: 'resume@fixture.local',
            expires_at: Date.now() / 1000 + 600,
        }),
    )
    for (const mount of fixture.mounted) mount()
    assert.equal(fixture.state.loginSession.value.session_id, 'resumable')
    assert.equal(fixture.state.loginSession.value.email, 'resume@fixture.local')
    assert.equal(fixture.requests.length, 0)
})

test('Profile import submits one background job and releases the modal without waiting for account validation', async () => {
    const accepted = deferred()
    const writes = []
    const events = []
    const bundles = ['first', 'second'].map((name) => ({
        format: 'lay4flow-cloak-session',
        version: 1,
        account: { email: `${name}@fixture.local` },
        storage_state: { cookies: [{ name: 'synthetic-cookie', value: 'synthetic-secret' }], origins: [] },
    }))
    const fixture = view('Accounts', {
        context: { window: { dispatchEvent: (event) => events.push(event) } },
        api: {
            post: (path, body) => {
                writes.push({ path, body: plain(body) })
                return accepted.promise
            },
        },
    })
    const s = fixture.state
    const source = JSON.stringify({ format: 'lay4flow-profile-bundle', version: 1, accounts: bundles })
    s.importText.value = source
    s.importProxy.value = 17
    s.importOpen.value = true
    const submitting = s.importAccounts()
    assert.equal(s.importing.value, true)
    let closed = false
    s.closeImport(() => {
        closed = true
    })
    assert.equal(closed, true)
    await s.importAccounts()
    assert.equal(writes.length, 1)
    assert.equal(writes[0].path, '/api/profile-transfer/jobs')
    assert.deepEqual(writes[0].body.bundles, bundles)
    assert.equal(writes[0].body.proxy_pool_id, 17)
    assert.equal(writes[0].body.request_id, 'request-fixture-id')
    accepted.resolve({
        success: true,
        job: {
            id: 'job-1',
            request_id: 'request-fixture-id',
            state: 'queued',
            total: 2,
            added: 0,
            failed: 0,
            items: bundles.map((bundle) => ({ email: bundle.account.email, state: 'queued', stage: 'queued' })),
        },
    })
    await submitting
    assert.equal(s.importing.value, false)
    assert.equal(s.importOpen.value, false)
    assert.equal(s.importText.value, source)
    assert.equal(s.importJobs.value[0].state, 'queued')
    assert.equal(fixture.errors.length, 0)
    assert.equal(s.sameImportSubmitted.value, true)
    await s.importAccounts()
    assert.equal(writes.length, 1)
    assert.equal(events.at(-1).type, 'lay4flow:profile-import-jobs')
    assert.equal(events.at(-1).detail.jobs[0].id, 'job-1')
    assert.equal(fixture.storage.size, 0)
})

test('background imports restore after navigation, show server stages and retry failed items without resending credentials', async () => {
    const writes = []
    let tick
    let job = {
        id: 'job-restored',
        state: 'running',
        total: 2,
        added: 0,
        failed: 0,
        can_retry: false,
        items: [
            { email: 'first@fixture.local', state: 'running', stage: 'cold_check_1', elapsed_seconds: 13 },
            { email: 'second@fixture.local', state: 'queued', stage: 'queued' },
        ],
    }
    const fixture = view('Accounts', {
        context: {
            setInterval: (callback) => {
                tick = callback
                return 1
            },
        },
        api: {
            get: async (path) =>
                path === '/api/profile-transfer/jobs' ? { success: true, jobs: [plain(job)] } : accountReads([])(path),
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                return { success: true, job: { ...job, state: 'queued', can_retry: false } }
            },
        },
    })
    const s = fixture.state
    await s.refreshImportJobs()
    assert.equal(s.activeImportJobs.value.length, 1)
    assert.match(s.importStageLabel(s.importJobs.value[0].items[0]), /1\/2/)
    assert.equal(s.importJobProgress(s.importJobs.value[0]), '0 / 2 \u9879')
    assert.equal(typeof tick, 'function')
    job = {
        ...job,
        state: 'completed',
        added: 1,
        failed: 1,
        can_retry: true,
        items: [
            { email: 'first@fixture.local', state: 'succeeded', stage: 'completed' },
            {
                email: 'second@fixture.local',
                state: 'failed',
                stage: 'flow_verification',
                reason: 'proxy_timeout',
                network_step: 'flow_identity',
                message: 'proxy fixture timed out; profile retained',
            },
        ],
    }
    tick()
    await new Promise(setImmediate)
    assert.equal(s.activeImportJobs.value.length, 0)
    assert.equal(s.importJobs.value[0].items[1].message, job.items[1].message)
    assert.equal(s.importStageLabel(s.importJobs.value[0].items[1]), '验证 Flow 会话失败')
    assert.equal(s.loginNetworkStepLabel(s.importJobs.value[0].items[1].network_step), '校验 Google 账号身份')
    assert.equal(s.importJobProgress(s.importJobs.value[0]), '2 / 2 \u9879')
    assert.equal(fixture.notices.at(-1).type, 'warning')
    await s.retryImportJob(s.importJobs.value[0])
    assert.deepEqual(writes, [{ path: '/api/profile-transfer/jobs/job-restored/retry', body: {} }])
    assert.equal(s.importJobs.value[0].state, 'queued')
    assert.equal(fixture.storage.size, 0)
    for (const dispose of fixture.cleanup) dispose()
    const before = s.importJobs.value
    await s.refreshImportJobs()
    assert.equal(s.importJobs.value, before)
})

test('a lost job response recovers by request ID and an ambiguous resubmit reuses the same receipt', async () => {
    const writes = []
    let jobs = []
    const fixture = view('Accounts', {
        api: {
            get: async () => ({ success: true, jobs }),
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                if (writes.length === 1) throw new Error('connection interrupted')
                jobs = [{ id: 'accepted-job', request_id: body.request_id, state: 'running', total: 1, items: [] }]
                throw new Error('accepted response interrupted')
            },
        },
    })
    const s = fixture.state
    const source = JSON.stringify({
        format: 'lay4flow-cloak-session',
        version: 1,
        account: { email: 'first@fixture.local' },
    })
    s.importText.value = source
    await s.importAccounts()
    assert.equal(s.importing.value, false)
    assert.equal(s.importText.value, source)
    assert.equal(fixture.errors.length, 1)
    await s.importAccounts()
    assert.equal(writes.length, 2)
    assert.equal(writes[0].body.request_id, writes[1].body.request_id)
    assert.equal(s.importJobs.value[0].id, 'accepted-job')
    assert.equal(s.sameImportSubmitted.value, true)
    assert.equal(fixture.errors.length, 1)
})

test('a stale list response never removes a newly accepted import job and polling failures retain progress', async () => {
    const stale = deferred()
    let reject = false
    const fixture = view('Accounts', {
        api: {
            get: async () => {
                if (reject) throw new Error('temporary disconnect')
                return stale.promise
            },
        },
    })
    const s = fixture.state
    const reading = s.refreshImportJobs()
    s.rememberImportJob({ id: 'fresh-job', state: 'running', total: 1, items: [] })
    stale.resolve({ success: true, jobs: [] })
    await reading
    assert.equal(s.importJobs.value[0].id, 'fresh-job')
    reject = true
    await s.refreshImportJobs()
    assert.equal(s.importJobs.value[0].id, 'fresh-job')
    assert.match(s.importJobsError.value, /temporary disconnect/)
    assert.equal(s.importJobsLoading.value, false)
})

test('expired task history releases the same input for resubmission after a service restart', async () => {
    const fixture = view('Accounts', {
        api: { get: async () => ({ success: true, jobs: [] }) },
    })
    const s = fixture.state
    s.importText.value = 'retained JSON fixture'
    s.importSubmitted.value = { id: 'old-task', text: s.importText.value, proxy: null }
    assert.equal(s.sameImportSubmitted.value, true)
    await s.refreshImportJobs()
    assert.equal(s.importSubmitted.value, null)
    assert.equal(s.sameImportSubmitted.value, false)
    assert.equal(s.importText.value, 'retained JSON fixture')
    assert.match(s.importJobsError.value, /服务已重启/)
})

test('background import also submits from an HTTP deployment without crypto.randomUUID', async () => {
    let requestId
    const fixture = view('Accounts', {
        context: { crypto: {} },
        api: {
            post: async (path, body) => {
                requestId = body.request_id
                return { success: true, job: { id: 'http-job', state: 'queued', total: 1, items: [] } }
            },
        },
    })
    fixture.state.importText.value = '[{"session_token":"synthetic"}]'
    await fixture.state.importAccounts()
    assert.match(requestId, /^import-\d+-[a-z0-9]+$/)
    assert.equal(fixture.errors.length, 0)
})

test('Cloak JSON export downloads verified backend bundles rather than row credentials', async () => {
    let downloaded
    const bundle = { format: 'lay4flow-cloak-session', version: 1, storage_state: { cookies: [], origins: [] } }
    const reads = []
    const fixture = view('Accounts', {
        context: {
            URL: {
                createObjectURL: (blob) => {
                    downloaded = blob
                    return 'blob:fixture'
                },
                revokeObjectURL() {},
            },
            document: { createElement: () => ({ click() {} }) },
        },
        api: {
            get: async (path) => {
                reads.push(path)
                return { success: true, bundle }
            },
        },
    })
    fixture.state.accounts.value = [
        {
            id: 8,
            email: 'profile@fixture.local',
            browser_profile_source: 'cloak',
            st: 'never-export-row-secret',
            at: 'never-export-at',
        },
    ]
    await fixture.state.exportAccounts()
    assert.deepEqual(reads, ['/api/tokens/8/profile-export'])
    const result = JSON.parse(await downloaded.text())
    assert.equal(result.format, 'lay4flow-profile-bundle')
    assert.deepEqual(result.accounts, [bundle])
    assert.doesNotMatch(JSON.stringify(result), /never-export/)
    assert.equal(fixture.state.exporting.value, false)
})

test('malformed stored payloads keep log details readable and reject executable media URLs', () => {
    const { state: s } = view('Logs')
    s.detail.value = {
        operation: 'generate_image',
        request_body: '{"messages":{"invalid":true}}',
        response_body: '{"data":[null,{"url":"javascript:alert(1)"},{"url":"/tmp/synthetic.png"}]}',
    }
    assert.equal(s.requestPrompt.value, null)
    assert.equal(s.media.value.length, 1)
    assert.equal(s.media.value[0].url, 'http://fixture.local/tmp/synthetic.png')
})
