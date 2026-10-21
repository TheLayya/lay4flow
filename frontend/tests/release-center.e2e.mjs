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
const onlineRequests = []
const page = await context.newPage()
page.on('pageerror', error => errors.push(error.message))
const record = name => { checks.push(name); console.log(`PASS ${name}`) }
await context.route('**/*', async route => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) { blocked.push(url.origin); return route.abort() }
    if (url.pathname === '/api/admin/release/check' || url.pathname === '/api/release/check') {
        onlineRequests.push(url.pathname)
        return route.fulfill({ json: { success: true, status: 'unpublished', message: 'GitHub 尚未发布稳定版，当前版本与本地更新记录仍可查看。', checked_at: '2026-10-21T12:00:00+08:00' } })
    }
    return route.continue()
})
try {
    let response = await context.request.get(`${base}/api/release`)
    assert.equal(response.status(), 200)
    const publicMetadata = await response.json()
    assert.ok(publicMetadata.version)
    record('Public release metadata is available before login')
    await page.goto(`${base}/#/login`, { waitUntil: 'domcontentloaded' })
    await page.locator('.login-release-trigger').click()
    let drawer = page.locator('.release-history-drawer')
    await drawer.getByText(`当前 v${publicMetadata.version}`, { exact: true }).waitFor()
    assert.equal(onlineRequests.length, 0)
    assert.ok(await drawer.locator('.release-timeline-entry').count())
    await page.screenshot({ path: resolve(output, 'login-release-history.png'), animations: 'disabled' })
    await drawer.getByRole('button', { name: /close|关闭/i }).click()
    await drawer.waitFor({ state: 'hidden' })
    record('Login has a direct history drawer with local notes and no startup network version check')
    response = await context.request.get(`${base}/api/admin/release`)
    assert.equal(response.status(), 401)
    response = await context.request.post(`${base}/api/admin/login`, { data: { username: 'preview-admin', password: 'preview-password' } })
    assert.equal(response.status(), 200)
    response = await context.request.get(`${base}/api/admin/release`)
    const metadata = await response.json()
    assert.equal(response.status(), 200)
    assert.equal(metadata.version, publicMetadata.version)
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
    await card.getByText(`v${metadata.version}`, { exact: true }).first().waitFor()
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
    for (const [width, height] of [[1440, 1000], [768, 1000], [390, 1000]]) {
        await page.setViewportSize({ width, height })
        if (width < 768) await page.getByRole('button', { name: '打开导航', exact: true }).click()
        await page.locator('button.release-history-trigger').click()
        drawer = page.locator('.release-history-drawer')
        await drawer.waitFor({ state: 'visible' })
        await page.waitForTimeout(100)
        const layout = await drawer.evaluate((element) => ({
            width: element.getBoundingClientRect().width,
            viewport: innerWidth,
            bodyHeight: element.querySelector('.el-drawer__body').clientHeight,
            contentHeight: element.querySelector('.el-drawer__body').scrollHeight,
        }))
        assert.ok(layout.width <= layout.viewport + 1, JSON.stringify(layout))
        assert.ok(layout.contentHeight > layout.bodyHeight, 'History scrolls inside the drawer')
        await page.screenshot({ path: resolve(output, `history-drawer-${width}.png`), animations: 'disabled' })
        await drawer.getByRole('button', { name: /close|关闭/i }).click()
        await drawer.waitFor({ state: 'hidden' })
    }
    await page.evaluate(() => window.dispatchEvent(new Event('open-release-history')))
    drawer = page.locator('.release-history-drawer')
    await drawer.waitFor({ state: 'visible' })
    await drawer.getByRole('button', { name: '检查更新', exact: true }).click()
    await drawer.getByText(/GitHub 尚未发布稳定版/).waitFor()
    assert.ok(onlineRequests.includes('/api/release/check'))
    await drawer.getByRole('button', { name: /close|关闭/i }).click()
    await drawer.waitFor({ state: 'hidden' })
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('open-release-history', { detail: { check: true } })))
    await drawer.waitFor({ state: 'visible' })
    record('Global sidebar history works at desktop, tablet and mobile widths and native menus can open and check it')
    assert.deepEqual(errors, [])
    assert.deepEqual(blocked, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ base, checks, errors, metadata }, null, 2))
} finally {
    await browser.close()
}
