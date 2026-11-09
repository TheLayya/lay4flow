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

// Isolated synthetic request history. Every API route is intercepted, and no
// production account, login, credentials, database or generation service is used.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/logs-attempts-ui')
await mkdir(output, { recursive: true })
const entry = `
import {createApp} from 'vue';
import Logs from '/src/views/LogsView.vue';
import 'element-plus/theme-chalk/index.css';
import '/src/styles/theme.css';
createApp(Logs).mount('#app');
`
const server = await createServer({
    configFile: false,
    root: frontend,
    logLevel: 'error',
    plugins: [
        vue(),
        Components({ dts: false, resolvers: [ElementPlusResolver()] }),
        {
            name: 'logs-attempts-fixture',
            resolveId(id) {
                if (id === '/__logs_entry__.js') return '\0logs-entry'
            },
            load(id) {
                if (id === '\0logs-entry') return entry
            },
            configureServer(instance) {
                instance.middlewares.use('/__logs_test__', async (_request, response) => {
                    response.setHeader('Content-Type', 'text/html; charset=utf-8')
                    response.end(
                        await instance.transformIndexHtml(
                            '/__logs_test__',
                            '<html><meta charset="UTF-8"><body><div id="app"></div><script type="module" src="/__logs_entry__.js"></script></body></html>',
                        ),
                    )
                })
            },
        },
    ],
    server: { host: '127.0.0.1', port: 0 },
})
await server.listen()
const base = server.resolvedUrls.local[0].replace(/\/$/, '')
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/chromium',
].find((path) => path && existsSync(path))
assert.ok(executablePath)
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const errors = [],
    reads = [],
    checks = []
page.on('pageerror', (error) => errors.push(error.message))
const history = {
    version: 1,
    total_attempts: 3,
    retry_count: 2,
    account_count: 2,
    limit: 3,
    accounts_limit: 3,
    attempts: [
        {
            attempt: 1,
            account_id: 5,
            account_email: 'first@example.test',
            status: 'failed',
            stage: 'captcha',
            started_at: '2026-11-09T01:00:00Z',
            duration_ms: 2500,
            error_code: 'SYNTHETIC_CAPTCHA',
            error: 'Synthetic captcha error',
            next_action: 'retry_same_account',
        },
        {
            attempt: 2,
            account_id: 5,
            account_email: 'first@example.test',
            status: 'failed',
            stage: 'submit',
            started_at: '2026-11-09T01:00:03Z',
            duration_ms: 4500,
            error_code: 'PUBLIC_ERROR_USER_QUOTA_REACHED',
            error: 'Synthetic upstream quota failure',
            rule_keyword: 'PUBLIC_ERROR_USER_QUOTA_REACHED',
            rule_action: 'LIMIT',
            rule_seconds: 86400,
            next_action: 'switch_account',
        },
        {
            attempt: 3,
            account_id: 9,
            account_email: 'second@example.test',
            status: 'success',
            stage: 'submit',
            started_at: '2026-11-09T01:00:08Z',
            duration_ms: 1300,
            next_action: 'completed',
        },
    ],
}
const logs = [
    {
        id: 101,
        token_id: 9,
        token_email: 'second@example.test',
        operation: 'generate_video',
        status_code: 200,
        status_text: 'completed',
        duration: 9.3,
        progress: 100,
        created_at: '2026-11-09T01:00:00Z',
        attempt_count: 3,
        retry_count: 2,
        account_count: 2,
    },
    {
        id: 100,
        token_id: 5,
        token_email: 'first@example.test',
        operation: 'generate_video',
        status_code: 429,
        status_text: 'failed',
        duration: 5,
        progress: 0,
        created_at: '2026-11-09T00:00:00Z',
        error_summary: 'Synthetic historical error',
    },
    {
        id: 99,
        operation: 'generate_image',
        status_code: 102,
        status_text: 'started',
        duration: 0,
        progress: 0,
        created_at: '2026-11-08T00:00:00Z',
        attempt_count: 0,
        retry_count: 0,
        account_count: 0,
    },
]
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!url.pathname.startsWith('/api/')) return route.continue()
    reads.push(url.pathname)
    if (request.method() !== 'GET') return json(route, { detail: 'Unexpected fixture write' }, 503)
    if (url.pathname === '/api/logs') return json(route, logs)
    const row = logs.find((item) => url.pathname === `/api/logs/${item.id}`)
    if (row)
        return json(route, {
            ...row,
            request_body: '{}',
            response_body: '{}',
            ...(row.id === 101 ? { attempt_history: history } : {}),
        })
    return json(route, { detail: `Unexpected fixture request ${url.pathname}` }, 503)
})
const record = (message) => {
    checks.push(message)
    console.log('PASS ' + message)
}
try {
    await page.goto(`${base}/__logs_test__`, { waitUntil: 'domcontentloaded' })
    const table = page.locator('.logs-table')
    await table.getByRole('button', { name: '查看请求 #101 的尝试记录', exact: true }).waitFor()
    assert.equal(await table.getByRole('columnheader', { name: '尝试与重试', exact: true }).count(), 1)
    assert.equal(await table.getByText('次数未记录', { exact: true }).count(), 1)
    assert.equal(await table.getByText('尝试 0 次', { exact: true }).count(), 1)
    assert.deepEqual(reads, ['/api/logs'])
    await page.screenshot({ path: resolve(output, 'desktop-list.png'), fullPage: true })
    record('List shows attempts, retries and account count without loading detail payloads; old logs stay unknown')

    await table.getByRole('button', { name: '查看请求 #101 的尝试记录', exact: true }).click()
    const drawer = page.locator('.el-drawer')
    const attempts = drawer.getByRole('region', { name: '尝试记录' })
    await attempts.getByText('第 3 次尝试', { exact: true }).waitFor()
    assert.equal(await attempts.locator('.attempt-item').count(), 3)
    assert.match(await attempts.innerText(), /重试 2 次/)
    const first = attempts.locator('.attempt-item').nth(0)
    assert.match(await first.innerText(), /first@example.test/)
    assert.match(await first.innerText(), /SYNTHETIC_CAPTCHA/)
    assert.match(await first.innerText(), /使用同一账号重试/)
    assert.match(await first.innerText(), /2.50 s/)
    const second = attempts.locator('.attempt-item').nth(1)
    assert.match(await second.innerText(), /PUBLIC_ERROR_USER_QUOTA_REACHED/)
    assert.match(await second.innerText(), /命中规则/)
    assert.match(await second.innerText(), /LIMIT/)
    assert.match(await second.innerText(), /账号冷却 24 小时/)
    assert.match(await second.innerText(), /切换账号/)
    assert.match(await attempts.locator('.attempt-item').nth(2).innerText(), /second@example.test/)
    await attempts.scrollIntoViewIfNeeded()
    await page.screenshot({ path: resolve(output, 'desktop-attempts.png'), fullPage: true })
    record(
        'Detail shows every account, attempt result, duration, original error, matched rule and next action in order',
    )

    await page.setViewportSize({ width: 390, height: 844 })
    await attempts.scrollIntoViewIfNeeded()
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1))
    assert.ok(await attempts.evaluate((element) => element.scrollWidth <= element.clientWidth + 1))
    await page.screenshot({ path: resolve(output, 'mobile-attempts.png'), fullPage: true })
    record('Long upstream errors and attempt cards fit the mobile drawer without horizontal overflow')

    await drawer.getByRole('button', { name: '完成', exact: true }).click()
    await drawer.waitFor({ state: 'hidden' })
    await page.setViewportSize({ width: 1440, height: 1000 })
    await table
        .locator('.el-table__row')
        .filter({ hasText: 'REQUEST #100' })
        .getByRole('button', { name: '查看请求详情', exact: true })
        .click()
    await drawer.getByText('这条日志未记录逐次尝试信息，无法还原每次重试情况。', { exact: false }).waitFor()
    assert.equal(await drawer.locator('.attempt-item').count(), 0)
    await page.screenshot({ path: resolve(output, 'desktop-historical.png'), fullPage: true })
    record('Historical detail explains the missing attempt history without fabricating retry counts')
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks, reads, errors }, null, 2))
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true })
    await writeFile(resolve(output, 'failure.json'), JSON.stringify({ message: error.message, errors, reads }, null, 2))
    throw error
} finally {
    await page.unrouteAll({ behavior: 'ignoreErrors' })
    await browser.close()
    server.ws.close()
    await server.watcher.close()
    await server.close()
}
