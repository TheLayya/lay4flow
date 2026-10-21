import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Use the isolated preview runtime; only the explicit online check is mocked.
// Local version, changelog, documentation routes and admin auth are real.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38129'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/release-center')
await mkdir(output, { recursive: true })
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
].filter(Boolean).find(existsSync)
assert.ok(executablePath, 'Set PLAYWRIGHT_BROWSER_PATH to an installed Chromium browser')
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, locale: 'zh-CN' })
const errors = [], checks = [], blocked = []
const page = await context.newPage()
page.on('pageerror', error => errors.push(error.message))
const record = name => { checks.push(name); console.log(`PASS ${name}`) }
await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) { blocked.push(url.origin); return route.abort() }
    if (url.pathname === '/api/admin/release/check')
        return route.fulfill({ json: { success: true, status: 'unpublished', message: 'GitHub 尚未发布稳定版，当前版本与本地更新记录仍可查看。', checked_at: '2026-10-21T12:00:00+08:00' } })
    return route.continue()
})
try {
    let response = await context.request.get(`${base}/api/admin/release`)
    assert.equal(response.status(), 401)
    response = await context.request.post(`${base}/api/admin/login`, { data: { username: 'preview-admin', password: 'preview-password' } })
    assert.equal(response.status(), 200)
    response = await context.request.get(`${base}/api/admin/release`)
    const metadata = await response.json()
    assert.equal(response.status(), 200)
    assert.equal(metadata.version, '1.2.0')
    assert.ok(metadata.entries.some(entry => entry.items.some(item => /Windows/.test(item))))
    assert.ok(metadata.entries.some(entry => entry.items.some(item => /Docker/.test(item))))
    record('Authenticated real endpoint returns the checked-in version and release changelog')
    for (const [name, path] of Object.entries(metadata.update_docs)) {
        response = await context.request.get(base + path)
        assert.equal(response.status(), 200, name)
        const text = await response.text()
        assert.ok(text.length > 100, name)
        assert.ok(response.headers()['content-type'].includes('text/plain'))
    }
    record('Both local Windows and Docker documentation links open their shipped instructions')
    await page.goto(`${base}/#/settings`, { waitUntil: 'domcontentloaded' })
    await page.locator('.settings-skeleton').waitFor({ state: 'hidden' })
    const search = page.locator('.settings-search input')
    await search.fill('更新记录')
    const card = page.locator('[data-setting-card="release"]')
    await card.waitFor({ state: 'visible' })
    await card.getByText('v1.2.0', { exact: true }).first().waitFor()
    await card.getByRole('button', { name: '检查线上版本' }).click()
    await card.getByText(/GitHub 尚未发布稳定版/).waitFor()
    await page.locator('.el-message').waitFor({ state: 'hidden' })
    assert.match(await card.innerText(), /Windows.*Docker|Docker.*Windows/s)
    assert.equal(await card.locator('a').count(), 3)
    record('Search reveals the update center and explicit online check retains its local changelog')
    for (const [width, height] of [[1440, 1100], [768, 1100], [390, 1000]]) {
        await page.setViewportSize({ width, height })
        await card.scrollIntoViewIfNeeded()
        await page.waitForTimeout(100)
        const layout = await page.evaluate(() => ({ page: document.documentElement.scrollWidth, width: innerWidth }))
        assert.ok(layout.page <= layout.width + 1, JSON.stringify(layout))
        await page.screenshot({ path: resolve(output, `updates-${width}.png`), fullPage: true, animations: 'disabled' })
        if (width === 1440) await card.screenshot({ path: resolve(output, 'updates-card.png'), animations: 'disabled' })
    }
    record('Desktop, tablet and mobile layouts show readable release notes without horizontal overflow')
    assert.deepEqual(errors, [])
    assert.deepEqual(blocked, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ base, checks, errors, metadata }, null, 2))
} finally {
    await browser.close()
}
