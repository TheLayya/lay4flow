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

// Isolated browser regression. Every API is mocked and external requests are blocked.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/account-editor-ui')
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
    cacheDir: resolve(frontend, '../tmp/account-editor-vite-cache'),
    optimizeDeps: { include: ['vue', 'vue-router', 'element-plus', 'dayjs'] },
    plugins: [
        vue(),
        Components({ dts: false, resolvers: [ElementPlusResolver()] }),
        {
            name: 'account-editor-test',
            resolveId(id) {
                if (id === '/__account_editor_entry__.js') return '\0account-editor-entry'
            },
            load(id) {
                if (id === '\0account-editor-entry') return entry
            },
            configureServer(instance) {
                instance.middlewares.use('/__account_editor_test__', async (_request, response) => {
                    response.setHeader('Content-Type', 'text/html; charset=utf-8')
                    response.end(
                        await instance.transformIndexHtml(
                            '/__account_editor_test__',
                            '<html><meta charset="UTF-8"><body><div id="app"></div><script type="module" src="/__account_editor_entry__.js"></script></body></html>',
                        ),
                    )
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
const errors = [],
    writes = [],
    checks = []
page.on('pageerror', (error) => errors.push(error.message))
const accounts = [
    {
        id: 41,
        email: 'first@example.test',
        st: 'profile-session:synthetic-first',
        remark: 'First account',
        browser_profile_source: 'cloak',
        browser_profile_status: 'ready',
        refresh_status: 'ready',
        protocol_mode: 'session',
        is_active: true,
        auto_refresh_enabled: true,
        refresh_interval_minutes: 30,
        image_enabled: true,
        video_enabled: true,
        image_concurrency: -1,
        video_concurrency: -1,
        proxy_pool_id: 7,
        proxy_url: 'socks5://synthetic-user:synthetic-password@fixed.example.test:1080',
        submission_proxy_mode: 'fixed',
        google_cookies: '[]',
    },
    {
        id: 42,
        email: 'second@example.test',
        st: 'profile-session:synthetic-second',
        remark: 'Keep second account',
        browser_profile_source: 'cloak',
        browser_profile_status: 'ready',
        is_active: true,
        auto_refresh_enabled: true,
        refresh_interval_minutes: 30,
        image_enabled: true,
        video_enabled: true,
    },
]
const json = (route, body) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    if (url.pathname === '/health') return json(route, { backend_running: true, viewer_presentation: 'relay' })
    if (url.pathname === '/api/tokens') return json(route, accounts)
    if (/^\/api\/tokens\/\d+$/.test(url.pathname) && request.method() === 'PUT') {
        const body = request.postDataJSON()
        writes.push({ path: url.pathname, body })
        Object.assign(
            accounts.find((item) => url.pathname.endsWith(`/${item.id}`)),
            body,
        )
        return json(route, { success: true })
    }
    if (url.pathname.startsWith('/api/admin/vids/')) return json(route, { accounts: [] })
    if (url.pathname === '/api/token-refresh/config') return json(route, { config: { protocol_refresh_enabled: true } })
    if (url.pathname === '/api/proxy/pool')
        return json(route, {
            entries: [{ id: 7, proxy_url: accounts[0].proxy_url, enabled: true, last_check_ok: true }],
        })
    if (url.pathname === '/api/profile-transfer/jobs') return json(route, { success: true, jobs: [] })
    return json(route, { success: true })
})
const record = (message) => {
    checks.push(message)
    console.log(`PASS ${message}`)
}
const settleEditor = async () => {
    await page.waitForFunction(() => {
        const element = document.querySelector('.account-editor')
        if (!element) return false
        const bounds = element.getBoundingClientRect()
        return (
            Math.abs(bounds.left - (innerWidth - Math.min(860, innerWidth))) < 1 &&
            Math.abs(bounds.right - innerWidth) < 1
        )
    })
}
try {
    await page.goto(`${base}/__account_editor_test__#/accounts`, { waitUntil: 'domcontentloaded', timeout: 60000 })
    await page.locator('.accounts-view h1').waitFor()
    for (const width of [1280, 1440, 1920]) {
        await page.setViewportSize({ width, height: 1000 })
        await page.waitForTimeout(120)
        const capabilityBounds = await page.locator('.accounts-table .capability-toggle').evaluateAll((buttons) =>
            buttons.map((button) => {
                const rect = button.getBoundingClientRect()
                const cell = button.closest('td').getBoundingClientRect()
                return { inside: rect.left >= cell.left && rect.right <= cell.right && rect.top >= cell.top && rect.bottom <= cell.bottom }
            }),
        )
        assert.ok(capabilityBounds.length > 0 && capabilityBounds.every((item) => item.inside), `Capabilities clipped at ${width}px`)
    }
    record('Image and video controls stay inside their table cells at 1280, 1440 and 1920 pixels')
    await page.setViewportSize({ width: 1440, height: 1000 })
    const table = page.locator('.accounts-table')
    const refresh = table.getByRole('switch', { name: 'first@example.test 自动刷新', exact: true })
    await refresh.locator('..').click()
    await page.waitForFunction(() => document.querySelector('.accounts-table [aria-label="first@example.test 自动刷新"]')?.getAttribute('aria-checked') === 'false')
    assert.deepEqual(writes.at(-1), { path: '/api/tokens/41', body: { auto_refresh_enabled: false } })
    const remark = table.getByRole('textbox', { name: 'first@example.test 备注', exact: true })
    await remark.fill('Inline remark')
    await remark.press('Enter')
    await page.waitForTimeout(150)
    assert.deepEqual(writes.at(-1), { path: '/api/tokens/41', body: { remark: 'Inline remark' } })
    assert.equal(await page.locator('.account-editor:visible').count(), 0)
    assert.equal(accounts[1].remark, 'Keep second account')
    await refresh.locator('..').click()
    await page.waitForFunction(() => document.querySelector('.accounts-table [aria-label="first@example.test 自动刷新"]')?.getAttribute('aria-checked') === 'true')
    writes.length = 0
    record('Refresh and remark update directly on the account row with isolated field writes')
    await page.locator('.accounts-table').getByRole('button', { name: '编辑 first@example.test', exact: true }).click()
    const editor = page.locator('.account-editor:visible')
    await editor.waitFor()
    await settleEditor()
    assert.equal(await editor.getByRole('tab').count(), 0)
    assert.equal(await editor.getByRole('tablist').count(), 0)
    assert.equal(await editor.locator('.editor-account').innerText(), 'first@example.test')
    for (const label of ['自动维护会话', '图片创作', '视频创作'])
        assert.ok(await editor.getByRole('switch', { name: label, exact: true }).locator('..').isVisible(), label)
    assert.equal(await editor.getByRole('spinbutton', { name: '会话维护间隔（分钟）', exact: true }).count(), 0)
    await editor.getByRole('link', { name: '工坊设置', exact: true }).waitFor()
    assert.ok(await editor.getByLabel('账号备注', { exact: true }).isVisible())
    assert.ok(await editor.getByLabel('Google Cookies', { exact: true }).isVisible())
    assert.ok(await editor.getByLabel('Session Token', { exact: true }).isVisible())
    assert.equal(await editor.getByLabel('账号代理', { exact: true }).getAttribute('readonly'), '')
    record('One click opens all account settings without tabs and retains the fixed proxy field')
    const { submission_proxy_mode, remark: originalRemark, video_enabled, ...fixedIdentity } = accounts[0]
    await editor.locator('.el-select').filter({
        has: page.getByRole('combobox', { name: '生成提交出口', exact: true }),
    }).click()
    const randomOption = page.getByRole('option', { name: '代理池随机（每次生成取健康出口）', exact: true })
    assert.equal(await randomOption.isDisabled(), false)
    await randomOption.click()
    await editor.getByLabel('账号备注', { exact: true }).fill('Edited in one form')
    await editor.getByRole('switch', { name: '视频创作', exact: true }).locator('..').click()
    await editor.getByRole('button', { name: '保存账号配置', exact: true }).click()
    await editor.waitFor({ state: 'hidden' })
    assert.equal(writes.length, 1)
    assert.deepEqual(writes[0], {
        path: '/api/tokens/41',
        body: { remark: 'Edited in one form', video_enabled: false, submission_proxy_mode: 'pool_random' },
    })
    const { submission_proxy_mode: savedMode, remark: savedRemark, video_enabled: savedVideo, ...savedIdentity } = accounts[0]
    assert.equal(savedMode, 'pool_random')
    assert.deepEqual(savedIdentity, fixedIdentity)
    assert.match(await table.locator('.el-table__row').filter({ hasText: 'first@example.test' })
        .locator('.account-proxy-trigger').innerText(), /生成 · 代理池随机 · 登录 #7/)
    assert.equal(accounts[1].remark, 'Keep second account')
    record('Full Cloak edit enables random generation egress, writes only changed fields and retains fixed binding, Cookie and Profile')
    await page.setViewportSize({ width: 390, height: 844 })
    const mobile = page.locator('.accounts-table .el-table__row').filter({ hasText: 'second@example.test' })
    await mobile.getByRole('switch', { name: 'second@example.test 自动刷新', exact: true }).locator('..').click()
    await page.waitForFunction(() => document.querySelector('.accounts-table [aria-label="second@example.test 自动刷新"]')?.getAttribute('aria-checked') === 'false')
    assert.deepEqual(writes.at(-1), { path: '/api/tokens/42', body: { auto_refresh_enabled: false } })
    const mobileRemark = mobile.getByRole('textbox', { name: 'second@example.test 备注', exact: true })
    await mobileRemark.fill('Mobile inline remark')
    await mobileRemark.press('Enter')
    await page.waitForTimeout(150)
    assert.deepEqual(writes.at(-1), { path: '/api/tokens/42', body: { remark: 'Mobile inline remark' } })
    record('Narrow horizontal table allows refresh and remark edits without opening another surface')
    await page
        .locator('.accounts-table .el-table__row')
        .filter({ hasText: 'second@example.test' })
        .getByRole('button', {
            name: '编辑 second@example.test',
            exact: true,
        })
        .click()
    await editor.waitFor()
    await settleEditor()
    assert.equal(await editor.locator('.editor-account').innerText(), 'second@example.test')
    assert.equal(await editor.getByLabel('账号代理', { exact: true }).inputValue(), '尚未绑定')
    assert.ok(await editor.getByRole('switch', { name: '自动维护会话', exact: true }).locator('..').isVisible())
    assert.equal(await editor.getByRole('tab').count(), 0)
    assert.ok(await editor.evaluate((element) => element.scrollWidth <= element.clientWidth + 1))
    await page.screenshot({ path: resolve(output, 'mobile.png') })
    await editor.getByRole('button', { name: '取消', exact: true }).click()
    await editor.waitFor({ state: 'hidden' })
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.locator('.accounts-table').getByRole('button', { name: '编辑 first@example.test', exact: true }).click()
    await editor.waitFor()
    await settleEditor()
    await page.screenshot({ path: resolve(output, 'desktop.png') })
    const beforeUnchanged = writes.length
    await editor.getByRole('button', { name: '保存账号配置', exact: true }).click()
    await editor.waitFor({ state: 'hidden' })
    assert.equal(writes.length, beforeUnchanged)
    record('Saving an unchanged full editor skips the account write')
    record('Mobile editing also opens in one click and keeps every setting in one responsive form')
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, errors, writes }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, errors }, null, 2))
    throw error
} finally {
    await page.close()
    await browser.close()
    await server.environments.client.close()
    await server.close()
}
