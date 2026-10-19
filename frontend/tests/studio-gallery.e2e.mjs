import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { chromium } from 'playwright'

// Run only against scripts/preview_frontend.py. The browser intercepts every
// generation/API request; historical works and stress-test media are local fixtures.
const base = process.env.FRONTEND_PREVIEW_URL || 'http://127.0.0.1:38123'
assert.match(base, /^http:\/\/(127\.0\.0\.1|localhost):\d+$/)
const root = fileURLToPath(new URL('../..', import.meta.url))
const output = resolve(root, '../../output/studio-density')
const browserPath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
]
    .filter(Boolean)
    .find(existsSync)
assert.ok(browserPath, 'Install Chromium or set PLAYWRIGHT_BROWSER_PATH')
await mkdir(output, { recursive: true })

const imageKinds = [
    { name: 'portrait', width: 540, height: 810, artwork: 'koharu-portrait.webp' },
    { name: 'landscape', width: 960, height: 540, artwork: 'koharu-banner.webp' },
    { name: 'square', width: 640, height: 640, artwork: 'koharu-welcome.webp' },
]
const imageBodies = await Promise.all(
    imageKinds.map(async (item) => {
        const bytes = await readFile(resolve(root, 'frontend/src/assets', item.artwork))
        return `<svg xmlns="http://www.w3.org/2000/svg" width="${item.width}" height="${item.height}" viewBox="0 0 ${item.width} ${item.height}"><image href="data:image/webp;base64,${bytes.toString('base64')}" width="100%" height="100%" preserveAspectRatio="xMidYMid slice"/></svg>`
    }),
)
function silentWav() {
    const samples = 8000,
        buffer = Buffer.alloc(44 + samples * 2)
    buffer.write('RIFF', 0)
    buffer.writeUInt32LE(buffer.length - 8, 4)
    buffer.write('WAVEfmt ', 8)
    buffer.writeUInt32LE(16, 16)
    buffer.writeUInt16LE(1, 20)
    buffer.writeUInt16LE(1, 22)
    buffer.writeUInt32LE(8000, 24)
    buffer.writeUInt32LE(16000, 28)
    buffer.writeUInt16LE(2, 32)
    buffer.writeUInt16LE(16, 34)
    buffer.write('data', 36)
    buffer.writeUInt32LE(samples * 2, 40)
    return buffer
}
const started = Date.UTC(2026, 9, 15, 12, 30)
const history = Array.from({ length: 12 }, (_, index) => {
    const kind = index < 9 ? 'image' : index < 11 ? 'video' : 'speech'
    const path = kind === 'image' ? `image-${index % 3}.svg` : kind === 'video' ? 'motion.webm' : 'voice.wav'
    return {
        provider: 'flow',
        kind,
        model: kind === 'image' ? 'gemini-3.0-pro-image' : kind === 'video' ? 'veo_3_1' : 'fixture-speech',
        status: 'completed',
        started: started - index * 60000,
        ended: started - index * 60000 + 1000,
        key: `gallery-fixture-${index}`,
        receipt: '',
        title: `Gallery fixture ${index + 1} · ${kind === 'image' ? imageKinds[index % 3].name : kind}`,
        message: 'Fixture completed locally',
        outputs: [{ kind: kind === 'speech' ? 'audio' : kind, url: `${base}/__studio_fixture__/${path}` }],
    }
})
const browser = await chromium.launch({ executablePath: browserPath, headless: true })
const context = await browser.newContext({
    viewport: { width: 1920, height: 1080 },
    reducedMotion: 'reduce',
    acceptDownloads: true,
    locale: 'zh-CN',
})
context.setDefaultTimeout(15000)
const page = await context.newPage()
const checks = [],
    pageErrors = [],
    failedAssets = [],
    generationRequests = [],
    unexpectedMutations = [],
    fixtureImageRequests = new Set(),
    density = []
let allowMockGeneration = false,
    mockGenerations = 0
const record = (value) => {
    checks.push(value)
    console.log(`PASS ${value}`)
}
page.on('pageerror', (error) => pageErrors.push(error.message))
page.on('response', (response) => {
    if (response.status() >= 400 && response.url().includes('/static/app/assets/'))
        failedAssets.push(`${response.status()} ${response.url()}`)
})
const json = (route, body, status = 200) =>
    route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
async function cardsLoaded(expected = 12) {
    await page.waitForFunction((count) => document.querySelectorAll('.gallery-card').length === count, expected)
    // Lazy images below the first viewport must be decoded before checking heights.
    for (const card of await page.locator('.gallery-card').all()) await card.scrollIntoViewIfNeeded()
    await page.waitForFunction(() =>
        [...document.querySelectorAll('.gallery-card img')].every((image) => image.complete && image.naturalWidth > 0),
    )
}
async function screenshot(name) {
    if (!name.includes('lightbox')) await resetScroll()
    await page.mouse.move(0, 0)
    await page.screenshot({ path: resolve(output, `${name}.png`), fullPage: false, animations: 'disabled' })
}
async function resetScroll() {
    await page.evaluate(() => {
        window.scrollTo(0, 0)
        document.querySelector('.studio-canvas')?.scrollTo(0, 0)
    })
}
async function layout(width) {
    await page.setViewportSize({ width, height: width < 700 ? 844 : 1080 })
    await resetScroll()
    await page.waitForFunction(() => {
        const grid = document.querySelector('.gallery-masonry')
        const signature = [...grid.children]
            .map((card) => {
                const rect = card.getBoundingClientRect()
                return `${rect.x}/${rect.y}/${rect.width}/${rect.height}`
            })
            .join('|')
        const prior = window.__galleryLayoutWait
        window.__galleryLayoutWait = { signature, frames: prior?.signature === signature ? prior.frames + 1 : 0 }
        return window.__galleryLayoutWait.frames >= 6
    })
    const metrics = await page.evaluate(() => {
        const main = document.querySelector('.main-content').getBoundingClientRect()
        const studio = document.querySelector('.studio-view').getBoundingClientRect()
        const shell = document.querySelector('.workspace-main').getBoundingClientRect()
        const canvas = document.querySelector('.studio-canvas')
        const grid = document.querySelector('.gallery-masonry')
        const cards = [...grid.children].map((card) => {
            const bounds = card.getBoundingClientRect()
            return {
                key: card.dataset.entryKey,
                width: Math.round(bounds.width),
                height: Math.round(bounds.height),
                x: Math.round(bounds.x),
                y: Math.round(bounds.y),
                right: Math.round(bounds.right),
                bottom: Math.round(bounds.bottom),
            }
        })
        const overlaps = []
        for (let a = 0; a < cards.length; a++)
            for (let b = a + 1; b < cards.length; b++) {
                if (
                    cards[a].x < cards[b].right - 1 &&
                    cards[b].x < cards[a].right - 1 &&
                    cards[a].y < cards[b].bottom - 1 &&
                    cards[b].y < cards[a].bottom - 1
                )
                    overlaps.push([a, b])
            }
        return {
            width: innerWidth,
            scroll: document.documentElement.scrollWidth,
            mainWidth: main.width,
            workspaceWidth: shell.width,
            left: studio.left - shell.left,
            right: shell.right - studio.right,
            columns: new Set(cards.map((card) => card.x)).size,
            gridColumns: getComputedStyle(grid).gridTemplateColumns.split(' ').length,
            canvasHeight: canvas.clientHeight,
            canvasScrollHeight: canvas.scrollHeight,
            canvasBottom: canvas.getBoundingClientRect().bottom,
            viewportHeight: innerHeight,
            cards,
            overlaps,
            cardHeights: cards.map((card) => card.height),
        }
    })
    assert.ok(metrics.scroll <= width + 1, `Document overflow at ${width}px: ${JSON.stringify(metrics)}`)
    assert.ok(Math.abs(metrics.mainWidth - metrics.workspaceWidth) <= 1, 'Main content should fill the workspace')
    assert.ok(metrics.left <= 32 && metrics.right <= 32, `Oversized side gutters: ${JSON.stringify(metrics)}`)
    assert.deepEqual(metrics.overlaps, [], `Gallery cards overlap at ${width}px`)
    assertAlignedRows(metrics)
    return metrics
}
function assertAlignedRows(metrics) {
    const { cards, gridColumns, width } = metrics
    if (!cards.length) return
    const describe = () => JSON.stringify({ width, gridColumns, cards })
    assert.ok(
        Math.max(...cards.map((card) => card.width)) - Math.min(...cards.map((card) => card.width)) <= 1,
        `Card widths differ: ${describe()}`,
    )
    assert.ok(
        Math.max(...cards.map((card) => card.height)) - Math.min(...cards.map((card) => card.height)) <= 1,
        `Mixed-media card heights differ: ${describe()}`,
    )
    const columns = cards.slice(0, gridColumns).map((card) => card.x)
    for (let start = 0; start < cards.length; start += gridColumns) {
        const row = cards.slice(start, start + gridColumns)
        assert.ok(
            Math.max(...row.map((card) => card.y)) - Math.min(...row.map((card) => card.y)) <= 1,
            `Row tops differ at card ${start}: ${describe()}`,
        )
        assert.ok(
            Math.max(...row.map((card) => card.bottom)) - Math.min(...row.map((card) => card.bottom)) <= 1,
            `Row bottoms differ at card ${start}: ${describe()}`,
        )
        row.forEach((card, index) =>
            assert.ok(
                Math.abs(card.x - columns[index]) <= 1,
                `DOM card ${start + index} skipped column ${index + 1}: ${describe()}`,
            ),
        )
        if (start) assert.ok(row[0].y > cards[start - 1].bottom, `Rows intersect at card ${start}: ${describe()}`)
    }
}

async function seedHistory(entries) {
    await page.evaluate((value) => {
        const serialized = JSON.stringify(value)
        sessionStorage.setItem('lay4flow_studio_receipts', serialized)
        localStorage.setItem(
            `lay4flow_studio_receipts:v2:${encodeURIComponent(localStorage.getItem('admin_username') || 'creator')}`,
            serialized,
        )
    }, entries)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('.studio-view h1').waitFor()
    await cardsLoaded(entries.length)
}
function scaleHistory(requests, outputsPerRequest = 1) {
    return Array.from({ length: requests }, (_, index) => ({
        ...history[index % 9],
        key: `scale-${index}`,
        started: started - index * 1000,
        title: `Scale fixture ${index}`,
        outputs: Array.from({ length: outputsPerRequest }, (_, outputIndex) => ({
            kind: 'image',
            url: `${base}/__studio_fixture__/scale-${index * outputsPerRequest + outputIndex}.svg`,
        })),
    }))
}
async function scrollGalleryToEnd(expected) {
    await page.locator('.gallery-card').last().scrollIntoViewIfNeeded()
    await page.waitForFunction((count) => document.querySelectorAll('.gallery-card').length === count, expected)
    const visible = await page
        .locator('.gallery-card')
        .last()
        .evaluate((card) => {
            const bounds = card.getBoundingClientRect()
            const canvas = document.querySelector('.studio-canvas')
            const canvasBounds = canvas.getBoundingClientRect()
            return {
                top: bounds.top,
                bottom: bounds.bottom,
                canvasTop: canvasBounds.top,
                canvasBottom: canvasBounds.bottom,
                viewport: innerHeight,
            }
        })
    assert.ok(visible.bottom > 0 && visible.top < visible.viewport, 'Last gallery card must be reachable')
    if (await page.evaluate(() => innerWidth >= 1051))
        assert.ok(visible.top < visible.canvasBottom && visible.bottom > visible.canvasTop)
    return visible
}
async function loadAllGalleryMedia(expected) {
    await resetScroll()
    for (let step = 0; step < 500; step++) {
        const end = await page.evaluate(() => {
            const canvas = document.querySelector('.studio-canvas')
            if (getComputedStyle(canvas).overflowY === 'auto' && canvas.scrollHeight > canvas.clientHeight) {
                canvas.scrollBy(0, canvas.clientHeight * 0.7)
                return canvas.scrollTop + canvas.clientHeight >= canvas.scrollHeight - 3
            }
            window.scrollBy(0, innerHeight * 0.7)
            return scrollY + innerHeight >= document.documentElement.scrollHeight - 3
        })
        await page.waitForTimeout(60)
        if (end) break
    }
    await page.waitForFunction((count) => {
        const images = [...document.querySelectorAll('.gallery-card img')]
        return images.length === count && images.every((image) => image.complete && image.naturalWidth > 0)
    }, expected)
}

try {
    // Create a valid local video without shipping a binary fixture or calling a provider.
    const videoBytes = await page.evaluate(async () => {
        const canvas = document.createElement('canvas')
        canvas.width = 480
        canvas.height = 270
        const painter = canvas.getContext('2d'),
            stream = canvas.captureStream(10),
            chunks = []
        const recorder = new MediaRecorder(stream, { mimeType: 'video/webm;codecs=vp8' })
        recorder.ondataavailable = (event) => {
            if (event.data.size) chunks.push(event.data)
        }
        const stopped = new Promise((resolve) => {
            recorder.onstop = resolve
        })
        recorder.start()
        for (let index = 0; index < 8; index++) {
            painter.fillStyle = '#fff0f5'
            painter.fillRect(0, 0, 480, 270)
            painter.fillStyle = '#ce3e77'
            painter.beginPath()
            painter.arc(50 + index * 45, 135, 28, 0, Math.PI * 2)
            painter.fill()
            await new Promise((resolve) => setTimeout(resolve, 100))
        }
        recorder.stop()
        await stopped
        stream.getTracks().forEach((track) => track.stop())
        return [...new Uint8Array(await new Blob(chunks, { type: 'video/webm' }).arrayBuffer())]
    })
    const videoBody = Buffer.from(videoBytes)
    assert.ok(videoBody.length > 100)
    const login = await context.request.post(`${base}/api/login`, {
        data: { username: 'preview-admin', password: 'preview-password' },
    })
    assert.equal(login.status(), 200, 'Use the synthetic preview server only')
    assert.ok((await context.cookies()).some((cookie) => cookie.name === 'lay4flow_remote_admin_session' && cookie.httpOnly))

    await context.addInitScript(
        ({ entries }) => {
            if (!sessionStorage.getItem('gallery_fixture_seeded')) {
                sessionStorage.setItem('lay4flow_studio_receipts', JSON.stringify(entries))
                sessionStorage.setItem('gallery_fixture_seeded', '1')
                // Prove the migration removes a stale key rather than merely starting empty.
                localStorage.setItem('studio_api_key', 'obsolete-fixture-key')
            }
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
            assert.match(source.pathname, /^\/tmp\//, 'Only temporary local results use archive POST')
            return json(route, {
                id: 'e'.repeat(64),
                url: `/api/admin/studio/media/${'e'.repeat(64)}/content`,
                kind: 'image',
                cached: true,
            })
        }
        if (url.pathname === `/api/admin/studio/media/${'e'.repeat(64)}/content`)
            return route.fulfill({ contentType: 'image/svg+xml', body: imageBodies[0] })
        if (url.pathname.startsWith('/__studio_fixture__/image-'))
            return route.fulfill({
                contentType: 'image/svg+xml',
                body: imageBodies[Number(url.pathname.match(/image-(\d)/)?.[1])],
            })
        if (url.pathname.startsWith('/__studio_fixture__/scale-')) {
            fixtureImageRequests.add(url.pathname)
            const index = Number(url.pathname.match(/scale-(\d+)/)?.[1] || 0)
            const size = imageKinds[index % imageKinds.length]
            return route.fulfill({
                contentType: 'image/svg+xml',
                body: `<svg xmlns="http://www.w3.org/2000/svg" width="${size.width}" height="${size.height}"><rect width="100%" height="100%" fill="hsl(${(index * 31) % 360} 45% 82%)"/><text x="24" y="60" fill="#283348" font-size="36">Fixture ${index}</text></svg>`,
            })
        }
        if (url.pathname === '/__studio_fixture__/motion.webm')
            return route.fulfill({ contentType: 'video/webm', body: videoBody, headers: { 'Accept-Ranges': 'bytes' } })
        if (url.pathname === '/__studio_fixture__/voice.wav')
            return route.fulfill({ contentType: 'audio/wav', body: silentWav(), headers: { 'Accept-Ranges': 'bytes' } })
        if (allowMockGeneration && url.pathname === '/api/admin/studio/chat/completions') {
            generationRequests.push(`${request.method()} ${url.pathname}`)
            mockGenerations++
            const content = Array.from(
                { length: 4 },
                (_, index) =>
                    `![Mock ${mockGenerations}-${index}](${base}/__studio_fixture__/scale-${mockGenerations * 4 + index}.svg)`,
            ).join('\n')
            return route.fulfill({
                contentType: 'text/event-stream',
                body: `data: ${JSON.stringify({ choices: [{ delta: { content } }] })}\n\ndata: [DONE]\n\n`,
            })
        }
        if (!['GET', 'HEAD'].includes(request.method())) {
            if (/generations|completions/.test(url.pathname))
                generationRequests.push(`${request.method()} ${url.pathname}`)
            else unexpectedMutations.push(`${request.method()} ${url.pathname}`)
            return json(route, { detail: 'Gallery E2E blocks every mutation' }, 503)
        }
        if (url.pathname === '/health') return json(route, { backend_running: true, has_active_tokens: true })
        if (url.pathname === '/api/stats') return json(route, { active_tokens: 1, total_tokens: 1 })
        if (url.pathname === '/api/admin/studio/models')
            return json(route, {
                data: [
                    {
                        id: 'gemini-3.0-pro-image',
                        displayName: 'Fixture image model',
                        type: 'image',
                        capabilities: { aspectRatios: ['16:9', '1:1'], imageSizes: ['1k'], modes: ['text'] },
                        defaults: { aspectRatio: '16:9', imageSize: '1k' },
                    },
                ],
            })
        if (url.pathname === `/api/admin/vids/v1/vids/generations/vids_${'6'.repeat(64)}`)
            return json(route, { id: `vids_${'6'.repeat(64)}`, kind: 'image', status: 'unknown' })
        if (url.pathname.startsWith('/api/'))
            return json(route, { detail: `Unexpected gallery API read: ${url.pathname}` }, 404)
        return route.continue()
    })
    await page.goto(`${base}/manage#/studio`, { waitUntil: 'domcontentloaded' })
    await page.locator('.studio-view h1').waitFor()
    await cardsLoaded()
    assert.equal(await page.locator('.gallery-card[data-media-kind="image"]').count(), 9)
    assert.equal(await page.locator('.gallery-card[data-media-kind="video"] video').count(), 2)
    assert.equal(await page.locator('.gallery-card[data-media-kind="audio"] audio').count(), 1)
    await page.waitForFunction(() =>
        [...document.querySelectorAll('.gallery-card video, .gallery-card audio')].every(
            (media) => media.readyState >= 2 && Number.isFinite(media.duration) && media.duration > 0,
        ),
    )
    assert.equal(await page.locator('.canvas-empty').count(), 0)
    record('All 12 historical works render together, with valid image/video/audio media')

    const desktop = await layout(1920)
    assert.ok(desktop.columns >= 2, `Expected gallery columns: ${JSON.stringify(desktop)}`)
    await screenshot('studio-gallery-1920')
    const wide = await layout(2560)
    assert.ok(wide.columns >= 3, `Wide gallery should use at least three columns: ${JSON.stringify(wide)}`)
    await screenshot('studio-gallery-2560')
    record('1920px/2560px workspaces show equal-height mixed-media cards in aligned rows with no skipped columns')

    const filters = page.locator('.gallery-toolbar .el-segmented')
    for (const [label, kind, count] of [
        ['图片', 'image', 9],
        ['视频', 'video', 2],
        ['声音', 'audio', 1],
    ]) {
        await filters.getByText(label, { exact: true }).click()
        await page.waitForFunction((expected) => document.querySelectorAll('.gallery-card').length === expected, count)
        assert.equal(await page.locator(`.gallery-card[data-media-kind="${kind}"]`).count(), count)
    }
    await filters.getByText('全部', { exact: true }).click()
    await cardsLoaded()
    record('Image, video, sound and all filters show exactly their historical works')

    const firstImage = page.locator('.gallery-card[data-media-kind="image"] img').first()
    await firstImage.click()
    await page.locator('.el-image-viewer__wrapper').waitFor({ state: 'visible' })
    assert.match(await page.locator('.el-image-viewer__img').first().getAttribute('src'), /image-0\.svg$/)
    await page.locator('.el-image-viewer__next').click()
    await page.waitForFunction(() =>
        [...document.querySelectorAll('.el-image-viewer__img')].some((image) => image.src.endsWith('image-1.svg')),
    )
    await screenshot('studio-gallery-lightbox')
    await page.keyboard.press('Escape')
    await page.locator('.el-image-viewer__wrapper').waitFor({ state: 'hidden' })
    record('Image lightbox opens historical artwork and switches to the next image')

    // Selecting request 1 must not change the download identity of request 8.
    await page.locator('.gallery-card').first().getByRole('button', { name: '记录', exact: true }).click()
    await page.locator('.el-drawer:visible .el-drawer__close-btn').click()
    const downloadEvent = page.waitForEvent('download')
    await page.locator('.gallery-card').nth(7).getByRole('button', { name: '下载作品', exact: true }).click()
    const download = await downloadEvent
    assert.equal(download.suggestedFilename(), `lay4flow-${history[7].started}-1.jpg`)
    await download.delete()
    record('Downloading an older card uses that card’s timestamp, independent of selected request')

    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('.studio-view h1').waitFor()
    await cardsLoaded()
    assert.equal(await page.evaluate(() => JSON.parse(sessionStorage.getItem('lay4flow_studio_receipts')).length), 12)
    assert.equal(await page.getByRole('button', { name: '创作凭据', exact: true }).count(), 0)
    assert.equal(await page.locator('.credential-drawer, input[placeholder*="API Key"]').count(), 0)
    assert.equal(await page.evaluate(() => localStorage.getItem('studio_api_key')), null)
    record('Reload preserves all historical works and removes the stale local API key without a key drawer')

    const mobile = await layout(390)
    assert.ok(mobile.columns >= 1)
    await page.locator('.studio-gallery').scrollIntoViewIfNeeded()
    await screenshot('studio-gallery-390')
    const smallMobile = await layout(360)
    await screenshot('studio-gallery-360')
    record('390px and 360px mobile layouts preserve the gallery with zero horizontal overflow')

    const sixMixed = [
        { ...history[0], key: 'aligned-image', title: 'Long prompt image · '.repeat(25) },
        { ...history[9], key: 'aligned-video' },
        { ...history[11], key: 'aligned-audio' },
        {
            ...history[1],
            key: 'aligned-failed',
            status: 'failed',
            outputs: [],
            message: 'Fixture generation failure · '.repeat(10),
        },
        {
            ...history[2],
            key: 'aligned-pending',
            provider: 'vids',
            receipt: `vids_${'6'.repeat(64)}`,
            status: 'unknown',
            outputs: [],
            message: 'Original receipt awaiting confirmation',
        },
        { ...history[2], key: 'aligned-sixth', title: 'Sixth result must begin the second row in column two' },
    ]
    await seedHistory(sixMixed)
    const sixCard = await layout(1920)
    assert.equal(sixCard.gridColumns, 4, 'The screenshot regression uses four desktop columns')
    assert.equal(sixCard.cards[5].key, 'aligned-sixth')
    assert.equal(sixCard.cards[5].x, sixCard.cards[1].x, 'Sixth card belongs to second-row column two, without a hole')
    assert.equal(sixCard.cards[5].y, sixCard.cards[4].y, 'Fifth and sixth cards share the second row')
    await screenshot('studio-gallery-six-mixed-aligned-1920')
    for (const width of [1440, 768, 390]) await layout(width)
    density.push({ scenario: 'six-mixed-aligned', ...sixCard })
    record(
        'Six mixed image/video/audio/failed/pending cards have equal heights; card six fills row two column two with no hole',
    )

    const sparse = []
    for (const count of [2, 3, 5]) {
        await seedHistory(history.slice(0, count))
        for (const width of [1920, 1440, 390, 2560]) {
            const metrics = await layout(width)
            const firstRow = metrics.cards.slice(0, Math.min(count, metrics.gridColumns))
            assert.equal(
                new Set(firstRow.map((card) => card.x)).size,
                firstRow.length,
                `${count} cards at ${width}px stack before filling the first row`,
            )
            assert.ok(
                Math.max(...firstRow.map((card) => card.y)) - Math.min(...firstRow.map((card) => card.y)) <= 1,
                `${count} cards at ${width}px do not share the first row`,
            )
            assert.ok(
                firstRow.every((card, index) => index === 0 || card.x > firstRow[index - 1].x),
                'Newest cards should fill left to right',
            )
            sparse.push({ count, ...metrics })
            if (width === 1440) await screenshot(`studio-gallery-sparse-${count}-1440`)
        }
    }
    await seedHistory(history)
    await layout(1920)
    record(
        'Sparse 2/3/5-card galleries fill rows in DOM order with equal heights and no gaps through four resize widths',
    )

    // The desktop work area must actually occupy the available height. Its gallery
    // scrolls independently while the editor and application shell stay in view.
    const sparseDesktop = await layout(1920)
    assert.ok(
        sparseDesktop.viewportHeight - sparseDesktop.canvasBottom <= 50,
        `Desktop gallery leaves unused screen height: ${JSON.stringify(sparseDesktop)}`,
    )
    const canvas = page.locator('.studio-canvas')
    await canvas.focus()
    const scrollBefore = await canvas.evaluate((element) => element.scrollTop)
    await page.keyboard.press('PageDown')
    await page.waitForTimeout(200)
    const scrollAfter = await canvas.evaluate((element) => element.scrollTop)
    assert.ok(scrollAfter > scrollBefore, 'Keyboard PageDown must scroll the gallery')
    assert.equal(await page.evaluate(() => scrollY), 0, 'Desktop PageDown must keep the outer workspace in place')
    record('Desktop canvas fills the viewport and supports independent keyboard scrolling')

    // Restore a large set in a single navigation so all 200 images have not already
    // crossed the viewport during generation. Only visible cards should request media.
    fixtureImageRequests.clear()
    const twoHundred = scaleHistory(200)
    await page.evaluate((entries) => {
        const serialized = JSON.stringify(entries)
        sessionStorage.setItem('lay4flow_studio_receipts', serialized)
        localStorage.setItem(
            `lay4flow_studio_receipts:v2:${encodeURIComponent(localStorage.getItem('admin_username') || 'creator')}`,
            serialized,
        )
    }, twoHundred)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForFunction(() => document.querySelectorAll('.gallery-card').length === 200)
    await page.waitForTimeout(500)
    const lazyRequests = fixtureImageRequests.size
    assert.ok(lazyRequests > 0 && lazyRequests < 60, `Expected bounded lazy media requests, got ${lazyRequests}/200`)
    density.push({ scenario: 'restore-200-lazy', cards: 200, mediaRequestedInitially: lazyRequests })
    await scrollGalleryToEnd(200)
    await screenshot('studio-200-lazy')
    record('200 restored cards use lazy media loading and their final card remains reachable')

    // Exercise the actual successful request path, not only a pre-seeded gallery.
    // Every POST is intercepted above; no provider or billable call is made.
    await page.evaluate(() => {
        sessionStorage.removeItem('lay4flow_studio_receipts')
        localStorage.removeItem(
            `lay4flow_studio_receipts:v2:${encodeURIComponent(localStorage.getItem('admin_username') || 'creator')}`,
        )
    })
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.locator('.studio-editor textarea').waitFor()
    allowMockGeneration = true
    const generationStart = performance.now()
    for (let index = 0; index < 50; index++) {
        await page.locator('.studio-editor textarea').fill(`Mock stress illustration ${index}`)
        await page.getByRole('button', { name: '开始绘梦', exact: true }).click()
        await page.waitForFunction(
            (count) => document.querySelectorAll('.gallery-card').length === count,
            (index + 1) * 4,
        )
    }
    allowMockGeneration = false
    const stored = await page.evaluate(() => JSON.parse(sessionStorage.getItem('lay4flow_studio_receipts') || '[]'))
    assert.equal(stored.length, 50, 'All 50 successful requests must be persisted')
    assert.equal(stored.flatMap((entry) => entry.outputs).length, 200, 'All 200 outputs must be persisted')
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForFunction(() => document.querySelectorAll('.gallery-card').length === 200)
    const twoHundredLayout = await layout(1920)
    await scrollGalleryToEnd(200)
    density.push({
        scenario: '50-four-image-generations',
        durationMs: performance.now() - generationStart,
        storedRequests: stored.length,
        storedOutputs: stored.flatMap((entry) => entry.outputs).length,
        restoredCards: await page.locator('.gallery-card').count(),
        ...twoHundredLayout,
    })
    record('50 mocked four-image requests retain all 200 results after refresh')

    // 250 valid requests with two outputs each stay within the documented session
    // bound while covering a 500-card gallery through desktop and mobile resizing.
    const fiveHundred = scaleHistory(250, 2)
    await page.evaluate((entries) => {
        const serialized = JSON.stringify(entries)
        sessionStorage.setItem('lay4flow_studio_receipts', serialized)
        localStorage.setItem(
            `lay4flow_studio_receipts:v2:${encodeURIComponent(localStorage.getItem('admin_username') || 'creator')}`,
            serialized,
        )
    }, fiveHundred)
    await page.reload({ waitUntil: 'domcontentloaded' })
    await page.waitForFunction(() => document.querySelectorAll('.gallery-card').length === 500)
    await loadAllGalleryMedia(500)
    for (const width of [1920, 1440, 768, 390]) {
        const metrics = await layout(width)
        const last = await scrollGalleryToEnd(500)
        density.push({ scenario: '500-results', width, ...metrics, last })
    }
    await page.setViewportSize({ width: 1920, height: 1080 })
    await resetScroll()
    await filters.getByText('视频', { exact: true }).click()
    assert.equal(await page.locator('.gallery-card').count(), 0)
    await filters.getByText('图片', { exact: true }).click()
    await page.waitForFunction(() => document.querySelectorAll('.gallery-card').length === 500)
    await scrollGalleryToEnd(500)
    record(
        '500 mixed-ratio images keep equal-height ordered rows through four viewport widths, filtering and scrolling to the final card',
    )

    assert.equal(mockGenerations, 50)
    assert.equal(generationRequests.length, 50)
    assert.deepEqual(unexpectedMutations, [])
    assert.deepEqual(pageErrors, [])
    assert.deepEqual(failedAssets, [])
    record(
        'Only 50 intercepted fixture generation calls, with zero real generations, unexpected mutations or page errors',
    )
    await writeFile(
        resolve(output, 'layout-metrics.json'),
        JSON.stringify({ desktop, wide, mobile, smallMobile, sparse, density }, null, 2),
    )
} catch (error) {
    console.error(error)
    console.error(
        (
            await page
                .locator('body')
                .innerText()
                .catch(() => '')
        ).slice(0, 2500),
    )
    await page.screenshot({ path: resolve(output, 'failure.png'), fullPage: true, timeout: 5000 }).catch(() => {})
    process.exitCode = 1
} finally {
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify(
            {
                base,
                browserPath,
                checks,
                generationRequests,
                unexpectedMutations,
                pageErrors,
                failedAssets,
                success: !process.exitCode,
            },
            null,
            2,
        ),
    )
    // Drop the large media document before closing Chromium. This keeps teardown
    // bounded after the 500-card stress case on resource-constrained Windows hosts.
    await page.goto('about:blank', { waitUntil: 'commit', timeout: 5000 }).catch(() => {})
    await browser.close()
}
