import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// Actual account table, local synthetic API only. No real account or provider.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/account-limit-visibility')
await mkdir(output, { recursive: true })
const script = `import {createApp,h} from 'vue';
import {createRouter,createWebHashHistory,RouterView} from 'vue-router';
import Accounts from '/src/views/AccountsView.vue';
import 'element-plus/theme-chalk/index.css';import '/src/styles/theme.css';
const router=createRouter({history:createWebHashHistory(),routes:[{path:'/:pathMatch(.*)*',component:Accounts}]});
createApp({render:()=>h(RouterView)}).use(router).mount('#app');`
const server = await createServer({
    configFile: false, root: frontend, logLevel: 'error',
    cacheDir: resolve(output, 'vite-cache'),
    optimizeDeps: { include: ['vue', 'vue-router', 'element-plus', 'dayjs'] },
    plugins: [vue(), Components({ dts: false, resolvers: [ElementPlusResolver()] }), {
        name: 'account-limit-fixture',
        resolveId(id) { if (id === '/__limit_entry__.js') return '\0limit-entry' },
        load(id) { if (id === '\0limit-entry') return script },
        configureServer(instance) {
            instance.middlewares.use('/__limit_test__', async (_, response) => {
                response.setHeader('Content-Type', 'text/html; charset=utf-8')
                response.end(await instance.transformIndexHtml('/__limit_test__',
                    '<html><meta charset="UTF-8"><body><div id="app"></div><script type="module" src="/__limit_entry__.js"></script></body></html>'))
            })
        },
    }], server: { host: '127.0.0.1', port: 0 },
})
await server.listen()
const base = server.resolvedUrls.local[0].replace(/\/$/, '')
const executablePath = [process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/chromium',
].find((path) => path && existsSync(path))
assert.ok(executablePath)
const browserServer = await chromium.launchServer({ executablePath, headless: true })
const browser = await chromium.connect(browserServer.wsEndpoint())
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const now = Date.parse('2026-11-10T04:00:00+08:00')
await page.clock.install({ time: new Date(now) })
await page.clock.pauseAt(new Date(now + 10000))
const unlock = '2026-11-11T04:00:00+08:00'
const baseAccount = { browser_profile_source: 'cloak', browser_profile_status: 'ready', refresh_status: 'ready',
    auto_refresh_enabled: true, refresh_interval_minutes: 30, image_enabled: true, video_enabled: true,
    is_active: true, credits: 100, user_paygate_tier: 'PAYGATE_TIER_ONE' }
const accounts = [
    { ...baseAccount, id: 1, email: 'flow-limit@example.test', generation_protection: { action: 'LIMIT', status: 'cooldown', until: unlock, reason: 'PUBLIC_ERROR_USER_QUOTA_REACHED' } },
    { ...baseAccount, id: 2, email: 'paused-limit@example.test', is_active: false, generation_protection: { action: 'LIMIT', status: 'cooldown', until: unlock, reason: '连续生成失败保护' } },
    { ...baseAccount, id: 3, email: 'abnormal@example.test', generation_protection: { action: 'DEACTIVE', status: 'abnormal', until: null, reason: 'synthetic unauthorized' } },
    { ...baseAccount, id: 4, email: 'vids-limit@example.test' },
    { ...baseAccount, id: 5, email: 'ready@example.test' },
]
const vids = [{ token_id: 4, email: 'vids-limit@example.test', enabled: true, ready: true, identity_verified: true,
    protection: { action: 'LIMIT', status: 'cooldown', until: unlock, reason: 'synthetic Vids quota' } }]
const writes = [], errors = [], reads = []
page.on('pageerror', (error) => errors.push(error.message))
const json = (route, value) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(value) })
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    if (request.method() !== 'GET') {
        assert.equal(url.pathname, '/api/account-error-policy/clear')
        const body = request.postDataJSON()
        writes.push(body)
        accounts.find((row) => row.id === body.token_id).generation_protection = null
        return json(route, { success: true })
    }
    reads.push(url.pathname)
    if (url.pathname === '/health') return json(route, { backend_running: true, viewer_presentation: 'relay' })
    if (url.pathname === '/api/tokens') return json(route, accounts)
    if (url.pathname.startsWith('/api/admin/vids/')) return json(route, { accounts: vids })
    if (url.pathname === '/api/proxy/pool') return json(route, { entries: [] })
    if (url.pathname === '/api/profile-transfer/jobs' || url.pathname === '/api/plugin/import/jobs') return json(route, { jobs: [] })
    return json(route, { success: true })
})
const row = (email) => page.locator('.accounts-table .el-table__row').filter({ hasText: email })
const checks = []
const record = (value) => { checks.push(value); console.log('PASS ' + value) }
try {
    await page.goto(`${base}/__limit_test__#/accounts`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await row('flow-limit@example.test').locator('.account-protection').waitFor()
    const limited = row('flow-limit@example.test').locator('.account-protection')
    assert.ok(await limited.locator('.protection-badge').isVisible())
    assert.ok(await limited.locator('.protection-badge').evaluate((element) => element.getBoundingClientRect().width > 70))
    assert.match(await limited.innerText(), /Flow\s+LIMIT · 冷却中/)
    assert.equal(await limited.locator('time').innerText(), '解锁 2026-11-11 04:00:00')
    assert.match(await limited.innerText(), /剩余 23 小时 59 分 50 秒/)
    assert.equal(await row('flow-limit@example.test').getByRole('switch', { name: 'flow-limit@example.test 账号状态', exact: true }).getAttribute('aria-checked'), 'true')
    assert.match(await row('paused-limit@example.test').innerText(), /账号已暂停，到期后仍需手动启用/)
    assert.match(await row('abnormal@example.test').innerText(), /需处理异常后手动解除/)
    assert.match(await row('vids-limit@example.test').locator('.account-protection').innerText(), /Vids\s+LIMIT/)
    assert.equal(await row('ready@example.test').locator('.account-protection').count(), 0)
    assert.equal(await page.locator('.el-table__expanded-cell').count(), 0)
    await page.screenshot({ path: resolve(output, 'desktop.png'), fullPage: true })
    record('Main table shows LIMIT, exact local unlock time, reason and separate Flow/Vids protection without expand/hover')

    const readsBefore = reads.length
    await page.clock.fastForward(2000)
    assert.match(await limited.innerText(), /剩余 23 小时 59 分 48 秒/)
    assert.equal(reads.length, readsBefore, 'Countdown must not make per-account network calls')
    await page.clock.fastForward(86400000)
    assert.match(await limited.innerText(), /已到解锁时间，等待状态同步/)
    assert.match(await row('paused-limit@example.test').innerText(), /账号已暂停，到期后仍需手动启用/)
    record('One page clock updates countdown without extra requests; deadline expiry waits for authoritative state and preserves manual pause')

    await row('paused-limit@example.test').getByRole('button', { name: '解除 paused-limit@example.test 的 Flow 生成保护', exact: true }).click()
    await row('paused-limit@example.test').locator('.account-protection').waitFor({ state: 'hidden' })
    assert.deepEqual(writes, [{ token_id: 2 }])
    assert.equal(accounts[1].is_active, false)
    assert.equal(await row('paused-limit@example.test').getByRole('switch', { name: 'paused-limit@example.test 账号状态', exact: true }).getAttribute('aria-checked'), 'false')
    record('Clear protection uses its existing explicit endpoint and never enables a paused account')

    await page.setViewportSize({ width: 390, height: 844 })
    await row('flow-limit@example.test').locator('.account-protection').scrollIntoViewIfNeeded()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: resolve(output, 'mobile.png'), fullPage: true })
    assert.deepEqual(errors, [])
    record('Narrow screens keep the same table and readable protection cells with no page overflow')
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, writes, errors }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, checks, writes, errors }, null, 2))
    throw error
} finally {
    await context.close()
    let closeTimer
    await Promise.race([
        (async () => { await browser.close(); await browserServer.close() })(),
        new Promise((done) => { closeTimer = setTimeout(() => { browserServer.kill().catch(() => {}); done() }, 5000) }),
    ])
    clearTimeout(closeTimer)
    await server.environments.client.close()
    await server.close()
}
