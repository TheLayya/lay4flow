import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { chromium } from 'playwright'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// Actual Settings view, account lists and writes fully synthetic; no backend,
// stored user credentials, profile browser, Google or captcha provider involved.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/refresh-settings-browser')
await mkdir(output, { recursive: true })
const entry = `import {createApp,h} from 'vue';import {createRouter,createWebHashHistory,RouterView} from 'vue-router';
import Settings from '/src/views/SettingsView.vue';import 'element-plus/theme-chalk/index.css';import '/src/styles/theme.css';
const router=createRouter({history:createWebHashHistory(),routes:[{path:'/:pathMatch(.*)*',component:Settings}]});
createApp({render:()=>h(RouterView)}).use(router).mount('#app');`
const server = await createServer({
    configFile: false, root: frontend, logLevel: 'error', cacheDir: resolve(output, 'vite-cache'),
    plugins: [vue(), Components({ dts: false, resolvers: [ElementPlusResolver()] }), {
        name: 'refresh-settings-fixture',
        resolveId(id) { if (id === '/__refresh_entry__.js') return '\0refresh-settings-entry' },
        load(id) { if (id === '\0refresh-settings-entry') return entry },
        configureServer(instance) {
            instance.middlewares.use('/__refresh_settings__', async (_, response) => {
                response.setHeader('Content-Type', 'text/html; charset=utf-8')
                response.end(await instance.transformIndexHtml('/__refresh_settings__',
                    '<html><meta charset="UTF-8"><body><div id="app"></div><script type="module" src="/__refresh_entry__.js"></script></body></html>'))
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
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const reads = [], writes = [], errors = [], checks = []
const record = (message) => { checks.push(message); console.log('PASS ' + message) }
page.on('pageerror', (error) => errors.push(error.message))
let failSave = false
let policy = { enabled: true, refresh_interval_minutes: 30 }
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (request.method() !== 'GET') {
        assert.equal(request.method(), 'POST')
        assert.equal(url.pathname, '/api/profile-maintenance/config')
        const body = request.postDataJSON()
        assert.deepEqual(Object.keys(body).sort(), ['enabled', 'refresh_interval_minutes'])
        writes.push({ path: url.pathname, body })
        if (failSave) return json(route, { detail: '合成保存失败，原设置保留' }, 503)
        policy = { ...body }
        return json(route, { success: true, config: { ...policy, scope: 'all_profile_accounts', revision: writes.length } })
    }
    reads.push(url.pathname)
    if (url.pathname === '/api/profile-maintenance/config') return json(route, { success: true, config: { ...policy, scope: 'all_profile_accounts' } })
    if (url.pathname === '/api/config/generation') return json(route, { config: { image_timeout: 300, video_timeout: 1500, max_retries: 20 } })
    if (url.pathname === '/api/account-error-policy') return json(route, { config: { rules: '', consecutive_failure_threshold: 3, failure_cooldown_seconds: 300 } })
    if (url.pathname === '/api/admin/release') return json(route, { version: 'synthetic', entries: [] })
    return json(route, { config: {} })
})
const maintenance = page.locator('[data-setting-card="refresh"]')
const interval = maintenance.getByRole('spinbutton')
const save = maintenance.getByRole('button', { name: '保存会话维护设置', exact: true })
try {
    await page.goto(`${base}/__refresh_settings__#/settings`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await page.locator('.settings-skeleton').waitFor({ state: 'hidden' })
    assert.equal(reads.filter(path => path === '/api/profile-maintenance/config').length, 1)
    assert.equal(reads.includes('/api/tokens'), false)
    await page.getByRole('tab').nth(1).click()
    await interval.waitFor()
    assert.equal(await interval.inputValue(), '30')
    assert.equal(await maintenance.getByRole('switch').count(), 1)
    assert.equal(await maintenance.getByRole('spinbutton').count(), 1)
    assert.equal(await maintenance.getByRole('table').count(), 0)
    assert.equal(writes.length, 0)
    record('One global Profile maintenance switch and interval replace per-account rows without credential reads')

    await interval.fill('60'); await interval.blur()
    await save.click()
    await page.waitForFunction(() => !document.querySelector('[data-setting-card="refresh"] .el-tag'))
    assert.deepEqual(writes[0], { path: '/api/profile-maintenance/config', body: { enabled: true, refresh_interval_minutes: 60 } })
    assert.equal(policy.refresh_interval_minutes, 60)
    assert.equal(reads.includes('/api/tokens'), false)
    record('Saving uses the dedicated global policy endpoint and exactly two maintenance fields')

    failSave = true
    await interval.fill('45'); await interval.blur()
    await save.click()
    await maintenance.locator('.el-form-item__error').waitFor()
    assert.equal(await interval.inputValue(), '45')
    assert.equal(policy.refresh_interval_minutes, 60)
    record('Failed policy saves preserve the rejected draft and the authoritative saved value')

    failSave = false
    await maintenance.locator('.el-switch').click()
    assert.equal(await maintenance.getByRole('switch').getAttribute('aria-checked'), 'false')
    await save.click()
    await page.waitForFunction(() => !document.querySelector('[data-setting-card="refresh"] .el-tag'))
    assert.equal(policy.enabled, false)
    assert.equal(writes.length, 3)
    await page.screenshot({ path: resolve(output, 'desktop.png'), fullPage: true })
    await page.goto(`${base}/__refresh_settings__#/settings?section=generation&account_id=12`, { waitUntil: 'domcontentloaded' })
    await interval.waitFor()
    assert.equal(await interval.inputValue(), '45')
    assert.equal(await maintenance.getByRole('table').count(), 0)
    await page.setViewportSize({ width: 390, height: 844 })
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: resolve(output, 'mobile.png'), fullPage: true })
    assert.ok(!reads.some(path => path.startsWith('/api/token-refresh') || path.startsWith('/api/tokens')))
    assert.deepEqual(errors, [])
    record('Master disable and old account deep links show the same shared policy on desktop and mobile')
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, reads, writes, errors }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true }).catch(() => {})
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, reads, writes, errors }, null, 2))
    throw error
} finally {
    await context.close()
    let timer
    await Promise.race([(async () => { await browser.close(); await browserServer.close() })().catch(() => {}), new Promise((done) => {
        timer = setTimeout(() => { if (process.platform !== 'win32') { browserServer.process().kill('SIGKILL'); done(); return }
            const child = spawn('taskkill', ['/PID', String(browserServer.process().pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
            child.once('exit', done); child.once('error', done)
        }, 5000)
    })])
    clearTimeout(timer)
    server.ws.close(); await server.watcher.close(); await server.close()
}
