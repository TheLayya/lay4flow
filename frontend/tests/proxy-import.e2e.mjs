import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Run against scripts/preview_frontend.py --port 38127 after building the UI.
// All API traffic is mocked; every external request and unexpected write is blocked.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38127'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/proxy-import-review')
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
]
    .filter(Boolean)
    .find(existsSync)
assert.ok(executablePath, 'Install Chrome/Edge or set PLAYWRIGHT_BROWSER_PATH')
await mkdir(output, { recursive: true })

const checks = [],
    errors = [],
    imports = [],
    unexpectedWrites = [],
    externalRequests = []
const rows = [
    { id: 1, proxy_url: 'socks5h://fixture:fixture-pass@192.0.2.20:8080', enabled: true, bound_account_count: 0 },
]
const good = '192.0.2.21:6525:fixture-user:fixture-pass'
const duplicate = '192.0.2.20:8080:fixture:fixture-pass'
const invalid = '192.0.2.22:6525fixture-user:fixture-pass'
let nextImport,
    releaseImport,
    holdNextImport = false
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
const result = (results) => ({
    success: true,
    total: results.length,
    added: results.filter((item) => item.status === 'added').length,
    skipped: results.filter((item) => item.status === 'skipped').length,
    failed: results.filter((item) => item.status === 'failed').length,
    results,
})
const record = (value) => {
    checks.push(value)
    console.log(`PASS ${value}`)
}
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
})
context.setDefaultTimeout(15000)
const page = await context.newPage()
page.on('pageerror', (error) => errors.push(error.message))
await context.route('**/*', async (route) => {
    const request = route.request(),
        url = new URL(request.url()),
        method = request.method()
    if (url.origin !== base) {
        externalRequests.push({ method, url: request.url() })
        return route.abort()
    }
    if (url.pathname === '/api/proxy/pool/import' && method === 'POST') {
        imports.push(request.postDataJSON())
        assert.ok(nextImport, 'Each import must have an explicit synthetic response')
        const response = nextImport
        nextImport = null
        if (holdNextImport) {
            holdNextImport = false
            await new Promise((resolve) => {
                releaseImport = resolve
            })
        }
        if (response.addRow) rows.push(response.addRow)
        return json(route, response.body, response.status || 200)
    }
    if (!['GET', 'HEAD'].includes(method)) {
        unexpectedWrites.push({ method, path: url.pathname })
        return json(route, { detail: 'Unexpected write blocked by browser fixture' }, 503)
    }
    if (url.pathname === '/api/proxy/pool') return json(route, { success: true, entries: rows })
    if (url.pathname === '/api/tokens') return json(route, [])
    if (url.pathname === '/api/stats')
        return json(route, { total_tokens: 0, active_tokens: 0, today_images: 0, today_videos: 0, today_errors: 0 })
    if (url.pathname === '/health')
        return json(route, { status: 'ok', backend_running: true, has_active_tokens: false })
    if (url.pathname.startsWith('/api/'))
        return json(route, { detail: 'Unexpected read blocked by browser fixture' }, 404)
    return route.continue()
})

const panel = page.locator('.proxy-pool-panel')
const dialog = page.getByRole('dialog', { name: '批量导入代理', exact: true })
const input = dialog.getByRole('textbox', { name: '批量代理地址，每行一个', exact: true })
const submit = () => dialog.getByRole('button', { name: /^(开始导入|重试保留的代理)$/ })
const status = dialog.getByRole('status')
const alert = dialog.getByRole('alert')
const noOverflow = async () => {
    const size = await page.evaluate(() => ({ document: document.documentElement.scrollWidth, viewport: innerWidth }))
    assert.ok(size.document <= size.viewport + 1, `Page overflow: ${JSON.stringify(size)}`)
    const bounds = await dialog.boundingBox()
    assert.ok(
        bounds && bounds.x >= 0 && bounds.x + bounds.width <= size.viewport + 1,
        'Import dialog stays inside viewport',
    )
    const overflow = await dialog.evaluate((element) => ({ client: element.clientWidth, scroll: element.scrollWidth }))
    assert.ok(overflow.scroll <= overflow.client + 1, `Dialog overflow: ${JSON.stringify(overflow)}`)
}
const screenshot = async (name) => {
    await noOverflow()
    await page.screenshot({ path: resolve(output, name), animations: 'disabled' })
}
const waitFinished = () =>
    page.waitForFunction(() => !document.querySelector('.proxy-import-dialog .el-button.is-loading'))

try {
    await page.goto(`${base}/login#/proxies`, { waitUntil: 'domcontentloaded' })
    await panel.waitFor()
    await panel.getByRole('button', { name: '批量导入', exact: true }).click()
    await dialog.waitFor()
    assert.match(await dialog.locator('.field-note').first().innerText(), /默认使用 SOCKS5/)
    assert.equal(await input.inputValue(), '')
    assert.ok(await submit().isDisabled())
    await input.fill(' \n\n  ')
    assert.ok(await submit().isDisabled(), 'Blank-only input is not submitted')
    record('Proxy pool opens an accessible bulk-import dialog and rejects blank-only input')

    const mixed = `\n${good}\n   \n${invalid}\n${duplicate}\n`
    await input.fill(mixed)
    assert.match(await dialog.locator('.pool-import-hint').innerText(), /3 \/ 500 行/)
    nextImport = {
        body: result([
            { line: 2, status: 'added', id: 2 },
            { line: 4, status: 'failed', error_code: 'invalid_format', message: 'fixture-secret-must-not-render' },
            { line: 5, status: 'skipped', id: 1 },
        ]),
        addRow: {
            id: 2,
            proxy_url: 'socks5h://fixture-user:fixture-pass@192.0.2.21:6525',
            enabled: true,
            bound_account_count: 0,
        },
    }
    holdNextImport = true
    await submit().click()
    await page.waitForFunction(() => !!document.querySelector('.proxy-import-dialog .el-button.is-loading'))
    assert.equal(imports.length, 1)
    assert.deepEqual(imports[0], { text: mixed }, 'Raw multiline input preserves original failure line numbering')
    assert.ok(await submit().isDisabled())
    assert.ok(await input.isDisabled())
    assert.ok(await dialog.getByRole('button', { name: '关闭', exact: true }).isDisabled())
    await submit().dispatchEvent('click')
    await page.keyboard.press('Escape')
    assert.ok(await dialog.isVisible())
    assert.equal(imports.length, 1, 'Repeated click does not create a second import')
    releaseImport()
    await waitFinished()
    await status.waitFor()
    assert.match(await status.innerText(), /已添加 1 个 · 跳过重复 1 个 · 失败\s*1 个/)
    assert.match(await status.innerText(), /第 4 行/)
    assert.doesNotMatch(await status.innerText(), /fixture-secret-must-not-render|fixture-pass/)
    assert.equal(await input.inputValue(), invalid)
    assert.equal(rows.length, 2)
    await screenshot('proxy-import-partial-1440.png')
    record(
        'Multiline import preserves original line numbers, skips blanks, retains only failed input and locks duplicate submissions',
    )

    const corrected = '192.0.2.22:6525:fixture-user:fixture-pass'
    await input.fill(corrected)
    nextImport = {
        body: result([{ line: 1, status: 'added', id: 3 }]),
        addRow: {
            id: 3,
            proxy_url: 'socks5h://fixture-user:fixture-pass@192.0.2.22:6525',
            enabled: true,
            bound_account_count: 0,
        },
    }
    await submit().click()
    await waitFinished()
    assert.deepEqual(imports[1], { text: corrected })
    assert.match(await status.innerText(), /已添加 1 个 · 跳过重复 0 个 · 失败\s*0 个/)
    assert.equal(await input.inputValue(), '')
    assert.ok(await submit().isDisabled())
    assert.equal(rows.length, 3)
    await dialog.getByRole('button', { name: '关闭', exact: true }).click()
    await dialog.waitFor({ state: 'hidden' })
    assert.equal(
        await panel.locator('.pool-table-wrap:visible tbody tr.el-table__row').count(),
        3,
        'Successful imports refresh the pool',
    )
    await panel.getByRole('button', { name: '批量导入', exact: true }).click()
    await input.fill(duplicate)
    nextImport = { body: result([{ line: 1, status: 'skipped', id: 1 }]) }
    await submit().click()
    await waitFinished()
    assert.equal(rows.length, 3)
    assert.equal(await input.inputValue(), '')
    assert.match(await status.innerText(), /跳过重复 1 个/)
    record(
        'Correcting a retained failure imports only that line; duplicate-only retry adds no rows and clears completed input',
    )

    const allFailed = `${invalid}\nnot-a-proxy`
    await input.fill(allFailed)
    nextImport = {
        body: result([
            { line: 1, status: 'failed', error_code: 'invalid_format' },
            { line: 2, status: 'failed', error_code: 'invalid_format' },
        ]),
    }
    await submit().click()
    await waitFinished()
    assert.equal(await input.inputValue(), allFailed)
    assert.match(await status.innerText(), /第 1 行[\s\S]*第 2 行/)
    assert.match(await status.innerText(), /失败\s*2 个/)
    record('All-invalid responses preserve every submitted nonblank line with individual failure numbers')

    const requestFailed = '192.0.2.23:6525:fixture-user:fixture-pass'
    await input.fill(requestFailed)
    nextImport = { body: { detail: 'fixture-secret-must-not-render' }, status: 503 }
    await submit().click()
    await waitFinished()
    await alert.waitFor()
    assert.match(await alert.innerText(), /输入已保留/)
    assert.doesNotMatch(await alert.innerText(), /fixture-secret-must-not-render|fixture-pass/)
    assert.equal(await input.inputValue(), requestFailed)
    assert.ok(await submit().isEnabled())
    await screenshot('proxy-import-request-failed-1440.png')
    record('Request errors keep submitted text, expose a retry and redact server error details')

    const requestsBeforeLimits = imports.length
    await input.fill(Array(501).fill(good).join('\n'))
    await submit().click()
    assert.match(await alert.innerText(), /最多导入 500 行/)
    await input.fill('x'.repeat(4097))
    await submit().click()
    assert.match(await alert.innerText(), /第 1 行超过 4096/)
    assert.equal(imports.length, requestsBeforeLimits, 'Oversized input never sends an import request')
    record('Client row and per-line limits reject oversized batches before sending a request')

    await page.setViewportSize({ width: 390, height: 844 })
    await input.fill(`${good}\n${invalid}`)
    nextImport = {
        body: result([
            { line: 1, status: 'skipped', id: 2 },
            { line: 2, status: 'failed', error_code: 'invalid_format' },
        ]),
    }
    await submit().click()
    await waitFinished()
    assert.equal(await input.inputValue(), invalid)
    await screenshot('proxy-import-partial-390.png')
    assert.ok(await dialog.getByRole('button', { name: '关闭', exact: true }).isVisible())
    assert.ok(await submit().isVisible())
    record('390px mobile dialog keeps input, numbered failures and footer actions inside the viewport')

    assert.deepEqual(unexpectedWrites, [])
    assert.deepEqual(externalRequests, [])
    assert.deepEqual(errors, [])
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify({ base, checks, imports, errors, unexpectedWrites, externalRequests }, null, 2),
    )
    console.log(`Verified ${checks.length} proxy import checks. Evidence: ${output}`)
} catch (error) {
    await page
        .screenshot({ path: resolve(output, 'failure.png'), fullPage: true, animations: 'disabled' })
        .catch(() => {})
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify(
            { base, checks, imports, errors, unexpectedWrites, externalRequests, failure: error.message },
            null,
            2,
        ),
    )
    throw error
} finally {
    releaseImport?.()
    await browser.close()
}
