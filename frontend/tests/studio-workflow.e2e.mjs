import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { chromium } from 'playwright'

// Only scripts/preview_frontend.py is used. All generation mutations are blocked;
// the original Vids content endpoint is fulfilled with local synthetic media.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38128'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, '../../output/studio-workflow')
const executablePath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/chromium',
]
    .filter(Boolean)
    .find(existsSync)
assert.ok(executablePath)
await mkdir(output, { recursive: true })
const art = await readFile(resolve(root, 'frontend/src/assets/koharu-banner.webp'))
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1680, height: 1080 },
    reducedMotion: 'reduce',
    locale: 'zh-CN',
})
context.setDefaultTimeout(15000)
const page = await context.newPage()
const pageErrors = [],
    mutations = [],
    contentReads = [],
    checks = []
let breakVidsImage = false,
    videoBody,
    allowMockGeneration = false
const mockBodies = []
const archiveRequests = []
const archiveId = 'f'.repeat(64)
const archiveURL = `/api/admin/studio/media/${archiveId}/content`
const receipt = (char) => `vids_${char.repeat(64)}`
const content = (char) => `/api/admin/vids/v1/vids/generations/${receipt(char)}/content?index=0`
const fullPrompt = `完整提示词：${'日漫海边少女、明亮自然光、精细线稿；'.repeat(18)}末尾唯一检索词-珊瑚星云`
const history = [
    {
        key: 'failed-a',
        provider: 'flow',
        kind: 'image',
        model: 'fixture-image',
        status: 'failed',
        title: '失败请求 A',
        description: '失败后可直接删除',
        started: Date.now(),
        outputs: [],
        message: 'Fixture upstream failure',
    },
    {
        key: 'failed-b',
        provider: 'flow',
        kind: 'image',
        model: 'fixture-image',
        status: 'failed',
        title: '失败请求 B',
        started: Date.now() - 1,
        outputs: [],
    },
    {
        key: 'flow-image',
        provider: 'flow',
        kind: 'image',
        model: 'fixture-image',
        status: 'completed',
        title: fullPrompt.slice(0, 100),
        description: fullPrompt,
        settings: { mode: 'text', aspectRatio: '1:1', imageSize: '1k' },
        started: Date.now() - 2,
        outputs: [{ kind: 'image', url: `${base}/__workflow__/art.webp` }],
    },
    {
        key: 'vids-video',
        receipt: receipt('a'),
        provider: 'vids',
        kind: 'video',
        model: 'fixture-video',
        status: 'completed',
        title: 'Vids 原视频持续可见',
        started: Date.now() - 3,
        outputs: [{ kind: 'video', path: content('a') }],
    },
    {
        key: 'vids-image',
        receipt: receipt('b'),
        provider: 'vids',
        kind: 'image',
        model: 'fixture-image',
        status: 'completed',
        title: 'Vids 原图片持续可见',
        started: Date.now() - 4,
        outputs: [{ kind: 'image', path: content('b') }],
    },
    {
        key: 'unknown',
        receipt: receipt('c'),
        provider: 'vids',
        kind: 'video',
        model: 'fixture-video',
        status: 'unknown',
        title: '待确认原收据',
        started: Date.now() - 5,
        outputs: [],
    },
]
const record = (value) => {
    checks.push(value)
    console.log(`PASS ${value}`)
}
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
function card(key, target = page) {
    return target.locator(`.gallery-card[data-entry-key="${key}"]`)
}
async function waitMedia(target = page) {
    await card('vids-image', target).scrollIntoViewIfNeeded()
    await card('vids-image', target).locator('img').waitFor()
    await target.waitForFunction(() => {
        const image = document.querySelector('.gallery-card[data-entry-key="vids-image"] img')
        return image?.complete && image.naturalWidth > 0
    })
    await card('vids-video', target).scrollIntoViewIfNeeded()
    await target.waitForFunction(() => {
        const video = document.querySelector('.gallery-card[data-entry-key="vids-video"] video')
        const image = document.querySelector('.gallery-card[data-entry-key="vids-image"] img')
        return video?.readyState >= 2 && image?.complete && image.naturalWidth > 0
    })
}
async function navigateAwayBack() {
    await page.evaluate(() => {
        location.hash = '#/guide'
    })
    await page.locator('.guide-view h1').waitFor()
    await page.evaluate(() => {
        location.hash = '#/studio'
    })
    await page.locator('.studio-view h1').waitFor()
}
try {
    // One-second synthetic VP8 fixture avoids headless recording timing variability.
    videoBody = Buffer.from(
        'GkXfo59ChoEBQveBAULygQRC84EIQoKEd2VibUKHgQJChYECGFOAZwEAAAAAAALWEU2bdLpNu4tTq4QVSalmU6yBoU27i1OrhBZUrmtTrIHWTbuMU6uEElTDZ1OsggEyTbuMU6uEHFO7a1OsggLA7AEAAAAAAABZAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAVSalmsCrXsYMPQkBNgIxMYXZmNjMuMS4xMDFXQYxMYXZmNjMuMS4xMDFEiYhAj0AAAAAAABZUrmvXrgEAAAAAAABO14EBc8WIoI5vcDdw4ImcgQAitZyDdW5kiIEAhoVWX1ZQOIOBASPjg4QHc1lA4JCwgaC6gVqagQJVsIRVuYEBVe6BAOwBAAAAAAAAAgAAElTDZ/pzc59jwIBnyJlFo4dFTkNPREVSRIeMTGF2ZjYzLjEuMTAxc3PVY8CLY8WIoI5vcDdw4IlnyKBFo4dFTkNPREVSRIeTTGF2YzYzLjEuMTAxIGxpYnZweGfIoUWjiERVUkFUSU9ORIeTMDA6MDA6MDEuMDAwMDAwMDAwAB9DtnVBCeeBAKPPgQAAgHAFAJ0BKqAAWgAARwiFhYiFhIgCAgJ08kDoLGqpNdxDqqTXcQ6qk13EOqpNdxDqqTXcQ6YA/v7WJHLM4V7L/+4OP/7g4//uDj90wKOYgQB9ABECAAEQEAAYABhYL/QACICBAAAAo5iBAPoAEQIAARAQABgAGFgv9AAIgIEAAACjmIEBdwARAgABEBAAGAAYWC/0AAiAgQAAAKOYgQH0ABECAAEQEAAYABhYL/QACICBAAAAo5iBAnEAEQIAARAQABgAGFgv9AAIgIEAAACjmIEC7gARAgABEBAAGAAYWC/0AAiAgQAAAKOXgQNrAPEBAAEQEBRgAGFgv9AAIgIEAAAcU7trkbuPs4EAt4r3gQHxggGx8IED',
        'base64',
    )
    const login = await context.request.post(`${base}/api/login`, {
        data: { username: 'preview-admin', password: 'preview-password' },
    })
    assert.equal(login.status(), 200, 'Use the synthetic preview server')
    await context.addInitScript(
        ({ entries }) => {
            localStorage.setItem('admin_username', 'preview-admin')
            if (!sessionStorage.getItem('studio_workflow_seeded') && !localStorage.getItem('studio_workflow_seeded')) {
                const serialized = JSON.stringify(entries)
                localStorage.setItem('lay4flow_studio_receipts:v2:preview-admin', serialized)
                sessionStorage.setItem('lay4flow_studio_receipts', serialized)
                sessionStorage.setItem('lay4flow_studio_receipts:owner', 'preview-admin')
                localStorage.setItem('studio_workflow_seeded', '1')
            }
            sessionStorage.setItem('studio_workflow_seeded', '1')
        },
        { entries: history },
    )
    await context.route('**/*', async (route) => {
        const request = route.request(),
            url = new URL(request.url())
        if (url.origin !== base) return route.abort()
        if (url.pathname === '/api/admin/studio/media/archive') {
            assert.equal(request.method(), 'POST')
            const source = new URL(request.postDataJSON().url, base)
            assert.equal(source.origin, base)
            assert.equal(source.pathname, '/tmp/workflow-legacy.webp')
            archiveRequests.push(source.href)
            return json(route, { id: archiveId, url: archiveURL, kind: 'image', cached: true })
        }
        if (url.pathname === archiveURL) return route.fulfill({ contentType: 'image/webp', body: art })
        if (url.pathname === '/tmp/workflow-legacy.webp')
            return json(route, { detail: 'Expired temporary media fixture' }, 404)
        if (
            allowMockGeneration &&
            request.method() === 'POST' &&
            url.pathname === '/api/admin/studio/chat/completions'
        ) {
            mockBodies.push(request.postDataJSON())
            return route.fulfill({
                contentType: 'text/event-stream',
                body: `data: ${JSON.stringify({ choices: [{ delta: { content: `![Edited fixture](${base}/__workflow__/art.webp)` } }] })}\n\ndata: [DONE]\n\n`,
            })
        }
        if (!['GET', 'HEAD'].includes(request.method())) {
            mutations.push(`${request.method()} ${url.pathname}`)
            return json(route, { detail: 'Workflow E2E blocks mutations' }, 503)
        }
        if (url.pathname === '/__workflow__/art.webp') return route.fulfill({ contentType: 'image/webp', body: art })
        if (url.pathname === content('a').split('?')[0]) {
            contentReads.push(url.pathname)
            return route.fulfill({ contentType: 'video/webm', body: videoBody, headers: { 'Accept-Ranges': 'bytes' } })
        }
        if (url.pathname === content('b').split('?')[0]) {
            contentReads.push(url.pathname)
            return breakVidsImage
                ? json(route, { detail: 'Fixture content temporarily unavailable' }, 503)
                : route.fulfill({ contentType: 'image/webp', body: art })
        }
        if (url.pathname === `/api/admin/vids/v1/vids/generations/${receipt('c')}`)
            return json(route, { id: receipt('c'), status: 'unknown' })
        if (url.pathname === '/health') return json(route, { backend_running: true, has_active_tokens: true })
        if (url.pathname === '/api/stats') return json(route, { active_tokens: 1, total_tokens: 1 })
        if (url.pathname === '/api/admin/studio/models')
            return json(route, {
                data: [
                    {
                        id: 'fixture-image',
                        displayName: 'Workflow image',
                        type: 'image',
                        capabilities: {
                            aspectRatios: ['16:9', '1:1'],
                            imageSizes: ['1k'],
                            modes: ['text', 'edit'],
                            maxImages: 4,
                        },
                        defaults: { aspectRatio: '16:9', imageSize: '1k' },
                    },
                ],
            })
        if (url.pathname === '/api/admin/vids/status')
            return json(route, { configured: true, available: { image: 1, video: 1 }, recoverable: {}, quota: {} })
        if (url.pathname === '/api/admin/vids/v1/models') return json(route, { data: [] })
        if (url.pathname.startsWith('/api/')) return json(route, {})
        return route.continue()
    })
    page.on('pageerror', (error) => pageErrors.push(error.message))
    await page.goto(`${base}/manage#/studio`, { waitUntil: 'domcontentloaded' })
    await page.waitForFunction(() => document.querySelectorAll('.gallery-card').length === 6)
    await waitMedia()
    assert.equal(await card('unknown').getByRole('button', { name: '删除记录', exact: true }).count(), 0)
    record('Vids image/video restore immediately; unresolved receipt retains its recovery actions')

    await card('failed-a').getByRole('button', { name: '删除记录', exact: true }).click()
    await card('failed-a').waitFor({ state: 'detached' })
    await page.reload({ waitUntil: 'domcontentloaded' })
    await waitMedia()
    assert.equal(await card('failed-a').count(), 0)
    await page.getByRole('button', { name: '清理失败 1', exact: true }).click()
    await card('failed-b').waitFor({ state: 'detached' })
    record('Failed cards delete directly and stay deleted after reload; clear failed removes the remaining failure')

    await page.getByRole('textbox', { name: '搜索作品', exact: true }).fill('珊瑚星云')
    assert.equal(await page.locator('.gallery-card').count(), 1)
    await card('flow-image').getByRole('button', { name: '重用提示词', exact: true }).click()
    await page.waitForFunction(
        (expected) => document.querySelector('.studio-editor textarea')?.value === expected,
        fullPrompt,
    )
    const dragged = await card('flow-image')
        .locator('.gallery-media')
        .evaluate((element) => {
            const transfer = new DataTransfer()
            element.dispatchEvent(new DragEvent('dragstart', { bubbles: true, dataTransfer: transfer }))
            return {
                media: transfer.getData('application/x-lay4flow-media'),
                prompt: transfer.getData('application/x-lay4flow-prompt'),
            }
        })
    assert.deepEqual(JSON.parse(dragged.media), { entryKey: 'flow-image', outputIndex: 0 })
    assert.equal(dragged.prompt, fullPrompt)
    assert.ok(!dragged.media.includes('http'), 'Drag identity contains no URL or request body')
    await page.getByRole('textbox', { name: '搜索作品', exact: true }).fill('')
    record(
        'Full prompt is searchable and reusable beyond the 100-character card title; image drag carries safe identity and full prompt',
    )

    for (let iteration = 0; iteration < 3; iteration++) {
        await navigateAwayBack()
        await waitMedia()
    }
    await page.reload({ waitUntil: 'domcontentloaded' })
    await waitMedia()
    const secondPage = await context.newPage()
    secondPage.on('pageerror', (error) => pageErrors.push(error.message))
    await secondPage.goto(`${base}/manage#/studio`)
    await waitMedia(secondPage)
    await secondPage.close()
    record('Vids video and image survive three SPA route round-trips, a refresh and a new tab')

    breakVidsImage = true
    await page.reload({ waitUntil: 'domcontentloaded' })
    await card('vids-image').getByText('预览加载失败', { exact: true }).waitFor()
    assert.match(await card('vids-image').innerText(), /已生成 · 预览失效/)
    breakVidsImage = false
    await card('vids-image').getByRole('button', { name: '重新读取预览', exact: true }).click()
    await waitMedia()
    assert.ok(
        await card('vids-image')
            .locator('img')
            .getAttribute('src')
            .then((value) => value.startsWith('blob:')),
    )
    breakVidsImage = true
    await navigateAwayBack()
    await waitMedia()
    assert.ok(
        await card('vids-image')
            .locator('img')
            .getAttribute('src')
            .then((value) => value.startsWith('blob:')),
    )
    record(
        'Expired preview retries the original content with GET and later restores from IndexedDB while the original endpoint is unavailable',
    )

    await page.setViewportSize({ width: 1680, height: 1080 })
    await card('flow-image').getByRole('button', { name: '重用提示词', exact: true }).click()
    const board = page.getByRole('region', { name: '参考图片素材区', exact: true })
    await board.locator('input[type="file"]').setInputFiles([
        { name: 'upload-first.webp', mimeType: 'image/webp', buffer: art },
        { name: 'upload-second.webp', mimeType: 'image/webp', buffer: art },
    ])
    await page.waitForFunction(() => document.querySelectorAll('.reference-item').length === 2)
    await board.getByRole('button', { name: '参考图 2 向前移动', exact: true }).click()
    assert.match(await board.locator('.reference-item').first().innerText(), /upload-second/)
    await page.evaluate(
        (bytes) => {
            const clipboardData = new DataTransfer()
            clipboardData.items.add(new File([new Uint8Array(bytes)], 'clipboard-third.webp', { type: 'image/webp' }))
            document.dispatchEvent(new ClipboardEvent('paste', { bubbles: true, clipboardData }))
        },
        [...art],
    )
    await page.waitForFunction(() => document.querySelectorAll('.reference-item').length === 3)
    assert.match(await board.locator('.reference-item').last().innerText(), /clipboard-third/)
    const mediaTransfer = await page.evaluateHandle(() => new DataTransfer())
    await card('flow-image').locator('.gallery-media').dispatchEvent('dragstart', { dataTransfer: mediaTransfer })
    await board.dispatchEvent('drop', { dataTransfer: mediaTransfer })
    await page.waitForFunction(() => document.querySelectorAll('.reference-item').length === 4)
    assert.match(await board.locator('.reference-item').last().innerText(), /作品参考/)
    await board.getByRole('button', { name: '移除参考图 2', exact: true }).click()
    assert.equal(await board.locator('.reference-item').count(), 3)
    await board.getByRole('button', { name: '清空', exact: true }).click()
    assert.equal(await board.locator('.reference-item').count(), 0)
    record(
        'Reference board imports multiple files, reorders, accepts clipboard images and dropped results, then removes or clears directly',
    )

    await card('flow-image').getByRole('button', { name: '编辑图片', exact: true }).click()
    const editor = page.locator('.studio-image-editor-dialog:visible')
    await editor.locator('canvas.editor-canvas').waitFor()
    await editor.getByRole('button', { name: '向右旋转', exact: true }).click()
    await editor.locator('.editor-controls .el-segmented').getByText('1 : 1', { exact: true }).click()
    await editor.getByPlaceholder('可选：描述你想让 AI 编辑的内容').fill('保留人物，变换为水彩夕阳海岸')
    await editor.getByRole('button', { name: '使用此图进行 AI 编辑', exact: true }).click()
    await editor.waitFor({ state: 'hidden' })
    await page.waitForFunction(() => document.querySelectorAll('.reference-item').length === 1)
    const imageData = await board.locator('.reference-item img').getAttribute('src')
    assert.match(imageData, /^data:image\/png;base64,/)
    assert.equal(await page.locator('.studio-editor textarea').inputValue(), '保留人物，变换为水彩夕阳海岸')
    assert.equal(mockBodies.length, 0, 'Editor apply prepares inputs; user explicitly starts generation')
    allowMockGeneration = true
    await page.getByRole('button', { name: '开始绘梦', exact: true }).click()
    await page.waitForFunction(
        () =>
            document.querySelector('.task-status')?.textContent?.includes('作品已完成') ||
            [...document.querySelectorAll('.gallery-card')].some((card) =>
                card.textContent.includes('保留人物，变换为水彩夕阳海岸'),
            ),
    )
    assert.equal(mockBodies.length, 1)
    const submitted = mockBodies[0]
    assert.equal(submitted.model, 'fixture-image')
    assert.deepEqual(
        submitted.messages[0].content.map((item) => item.type),
        ['text', 'image_url'],
    )
    assert.equal(submitted.messages[0].content[0].text, '保留人物，变换为水彩夕阳海岸')
    assert.equal(submitted.messages[0].content[1].image_url.url, imageData)
    allowMockGeneration = false
    record(
        'Gallery image editor prepares transformed PNG and edit prompt; explicit submit sends the real multimodal Flow contract exactly once',
    )

    await page.evaluate(
        (entry) => {
            const key = 'lay4flow_studio_receipts:v2:preview-admin'
            const entries = JSON.parse(localStorage.getItem(key) || '[]')
            entries.unshift(entry)
            localStorage.setItem(key, JSON.stringify(entries))
        },
        {
            key: 'legacy-flow',
            provider: 'flow',
            kind: 'image',
            model: 'fixture-image',
            status: 'completed',
            title: '旧 Flow 临时作品自动归档',
            started: Date.now(),
            outputs: [{ kind: 'image', url: `${base}/tmp/workflow-legacy.webp` }],
        },
    )
    await page.reload({ waitUntil: 'domcontentloaded' })
    await card('legacy-flow').scrollIntoViewIfNeeded()
    await page.waitForFunction((path) => {
        const image = document.querySelector('.gallery-card[data-entry-key="legacy-flow"] img')
        return image?.complete && image.naturalWidth > 0 && new URL(image.src).pathname === path
    }, archiveURL)
    assert.equal(archiveRequests.length, 1)
    const storedArchive = await page.evaluate(() =>
        JSON.parse(localStorage.getItem('lay4flow_studio_receipts:v2:preview-admin')).find(
            (entry) => entry.key === 'legacy-flow',
        ),
    )
    assert.equal(new URL(storedArchive.outputs[0].url, base).pathname, archiveURL)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await card('legacy-flow').scrollIntoViewIfNeeded()
    await page.waitForFunction((path) => {
        const image = document.querySelector('.gallery-card[data-entry-key="legacy-flow"] img')
        return image?.complete && image.naturalWidth > 0 && new URL(image.src).pathname === path
    }, archiveURL)
    assert.equal(archiveRequests.length, 1, 'Archived results never rearchive on refresh')
    record(
        'A historical expired Flow /tmp result archives without generation and reloads from its stable protected content URL',
    )

    for (const width of [1680, 768, 390]) {
        await page.setViewportSize({ width, height: width === 390 ? 844 : 1080 })
        const metrics = await page.evaluate(() => ({ width: innerWidth, scroll: document.documentElement.scrollWidth }))
        assert.ok(metrics.scroll <= metrics.width + 1)
        await page.screenshot({
            path: resolve(output, `workflow-${width}.png`),
            fullPage: false,
            animations: 'disabled',
        })
    }
    assert.deepEqual(mutations, [], 'History/preview actions never generate again')
    assert.deepEqual(pageErrors, [])
    record(
        'Desktop/tablet/mobile remain usable without horizontal overflow; no unexpected generation POST or runtime error',
    )
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify(
            { checks, contentReadCount: contentReads.length, archiveRequests, mutations, pageErrors },
            null,
            2,
        ),
    )
} catch (error) {
    console.error(
        await page.evaluate(() =>
            [...document.querySelectorAll('.gallery-card')].map((card) => ({
                key: card.dataset.entryKey,
                text: card.textContent,
                image: card.querySelector('img')?.src,
                video: card.querySelector('video')
                    ? {
                          source: card.querySelector('video').src,
                          state: card.querySelector('video').readyState,
                          error: card.querySelector('video').error?.message,
                      }
                    : null,
            })),
        ),
    )
    await page.screenshot({ path: resolve(output, 'workflow-failure.png'), fullPage: false })
    throw error
} finally {
    await context.close()
    await browser.close()
}
