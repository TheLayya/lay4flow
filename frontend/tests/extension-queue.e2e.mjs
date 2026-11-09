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

// All APIs are mocked. No real account, extension store or external service is used.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/extension-queue-ui')
const temporary = resolve(output, 'browser-temp')
await mkdir(temporary, { recursive: true })
process.env.TEMP = temporary
process.env.TMP = temporary
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
            name: 'extension-queue-test',
            resolveId(id) {
                if (id === '/__extension_queue_entry__.js') return '\0extension-queue-entry'
            },
            load(id) {
                if (id === '\0extension-queue-entry') return entry
            },
            configureServer(instance) {
                instance.middlewares.use('/__extension_queue_test__', async (_request, response) => {
                    response.setHeader('Content-Type', 'text/html; charset=utf-8')
                    response.end(
                        await instance.transformIndexHtml(
                            '/__extension_queue_test__',
                            '<html><meta charset="UTF-8"><body><div id="app"></div><script type="module" src="/__extension_queue_entry__.js"></script></body></html>',
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
const browser = await chromium.launch({ executablePath: browserPath, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const page = await context.newPage()
page.setDefaultTimeout(12000)
const errors = [], writes = [], checks = []
page.on('pageerror', (error) => errors.push(error.message))
const record = (message) => { checks.push(message); console.log(`PASS ${message}`) }
let jobs = []
let accounts = []
const job = (id, session, state, extra = {}) => ({
    id, session_id: session, state, stage: state, message: '正在后台验证身份',
    created_at: Date.now() / 1000, attempts: 1, elapsed_seconds: 3, can_retry: false, ...extra,
})
const json = (route, body) => route.fulfill({ contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    if (url.pathname === '/health') return json(route, { backend_running: true, viewer_presentation: 'relay' })
    if (url.pathname === '/api/tokens') return json(route, accounts)
    if (url.pathname === '/api/plugin/import/jobs') return json(route, { success: true, jobs })
    if (url.pathname === '/api/plugin/import/start') {
        writes.push({ path: url.pathname, body: request.postDataJSON() })
        return json(route, {
            success: true, session_id: 'first-session', nonce: 'synthetic-receipt-nonce',
            expires_at: Date.now() / 1000 + 300, complete_url: base + '/api/plugin/import/complete',
            return_url: base + '/manage#/accounts',
        })
    }
    if (url.pathname === '/api/plugin/import/jobs/first-job/retry') {
        writes.push({ path: url.pathname, body: request.postDataJSON() })
        jobs = jobs.map((item) => item.id === 'first-job'
            ? { ...item, state: 'queued', stage: 'queued', message: '已重新排队，保留原登录资料', can_retry: false }
            : item)
        return json(route, { success: true, job: jobs.find((item) => item.id === 'first-job') })
    }
    if (url.pathname.startsWith('/api/admin/vids/')) return json(route, { accounts: [] })
    if (url.pathname === '/api/token-refresh/config') return json(route, { config: { protocol_refresh_enabled: true } })
    if (url.pathname === '/api/proxy/pool') return json(route, { entries: [] })
    if (url.pathname === '/api/profile-transfer/jobs') return json(route, { success: true, jobs: [] })
    throw new Error(`Unexpected fixture API: ${request.method()} ${url.pathname}`)
})
const queue = () => page.getByRole('region', { name: '扩展登录队列', exact: true })
const row = (id) => queue().locator(`[data-job-id="${id}"]`)
try {
    await page.goto(`${base}/__extension_queue_test__#/accounts`, { waitUntil: 'domcontentloaded', timeout: 30000 })
    await page.locator('.accounts-view h1').waitFor()
    await page.locator('.page-actions').getByRole('button', { name: '扩展登录', exact: true }).click()
    const dialog = page.getByRole('dialog', { name: '扩展登录 · 本机浏览器' })
    await dialog.waitFor()
    assert.equal(await dialog.locator('input[value="isolated"]').isChecked(), true)
    await dialog.getByText('使用普通 Chrome 或 Edge 即可。', { exact: false }).waitFor()
    await page.evaluate(() => window.postMessage({
        source: 'lay4flow-import-extension', extension_id: 'synthetic-extension', type: 'import.ready', version: '1.8.4',
    }, location.origin))
    await dialog.getByRole('button', { name: '开始扩展登录', exact: true }).click()
    await dialog.getByRole('button', { name: '取消本次登录', exact: true }).waitFor()
    jobs = [job('first-job', 'first-session', 'running'), job('second-job', 'second-session', 'queued')]
    await page.evaluate(() => window.postMessage({
        source: 'lay4flow-import-extension', extension_id: 'synthetic-extension', type: 'import.result',
        session_id: 'first-session', success: true, result: { action: 'queued' },
    }, location.origin))
    await dialog.getByText('登录资料已安全保存，后台验证中', { exact: false }).waitFor()
    await dialog.getByRole('button', { name: '开始扩展登录', exact: true }).waitFor()
    assert.equal(await dialog.getByText('账号已验证并保存', { exact: false }).count(), 0)
    await dialog.getByRole('button', { name: '关闭', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    await row('first-job').getByText('正在验证', { exact: true }).waitFor()
    await row('second-job').getByText('等待验证', { exact: true }).waitFor()
    record('Durably queued capture releases the modal for the next account without claiming verification success')

    jobs = [
        job('first-job', 'first-session', 'failed', { message: '身份接口暂时不可用，登录资料已保留', can_retry: true }),
        job('second-job', 'second-session', 'completed', { message: '账号已验证并保存', email: 'second@example.test', token_id: 22 }),
    ]
    accounts = [{ id: 22, email: 'second@example.test', is_active: true, auto_refresh_enabled: true, browser_profile_source: 'cloak', browser_profile_status: 'ready', refresh_status: 'ready' }]
    await queue().getByRole('button', { name: '刷新验证进度', exact: true }).click()
    await row('first-job').getByText('验证失败', { exact: true }).waitFor()
    await row('second-job').getByText('已保存账号', { exact: true }).waitFor()
    await row('first-job').getByText('身份接口暂时不可用，登录资料已保留', { exact: true }).waitFor()
    assert.ok(await row('first-job').getByRole('button', { name: '重试扩展登录任务 first-job' }).isVisible())
    record('The first account failure remains visible beside the second saved account')

    await page.reload()
    await row('first-job').getByText('验证失败', { exact: true }).waitFor()
    await row('second-job').getByText('second@example.test', { exact: true }).waitFor()
    assert.equal(await page.evaluate(() => sessionStorage.getItem('lay4flow.extensionLogin')), null)
    record('Reload restores durable success and failure records without credentials stored in the browser')

    await row('first-job').getByRole('button', { name: '重试扩展登录任务 first-job' }).click()
    await row('first-job').getByText('等待验证', { exact: true }).waitFor()
    assert.deepEqual(writes.filter((item) => item.path.endsWith('/retry')), [
        { path: '/api/plugin/import/jobs/first-job/retry', body: {} },
    ])
    assert.ok(await row('second-job').getByText('已保存账号', { exact: true }).isVisible())
    assert.equal(JSON.stringify(writes).includes('cookie'), false)
    await page.screenshot({ path: resolve(output, 'retry-and-saved-account.png'), fullPage: true })
    record('Retry reuses exactly the failed task and does not resend login cookies or disturb a completed account')
    await page.addInitScript(() => {
        window.lay4flowDesktop = { runtime: 'native', version: 'fixture' }
        const listeners = new Set()
        window.__browserCommands = []
        window.chrome.webview = {
            addEventListener(type, listener) { if (type === 'message') listeners.add(listener) },
            removeEventListener(type, listener) { if (type === 'message') listeners.delete(listener) },
            postMessage(command) {
                window.__browserCommands.push(command)
                for (const listener of listeners)
                    queueMicrotask(() => listener({ data: {
                        type: 'desktop.loginBrowser.status', requestId: command.requestId,
                        state: 'opened', message: '本机浏览器已打开。首次请安装扩展并允许无痕运行。',
                    } }))
            },
        }
    })
    await page.reload()
    await page.locator('.page-actions').getByRole('button', { name: '扩展登录', exact: true }).click()
    await dialog.waitFor()
    await dialog.getByRole('button', { name: '在本机浏览器中打开', exact: true }).click()
    await dialog.getByText('本机浏览器已打开。首次请安装扩展并允许无痕运行。', { exact: true }).waitFor()
    const commands = await page.evaluate(() => window.__browserCommands)
    assert.equal(commands.length, 1)
    assert.equal(commands[0].type, 'desktop.openLoginBrowser')
    assert.equal(typeof commands[0].requestId, 'string')
    assert.equal('url' in commands[0], false)
    assert.equal(await dialog.getByRole('button', { name: '检测扩展', exact: true }).count(), 0)
    await page.screenshot({ path: resolve(output, 'desktop-browser-handoff.png'), fullPage: true })
    record('Desktop opens the trusted local browser with a native command and explains incognito extension setup')
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, errors, writes }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, errors, writes }, null, 2))
    throw error
} finally {
    await context.close()
    await browser.close()
    await server.environments.client.close()
    await server.close()
}
