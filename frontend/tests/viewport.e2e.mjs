import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Use scripts/preview_frontend.py. APIs are synthetic; no generation is submitted.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38131'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, 'tmp/viewport-review')
await mkdir(output, { recursive: true })
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    '/usr/bin/chromium',
]
    .filter(Boolean)
    .find(existsSync)
const catalogSource = await readFile(resolve(root, 'static/js/model_catalog.js'), 'utf8')
const catalog = Object.entries(
    JSON.parse(catalogSource.slice(catalogSource.indexOf('{'), catalogSource.lastIndexOf('}') + 1)),
).map(([id, entry]) => ({ id, ...entry }))
const browser = await chromium.launch({ executablePath, headless: true })
const results = [],
    errors = []
try {
    for (const [width, height] of [
        [1100, 650],
        [1366, 700],
        [1440, 800],
        [1920, 1080],
    ]) {
        const context = await browser.newContext({
            viewport: { width, height },
            locale: 'zh-CN',
            reducedMotion: 'reduce',
        })
        await context.route('**/*', (route) => {
            const request = route.request(),
                url = new URL(request.url())
            if (url.origin !== base) return route.abort()
            if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
            assert.equal(request.method(), 'GET', 'The viewport check never submits a mutation')
            const fixtures = {
                '/health': {
                    backend_running: true,
                    has_active_tokens: true,
                    remote_browser_configured: true,
                    version: '1.3.0',
                },
                '/api/stats': { total_tokens: 1, active_tokens: 1 },
                '/api/profile-transfer/jobs': { success: true, jobs: [] },
                '/api/admin/studio/jobs': { jobs: [] },
                '/api/admin/studio/models': { data: catalog },
                '/api/admin/vids/status': {
                    configured: true,
                    available: { image: 1, video: 1, speech: 1 },
                    quota: {},
                    recoverable: {},
                },
                '/api/admin/vids/v1/models': { data: [] },
                '/api/admin/config': {
                    admin_username: 'fixture-admin',
                    api_key: 'fixture-key',
                    error_ban_threshold: 3,
                    debug_enabled: false,
                },
                '/api/config/generation': { config: { image_timeout: 300, video_timeout: 1500, max_retries: 3 } },
                '/api/call-logic/config': { config: { call_mode: 'default' } },
                '/api/token-refresh/config': {
                    config: { protocol_refresh_enabled: true, refresh_interval_minutes: 120 },
                },
                '/api/cache/config': {
                    config: { enabled: true, timeout: 7200, base_url: '', effective_base_url: 'http://127.0.0.1:8000' },
                },
                '/api/captcha/config': { remote_browser_api_key: 'fixture-captcha-key', remote_browser_timeout: 60 },
                '/api/admin/release': { version: '1.3.0', release_date: '2026-10-22', entries: [] },
            }
            return route.fulfill({ json: fixtures[url.pathname] || { entries: [], data: [], accounts: [] } })
        })
        const page = await context.newPage()
        page.on('pageerror', (error) => errors.push(error.message))
        await page.goto(`${base}/manage#/studio`)
        await page.locator('.studio-editor').waitFor()
        async function verify(label, selector, fitsMain = true) {
            await page.waitForTimeout(80)
            const measurements = await page.evaluate(
                ({ selector }) => {
                    const main = document.querySelector('.main-content'),
                        editor = document.querySelector(selector)
                    const control = document.querySelector('.editor-submit')
                    const bounds = editor?.getBoundingClientRect()
                    return {
                        documentWidth: document.documentElement.scrollWidth,
                        documentHeight: document.documentElement.scrollHeight,
                        mainHeight: main.clientHeight,
                        mainScroll: main.scrollHeight,
                        panelHeight: editor?.clientHeight,
                        panelScroll: editor?.scrollHeight,
                        panelWidth: editor?.clientWidth,
                        panelScrollWidth: editor?.scrollWidth,
                        panelOverflowY: editor && getComputedStyle(editor).overflowY,
                        panelBottom: bounds?.bottom,
                        submitBottom: control?.getBoundingClientRect().bottom,
                    }
                },
                { selector },
            )
            assert.ok(measurements.documentWidth <= width + 1, `${label}: horizontal overflow`)
            assert.ok(measurements.documentHeight <= height + 1, `${label}: document overflow`)
            if (fitsMain)
                assert.ok(
                    measurements.mainScroll <= measurements.mainHeight + 1,
                    `${label}: main scroll ${JSON.stringify(measurements)}`,
                )
            assert.ok(
                measurements.panelScrollWidth <= measurements.panelWidth + 1,
                `${label}: panel horizontal overflow ${JSON.stringify(measurements)}`,
            )
            if (selector === '.studio-editor') {
                assert.ok(
                    ['auto', 'scroll'].includes(measurements.panelOverflowY),
                    `${label}: editor scroll is missing`,
                )
                await page.locator(selector).evaluate((editor) => {
                    editor.scrollTop = editor.scrollHeight
                })
                const submit = await page.locator('.editor-submit').boundingBox()
                const panel = await page.locator(selector).boundingBox()
                assert.ok(submit.y >= panel.y - 1, `${label}: submit starts outside editor`)
                assert.ok(submit.y + submit.height <= panel.y + panel.height + 1, `${label}: submit leaves editor`)
                await page.screenshot({
                    path: resolve(output, `${label}-submit-${width}x${height}.png`),
                    animations: 'disabled',
                })
                await page.locator(selector).evaluate((editor) => {
                    editor.scrollTop = 0
                })
            } else
                assert.ok(
                    measurements.panelScroll <= measurements.panelHeight + 1,
                    `${label}: panel overflow ${JSON.stringify(measurements)}`,
                )
            results.push({ label, width, height, measurements })
            await page.screenshot({ path: resolve(output, `${label}-${width}x${height}.png`), animations: 'disabled' })
        }
        assert.equal(await page.locator('.studio-editor [role="tab"]').count(), 0)
        for (const panel of ['.editor-prompt', '.editor-parameters', '.editor-materials'])
            assert.ok(await page.locator(panel).isVisible())
        await verify('studio-image', '.studio-editor')
        await page.locator('[data-kind="video"]').click()
        for (const panel of ['.editor-prompt', '.editor-parameters', '.editor-materials'])
            assert.ok(await page.locator(panel).isVisible())
        await verify('studio-video', '.studio-editor')
        await page.goto(`${base}/manage#/settings`)
        await page.locator('.settings-view h1').waitFor()
        await page.locator('.settings-skeleton').waitFor({ state: 'hidden' })
        for (const label of ['基础与访问', '生成与调度', '远程有头打码', '媒体缓存', '运行诊断']) {
            await page.locator('.settings-nav button').filter({ hasText: label }).click()
            assert.equal(await page.locator('.settings-section:visible').count(), 1)
            await verify(`settings-${label}`, '.settings-view')
        }
        await context.close()
    }
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ results, errors }, null, 2))
    console.log(
        `PASS ${results.length} viewport checks; editor sections stay visible and submission is reachable within its scroll area`,
    )
} finally {
    await browser.close()
}
