import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { chromium } from 'playwright'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// Synthetic settings only: no production settings, credentials or provider calls.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/account-error-policy-ui')
await mkdir(output, { recursive: true })
const entry = `
import {createApp,h} from 'vue';
import {createRouter,createWebHashHistory,RouterView} from 'vue-router';
import Settings from '/src/views/SettingsView.vue';
import 'element-plus/theme-chalk/index.css';
import '/src/styles/theme.css';
const router=createRouter({history:createWebHashHistory(),routes:[{path:'/:pathMatch(.*)*',component:Settings}]});
createApp({render:()=>h(RouterView)}).use(router).mount('#app');
`
const server = await createServer({
    configFile: false, root: frontend, logLevel: 'error',
    plugins: [vue(), Components({ dts: false, resolvers: [ElementPlusResolver()] }), {
        name: 'account-error-policy-fixture',
        resolveId(id) { if (id === '/__policy_entry__.js') return '\0policy-entry' },
        load(id) { if (id === '\0policy-entry') return entry },
        configureServer(instance) {
            instance.middlewares.use('/__policy_test__', async (_request, response) => {
                response.setHeader('Content-Type', 'text/html; charset=utf-8')
                response.end(await instance.transformIndexHtml('/__policy_test__',
                    '<html><meta charset="UTF-8"><body><div id="app"></div><script type="module" src="/__policy_entry__.js"></script></body></html>'))
            })
        },
    }],
    server: { host: '127.0.0.1', port: 0 },
})
await server.listen()
const base = server.resolvedUrls.local[0].replace(/\/$/, '')
const executablePath = [process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/chromium',
].find((path) => path && existsSync(path))
assert.ok(executablePath)
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const errors = [], writes = [], checks = []
page.on('pageerror', (error) => errors.push(error.message))
const initialRules = [
    'PUBLIC_ERROR_MODEL_ACCESS_DENIED|LIMIT|0s',
    'PUBLIC_ERROR_USER_QUOTA_REACHED|LIMIT|24h',
    'Resource has been exhausted (e.g. check quota).|LIMIT|1s',
    'PUBLIC_ERROR_USER_REQUESTS_THROTTLED|LIMIT|3s',
    'RESOURCE_EXHAUSTED|LIMIT|5s',
].join('\n')
let settings = { rules: initialRules, consecutive_failure_threshold: 3, failure_cooldown_seconds: 300, max_account_attempts: 3 }
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    if (url.pathname === '/api/account-error-policy') {
        if (request.method() === 'POST') {
            const body = request.postDataJSON()
            writes.push(body)
            if (body.rules.endsWith('|DELAY'))
                return json(route, { detail: '第 2 行：DELAY 必须指定延时时间' }, 400)
            settings = { ...settings, ...body }
        }
        return json(route, { success: true, config: settings })
    }
    if (request.method() !== 'GET') return json(route, { detail: 'Unexpected fixture write' }, 503)
    if (url.pathname === '/api/admin/config') return json(route, { admin_username: 'synthetic-admin', error_ban_threshold: 3 })
    if (url.pathname === '/api/config/generation') return json(route, { config: { image_timeout: 300, video_timeout: 1500, max_retries: 3 } })
    if (url.pathname === '/api/call-logic/config') return json(route, { config: { call_mode: 'default' } })
    if (url.pathname === '/api/admin/vids/config') return json(route, { config: { keepalive_enabled: true,
        keepalive_interval_minutes: 10, repair_cooldown_seconds: 300, consecutive_failure_threshold: 3,
        failure_cooldown_seconds: 300, rules: '' } })
    if (url.pathname === '/api/token-refresh/config') return json(route, { config: { protocol_refresh_enabled: true, refresh_interval_minutes: 120 } })
    if (url.pathname === '/api/cache/config') return json(route, { config: { enabled: true, timeout: 7200, base_url: '' } })
    if (url.pathname === '/api/captcha/config') return json(route, { remote_browser_api_key: 'synthetic-key', remote_browser_timeout: 60 })
    if (url.pathname === '/api/admin/release') return json(route, { version: 'fixture', entries: [], update_docs: {} })
    return json(route, { detail: `Unexpected fixture read ${url.pathname}` }, 503)
})
const record = (message) => { checks.push(message); console.log('PASS ' + message) }
const card = page.locator('[data-setting-card="errorPolicy"]')
try {
    await page.goto(`${base}/__policy_test__#/settings`, { waitUntil: 'domcontentloaded' })
    await page.locator('.settings-skeleton').waitFor({ state: 'hidden' })
    await page.locator('[data-setting-card="login"]').waitFor({ state: 'visible' })
    assert.equal(await page.locator('.settings-section:visible').count(), 1)
    assert.equal(await page.getByRole('tab', { name: '基础与访问', exact: true }).getAttribute('aria-selected'), 'true')
    await page.screenshot({ path: resolve(output, 'desktop-basic-compact.png'), fullPage: true })
    await page.getByRole('tab', { name: '自定义规则', exact: true }).click()
    await card.getByRole('heading', { name: 'Flow 自定义规则', exact: true }).waitFor()
    assert.equal(await page.locator('.settings-card:visible').count(), 2)
    const input = card.getByRole('textbox', { name: '自定义错误规则', exact: true })
    assert.equal(await input.inputValue(), initialRules)
    assert.equal(await card.locator('details').count(), 0)
    assert.equal(await page.getByRole('tab').count(), 6)
    await page.screenshot({ path: resolve(output, 'desktop-policy-defaults.png'), fullPage: true })
    record('Six compact tabs open a single section and show all five editable initial rules directly')

    const invalid = 'synthetic quota|LIMIT|24h\nsynthetic delay|DELAY'
    await input.fill(invalid)
    await card.getByRole('button', { name: '保存错误规则', exact: true }).click()
    await card.getByText('第 2 行：DELAY 必须指定延时时间', { exact: true }).waitFor()
    assert.equal(await input.inputValue(), invalid)
    await card.getByText('未保存', { exact: true }).waitFor()
    record('A server rule-line error stays beside the editor without losing the rejected draft')

    const valid = 'synthetic quota|LIMIT|24h\nsynthetic delay|DELAY|10s'
    await input.fill(valid)
    await card.getByRole('button', { name: '保存错误规则', exact: true }).click()
    await card.getByText('未保存', { exact: true }).waitFor({ state: 'hidden' })
    assert.equal(writes.length, 2)
    assert.equal(writes[1].rules, valid)
    assert.ok(!('builtin_rules' in writes[1]))
    await input.fill('')
    await card.getByRole('button', { name: '保存错误规则', exact: true }).click()
    await card.getByText('未保存', { exact: true }).waitFor({ state: 'hidden' })
    assert.equal(writes[2].rules, '')
    await page.reload()
    await page.locator('.settings-skeleton').waitFor({ state: 'hidden' })
    await page.getByRole('tab', { name: '自定义规则', exact: true }).click()
    assert.equal(await input.inputValue(), '')
    record('Clearing and saving the rule editor stays empty after reload without hidden fallback rules')
    await card.getByRole('button', { name: '填入示例规则', exact: true }).click()
    assert.equal(await input.inputValue(), initialRules)
    assert.equal(writes.length, 3, 'Example insertion edits the draft without saving')
    await page.waitForFunction(() => !document.querySelector('.el-message'))
    await page.screenshot({ path: resolve(output, 'desktop-policy.png'), fullPage: true })
    record('The example button edits only the visible draft and does not write settings')

    await page.setViewportSize({ width: 390, height: 844 })
    await card.scrollIntoViewIfNeeded()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    await page.screenshot({ path: resolve(output, 'mobile-policy.png'), fullPage: true })
    record('The rules editor and action descriptions stay within the mobile viewport')
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, writes, errors }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, errors, writes }, null, 2))
    throw error
} finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await browser.close()
    server.ws.close()
    await server.watcher.close()
    await server.close()
}
