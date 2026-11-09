import assert from 'node:assert/strict'
import { mkdtemp, mkdir, readFile, writeFile } from 'node:fs/promises'
import { createServer } from 'node:http'
import { tmpdir } from 'node:os'
import { extname, resolve, sep } from 'node:path'
import { fileURLToPath } from 'node:url'
import { execFileSync } from 'node:child_process'
import { chromium } from 'playwright'

// Loads the real MV3 worker/content scripts in a fresh Chromium Profile.
// This read-only smoke checks detection and browser setup. Login capture and
// cleanup are covered separately by mocked Chrome API and backend tests.
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/extension-browser')
const docs = resolve(root, 'docs/images')
await mkdir(output, { recursive: true })
await mkdir(docs, { recursive: true })
const build = resolve(root, 'static/app')
let fixture = null
const sendJSON = (response, body, status = 200) => {
    response.writeHead(status, { 'Content-Type': 'application/json' })
    response.end(JSON.stringify(body))
}
const server = createServer(async (request, response) => {
    const path = new URL(request.url, 'http://127.0.0.1').pathname
    if (path === '/health') return sendJSON(response, { backend_running: true, viewer_presentation: 'relay', remote_browser_configured: true })
    if (path.startsWith('/api/')) {
        if (request.method !== 'GET') {
            fixture.writes.push({ path })
            return sendJSON(response, { detail: 'This read-only detection check does not start logins' }, 405)
        }
        if (path === '/api/plugin/import/jobs') return sendJSON(response, { jobs: fixture.jobs })
        if (path === '/api/tokens') return sendJSON(response, [])
        if (path === '/api/profile-transfer/jobs') return sendJSON(response, { jobs: [] })
        if (path === '/api/proxy/pool') return sendJSON(response, { entries: [] })
        if (path.startsWith('/api/admin/vids/')) return sendJSON(response, { accounts: [] })
        if (path === '/api/token-refresh/config') return sendJSON(response, { config: {} })
        if (path === '/api/admin/release') return sendJSON(response, { version: '1.6.0', entries: [] })
        return sendJSON(response, {})
    }
    const target = path.startsWith('/static/app/assets/')
        ? resolve(build, 'assets', decodeURIComponent(path.slice('/static/app/assets/'.length)))
        : resolve(build, 'index.html')
    if (!target.startsWith(build + sep)) return response.writeHead(404).end()
    try {
        response.writeHead(200, { 'Content-Type': {
            '.html': 'text/html; charset=utf-8', '.js': 'text/javascript', '.css': 'text/css',
        }[extname(target)] || 'application/octet-stream' })
        response.end(await readFile(target))
    } catch { response.writeHead(404).end() }
})
await new Promise((done) => server.listen(0, '0.0.0.0', done))
const base = `http://127.0.0.1:${server.address().port}`
const packagePath = await mkdtemp(resolve(tmpdir(), 'lay4flow-extension-static-'))
const packageDir = resolve(packagePath, 'extension')
execFileSync('python', [resolve(root, 'scripts/build_account_import_extension.py'), '--origin', base, '--output', packageDir])
const checks = []
const record = (message) => { checks.push(message); console.log('PASS ' + message) }
let context
let browserPid
async function closeBrowser() {
    if (!context) return
    let timer
    await Promise.race([context.close().catch(() => {}), new Promise((done) => {
        timer = setTimeout(() => {
            if (browserPid && process.platform === 'win32') {
                try { execFileSync('taskkill', ['/PID', String(browserPid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' }) } catch {}
            }
            done()
        }, 5000)
    })])
    clearTimeout(timer)
    context = null
}
try {
    for (const mode of ['source', 'site-package']) {
        const extension = mode === 'source' ? resolve(root, 'account_import_extension') : packageDir
        const browserProfile = await mkdtemp(resolve(tmpdir(), 'lay4flow-extension-browser-'))
        context = await chromium.launchPersistentContext(browserProfile, {
            channel: 'chromium', headless: true, locale: 'zh-CN', viewport: { width: 1440, height: 1100 },
            ignoreHTTPSErrors: true,
            ...(process.env.PLAYWRIGHT_EXTENSION_BROWSER_PATH ? { executablePath: process.env.PLAYWRIGHT_EXTENSION_BROWSER_PATH } : {}),
            args: [`--disable-extensions-except=${extension}`, `--load-extension=${extension}`,
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost, EXCLUDE 127.0.0.1'],
        })
        const browserCDP = await context.browser().newBrowserCDPSession()
        browserPid = (await browserCDP.send('SystemInfo.getProcessInfo')).processInfo.find((item) => item.type === 'browser')?.id
        const errors = [], writes = [], blocked = []
        fixture = { mode, writes, jobs: [] }
        context.on('page', (page) => page.on('pageerror', (error) => errors.push(error.message)))
        await context.route('https://**/*', (route) => {
            if (new URL(route.request().url()).hostname !== 'flow.google.com') return route.abort()
            return route.fulfill({ contentType: 'text/html', body: '<!doctype html><meta charset="utf-8"><title>Synthetic Flow login</title><h1>本地模拟 Flow 登录</h1>' })
        })
        const [existingWorker] = context.serviceWorkers()
        const worker = existingWorker || await context.waitForEvent('serviceworker', { timeout: 10000 })
        const manifest = await worker.evaluate(() => chrome.runtime.getManifest())
        assert.equal(manifest.version, '1.8.4')
        const page = context.pages()[0] || await context.newPage()
        page.setDefaultTimeout(12000)
        for (const origin of mode === 'source' ? [base, base.replace('127.0.0.1', 'localhost')] : [base]) {
            await page.goto(origin + '/#/accounts', { waitUntil: 'domcontentloaded' })
            await page.locator('.page-actions').getByRole('button', { name: '扩展登录', exact: true }).click()
            const dialog = page.getByRole('dialog', { name: '扩展登录 · 本机浏览器', exact: true })
            await dialog.getByText('扩展已连接 · 1.8.4', { exact: true }).waitFor()
            assert.ok(await dialog.getByRole('button', { name: '开始扩展登录', exact: true }).isEnabled())
            if (origin === base && mode === 'source') {
                await dialog.scrollIntoViewIfNeeded()
                await dialog.screenshot({ path: resolve(docs, 'extension-connected.png') })
            }
            await dialog.getByRole('button', { name: '关闭', exact: true }).click()
        }
        record(`${mode}: actual MV3 worker and content bridge detect 1.8.4 on allowed management origins`)
        await page.goto(base + '/login#/accounts')
        await page.locator('.page-actions').getByRole('button', { name: '扩展登录', exact: true }).click()
        const dialog = page.getByRole('dialog', { name: '扩展登录 · 本机浏览器', exact: true })
        await dialog.getByText('扩展已连接 · 1.8.4', { exact: true }).waitFor()
        record(`${mode}: the /login#/accounts SPA entry retains actual extension detection`)
        const persisted = await worker.evaluate(async () => ({ session: await chrome.storage.session.get(null), local: await chrome.storage.local.get(null) }))
        assert.deepEqual(persisted, { session: {}, local: {} })
        assert.deepEqual(writes, [])
        if (mode === 'source') {
            await dialog.getByRole('button', { name: '关闭', exact: true }).click()
            const details = await context.newPage()
            const extensionId = worker.url().split('/')[2]
            await details.goto(`chrome://extensions/?id=${extensionId}`)
            await details.locator('extensions-detail-view').waitFor()
            await writeFile(resolve(output, 'details-controls.json'), JSON.stringify(await details.locator('extensions-detail-view').evaluate((view) =>
                [...view.shadowRoot.querySelectorAll('cr-toggle')].map((item) => ({ id: item.id, label: item.getAttribute('aria-label') }))
            )))
            await details.screenshot({ path: resolve(output, 'extension-details-full.png'), fullPage: true })
            execFileSync('python', ['-c', 'from PIL import Image; import sys; im=Image.open(sys.argv[1]); im.crop((380,55,1060,965)).save(sys.argv[2])',
                resolve(output, 'extension-details-full.png'), resolve(docs, 'extension-install.png')])
            await details.close()
        }
        record(`${mode}: detection does not submit accounts or store credentials`)
        assert.deepEqual(errors, [])
        await writeFile(resolve(output, `${mode}.json`), JSON.stringify({ mode, checks, writes, errors, blocked }, null, 2))
        await closeBrowser()
    }
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ checks }, null, 2))
} finally {
    await closeBrowser()
    server.closeAllConnections(); await new Promise((done) => server.close(done))
}
