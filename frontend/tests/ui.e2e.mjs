import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
import { chromium } from 'playwright'

// The preview must be scripts/preview_frontend.py: temporary settings/database,
// synthetic login only. Every generation and account mutation below is intercepted.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38123'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, '../../output/frontend-review')
const screenshotMode = process.env.FRONTEND_SCREENSHOTS || 'all'
const captureScreenshots = screenshotMode !== '0'
const keyScreenshots = new Set([
    'login-clean-1440',
    'login-clean-390',
    'dashboard-390',
    'dashboard-390-fixture',
    'accounts-1440-fixture',
    'accounts-390-fixture',
    'studio-1440-fixture',
    'studio-390-fixture',
    'settings-390',
    'settings-390-fixture',
])
const candidates = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
].filter(Boolean)
const executablePath = candidates.find(existsSync)
assert.ok(executablePath, 'Install Chrome/Edge or set PLAYWRIGHT_BROWSER_PATH to an existing Chromium executable')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1440, height: 1000 },
    locale: 'zh-CN',
    timezoneId: 'Asia/Shanghai',
    reducedMotion: 'reduce',
})
context.setDefaultTimeout(15000)
context.setDefaultNavigationTimeout(20000)
await context.grantPermissions(['clipboard-read', 'clipboard-write'], { origin: base })
const page = await context.newPage()
const pageErrors = [],
    failedAssets = [],
    checks = [],
    blockedMutations = []
let loginPosts = 0
page.on('request', (request) => {
    if (new URL(request.url()).pathname === '/api/login' && request.method() === 'POST') loginPosts++
})
page.on('pageerror', (error) => pageErrors.push(error.message))
page.on('response', (response) => {
    if (response.url().includes('/static/app/assets/') && response.status() >= 400)
        failedAssets.push(`${response.status()} ${response.url()}`)
})
const record = (value) => {
    checks.push(value)
    console.log(`PASS ${value}`)
}
const json = (route, body, status = 200, headers = {}) =>
    route.fulfill({ status, headers, contentType: 'application/json', body: JSON.stringify(body) })
const waitText = async (locator, text) => {
    await locator.filter({ hasText: text }).first().waitFor({ state: 'visible' })
}
const routes = ['dashboard', 'studio', 'accounts', 'proxies', 'logs', 'settings', 'guide']
const viewClass = {
    dashboard: 'dashboard-page',
    studio: 'studio-view',
    accounts: 'accounts-view',
    proxies: 'proxy-pool-view',
    logs: 'logs-view',
    settings: 'settings-view',
    guide: 'guide-view',
}
async function go(name) {
    await page.goto(`${base}/manage#/${name}`, { waitUntil: 'domcontentloaded' })
    await page.locator(`.${viewClass[name]} h1`).waitFor()
    await page
        .locator('.el-skeleton')
        .first()
        .waitFor({ state: 'hidden' })
        .catch(() => {})
    await page.evaluate(() => document.fonts.ready)
}
async function screenshot(name, width, height, suffix = '') {
    await page.setViewportSize({ width, height })
    await page.evaluate(() => window.scrollTo(0, 0))
    await page.mouse.move(0, 0)
    const shouldCapture =
        captureScreenshots && (screenshotMode !== 'key' || keyScreenshots.has(`${name}-${width}${suffix}`))
    if (shouldCapture) {
        await page
            .locator('.el-message:visible')
            .last()
            .waitFor({ state: 'hidden', timeout: 5000 })
            .catch(() => {})
        await page
            .locator('.el-tooltip__popper:visible')
            .last()
            .waitFor({ state: 'hidden', timeout: 1500 })
            .catch(() => {})
    }
    if (shouldCapture)
        await page.screenshot({
            path: resolve(output, `${name}-${width}${suffix}.png`),
            fullPage: true,
            animations: 'disabled',
        })
    const bounds = await page.evaluate(() => ({ scroll: document.documentElement.scrollWidth, width: innerWidth }))
    assert.ok(bounds.scroll <= bounds.width + 1, `${name} at ${width}px overflows document: ${JSON.stringify(bounds)}`)
}

try {
    // Catch any unexpected mutating request before a fixture is installed.
    await context.route('**/*', async (route) => {
        const request = route.request(),
            url = new URL(request.url())
        if (url.origin !== base) return route.abort()
        if (!['GET', 'HEAD'].includes(request.method()) && !['/api/login', '/api/logout'].includes(url.pathname)) {
            blockedMutations.push(`${request.method()} ${url.pathname}`)
            return json(route, { detail: 'E2E blocked an unmocked mutation' }, 503)
        }
        return route.continue()
    })
    await context.route('**/api/logs?*', (route) => json(route, []))
    for (const path of ['/manage', '/test', '/vids', '/vids/accounts']) {
        await page.goto(base + path)
        await page.getByPlaceholder('输入你的账号').waitFor()
        assert.match(page.url(), /\/login/)
    }
    record('Unauthenticated protected pages redirect to login')
    for (const handbook of ['/legacy/api-guide', '/legacy/vids-api-guide']) {
        const response = await context.request.get(base + handbook)
        assert.equal(response.status(), 200)
        assert.ok((await response.text()).includes('Idempotency-Key'))
    }
    record('Original complete API handbooks remain public before login')
    const animeScene = page.locator('.login-world svg.login-scene')
    await animeScene.waitFor({ state: 'visible' })
    const animeArtwork = await page
        .locator('.login-page img, .login-page svg image')
        .evaluateAll((images) => [
            ...new Set(
                images.map((image) =>
                    image instanceof SVGImageElement ? image.href.baseVal : image.currentSrc || image.src,
                ),
            ),
        ])
    assert.ok(animeArtwork.length >= 3, 'The SVG scene must load the approved anime character layers and setting')
    const artworkLoads = await page.evaluate(
        async (sources) =>
            Promise.all(
                sources.map(async (src) => {
                    const image = new Image()
                    image.src = src
                    await image.decode()
                    return image.naturalWidth > 0
                }),
            ),
        animeArtwork,
    )
    assert.ok(artworkLoads.every(Boolean))
    record('Layered SVG login scene preserves and loads the approved Japanese anime artwork')
    await page.getByRole('button', { name: '进入灵感工坊' }).click()
    await waitText(page.locator('.el-form-item__error'), '请输入管理员账号')
    await page.getByPlaceholder('输入你的账号').fill('preview-admin')
    await page.getByPlaceholder('输入登录密码').fill('incorrect-fixture-password')
    await page.getByRole('button', { name: '进入灵感工坊' }).click()
    await waitText(page.locator('.login-error'), '账号或密码不匹配')
    record('Login required fields and incorrect password feedback')
    for (const [width, height] of [
        [1440, 1000],
        [768, 1024],
        [390, 844],
    ])
        await screenshot('login', width, height)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByPlaceholder('输入你的账号').waitFor()
    for (const [width, height] of [
        [1440, 1000],
        [390, 844],
    ])
        await screenshot('login-clean', width, height)
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.getByPlaceholder('输入你的账号').fill('preview-admin')
    await page.getByPlaceholder('输入登录密码').fill('preview-password')
    const loginPostsBeforeEnter = loginPosts
    await page.getByPlaceholder('输入登录密码').press('Enter')
    await page.locator('.dashboard-page h1').waitFor()
    assert.equal(loginPosts - loginPostsBeforeEnter, 1, 'Enter must submit login exactly once')
    assert.ok((await context.cookies()).some((cookie) => cookie.name === 'lay4flow_remote_admin_session' && cookie.httpOnly))
    assert.equal(await page.evaluate(() => localStorage.getItem('token')), null)
    record('Synthetic cookie login succeeds without storing admin bearer token')

    for (const name of routes) {
        await go(name)
        for (const [width, height] of [
            [1440, 1000],
            [768, 1024],
            [390, 844],
        ])
            await screenshot(name, width, height)
        record(`${name}: desktop/tablet/mobile loads with no document overflow`)
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    await go('dashboard')
    const skipHash = new URL(page.url()).hash
    await page.locator('.skip-link').focus()
    await page.keyboard.press('Enter')
    assert.equal(new URL(page.url()).hash, skipHash, 'Skip link must not change the hash route')
    assert.equal(await page.evaluate(() => document.activeElement?.id), 'main-content')
    record('Keyboard skip link focuses main content without changing the route')
    await page.getByRole('button', { name: '收起侧栏', exact: true }).click()
    assert.ok(await page.locator('.app-shell').evaluate((element) => element.classList.contains('rail-collapsed')))
    await page.getByRole('button', { name: '展开侧栏', exact: true }).click()
    await page.keyboard.press('Control+k')
    await page.getByPlaceholder('想去哪里？搜索页面…').fill('日志')
    await page.getByPlaceholder('想去哪里？搜索页面…').press('Enter')
    await page.locator('.logs-view h1').waitFor()
    await page.getByRole('button', { name: '查看工坊状态', exact: true }).click()
    await page.locator('.el-drawer').filter({ hasText: '工坊状态' }).waitFor()
    await page.locator('.el-drawer:visible .el-drawer__close-btn').click()
    record('Sidebar collapse, command palette navigation and status drawer')

    await go('accounts')
    assert.ok(await page.locator('.page-actions').getByRole('button', { name: '添加账号', exact: true }).isVisible())
    assert.ok(await page.getByRole('button', { name: '导入 JSON', exact: true }).isVisible())
    assert.equal(await page.getByRole('button', { name: '导出全部账号', exact: true }).isEnabled(), false)
    assert.equal(await page.getByRole('button', { name: '更多账号操作', exact: true }).count(), 0)
    await page.getByRole('button', { name: '导入 JSON', exact: true }).click()
    const importDialog = page.getByRole('dialog').filter({ hasText: '导入账号 Profile' })
    await importDialog.waitFor()
    assert.equal(await importDialog.getByRole('button', { name: '后台导入', exact: true }).isEnabled(), false)
    await importDialog.getByRole('button', { name: '关闭', exact: true }).click()
    record('Account toolbar exposes Profile import/export and empty imports stay disabled')
    await go('settings')
    for (const label of ['基础与访问', '生成与调度', '验证码服务', '媒体缓存', '运行诊断']) {
        await page.locator('.settings-nav button').filter({ hasText: label }).click()
        await waitText(page.locator('.settings-section-title'), label)
        assert.equal(await page.locator('.settings-alert').count(), 0)
    }
    record('All five Settings groups read real synthetic API configuration')
    await go('logs')
    await waitText(page.locator('.el-empty'), '第一份作品')
    await page.getByPlaceholder('搜索账号、请求 ID 或错误内容').fill('no-match')
    await waitText(page.locator('.el-empty'), '没有匹配')
    record('Empty logs and search feedback')
    await go('guide')
    const guideSearch = page.locator('.guide-sidebar input').first()
    await guideSearch.fill('视频')
    await page.locator('.guide-sidebar nav button:visible').first().waitFor()
    await guideSearch.fill('')
    await page.locator('.guide-sidebar nav button').filter({ hasText: '模型与能力目录' }).click()
    for (const language of ['Python', 'cURL', 'JSON', 'Fetch']) {
        await page.locator('.code-toolbar').getByText(language, { exact: true }).click()
        assert.ok((await page.locator('.code-window pre').textContent()).trim())
    }
    await page.locator('.code-toolbar').getByRole('button', { name: '复制', exact: true }).click()
    assert.ok((await page.evaluate(() => navigator.clipboard.readText())).includes('/v1/models'))
    record('Guide endpoint search, four code languages and clipboard copy')

    // Populated fixtures are confined to this test process. These requests never
    // reach account services, browser profiles, captcha providers or generators.
    const accounts = [
        {
            id: 41,
            email: 'koharu@example.test',
            name: 'Koharu',
            remark: 'E2E fixture',
            st: 'synthetic-st',
            at: 'synthetic-at',
            is_active: true,
            credits: 1200,
            image_enabled: true,
            video_enabled: true,
            image_count: 28,
            video_count: 7,
            browser_profile_source: 'cookies',
            refresh_status: 'idle',
            protocol_mode: 'session',
            auto_refresh_enabled: true,
            refresh_interval_minutes: 120,
            current_project_id: 'fixture-project',
            current_project_name: 'Fixture project',
        },
    ]
    const logs = [
        {
            id: 71,
            token_id: 41,
            token_email: accounts[0].email,
            operation: 'generate_image',
            status_code: 200,
            status_text: 'completed',
            duration: 2.4,
            progress: 100,
            created_at: new Date().toISOString(),
            updated_at: new Date().toISOString(),
        },
    ]
    const image = Buffer.from(
        'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII=',
        'base64',
    )
    let accountEdits = 0,
        settingSaves = 0,
        flowPosts = 0,
        vidsPosts = 0,
        downloadPosts = 0,
        receipt = '',
        receiptStatus = 'unknown',
        failTokens = false,
        failSave = false,
        expiredSession = false
    let quotaPosts = 0,
        autoQuotaReads = 0,
        failQuota = false
    const quotaTime = new Date().toISOString()
    const quotaBody = () =>
        Object.fromEntries(
            [
                ['image', 19, 20],
                ['video', 427, 430],
            ].map(([kind, available, total]) => [
                kind,
                {
                    available,
                    total,
                    consumed: total - available,
                    queried_at: quotaTime,
                    error: failQuota ? 'Synthetic quota refresh interruption' : null,
                    stale: failQuota,
                },
            ]),
        )
    await context.route('**/api/tokens', (route) =>
        json(route, failTokens ? { detail: 'Synthetic refresh interruption' } : accounts, failTokens ? 503 : 200),
    )
    await context.route('**/api/tokens/41', (route) => {
        assert.equal(route.request().method(), 'PUT')
        accountEdits++
        Object.assign(accounts[0], route.request().postDataJSON())
        return json(route, { success: true })
    })
    await context.route('**/api/tokens/41/disable', (route) => {
        accounts[0].is_active = false
        return json(route, { success: true })
    })
    await context.route('**/api/config/generation', (route) =>
        route.request().method() === 'POST'
            ? (settingSaves++,
              json(
                  route,
                  failSave ? { detail: 'Synthetic save interruption' } : { success: true },
                  failSave ? 503 : 200,
              ))
            : route.continue(),
    )
    await context.route('**/api/stats', (route) =>
        json(
            route,
            expiredSession
                ? { detail: 'Synthetic session expired' }
                : {
                      total_tokens: accounts.length,
                      active_tokens: 1,
                      today_images: 6,
                      total_images: 28,
                      today_videos: 2,
                      total_videos: 7,
                      today_errors: 0,
                      total_errors: 1,
                  },
            expiredSession ? 401 : 200,
            expiredSession ? { 'X-Admin-Session': 'expired' } : {},
        ),
    )
    await context.route('**/api/logs?*', (route) => json(route, logs))
    await context.route('**/api/logs/71', (route) =>
        json(route, {
            ...logs[0],
            request_body: JSON.stringify({ prompt: 'Synthetic artwork' }),
            response_body: JSON.stringify({ generated_assets: { final_image_url: '/tmp/e2e-image.png' } }),
        }),
    )
    await context.route('**/tmp/e2e-image.png', (route) => route.fulfill({ contentType: 'image/png', body: image }))
    const archiveId = 'd'.repeat(64)
    await context.route('**/api/admin/studio/media/archive', (route) => {
        assert.equal(route.request().method(), 'POST')
        const source = new URL(route.request().postDataJSON().url, base)
        assert.equal(source.origin, base)
        assert.equal(source.pathname, '/tmp/e2e-image.png')
        return json(route, {
            id: archiveId,
            url: `/api/admin/studio/media/${archiveId}/content`,
            kind: 'image',
            cached: true,
        })
    })
    await context.route(`**/api/admin/studio/media/${archiveId}/content`, (route) =>
        route.fulfill({ contentType: 'image/png', body: image }),
    )
    await context.route(/\/api\/admin\/vids\/status(?:\?|$)/, (route) =>
        json(route, {
            configured: true,
            configured_count: 1,
            available: { image: 1, video: 1, speech: 1 },
            recoverable: { image: 0, video: 0, speech: 0 },
            quota: Object.fromEntries(
                Object.entries(quotaBody()).map(([kind, quota]) => [
                    kind,
                    {
                        remaining: quota.available,
                        total: quota.total,
                        queried_at: quota.queried_at,
                        covered: 1,
                        eligible: 1,
                        stale: quota.stale,
                    },
                ]),
            ),
        }),
    )
    await context.route(/\/api\/admin\/vids\/accounts\/detail(?:\?|$)/, (route) => {
        if (new URL(route.request().url()).searchParams.get('refresh') === 'true') autoQuotaReads++
        return json(route, {
            accounts: [
                {
                    token_id: 41,
                    enabled: true,
                    image_enabled: true,
                    video_enabled: true,
                    ready: true,
                    session_state: 'ready',
                    identity_verified: true,
                    vids_identity: 'koharu@example.test',
                    last_quota: quotaBody(),
                },
            ],
        })
    })
    await context.route('**/api/admin/vids/accounts/41/quota-refresh', (route) => {
        quotaPosts++
        return json(route, { success: true, quota: quotaBody() })
    })
    await context.route('**/api/admin/studio/chat/completions', (route) => {
        flowPosts++
        assert.equal(route.request().headers()['authorization'], undefined)
        const body = route.request().postDataJSON()
        assert.equal(body.stream, true)
        assert.ok(body.generationConfig.imageConfig)
        return route.fulfill({
            contentType: 'text/event-stream',
            body: `data: ${JSON.stringify({ choices: [{ delta: { reasoning_content: 'fixture-progress', content: '![fixture](/tmp/e2e-image.png)' } }] })}\n\ndata: [DONE]\n\n`,
        })
    })
    const receiptBody = () => ({
        id: receipt,
        kind: 'image',
        status: receiptStatus,
        error: null,
        content_url: receiptStatus === 'completed' ? `/api/admin/vids/v1/vids/generations/${receipt}/content` : null,
    })
    await context.route('**/api/admin/vids/v1/vids/generations', (route) => {
        vidsPosts++
        const body = route.request().postDataJSON()
        assert.equal(body.kind, 'image')
        assert.equal(body.mode, 'text')
        const key = route.request().headers()['idempotency-key']
        assert.ok(key)
        receipt = 'vids_' + createHash('sha256').update(key).digest('hex')
        return json(route, receiptBody(), 202)
    })
    await context.route('**/api/admin/vids/v1/vids/generations/**', (route) => {
        const url = new URL(route.request().url())
        if (url.pathname.endsWith('/content')) return route.fulfill({ contentType: 'image/png', body: image })
        if (url.pathname.endsWith('/download')) {
            downloadPosts++
            receiptStatus = 'completed'
        } else receiptStatus = 'received'
        return json(route, receiptBody())
    })

    await page.setViewportSize({ width: 1440, height: 1000 })
    await go('accounts')
    await waitText(page.locator('.accounts-table'), 'koharu@example.test')
    const quotaCell = page.locator('.accounts-table .vids-quota-cell').first()
    await waitText(quotaCell, '19')
    assert.match(await quotaCell.innerText(), /427\s*\/\s*430/)
    await waitText(quotaCell, '已自动同步')
    assert.ok(autoQuotaReads > 0, 'Opening the account page must automatically request refreshed quotas')
    assert.equal(
        await page.locator('.el-drawer:visible').count(),
        0,
        'Quota numbers must be visible without opening details',
    )
    assert.ok(await quotaCell.locator('time').isVisible(), 'Quota freshness must be visible alongside the numbers')
    await quotaCell.getByRole('button', { name: '刷新 koharu@example.test 的 Vids 额度', exact: true }).click()
    await page.waitForFunction(() => !document.querySelector('.vids-quota-cell .is-loading'))
    assert.equal(quotaPosts, 1)
    await screenshot('accounts', 1440, 1000, '-fixture')
    await screenshot('accounts', 390, 844, '-fixture')
    const mobileQuota = page.locator('.mobile-account').first()
    assert.ok(await mobileQuota.isVisible())
    assert.match(
        await mobileQuota.innerText(),
        /Flow 积分[\s\S]*1,200[\s\S]*Vids 图片[\s\S]*19[\s\S]*20[\s\S]*Vids 视频[\s\S]*427[\s\S]*430/,
    )
    await mobileQuota.getByRole('button', { name: '刷新 Vids', exact: true }).click()
    await page.waitForFunction(() => !document.querySelector('.mobile-account .is-loading'))
    assert.equal(quotaPosts, 2)
    await page.setViewportSize({ width: 1440, height: 1000 })
    failQuota = true
    await page.locator('.page-actions').getByRole('button', { name: '刷新', exact: true }).click()
    await waitText(quotaCell, '更新失败 · 保留上次额度')
    assert.match(await quotaCell.innerText(), /19[\s\S]*427/)
    await waitText(quotaCell, 'Synthetic quota refresh interruption')
    failQuota = false
    await page.locator('.page-actions').getByRole('button', { name: '刷新', exact: true }).click()
    await waitText(quotaCell, '已自动同步')
    record('Vids quotas auto-refresh, remain directly visible on desktop/mobile and retain values on refresh failure')
    await page.getByRole('button', { name: '操作 koharu@example.test', exact: true }).click()
    await page.getByRole('menuitem', { name: '编辑配置', exact: true }).click()
    await page.getByPlaceholder('输入 Google Flow 的 Session Token').fill('fixture-updated-session')
    await page.locator('.el-drawer:visible').getByRole('button', { name: '保存成员配置', exact: true }).click()
    await page.locator('.el-drawer:visible').waitFor({ state: 'hidden' })
    assert.equal(accountEdits, 1)
    assert.equal(accounts[0].st, 'fixture-updated-session')
    await page.getByRole('button', { name: '操作 koharu@example.test', exact: true }).click()
    await page.getByRole('menuitem', { name: '暂停账号', exact: true }).click()
    assert.equal(accounts[0].is_active, false)
    failTokens = true
    await page.locator('.page-actions').getByRole('button', { name: '刷新', exact: true }).click()
    await waitText(page.locator('.account-alert'), 'Synthetic refresh interruption')
    await waitText(page.locator('.accounts-table'), 'koharu@example.test')
    failTokens = false
    record('Mocked account edit and pause actions update the Vue table')
    await go('settings')
    await page.locator('.settings-nav button').filter({ hasText: '生成与调度' }).click()
    await page.getByRole('button', { name: '保存生成配置', exact: true }).click()
    await waitText(page.locator('.el-message'), '这组配置已保存')
    assert.equal(settingSaves, 1)
    const timeoutInput = page.locator('.el-form-item').filter({ hasText: '图片生成超时' }).locator('input')
    await timeoutInput.fill('601')
    await timeoutInput.blur()
    failSave = true
    await page.getByRole('button', { name: '保存生成配置', exact: true }).click()
    await waitText(page.locator('.el-message'), 'Synthetic save interruption')
    assert.equal(await timeoutInput.inputValue(), '601')
    await waitText(page.locator('.settings-card').filter({ hasText: '任务等待时间' }), '未保存')
    failSave = false
    record('Mocked Settings save submits once and reports success')
    await go('logs')
    await page.getByRole('button', { name: '查看请求详情', exact: true }).click()
    await waitText(page.locator('.el-drawer:visible'), 'Synthetic artwork')
    await page.locator('.el-drawer:visible .el-drawer__close-btn').click()
    record('Mocked populated logs load request details on demand')

    await go('studio')
    assert.equal(await page.getByRole('button', { name: '创作凭据', exact: true }).count(), 0)
    assert.equal(await page.evaluate(() => localStorage.getItem('studio_api_key')), null)
    await page.locator('.studio-editor textarea').fill('E2E synthetic illustration')
    await page.getByRole('button', { name: '开始绘梦', exact: true }).click()
    await waitText(page.locator('.task-status'), '作品已完成')
    assert.equal(flowPosts, 1)
    await page.locator('.generated-media img').first().waitFor({ state: 'visible' })
    assert.equal(await page.locator('.generated-media img').count(), 1)
    await page.locator('.studio-kind-tabs button').filter({ hasText: '视频' }).click()
    await waitText(page.locator('.studio-editor'), '输出时长')
    await page.locator('.studio-kind-tabs button').filter({ hasText: '图片' }).click()
    await page.locator('.studio-nav').getByText('Vids 媒体', { exact: true }).click()
    await page.locator('.studio-editor textarea').fill('E2E synthetic Vids illustration')
    await page.getByRole('button', { name: '开始绘梦', exact: true }).click()
    await page.waitForFunction(() =>
        /等待确认原请求|成品正在准备/.test(document.querySelector('.task-status')?.textContent || ''),
    )
    await page.getByRole('button', { name: '开始绘梦', exact: true }).click()
    assert.equal(vidsPosts, 1, 'Unknown original request must not be submitted twice')
    await page.getByRole('button', { name: '查询原收据', exact: true }).click()
    await waitText(page.locator('.task-status'), '成品正在准备')
    await page.getByRole('button', { name: '恢复下载', exact: true }).click()
    await waitText(page.locator('.task-status'), '作品已完成')
    assert.equal(downloadPosts, 1)
    assert.equal(vidsPosts, 1)
    record('Mocked Flow SSE media plus Vids unknown/received/completed recovery preserves idempotency')

    for (const name of routes) {
        await go(name)
        for (const [width, height] of [
            [1440, 1000],
            [768, 1024],
            [390, 844],
        ])
            await screenshot(name, width, height, '-fixture')
    }
    await page.setViewportSize({ width: 1440, height: 1000 })
    await go('studio')
    await page.locator('.studio-nav').getByText('Vids 媒体', { exact: true }).click()
    record('Populated synthetic fixture screenshots cover desktop/tablet/mobile without production data')

    let pendingGeneration, releaseGeneration
    const pendingStarted = new Promise((resolve) => {
        pendingGeneration = resolve
    })
    const pendingRelease = new Promise((resolve) => {
        releaseGeneration = resolve
    })
    await context.route('**/api/admin/vids/v1/vids/generations', async (route) => {
        vidsPosts++
        const key = route.request().headers()['idempotency-key']
        receipt = 'vids_' + createHash('sha256').update(key).digest('hex')
        receiptStatus = 'unknown'
        pendingGeneration()
        await pendingRelease
        return json(route, receiptBody(), 202).catch(() => {})
    })
    await page.locator('.studio-editor textarea').fill('E2E pending navigation fixture')
    await page.getByRole('button', { name: '开始绘梦', exact: true }).click()
    await pendingStarted
    const pendingReceipt = receipt
    await page.locator('.sidebar a[href$="#/accounts"]').click()
    await page.locator('.accounts-view h1').waitFor()
    releaseGeneration()
    await page.waitForFunction(
        (expected) =>
            JSON.parse(sessionStorage.getItem('lay4flow_studio_receipts') || '[]').some(
                (entry) => entry.receipt === expected,
            ),
        pendingReceipt,
    )
    assert.equal(vidsPosts, 2)
    record('Navigating away from a pending Vids request preserves its original receipt without replay')

    for (const name of routes) {
        await go(name)
        await page.getByRole('button', { name: '切换外观', exact: true }).click()
        await page.getByRole('menuitem', { name: '星空夜间', exact: true }).click()
        const background = await page
            .locator('.surface')
            .first()
            .evaluate((element) => getComputedStyle(element).backgroundColor)
        const rgb = background.match(/\d+/g)?.map(Number) || []
        assert.ok(
            rgb.length >= 3 && rgb.slice(0, 3).every((value) => value < 150),
            `${name} night surface is bright: ${background}`,
        )
        await screenshot(name, 1440, 1000, '-dark')
        await page.getByRole('button', { name: '切换外观', exact: true }).click()
        await page.getByRole('menuitem', { name: '樱花日间', exact: true }).click()
    }
    await go('dashboard')
    await page.setViewportSize({ width: 390, height: 844 })
    await page.locator('.skip-link').focus()
    for (let index = 0; index < 12; index++) {
        await page.keyboard.press('Tab')
        assert.equal(
            await page.evaluate(() => Boolean(document.activeElement?.closest('.sidebar:not(.mobile-open)'))),
            false,
            'Closed mobile navigation must not receive keyboard focus',
        )
    }
    await page.getByRole('button', { name: '打开导航', exact: true }).click()
    await page
        .locator('.sidebar')
        .getByRole('link', { name: /账号管理/ })
        .click()
    await page.locator('.accounts-view h1').waitFor()
    assert.equal(await page.locator('.mobile-overlay').count(), 0)
    record('Night theme uses dark surfaces on every route; mobile navigation opens/closes')
    await page.setViewportSize({ width: 1440, height: 1000 })
    await page.goto(`${base}/manage#/missing-fixture-page`)
    await page.locator('.not-found h1').waitFor()
    await page.getByRole('link', { name: '回到工坊总览' }).click()
    await page.locator('.dashboard-page h1').waitFor()
    expiredSession = true
    await page.goto(`${base}/manage#/accounts`, { waitUntil: 'domcontentloaded' })
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.getByPlaceholder('输入你的账号').waitFor()
    assert.match(page.url(), /#\/login/)
    expiredSession = false
    await page.getByPlaceholder('输入你的账号').fill('preview-admin')
    await page.getByPlaceholder('输入登录密码').fill('preview-password')
    await page.getByRole('button', { name: '进入灵感工坊' }).click()
    await page.locator('.accounts-view h1').waitFor()
    record('Missing-page return and expired-cookie API redirect recover correctly')

    // The backend ownership/session contract is covered by Python route tests.
    // This local fixture exercises the Vue page and its unchanged RFB module only.
    const entryHtml = await (await context.request.get(base + '/')).text()
    await context.route(/\/api\/profile-browser\/4\?viewer_id=e2e-viewer/, (route) =>
        route.fulfill({
            contentType: 'text/html',
            headers: { 'Cache-Control': 'no-store', 'X-Frame-Options': 'DENY' },
            body: entryHtml,
        }),
    )
    await context.route('**/api/profile-browser-assets/core/rfb.js', (route) =>
        route.fulfill({
            contentType: 'text/javascript',
            body: 'export default class RFB { constructor() { this.handlers = {}; } addEventListener(name, callback) { this.handlers[name] = callback; if (name === "connect") setTimeout(callback, 0); } focus() {} sendKey() {} disconnect() {} }',
        }),
    )
    await context.route('**/api/tokens/4/browser?*', (route) =>
        json(route, { success: true, status: 'ready', viewer_id: 'e2e-viewer' }),
    )
    await page.goto(`${base}/api/profile-browser/4?viewer_id=e2e-viewer`)
    await waitText(page.locator('#status'), '已连接')
    assert.equal(await page.locator('#pasteText').isEnabled(), true)
    await page.locator('#pasteText').click()
    await page.locator('#pasteInput').fill('synthetic-password')
    await page.locator('#pasteSend').click()
    assert.equal(await page.locator('#pasteInput').inputValue(), '')
    assert.equal(await page.locator('#pastePanel').isVisible(), false)
    for (const [width, height] of [
        [1440, 1000],
        [768, 1024],
        [390, 844],
    ])
        await screenshot('profile', width, height)
    await page.locator('.profile-actions').getByRole('button', { name: '返回管理页', exact: true }).click()
    await page.locator('.dashboard-page h1').waitFor()
    record('Profile Vue uses original viewer query, clipboard controls and full-page return cleanup')

    await page.setViewportSize({ width: 1440, height: 1000 })
    await go('studio')
    assert.equal(await page.getByRole('button', { name: '创作凭据', exact: true }).count(), 0)
    let releaseFlow, flowStarted
    const flowPending = new Promise((resolve) => {
        releaseFlow = resolve
    })
    const flowDidStart = new Promise((resolve) => {
        flowStarted = resolve
    })
    await context.route('**/api/admin/studio/chat/completions', async (route) => {
        flowPosts++
        flowStarted()
        await flowPending
        return route
            .fulfill({
                contentType: 'text/event-stream',
                body: `data: ${JSON.stringify({ choices: [{ delta: { content: 'Synthetic final response after logout' } }] })}\n\ndata: [DONE]\n\n`,
            })
            .catch(() => {})
    })
    await page.locator('.studio-editor textarea').fill('E2E pending logout fixture')
    await page.getByRole('button', { name: '开始绘梦', exact: true }).click()
    await flowDidStart
    expiredSession = true
    await page.locator('.user-menu').click()
    await page.getByRole('menuitem', { name: '退出登录', exact: true }).click()
    await page.getByPlaceholder('输入你的账号').waitFor()
    releaseFlow()
    assert.equal(await page.evaluate(() => localStorage.getItem('studio_api_key')), null)
    assert.equal(await page.evaluate(() => localStorage.getItem('token')), null)
    assert.equal(flowPosts, 2)
    record('Logging out during a fixture Flow request clears credentials and returns to login')
    assert.deepEqual(pageErrors, [], 'No uncaught browser script errors')
    assert.deepEqual(failedAssets, [], 'No failed bundled frontend assets')
    assert.deepEqual(blockedMutations, [], 'Every mutating fixture must be intercepted explicitly')
    record('Zero uncaught page errors and zero failed bundled assets')
} catch (error) {
    console.error(
        (
            await page
                .locator('body')
                .innerText()
                .catch(() => '')
        ).slice(0, 3500),
    )
    if (captureScreenshots)
        await page
            .screenshot({ path: resolve(output, 'e2e-failure.png'), fullPage: true, timeout: 5000 })
            .catch(() => {})
    console.error(error)
    process.exitCode = 1
} finally {
    await writeFile(
        resolve(output, 'e2e-results.json'),
        JSON.stringify(
            { base, executablePath, checks, pageErrors, failedAssets, blockedMutations, success: !process.exitCode },
            null,
            2,
        ),
    )
    await browser.close().catch(() => {})
}
