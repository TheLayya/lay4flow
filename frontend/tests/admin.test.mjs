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
    assert.equal(s.matchingCards.value.length, 9)
    assert.equal(s.dirtyCount.value, 3)
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

test('submission strategy saves independently of the login binding and retains failed choices', async () => {
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
        browser_profile_source: 'cloak',
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
    const cached = [{ token_id: 1, enabled: true, ready: true, last_quota: { image: { available: 10, total: 100 } } }]
    const {
        state: s,
        api,
        notices,
    } = view('Accounts', {
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
    api.get = async () => {
        throw new Error('额度连接暂时中断')
    }
    await s.refreshVidsQuota()
    assert.equal(s.rows.value[0].vids.last_quota.image.available, 8)
    assert.equal(s.quotaInfo(s.rows.value[0]).label, '更新失败 · 保留上次额度')
    assert.match(s.quotaInfo(s.rows.value[0]).error, /额度连接/)
    assert.equal(s.quotaRefreshing.value, false)
    assert.equal(notices.length, 0)
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
    s.openEditor()
    s.formRef.value = {
        validate: async () => {
            throw new Error('field required')
        },
    }
    await s.saveAccount()
    assert.equal(fixture.requests.length, 0)
    s.accounts.value = [{ id: 1, email: 'owner@fixture.local', st: 'synthetic' }]
    s.importText.value = '[{"session_token":"rejected-synthetic"}]'
    await s.importAccounts()
    assert.equal(fixture.requests.length, 2)
    assert.equal(fixture.requests[0].path, '/api/profile-transfer/jobs')
    assert.equal(s.accounts.value.length, 1)
    assert.equal(s.importing.value, false)
    assert.match(fixture.errors.at(-1).message, /Unexpected fixture request/)
})

test('Cloak login opens directly, persists only its receipt, keeps validation failures resumable and completes', async () => {
    const writes = []
    const viewer = {
        document: { body: {} },
        location: {},
        close() {
            this.closed = true
        },
    }
    let rejectComplete = true
    const receipt = {
        success: true,
        session_id: 'fixture-session',
        token_id: 71,
        viewer_id: 'fixture-viewer',
        viewer_url: '/api/profile-browser/71',
        expires_at: '2099-01-01T00:00:00Z',
    }
    const fixture = view('Accounts', {
        context: { window: { open: () => viewer } },
        api: {
            get: accountReads([{ id: 71, email: 'direct@fixture.local', browser_profile_source: 'cloak' }]),
            post: async (path, body) => {
                writes.push({ path, body: plain(body) })
                if (path === '/api/profile-login') return receipt
                if (rejectComplete) throw new Error('Finish Google login first')
                return { success: true, token_id: 71, email: 'direct@fixture.local', is_active: true }
            },
        },
    })
    const s = fixture.state
    s.loginForm.email = 'direct@fixture.local'
    s.loginForm.proxy_pool_id = 17
    await s.startLogin()
    assert.equal(writes[0].path, '/api/profile-login')
    assert.equal(writes[0].body.proxy_pool_id, 17)
    assert.equal(viewer.location.href, 'http://fixture.local/api/profile-browser/71?viewer_id=fixture-viewer')
    assert.equal(viewer.opener, null)
    const stored = JSON.parse(fixture.storage.get('lay4flow.profileLogin'))
    assert.equal(stored.session_id, 'fixture-session')
    assert.equal('proxy_url' in stored, false)
    assert.equal('cookies' in stored, false)
    await s.completeLogin()
    assert.equal(s.loginSession.value.session_id, 'fixture-session')
    assert.equal(s.loginFinishing.value, false)
    rejectComplete = false
    await s.completeLogin()
    assert.equal(s.loginSession.value, null)
    assert.equal(fixture.storage.has('lay4flow.profileLogin'), false)
    assert.equal(s.accounts.value[0].id, 71)
    assert.deepEqual(writes[1].body, { viewer_id: 'fixture-viewer' })
    assert.ok(writes.every(({ path }) => !path.includes('/plugin/')))
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
    assert.equal(fixture.state.loginForm.email, 'resume@fixture.local')
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
                stage: 'oauth_renewal',
                reason: 'proxy_timeout',
                message: 'proxy fixture timed out; profile retained',
            },
        ],
    }
    tick()
    await new Promise(setImmediate)
    assert.equal(s.activeImportJobs.value.length, 0)
    assert.equal(s.importJobs.value[0].items[1].message, job.items[1].message)
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
