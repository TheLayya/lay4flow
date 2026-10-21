import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// All API reads and the deliberate save failure are synthetic. No settings,
// credentials, browser profiles, upstream services or runtime data are changed.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38126'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, '../../output/settings-overview')
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
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
})
context.setDefaultTimeout(12000)
context.setDefaultNavigationTimeout(20000)
const page = await context.newPage()
const checks = [],
    errors = [],
    blockedMutations = [],
    unexpectedReads = [],
    apiReads = [],
    saveRequests = [],
    layout = []
page.on('pageerror', (error) => errors.push(error.message))
const record = (label) => {
    checks.push(label)
    console.log(`PASS ${label}`)
}
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
const fixtureKey = 'fixture-current-secret-key'
const fixtures = {
    '/health': { status: 'ok', backend_running: true, has_active_tokens: true },
    '/api/stats': { total_tokens: 3, active_tokens: 3, today_images: 0, today_videos: 0, today_errors: 0 },
    '/api/profile-transfer/jobs': { jobs: [] },
    '/api/admin/config': {
        admin_username: 'fixture-admin',
        api_key: fixtureKey,
        error_ban_threshold: 5,
        debug_enabled: false,
    },
    '/api/config/generation': { config: { image_timeout: 300, video_timeout: 1500, max_retries: 3 } },
    '/api/call-logic/config': { config: { call_mode: 'default' } },
    '/api/token-refresh/config': { config: { protocol_refresh_enabled: true, refresh_interval_minutes: 120 } },
    '/api/proxy/config': {
        proxy_enabled: true,
        proxy_url: 'http://fixture.proxy.test:8000',
        media_proxy_enabled: false,
        media_proxy_url: '',
    },
    '/api/cache/config': {
        config: { enabled: true, timeout: 7200, base_url: '', effective_base_url: 'http://fixture.cache.test:8000' },
    },
    '/api/captcha/config': {
        captcha_method: 'remote_browser',
        remote_browser_base_url: 'https://captcha.example.test',
        remote_browser_api_key: 'synthetic-key',
        remote_browser_timeout: 60,
    },
    '/api/proxy/pool': {
        entries: [
            {
                id: 1,
                proxy_url: 'http://fixture.pool.test:8080',
                enabled: true,
                note: '测试出口',
                last_check_ok: true,
                last_check_at: '2026-10-16T02:00:00Z',
            },
        ],
    },
    '/api/admin/release': {
        success: true,
        project: 'lay4flow-remote',
        version: '1.2.0',
        release_date: '2026-10-21',
        entries: [{ version: '1.2.0', date: '2026-10-21', items: ['合成更新记录测试'] }],
        release_page: 'https://github.com/TheLayya/lay4flow/releases',
        update_docs: { windows: '/docs/WINDOWS_INSTALL.md', docker: '/docs/DOCKER_UPDATE.md' },
    },
}
await context.route('**/*', (route) => {
    const request = route.request(),
        url = new URL(request.url())
    if (url.origin !== base) return route.abort()
    if (!['GET', 'HEAD'].includes(request.method())) {
        if (url.pathname === '/api/config/generation' && request.method() === 'POST') {
            saveRequests.push({ path: url.pathname, body: request.postDataJSON() })
            return json(route, { detail: 'Synthetic settings save interruption' }, 503)
        }
        blockedMutations.push(`${request.method()} ${url.pathname}`)
        return json(route, { detail: 'Settings overview test blocks unexpected writes' }, 503)
    }
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    apiReads.push(url.pathname)
    if (fixtures[url.pathname]) return json(route, fixtures[url.pathname])
    unexpectedReads.push(url.pathname + url.search)
    return json(route, { detail: `Unexpected API read ${url.pathname}` }, 404)
})
const ids = ['general', 'generation', 'captcha', 'cache', 'integration']
const section = (id) => page.locator(`.settings-section[data-section="${id}"]`)
const card = (id, heading) =>
    section(id)
        .locator('.settings-card')
        .filter({ has: page.getByRole('heading', { name: heading, exact: true, includeHidden: true }) })
const field = (area, label) =>
    area
        .locator('.el-form-item')
        .filter({ has: page.locator('.el-form-item__label').filter({ hasText: label }) })
        .locator('input')
        .first()
async function noOverflow() {
    const bounds = await page.evaluate(() => ({
        viewport: innerWidth,
        document: document.documentElement.scrollWidth,
        main: document.querySelector('.settings-main')?.clientWidth,
        content: document.querySelector('.settings-main')?.scrollWidth,
    }))
    assert.ok(bounds.document <= bounds.viewport + 1, `Document overflows: ${JSON.stringify(bounds)}`)
    assert.ok(bounds.content <= bounds.main + 1, `Settings content overflows: ${JSON.stringify(bounds)}`)
}
async function screenshot(name, fullPage = false) {
    await noOverflow()
    await page
        .locator('.el-message:visible')
        .last()
        .waitFor({ state: 'hidden', timeout: 5000 })
        .catch(() => {})
    await page.screenshot({ path: resolve(output, `${name}.png`), fullPage, animations: 'disabled' })
}
async function assertActiveSection(id = 'general') {
    assert.equal(await page.locator('.settings-section:visible').count(), 1)
    for (const sectionId of ids) {
        assert.equal(await section(sectionId).count(), 1)
        if (sectionId === id) {
            assert.ok(await section(sectionId).isVisible(), `${id} must be active`)
            assert.ok((await section(sectionId).locator('.settings-grid > .settings-card').count()) > 0)
        }
    }
}
async function measureColumns(width, minimum) {
    await page.setViewportSize({ width, height: 1080 })
    const cards = await section('general')
        .locator('.settings-grid > .settings-card')
        .evaluateAll((elements) =>
            elements.map((element) => {
                const r = element.getBoundingClientRect()
                return {
                    x: Math.round(r.x),
                    y: Math.round(r.y),
                    width: Math.round(r.width),
                    height: Math.round(r.height),
                }
            }),
        )
    layout.push({ width, cards })
    assert.equal(cards.length, 3)
    assert.ok(
        new Set(cards.map((item) => item.x)).size >= minimum,
        `General cards must use at least ${minimum} columns at ${width}px: ${JSON.stringify(cards)}`,
    )
    await noOverflow()
}
const search = page.locator('.settings-search input')
try {
    await page.goto(`${base}/login#/settings`, { waitUntil: 'domcontentloaded' })
    await page.locator('.settings-view h1').waitFor()
    await page.locator('.settings-skeleton').waitFor({ state: 'hidden' })
    await page.evaluate(() => document.fonts.ready)
    await assertActiveSection()
    assert.equal(await page.locator('.settings-alert').count(), 0)
    assert.equal(await section('captcha').locator('.settings-grid > .settings-card').count(), 1)
    assert.equal(await page.locator('[data-setting-card="score"]').count(), 0)
    assert.equal(await page.getByRole('button', { name: '打开分数测试', exact: true }).count(), 0)
    assert.equal(await page.getByText('给服务做一次小体检', { exact: true }).count(), 0)
    assert.equal(await page.getByRole('dialog', { name: '验证码服务体检' }).count(), 0)
    record('Captcha settings retain their configuration and remove the score test card, action and dialog')
    record('Settings opens one active section with its controls visible')

    await measureColumns(1920, 3)
    await screenshot('settings-1920-top')
    await measureColumns(1440, 2)
    await screenshot('settings-1440-top')
    record('Wide screens use three columns and ordinary desktop screens use at least two columns')

    const summary = page.locator('.settings-summary')
    const summaryText = await summary.innerText()
    assert.match(summaryText, /API|密钥/)
    assert.match(summaryText, /验证码|打码/)
    assert.match(summaryText, /缓存/)
    assert.match(summaryText, /API 访问\s*已配置/)
    assert.match(summaryText, /验证码\s*已填写密钥/)
    assert.match(summaryText, /媒体缓存\s*120 分钟/)
    assert.match(summaryText, /待保存\s*0 组修改/)
    assert.doesNotMatch(summaryText, new RegExp(fixtureKey))
    const currentKey = field(section('general'), '当前 API 密钥')
    assert.equal(await currentKey.getAttribute('type'), 'password')
    assert.equal(await currentKey.inputValue(), fixtureKey)
    assert.equal(await field(section('general'), '新 API 密钥').getAttribute('type'), 'password')
    assert.equal(await section('integration').getByText('浏览器扩展连接', { exact: true }).count(), 0)
    for (const label of ['当前密码', '新密码', '确认新密码'])
        assert.equal(await field(card('general', '控制台登录'), label).getAttribute('type'), 'password')
    record('Status overview exposes configured services while API keys and passwords stay masked')

    const generation = card('generation', '任务等待时间')
    const timeout = field(generation, '图片生成超时')
    await page.locator('.settings-nav button').filter({ hasText: '生成与调度' }).click()
    await timeout.fill('480')
    await timeout.blur()
    const newKey = field(section('general'), '新 API 密钥')
    await page.locator('.settings-nav button').filter({ hasText: '基础与访问' }).click()
    await newKey.fill('fixture-new-draft-key')
    assert.match(await summary.innerText(), /未保存|待保存|待处理/)
    assert.match(await summary.innerText(), /待保存\s*2 组修改/)

    await search.fill('图片生成超时')
    await page.waitForFunction(
        () =>
            [...document.querySelectorAll('.settings-card')].filter((element) => element.getClientRects().length)
                .length === 1,
    )
    assert.equal(await page.locator('.settings-card:visible').count(), 1, 'Field search must isolate the matching card')
    assert.ok(await generation.isVisible())
    assert.equal(await timeout.inputValue(), '480')
    await screenshot('settings-search-field')
    await search.fill('调试日志')
    assert.ok(await card('integration', '运行诊断').isVisible())
    assert.equal(await page.locator('.settings-card:visible').count(), 1)
    await search.fill('fixture-no-setting-matches')
    await page.locator('.settings-view .el-empty').filter({ hasText: '没有匹配设置' }).waitFor()
    assert.equal(await page.locator('.settings-card:visible').count(), 0)
    await search.fill('')
    await assertActiveSection()
    assert.equal(await timeout.inputValue(), '480')
    assert.equal(await newKey.inputValue(), 'fixture-new-draft-key')
    record('Live search finds specific fields and clearing it restores all sections with unsaved drafts intact')

    for (const [id, label] of [
        ['integration', '运行诊断'],
        ['general', '基础与访问'],
    ]) {
        await page.locator('.settings-nav button').filter({ hasText: label }).click()
        await assertActiveSection(id)
        assert.ok(await section(id).isVisible(), `${id} tab must switch the active section`)
    }
    assert.equal(await timeout.inputValue(), '480')
    assert.equal(await newKey.inputValue(), 'fixture-new-draft-key')
    record('Section shortcuts switch in place and preserve in-progress edits')

    await page.locator('.settings-nav button').filter({ hasText: '生成与调度' }).click()
    await generation.getByRole('button', { name: '保存生成配置', exact: true }).click()
    await page.getByText('Synthetic settings save interruption', { exact: true }).waitFor()
    assert.equal(saveRequests.length, 1)
    assert.deepEqual(saveRequests[0].body, { image_timeout: 480, video_timeout: 1500, max_retries: 3 })
    assert.equal(await timeout.inputValue(), '480')
    assert.match(await generation.innerText(), /未保存/)
    assert.equal(await newKey.inputValue(), 'fixture-new-draft-key')
    assert.equal(await generation.getByRole('button', { name: '保存生成配置', exact: true }).isEnabled(), true)
    await screenshot('settings-save-failure')
    record('A failed independent save retains its draft and every other pending edit and permits retry')

    await page.setViewportSize({ width: 768, height: 1024 })
    await search.fill('图片生成超时')
    assert.equal(await page.locator('.settings-card:visible').count(), 1)
    await search.fill('')
    await assertActiveSection('generation')
    await noOverflow()
    await page.locator('.settings-nav button').filter({ hasText: '基础与访问' }).click()
    await screenshot('settings-768-top')
    await page.locator('.settings-nav button').filter({ hasText: '媒体缓存' }).click()
    await screenshot('settings-768-cache')

    await page.setViewportSize({ width: 390, height: 844 })
    await search.fill('')
    await assertActiveSection('cache')
    await page.evaluate(() => window.scrollTo(0, 0))
    await screenshot('settings-390-top')
    for (const id of ids) {
        await page.locator('.settings-nav button').nth(ids.indexOf(id)).click()
        await section(id).scrollIntoViewIfNeeded()
        await noOverflow()
        assert.ok(await section(id).locator('.settings-card').first().isVisible())
    }
    await section('integration').scrollIntoViewIfNeeded()
    await screenshot('settings-390-bottom')
    assert.equal(await timeout.inputValue(), '480')
    record('Mobile keeps all five sections reachable with vertical scrolling and no horizontal overflow')

    assert.deepEqual(blockedMutations, [])
    assert.deepEqual(unexpectedReads, [])
    assert.deepEqual(errors, [])
    record('All requests are mocked; no runtime configuration, credentials or upstream services are mutated')
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify(
            { base, checks, errors, blockedMutations, unexpectedReads, apiReads, saveRequests, layout },
            null,
            2,
        ),
    )
    console.log(`Verified ${checks.length} settings overview checks. Evidence: ${output}`)
} catch (error) {
    await page.screenshot({ path: resolve(output, 'failure.png'), animations: 'disabled' }).catch(() => {})
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify(
            {
                base,
                checks,
                errors,
                blockedMutations,
                unexpectedReads,
                apiReads,
                saveRequests,
                layout,
                failure: error.message,
            },
            null,
            2,
        ),
    )
    throw error
} finally {
    await browser.close()
}
