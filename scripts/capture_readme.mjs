import assert from 'node:assert/strict'
import { existsSync, readFileSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'
import { spawnSync } from 'node:child_process'

// npm --prefix frontend ci && npm --prefix frontend run build
// python scripts/preview_frontend.py --port 38127
// node scripts/capture_readme.mjs
// The preview only serves UI assets. Every API response and displayed account is
// synthetic; upstream requests and writes are intercepted before they leave Chromium.
const root = fileURLToPath(new URL('..', import.meta.url))
const require = createRequire(new URL('../frontend/package.json', import.meta.url))
const { chromium } = require('playwright')
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38127'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const review = resolve(root, 'tmp/readme-review')
const output = resolve(root, 'docs/screenshots')
await mkdir(review, { recursive: true })
await mkdir(output, { recursive: true })
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
].filter(Boolean).find(existsSync)
assert.ok(executablePath, 'Install Chrome/Edge or set PLAYWRIGHT_BROWSER_PATH')
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1600, height: 1000 },
    locale: 'zh-CN', timezoneId: 'Asia/Shanghai', reducedMotion: 'reduce',
})
context.setDefaultTimeout(15000)
const page = await context.newPage()
const errors = [], failedAssets = [], unexpected = [], external = [], requests = [], captures = []
let authenticated = false
page.on('pageerror', (error) => errors.push(error.message))
page.on('response', (response) => {
    if (response.status() >= 400 && /\/static\/app\/assets\//.test(response.url()))
        failedAssets.push(`${response.status()} ${response.url()}`)
})
const now = Date.now()
const date = (minutes) => new Date(now + minutes * 60000).toISOString()
const quota = (available, total) => ({ available, total, queried_at: date(-3), stale: false, error: null })
const accounts = ['sakura', 'aoi', 'momo'].map((name, index) => ({
    id: index + 1, email: `${name}@example.test`, name: ['樱花创作组', '晴空影像组', '桃色灵感组'][index],
    remark: 'README 演示账号', is_active: true, credits: [12600, 8400, 6200][index],
    image_enabled: true, video_enabled: true, image_count: [128, 86, 52][index],
    video_count: [28, 16, 12][index], today_error_count: 0,
    at_expired: false, at_expires: date(65), browser_profile_source: 'cloak',
    browser_profile_status: 'ready', refresh_status: 'ready', protocol_mode: 'session',
    auto_refresh_enabled: true, refresh_interval_minutes: 120,
    last_refresh_at: date(-55), next_refresh_at: date(65), refresh_failures: 0,
    current_project_id: `demo-project-${index + 1}`, current_project_name: 'lay 的灵感工坊',
    proxy_pool_id: index + 1, created_at: date(-7 * 24 * 60), last_used_at: date(-2),
    user_paygate_tier: 'PAYGATE_TIER_TWO',
}))
const vidsAccounts = accounts.map((row, index) => ({
    token_id: row.id, email: row.email, enabled: true, image_enabled: true, video_enabled: true,
    ready: true, session_state: 'ready', identity_verified: true, vids_identity: row.email,
    last_quota: { image: quota(25 - index * 3, 30), video: quota(460 - index * 40, 500) },
}))
const pool = accounts.map((row, index) => ({
    id: row.id, proxy_url: `socks5://192.0.2.${20 + index}:1080`, enabled: true,
    note: ['创作出口 A', '创作出口 B', '媒体下载出口'][index],
    bound_account_count: 1, last_check_ok: true, last_check_at: date(-5),
}))
const modelsSource = readFileSync(resolve(root, 'static/js/model_catalog.js'), 'utf8')
const models = Object.entries(JSON.parse(modelsSource.slice(modelsSource.indexOf('{'), modelsSource.lastIndexOf('}') + 1)))
    .map(([id, model]) => ({ id, ...model }))
const voiceSource = readFileSync(resolve(root, 'src/services/vids/client.py'), 'utf8')
const voiceOptions = JSON.parse(`[${voiceSource.split('PRESET_VOICE_OPTIONS = (')[1].split('\n)')[0].trim().replace(/,$/, '')}]`)
const vidsModels = [
    { id: 'vids-image', kind: 'image', description: 'Vids 图片 · 文生 / 编辑', capabilities: {
        modes: ['text', 'edit'], counts: [1, 2, 3, 4], aspect_ratios: ['1:1', '16:9', '9:16'],
        styles: ['auto', 'photography', 'background', 'vector', 'sketch', 'watercolor', 'cyberpunk'], max_input_images: 1,
    } },
    { id: 'vids-video', kind: 'video', description: 'Vids 视频 · 六种创作模式', capabilities: {
        modes: ['text', 'animate', 'ingredients', 'edit', 'extend', 'upscale'], seconds: [3, 4, 5, 6, 7, 8, 9, 10],
        source_sizes: ['1280x720', '720x1280', '1920x1080', '1080x1920'],
        max_reference_images: 7, max_edit_reference_images: 3, shot_modes: ['auto', 'long_take', 'montage'],
    } },
    { id: 'vids-speech', kind: 'speech', description: 'Vids 语音 · 30 个官方预设', capabilities: {
        voices: voiceOptions.map((voice) => voice.id), voice_options: voiceOptions,
        verified_voices: ['Puck', 'Gacrux', 'Algenib'], speech_response_formats: ['wav'], speech_input_limit: 4096,
    } },
]
const images = ['koharu-portrait.webp', 'koharu-banner.webp', 'koharu-welcome.webp']
const titles = ['樱花树下的 lay · 春日插画', '微风与花瓣 · 灵感时刻', '你好，新的创作日常', '把春天藏进画面里', '与 lay 一起出发', '暖色午后的问候']
const history = titles.map((title, index) => ({
    provider: 'flow', kind: 'image', model: 'gemini-3.0-pro-image',
    status: 'completed', started: now - index * 180000, ended: now - index * 180000 + 2000,
    key: `readme-artwork-${index}`, receipt: '', title, description: title,
    message: '已完成', outputs: [{ kind: 'image', url: `${base}/__readme__/${images[index % images.length]}` }],
}))
const logs = Array.from({ length: 72 }, (_, index) => ({
    id: index + 1, token_id: index % 3 + 1, email: accounts[index % 3].email,
    operation: index % 4 === 0 ? 'video_generation' : 'image_generation',
    model: index % 4 === 0 ? 'veo_3_1' : 'gemini-3.0-pro-image',
    status_code: 200, status_text: '已完成', duration: index % 4 === 0 ? 43.2 : 12.8,
    created_at: date(-index * 18),
}))
const vidsStatus = {
    configured: true, configured_count: 3, available: { image: 3, video: 3, speech: 3 },
    recoverable: { image: 0, video: 0, speech: 0 },
    quota: Object.fromEntries(['image', 'video'].map((kind) => [kind, {
        remaining: kind === 'image' ? 66 : 1260, total: kind === 'image' ? 90 : 1500,
        active_remaining: kind === 'image' ? 66 : 1260, active_total: kind === 'image' ? 90 : 1500,
        active_covered: 3, eligible: 3, queried_at: date(-3), active_queried_at: date(-3),
    }])),
}
const fixtures = {
    '/health': { status: 'ok', backend_running: true, has_active_tokens: true, remote_browser_configured: true },
    '/api/stats': { total_tokens: 3, active_tokens: 3, today_images: 48, today_videos: 12, today_errors: 0, total_images: 268, total_videos: 56, total_errors: 0 },
    '/api/tokens': accounts,
    '/api/logs': logs,
    '/api/admin/vids/status': vidsStatus,
    '/api/admin/vids/accounts/detail': { accounts: vidsAccounts },
    '/api/admin/vids/onboard/candidates': { accounts: [] },
    '/api/admin/studio/models': { object: 'list', data: models },
    '/api/admin/vids/v1/models': { object: 'list', data: vidsModels },
    '/api/proxy/pool': { success: true, entries: pool },
    '/api/admin/config': { admin_username: 'admin', api_key: '', error_ban_threshold: 5, debug_enabled: false },
    '/api/config/generation': { config: { image_timeout: 300, video_timeout: 1500, max_retries: 3 } },
    '/api/call-logic/config': { config: { call_mode: 'default' } },
    '/api/token-refresh/config': { config: { protocol_refresh_enabled: true, refresh_interval_minutes: 120 } },
    '/api/proxy/config': { proxy_enabled: false, proxy_url: '', media_proxy_enabled: true, media_proxy_url: '' },
    '/api/cache/config': { config: { enabled: true, timeout: 7200, base_url: '', effective_base_url: 'https://media.example.test' } },
    '/api/captcha/config': { captcha_method: 'remote_browser', remote_browser_base_url: 'https://cap.tk218.top', remote_browser_api_key: 'readme-demo-key', remote_browser_timeout: 60 },
}
const json = (route, value, status = 200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(value) })
await context.addInitScript((entries) => {
    localStorage.setItem('admin_username', 'lay')
    localStorage.setItem('theme', 'light')
    if (!sessionStorage.getItem('readme_history_seeded')) {
        localStorage.setItem('lay4flow_studio_receipts:v2:lay', JSON.stringify(entries))
        sessionStorage.setItem('lay4flow_studio_receipts', JSON.stringify(entries))
        sessionStorage.setItem('readme_history_seeded', '1')
    }
}, history)
const speechHistory = ['Welcome to the creative studio', 'A little inspiration, a new beginning', 'Stories come alive with sound'].map((title, index) => {
    const receipt = `vids_${String(index + 1).repeat(64)}`
    return { provider: 'vids', kind: 'speech', model: 'vids-speech', status: 'completed',
        started: now - index * 180000, ended: now - index * 180000 + 5000,
        key: `readme-speech-${index}`, receipt, title, description: title, message: '演示音频已就绪',
        outputs: [{ kind: 'audio', path: `/api/admin/vids/v1/vids/generations/${receipt}/content` }] }
})
function demoWav() {
    const samples = 16000 * 4, buffer = Buffer.alloc(44 + samples * 2)
    buffer.write('RIFF', 0); buffer.writeUInt32LE(buffer.length - 8, 4); buffer.write('WAVEfmt ', 8)
    buffer.writeUInt32LE(16, 16); buffer.writeUInt16LE(1, 20); buffer.writeUInt16LE(1, 22)
    buffer.writeUInt32LE(16000, 24); buffer.writeUInt32LE(32000, 28); buffer.writeUInt16LE(2, 32)
    buffer.writeUInt16LE(16, 34); buffer.write('data', 36); buffer.writeUInt32LE(samples * 2, 40)
    for (let index = 0; index < samples; index++)
        buffer.writeInt16LE(Math.round(Math.sin(index * Math.PI * 440 / 16000) * 2000 * Math.sin(index / samples * Math.PI)), 44 + index * 2)
    return buffer
}
const audioBody = demoWav()
await context.route('**/*', async (route) => {
    const request = route.request(), url = new URL(request.url())
    if (url.origin !== base) {
        external.push({ method: request.method(), url: request.url() })
        return route.abort()
    }
    if (url.pathname.startsWith('/__readme__/')) {
        const name = url.pathname.split('/').pop()
        assert.ok(images.includes(name), 'Only approved repository illustrations are served')
        return route.fulfill({ contentType: 'image/webp', body: readFileSync(resolve(root, 'frontend/src/assets', name)) })
    }
    const speechEntry = speechHistory.find((entry) => url.pathname === `/api/admin/vids/v1/vids/generations/${entry.receipt}` || url.pathname === entry.outputs[0].path)
    if (speechEntry && ['GET', 'HEAD'].includes(request.method())) {
        requests.push(`${request.method()} ${url.pathname}`)
        return url.pathname.endsWith('/content')
            ? route.fulfill({ contentType: 'audio/wav', body: audioBody, headers: { 'Accept-Ranges': 'bytes' } })
            : json(route, { id: speechEntry.receipt, kind: 'speech', status: 'completed', outputs: [{ index: 0, content_url: speechEntry.outputs[0].path }] })
    }
    if (!url.pathname.startsWith('/api/') && url.pathname !== '/health') return route.continue()
    requests.push(`${request.method()} ${url.pathname}`)
    if (!['GET', 'HEAD'].includes(request.method())) {
        unexpected.push(`${request.method()} ${url.pathname}`)
        return json(route, { detail: 'README screenshot fixture blocks every write' }, 503)
    }
    if (url.pathname === '/api/stats' && !authenticated) return json(route, { detail: 'Preview login' }, 401)
    if (Object.hasOwn(fixtures, url.pathname)) return json(route, fixtures[url.pathname])
    unexpected.push(`${request.method()} ${url.pathname}`)
    return json(route, { detail: 'Unknown README fixture request' }, 404)
})
async function ready(selector) {
    await page.locator(selector).first().waitFor()
    await page.evaluate(() => document.fonts.ready)
    await page.waitForFunction(() => [...document.querySelectorAll('img')].filter((image) => {
        const r = image.getBoundingClientRect()
        return r.top < innerHeight && r.bottom > 0 && r.width > 0
    }).every((image) => image.complete && image.naturalWidth > 0))
    await page.locator('.el-skeleton:visible, .settings-skeleton:visible').first().waitFor({ state: 'hidden' }).catch(() => {})
}
async function go(route, selector) {
    await page.goto(`${base}/manage#/${route}`, { waitUntil: 'domcontentloaded' })
    await ready(selector)
}
async function capture(name, description) {
    await page.mouse.move(0, 0)
    await page.evaluate(() => {
        if (document.activeElement instanceof HTMLElement) document.activeElement.blur()
    })
    const bounds = await page.evaluate(() => ({ viewport: innerWidth, document: document.documentElement.scrollWidth, height: innerHeight }))
    assert.ok(bounds.document <= bounds.viewport + 1, `${name}: document overflow ${JSON.stringify(bounds)}`)
    await page.screenshot({ path: resolve(review, `${name}.png`), animations: 'disabled' })
    captures.push({ file: `docs/screenshots/${name}.webp`, description, width: bounds.viewport, height: bounds.height })
    console.log(`CAPTURE ${name}`)
}
try {
    await page.goto(`${base}/login#/login`, { waitUntil: 'domcontentloaded' })
    await ready('.login-page .login-scene')
    await capture('login', '萌娘 lay 登录页与分层插画场景')
    authenticated = true
    await go('dashboard', '.dashboard-page h1')
    await capture('dashboard', 'Flow 与 Vids 工坊总览、创作统计及可用额度')
    await go('studio', '.studio-view h1')
    await page.locator('.gallery-card').nth(5).waitFor()
    for (const card of await page.locator('.gallery-card').all()) {
        await card.scrollIntoViewIfNeeded()
        await card.locator('img').waitFor()
        await card.locator('img').evaluate((image) => image.decode())
    }
    await page.evaluate(() => {
        window.scrollTo(0, 0)
        document.querySelector('.studio-canvas')?.scrollTo(0, 0)
    })
    await ready('.gallery-card img')
    await page.locator('.studio-editor textarea').fill('樱花树下的 lay，轻盈的春日微风，温暖明亮的日系插画。')
    await capture('studio', '聚合创作工作台、模型参数及本地示例作品')
    await page.locator('.reference-board input[type="file"]').setInputFiles({
        name: 'lay-春日灵感.webp', mimeType: 'image/webp',
        buffer: readFileSync(resolve(root, 'frontend/src/assets/koharu-banner.webp')),
    })
    await page.locator('.reference-board .reference-item').first().getByRole('button', { name: '编辑', exact: true }).click()
    const editor = page.locator('.studio-image-editor-dialog')
    await ready('.editor-canvas')
    await editor.getByText('16 : 9', { exact: true }).click()
    await editor.locator('textarea').fill('保留 lay 的人物与姿态，让背景增添细小的樱花花瓣，柔和的粉色春日光线。')
    const canvasBox = await editor.locator('.editor-canvas').boundingBox()
    await page.mouse.move(canvasBox.x + canvasBox.width * 0.18, canvasBox.y + canvasBox.height * 0.63)
    await page.mouse.down()
    await page.mouse.move(canvasBox.x + canvasBox.width * 0.25, canvasBox.y + canvasBox.height * 0.55, { steps: 8 })
    await page.mouse.move(canvasBox.x + canvasBox.width * 0.31, canvasBox.y + canvasBox.height * 0.62, { steps: 8 })
    await page.mouse.up()
    await capture('image-editor', '本地图片编辑：裁剪、旋转、画笔图层、PNG 下载与 AI 编辑衔接')
    await editor.getByRole('button', { name: '关闭', exact: true }).click()
    await go('accounts', '.accounts-view h1')
    await page.locator('.accounts-table .account-identity').filter({ hasText: 'sakura@example.test' }).click()
    await ready('.account-drawer .account-balances')
    await capture('accounts', 'Flow AT 自动刷新与 Vids 会话、独立额度一览')
    await page.locator('.account-drawer .el-drawer__close-btn').click()
    await page.locator('.account-drawer').waitFor({ state: 'hidden' })
    await page.getByRole('button', { name: '更多账号操作', exact: true }).click()
    await page.getByRole('menuitem', { name: '导入 JSON', exact: true }).click()
    await ready('.el-dialog:visible .import-text')
    const bundle = {
        format: 'lay4flow-profile-bundle', version: 1,
        accounts: [{ format: 'lay4flow-cloak-session', version: 1,
            account: { email: 'sakura@example.test', is_active: true },
            browser: { fingerprint_seed: 4101 }, storage_state: { cookies: [], origins: [] } }],
    }
    await page.getByPlaceholder('粘贴导出的 Profile JSON').fill(JSON.stringify(bundle, null, 2))
    await page.getByPlaceholder('粘贴导出的 Profile JSON').evaluate((input) => {
        input.scrollTop = 0
        input.setSelectionRange(0, 0)
    })
    await capture('session-transfer', '免重复登录 Profile JSON 导入与目标服务器代理选择')
    await page.getByRole('dialog', { name: '导入账号 Profile', exact: true }).getByRole('button', { name: '关闭', exact: true }).click()
    await go('proxies', '.proxy-pool-view h1')
    await page.getByRole('button', { name: '批量导入', exact: true }).click()
    await ready('.proxy-import-dialog')
    await page.getByRole('textbox', { name: '批量代理地址，每行一个', exact: true }).fill('192.0.2.31:1080:USER:PASSWORD\n192.0.2.32:1080:USER:PASSWORD\n192.0.2.33:1080:USER:PASSWORD')
    await capture('proxy-pool', 'SOCKS5 代理池与每行一条四段格式批量导入')
    await page.getByRole('dialog', { name: '批量导入代理', exact: true }).getByRole('button', { name: '关闭', exact: true }).click()
    await go('guide?platform=vids', '.guide-view h1')
    await capture('api-guide', 'Flow、Vids 与商务固定 SKU 三种接入目录')
    await go('settings', '.settings-view h1')
    await page.locator('.settings-search input').fill('远程')
    await ready('[data-setting-card="captcha"]')
    await capture('settings', '远程有头浏览器服务配置')
    await page.setViewportSize({ width: 390, height: 844 })
    await go('dashboard', '.dashboard-page h1')
    await capture('mobile', '手机端工坊总览')
    await page.setViewportSize({ width: 1600, height: 1000 })
    await page.evaluate((entries) => {
        localStorage.setItem('lay4flow_studio_receipts:v2:lay', JSON.stringify(entries))
        sessionStorage.setItem('lay4flow_studio_receipts', JSON.stringify(entries))
    }, speechHistory)
    await go('studio?platform=vids', '.studio-view h1')
    await page.getByRole('tab', { name: '声线 · 语音', exact: true }).click()
    await page.locator('.studio-editor textarea').fill('Welcome to our little creative studio. Let your imagination bloom, and bring every story to life.')
    await page.waitForFunction(() => document.querySelectorAll('.gallery-card audio').length === 3 && [...document.querySelectorAll('.gallery-card audio')].every((audio) => audio.readyState >= 2))
    await page.locator('.el-form-item').filter({ has: page.locator('.el-form-item__label').getByText('选择声线', { exact: true }) }).locator('.el-select').click()
    await page.getByRole('option', { name: 'Neo · 欢快、音调适中', exact: true }).waitFor()
    await capture('vids-studio', 'Vids 语音工作台、官方声线选择与可播放的合成示例音频')
    assert.deepEqual(errors, [], 'No browser page errors')
    assert.deepEqual(failedAssets, [], 'All bundled assets load')
    assert.deepEqual(unexpected, [], 'All API reads are explicitly mocked; no writes')
    assert.deepEqual(external, [], 'No upstream or external request')
    // Pillow is already used by repository artwork tooling. Keep product images
    // compact while retaining readable Chinese text at native screenshot size.
    const converted = spawnSync(process.env.PYTHON || 'python', ['-c',
        'from pathlib import Path; from PIL import Image; import sys; src=Path(sys.argv[1]); dst=Path(sys.argv[2]); [Image.open(p).convert("RGB").save(dst/(p.stem+".webp"), "WEBP", quality=90, method=6) for p in src.glob("*.png") if p.stem in sys.argv[3:]]',
        review, output, ...captures.map((item) => item.file.split('/').pop().replace('.webp', '')),
    ], { encoding: 'utf8' })
    assert.equal(converted.status, 0, converted.stderr)
    await writeFile(resolve(review, 'verification.json'), JSON.stringify({
        base, captures, errors, failedAssets, unexpected, external, requests,
        fixtures: { accounts: accounts.map((row) => row.email), proxyNetwork: '192.0.2.0/24', artwork: images,
            data: 'Synthetic UI fixtures; every API request mocked; no generation request submitted; cookies/origins empty. Speech previews are local synthesized tones, not upstream voice generation.' },
    }, null, 2))
    console.log(`Verified ${captures.length} screenshots in docs/screenshots`)
} catch (error) {
    await writeFile(resolve(review, 'failure.json'), JSON.stringify({ message: error.message, errors, failedAssets, unexpected, external, requests, body: await page.locator('body').innerText() }, null, 2))
    await page.screenshot({ path: resolve(review, 'failure.png'), animations: 'disabled' })
    throw error
} finally {
    await browser.close()
}
