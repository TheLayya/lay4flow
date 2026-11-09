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

// Isolated UI acceptance: synthetic Cookie values, mocked APIs and no external traffic.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/cookie-import-ui')
const entry = `
import {createApp,h} from 'vue';
import {createRouter,createWebHashHistory,RouterView} from 'vue-router';
import Accounts from '/src/views/AccountsView.vue';
import 'element-plus/theme-chalk/index.css';
import '/src/styles/theme.css';
const router=createRouter({history:createWebHashHistory(),routes:[{path:'/:pathMatch(.*)*',component:Accounts}]});
createApp({render:()=>h(RouterView)}).use(router).mount('#app');
`
const server = await createServer({
    configFile: false,
    root: frontend,
    plugins: [
        vue(),
        Components({ dts: false, resolvers: [ElementPlusResolver()] }),
        {
            name: 'cookie-import-test',
            resolveId(id) {
                if (id === '/__cookie_import_entry__.js') return '\0cookie-import-entry'
            },
            load(id) {
                if (id === '\0cookie-import-entry') return entry
            },
            configureServer(instance) {
                instance.middlewares.use('/__cookie_import_test__', async (_request, response) => {
                    response.setHeader('Content-Type', 'text/html; charset=utf-8')
                    response.end(await instance.transformIndexHtml(
                        '/__cookie_import_test__',
                        '<html><meta charset="UTF-8"><body><div id="app"></div><script type="module" src="/__cookie_import_entry__.js"></script></body></html>',
                    ))
                })
            },
        },
    ],
    server: { host: '127.0.0.1', port: 0 },
    logLevel: 'error',
})
await server.listen()
const base = server.resolvedUrls.local[0].replace(/\/$/, '')
const browserPath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
].find((value) => value && existsSync(value))
assert.ok(browserPath, 'Install Chromium or set PLAYWRIGHT_BROWSER_PATH')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ executablePath: browserPath, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const page = await context.newPage()
page.setDefaultTimeout(12000)
const errors = [], writes = [], checks = []
const cookieAccount = {
    cookies: { SID: 'synthetic-sid', OSID: 'synthetic-osid', '__Secure-1PSID': 'synthetic-1psid' },
    email: 'cookie@example.test', projectId: 'source-project', sid: '-123456789', userPaygateTier: 'PAYGATE_TIER_ONE',
}
let job = null
let accounts = []
page.on('pageerror', (error) => errors.push(error.message))
const json = (route, body, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    if (url.pathname === '/health') return json(route, { backend_running: true, viewer_presentation: 'relay' })
    if (url.pathname === '/api/tokens') return json(route, accounts)
    if (url.pathname.startsWith('/api/admin/vids/')) return json(route, { accounts: [] })
    if (url.pathname === '/api/token-refresh/config') return json(route, { config: { protocol_refresh_enabled: false } })
    if (url.pathname === '/api/plugin/import/jobs') return json(route, { success: true, jobs: [] })
    if (url.pathname === '/api/proxy/pool') return json(route, {
        entries: [{ id: 17, proxy_url: 'socks5://fixed.example.test:1080', note: '固定出口', enabled: true, last_check_ok: true }],
    })
    if (url.pathname === '/api/profile-transfer/jobs') {
        if (request.method() === 'GET') return json(route, { success: true, jobs: job ? [job] : [] })
        writes.push({ path: url.pathname, body: request.postDataJSON() })
        job = {
            id: 'cookie-job', request_id: request.postDataJSON().request_id, state: 'running', total: 1,
            added: 0, updated: 0, failed: 0, elapsed_seconds: 2, can_retry: false,
            items: [{ email: cookieAccount.email, state: 'running', stage: 'cookie_import' }],
        }
        return json(route, { success: true, job }, 202)
    }
    return json(route, { detail: `Unexpected fixture request ${url.pathname}` }, 503)
})
const record = (message) => {
    checks.push(message)
    console.log(`PASS ${message}`)
}
const dialog = () => page.getByRole('dialog', { name: '导入账号 JSON', exact: true })
const settleDialog = async () => {
    await page.waitForFunction(() => {
        const element = document.querySelector('.account-import-dialog')
        return element && element.getBoundingClientRect().height > 0 && !element.getAnimations().some((animation) => animation.playState === 'running')
    })
    await page.waitForTimeout(250)
}
try {
    await page.goto(`${base}/__cookie_import_test__#/accounts`, { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.locator('.accounts-view h1').waitFor()
    await page.getByRole('button', { name: '导入 JSON', exact: true }).click()
    await dialog().getByText('支持包含 cookies、email 的 Cookie JSON', { exact: false }).waitFor()
    await dialog().getByText('Cookie 导入会在目标固定代理上核对真实账号、Flow 会话和额度，通过后保存。', { exact: true }).waitFor()
    await dialog().getByPlaceholder('粘贴 Cookie JSON、Profile JSON 或账号数组').fill(JSON.stringify(cookieAccount, null, 2))
    await dialog().locator('.el-select').click()
    await page.getByRole('option', { name: /#17/ }).click()
    await page.keyboard.press('Escape')
    await settleDialog()
    await page.screenshot({ path: resolve(output, 'desktop-input.png'), fullPage: true })
    await dialog().getByRole('button', { name: '后台导入', exact: true }).click()
    await dialog().waitFor({ state: 'hidden' })
    assert.equal(writes.length, 1)
    assert.deepEqual(writes[0].body.bundles, [cookieAccount])
    assert.equal(writes[0].body.proxy_pool_id, 17)
    await page.locator('#profile-import-jobs').getByText('验证 Cookie 登录态', { exact: true }).waitFor()
    record('Single Cookie JSON submits unchanged on the selected proxy and shows a background validation task')

    job.items[0] = { email: cookieAccount.email, state: 'running', stage: 'flow_verification' }
    await page.reload()
    await page.locator('#profile-import-jobs').getByText('验证 Flow 会话', { exact: true }).waitFor()
    assert.equal(writes.length, 1)
    record('Reload recovers validation progress without resending Cookie credentials')

    job = {
        ...job, state: 'completed', added: 1,
        items: [{ email: cookieAccount.email, state: 'succeeded', stage: 'completed', message: '导入完成' }],
    }
    accounts = [{ id: 81, email: cookieAccount.email, browser_profile_source: 'cloak', browser_profile_status: 'ready', is_active: true, credits: 1026 }]
    await page.locator('.accounts-table').getByText(cookieAccount.email, { exact: true }).waitFor()
    await page.screenshot({ path: resolve(output, 'desktop-completed.png'), fullPage: true })
    record('Completed validation refreshes the account list')

    await page.setViewportSize({ width: 390, height: 844 })
    await page.getByRole('button', { name: '导入 JSON', exact: true }).click()
    await dialog().waitFor()
    await settleDialog()
    assert.ok(await dialog().locator('.account-import-dialog').evaluate((element) => {
        const bounds = element.getBoundingClientRect()
        return bounds.left >= 0 && bounds.right <= innerWidth && bounds.top >= 0 && bounds.bottom <= innerHeight && element.scrollWidth <= element.clientWidth + 1
    }))
    await dialog().getByRole('button', { name: '后台导入', exact: true }).scrollIntoViewIfNeeded()
    assert.ok(await dialog().getByRole('button', { name: '后台导入', exact: true }).isVisible())
    await page.screenshot({ path: resolve(output, 'mobile-input.png') })
    record('Cookie import remains usable within the mobile viewport')
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, errors, writes }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, errors, writes }, null, 2))
    throw error
} finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await page.close()
    await browser.close()
    server.ws.close()
    await server.watcher.close()
    await server.close()
}
