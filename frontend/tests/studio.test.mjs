import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import { webcrypto } from 'node:crypto'
import * as Vue from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createApi } from '../src/lib/http.js'
import {
    STUDIO_HISTORY_KEY,
    STUDIO_HISTORY_OWNER_KEY,
    readStudioHistory,
    selectStudioHistory,
    studioHistoryKey,
    writeStudioHistory,
} from '../src/lib/studio-history.js'
import { studioMediaNamespace, getStudioMedia, putStudioMedia, deleteStudioMedia } from '../src/lib/studio-cache.js'

const source = readFileSync(new URL('../src/views/StudioView.vue', import.meta.url), 'utf8')
const modelCatalog = readFileSync(new URL('../../static/js/model_catalog.js', import.meta.url), 'utf8')

function makeStudio({
    routeQuery = {},
    responder = async () => new Response('{}'),
    realHttp = false,
    storageFailure = false,
    archiveResponder,
} = {}) {
    const requests = []
    const archiveRequests = []
    const store = new Map()
    const localStore = new Map()
    const unmountCallbacks = []
    async function respond(path, options) {
        if (path === '/api/admin/studio/media/archive' && options.method === 'POST') {
            const body = JSON.parse(options.body || '{}')
            const source = new URL(body.url, 'http://fixture.local')
            assert.equal(source.origin, 'http://fixture.local', 'Archive fixtures stay on the synthetic origin')
            assert.match(source.pathname, /^\/tmp\//)
            archiveRequests.push({ path, options, body })
            if (archiveResponder) return archiveResponder(body, options)
            const id = await webcrypto.subtle
                .digest('SHA-256', new TextEncoder().encode(source.href))
                .then((value) => Buffer.from(value).toString('hex'))
            return new Response(
                JSON.stringify({
                    id,
                    url: `/api/admin/studio/media/${id}/content`,
                    kind: /\.(mp4|webm)$/i.test(source.pathname) ? 'video' : 'image',
                    cached: true,
                }),
            )
        }
        return responder(path, options)
    }
    const context = vm.createContext({
        ...Vue,
        console,
        window: { location: { origin: 'http://fixture.local' } },
        document: { addEventListener() {}, removeEventListener() {} },
        location: { origin: 'http://fixture.local' },
        crypto: webcrypto,
        TextEncoder,
        TextDecoder,
        URL,
        Blob,
        Headers,
        Response,
        ReadableStream,
        FormData,
        File,
        localStorage: {
            getItem: (key) => localStore.get(key) || null,
            setItem: (key, value) => {
                if (storageFailure) throw new Error('storage disabled')
                localStore.set(key, value)
            },
            removeItem: (key) => localStore.delete(key),
        },
        sessionStorage: {
            getItem: (key) => store.get(key) || null,
            setItem: (key, value) => {
                if (storageFailure) throw new Error('storage disabled')
                store.set(key, value)
            },
            removeItem: (key) => store.delete(key),
        },
        STUDIO_HISTORY_KEY,
        STUDIO_HISTORY_OWNER_KEY,
        readStudioHistory,
        studioHistoryKey,
        studioMediaNamespace: () => 'creator',
        getStudioMedia,
        putStudioMedia,
        deleteStudioMedia,
        selectStudioHistory,
        writeStudioHistory,
        useRoute: () => ({ query: routeQuery }),
        useRouter: () => ({ push() {} }),
        onMounted() {},
        onUnmounted: (callback) => unmountCallbacks.push(callback),
        setInterval: () => 1,
        clearInterval() {},
        ElMessage: { success() {} },
        notifyError() {},
        Mascot: {},
        StudioGallery: {},
        StudioReferenceBoard: {},
        StudioImageEditor: {},
        fallbackSource: modelCatalog,
        api: {
            post: async (path, body) => {
                const options = { method: 'POST', body: JSON.stringify(body) }
                requests.push({ path, options })
                const response = await respond(path, options)
                const data = await response.json()
                if (!response.ok) throw new Error(data.detail || 'Fixture request failed')
                return data
            },
            raw: async (path, options = {}) => {
                requests.push({ path, options })
                return respond(path, options)
            },
            get: async () => ({}),
        },
    })
    for (const icon of [
        'Picture',
        'VideoCamera',
        'Microphone',
        'Document',
        'MagicStick',
        'UploadFilled',
        'Delete',
        'Download',
        'Refresh',
        'CopyDocument',
        'Timer',
        'View',
        'Connection',
        'ArrowRight',
        'Check',
        'InfoFilled',
    ])
        context[icon] = {}
    if (realHttp)
        context.api = createApi({
            getToken: () => 'fixture-admin-token',
            fetcher: async (path, options) => {
                requests.push({ path, options })
                return respond(path, options)
            },
        })
    class FixtureReader {
        readAsDataURL(file) {
            queueMicrotask(() => {
                this.result = file.fixtureData || 'data:image/png;base64,AAAA'
                this.onload()
            })
        }
    }
    context.FileReader = FixtureReader
    const { descriptor, errors } = parse(source)
    assert.deepEqual(errors, [])
    const script = compileScript(descriptor, { id: 'studio-test' })
    const template = compileTemplate({
        source: descriptor.template.content,
        filename: 'StudioView.vue',
        id: 'studio-test',
        compilerOptions: { bindingMetadata: script.bindings },
    })
    assert.deepEqual(template.errors, [])
    const executable = script.content
        .replace(/^import[\s\S]*?from\s+['"][^'"]+['"];?\s*$/gm, '')
        .replace('export default {', 'globalThis.component = {')
    vm.runInContext(executable, context)
    const studio = context.component.setup({}, { expose() {} })
    return {
        studio,
        requests,
        archiveRequests,
        store,
        localStore,
        unmount: () => unmountCallbacks.forEach((callback) => callback()),
    }
}

function fixtureJob(request, id = 1) {
    const provider = request.provider || 'flow'
    const kind = request.body?.kind || 'image'
    return {
        id: `studio_fixture_${id}`,
        request_id: request.request_id,
        state: 'queued',
        provider,
        kind,
        total: request.count || 1,
        created_at: 1700000000,
        title: 'Synthetic background job',
        items: Array.from({ length: request.count || 1 }, (_, index) => ({
            id: `fixture_${id}_${index}`,
            key: `fixture_${id}_${index}`,
            index,
            provider,
            kind,
            status: 'queued',
            message: 'Waiting in background',
            outputs: [],
        })),
    }
}

async function flush() {
    await Vue.nextTick()
    await new Promise((resolve) => setImmediate(resolve))
}

test('Flow enforces mode image limits and emits the real generation contract', async () => {
    const { studio } = makeStudio()
    studio.kind.value = 'video'
    studio.provider.value = 'flow'
    studio.selectedId.value = 'veo_3_1'
    studio.selectModel()
    await flush()
    studio.prompt.value = 'A blue train crossing a summer meadow'
    studio.params.mode = 'reference_to_video'
    await flush()
    assert.match(studio.validation.value, /至少 1 张图片/)
    studio.materials.value = [{ data: 'data:image/png;base64,FIRST', bytes: 5 }]
    assert.equal(studio.validation.value, '')
    const body = studio.buildBody()
    assert.equal(body.model, 'veo_3_1')
    assert.equal(body.generationConfig.videoConfig.mode, 'reference_to_video')
    assert.equal(body.messages[0].content[1].image_url.url, 'data:image/png;base64,FIRST')
    studio.materials.value.push(
        { data: 'data:image/png;base64,TWO', bytes: 5 },
        { data: 'data:image/png;base64,THREE', bytes: 5 },
        { data: 'data:image/png;base64,FOUR', bytes: 5 },
    )
    assert.match(studio.validation.value, /最多.*3 张图片/)
})

test('missing captcha setup blocks Flow submissions while Vids remains independent', async () => {
    const { studio, requests } = makeStudio()
    studio.captchaConfigured.value = false
    assert.equal(studio.captchaRequired.value, true)
    studio.prompt.value = 'Synthetic test'
    await studio.generate()
    assert.match(studio.error.value, /打码服务/)
    assert.equal(requests.length, 0)
    assert.equal(studio.history.value.length, 0)
    studio.provider.value = 'vids'
    assert.equal(studio.captchaRequired.value, false)
    studio.provider.value = 'flow'
    studio.captchaConfigured.value = true
    assert.equal(studio.captchaRequired.value, false)
})

test('Flow background task updates each card without blocking a second submission', async () => {
    const jobs = []
    const { studio, requests } = makeStudio({
        responder: async (path, options) => {
            if (!options.method) return new Response(JSON.stringify({ jobs }))
            const request = JSON.parse(options.body)
            const job = fixtureJob(request, jobs.length + 1)
            jobs.push(job)
            return new Response(JSON.stringify({ success: true, job }), { status: 202 })
        },
    })
    studio.prompt.value = 'Synthetic meadow image'
    studio.params.count = 3
    await studio.generate()
    assert.equal(studio.submitting.value, false)
    assert.equal(studio.busy.value, false)
    assert.equal(studio.history.value.length, 3)
    assert.equal(studio.queuedGenerationCount.value, 3)
    studio.prompt.value = 'A second synthetic image'
    studio.params.count = 2
    await studio.generate()
    assert.equal(studio.history.value.length, 5)
    assert.equal(requests.filter((entry) => entry.options.method === 'POST').length, 2)
    assert.notEqual(jobs[0].request_id, jobs[1].request_id)
    assert.ok(studio.history.value.every((entry) => entry.status === 'queued'))
})

test('an uncertain job acknowledgement retries its original request ID and never issues a legacy generation', async () => {
    const { studio, requests } = makeStudio({
        responder: async (path, options) => {
            if (!options.method) return new Response(JSON.stringify({ jobs: [] }))
            throw new TypeError('synthetic connection closed')
        },
    })
    studio.prompt.value = 'Synthetic uncertain acknowledgement'
    await studio.generate()
    const requestID = studio.submissionRequest.value.request_id
    assert.match(studio.error.value, /等待确认/)
    await studio.generate()
    const posts = requests.filter((request) => request.options.method === 'POST')
    assert.equal(posts.length, 2)
    assert.ok(posts.every((request) => request.path === '/api/admin/studio/jobs'))
    assert.equal(JSON.parse(posts[0].options.body).request_id, requestID)
    assert.equal(JSON.parse(posts[1].options.body).request_id, requestID)
})

test('malformed persisted receipts are isolated while valid unknown receipts survive for recovery', () => {
    const { studio, store } = makeStudio()
    const valid = 'vids_' + 'a'.repeat(64)
    store.set(
        'lay4flow_studio_receipts',
        JSON.stringify([
            {
                provider: 'vids',
                kind: 'image',
                status: 'unknown',
                started: Date.now(),
                receipt: valid,
                outputs: [{ path: `/api/admin/vids/v1/vids/generations/${valid}/content`, kind: 'image' }],
                title: 'keep this receipt',
            },
            {
                provider: 'vids',
                kind: 'image',
                status: 'unknown',
                started: Date.now(),
                receipt: 'vids_bad',
                outputs: [],
                title: 'drop this receipt',
            },
            {
                provider: 'vids',
                kind: 'image',
                status: 'unknown',
                started: Date.now(),
                receipt: valid,
                outputs: [{ path: 'https://untrusted.example/result.jpg' }],
                title: 'keep but drop bad output',
            },
        ]),
    )
    studio.restoreHistory()
    assert.equal(studio.history.value.length, 2)
    assert.equal(studio.history.value[0].receipt, valid)
    assert.equal(studio.history.value[0].outputs.length, 1)
    assert.equal(studio.history.value[1].outputs.length, 0)
})

test('receipt content URLs require the original receipt, local origin, single safe index, and no hash', () => {
    const { studio } = makeStudio()
    const receipt = 'vids_' + 'b'.repeat(64)
    assert.equal(
        studio
            .contentPath(`/api/admin/vids/v1/vids/generations/${receipt}/content?index=2`, receipt)
            .endsWith('index=2'),
        true,
    )
    for (const value of [
        `https://elsewhere.example/api/admin/vids/v1/vids/generations/${receipt}/content`,
        `/api/admin/vids/v1/vids/generations/${receipt}/content?index=1&index=2`,
        `/api/admin/vids/v1/vids/generations/${receipt}/content#unsafe`,
        `/api/admin/vids/v1/vids/generations/${receipt}/content?index=9`,
    ])
        assert.throws(() => studio.contentPath(value, receipt))
})

test('background failures retain the real error per item without locking the editor', () => {
    const { studio } = makeStudio()
    const job = fixtureJob({ provider: 'flow', count: 2, request_id: 'fixture' }, 1)
    job.state = 'partial'
    job.items[0].status = 'failed'
    job.items[0].message = 'Synthetic upstream quota error'
    job.items[1].status = 'completed'
    job.items[1].outputs = [{ kind: 'image', url: '/stable/result.jpg' }]
    studio.applyGenerationJob(job)
    assert.equal(studio.history.value.length, 2)
    assert.equal(
        studio.history.value.find((entry) => entry.status === 'failed').message,
        'Synthetic upstream quota error',
    )
    assert.equal(studio.busy.value, false)
    assert.equal(studio.submitting.value, false)
})

test('background submissions and Vids recovery reuse the admin session', async () => {
    const receipt = 'vids_' + 'c'.repeat(64)
    const { studio, requests } = makeStudio({
        realHttp: true,
        responder: async (path, options) =>
            path === '/api/admin/studio/jobs'
                ? new Response(JSON.stringify({ job: fixtureJob(JSON.parse(options.body), 1) }), { status: 202 })
                : new Response(JSON.stringify({ id: receipt, kind: 'image', status: 'unknown' })),
    })
    studio.prompt.value = 'Synthetic admin session fixture'
    await studio.generate()
    assert.equal(requests[0].path, '/api/admin/studio/jobs')
    assert.equal(requests[0].options.headers.get('Authorization'), 'Bearer fixture-admin-token')
    assert.equal(requests[0].options.credentials, 'include')
    assert.equal(requests[0].options.redirect, 'error')
    await studio.inspect({ receipt, kind: 'image', status: 'unknown', outputs: [] })
    assert.equal(requests[1].options.headers.get('Authorization'), 'Bearer fixture-admin-token')
    assert.equal(requests[1].options.credentials, 'include')
})

test('a signed-in Flow Studio works without a locally stored service key and uses the live catalog', async () => {
    const { studio, requests } = makeStudio({
        responder: async () =>
            new Response(
                JSON.stringify({
                    data: [
                        {
                            id: 'synthetic-image',
                            type: 'image',
                            capabilities: { aspectRatios: ['1:1'] },
                            defaults: { mode: 'text' },
                        },
                    ],
                }),
            ),
    })
    await studio.loadCatalog()
    assert.equal(requests[0].path, '/api/admin/studio/models')
    assert.equal(new Headers(requests[0].options.headers).has('Authorization'), false)
    studio.prompt.value = 'A synthetic illustration'
    assert.equal(studio.available.value, true)
    assert.equal(studio.buildBody().model, 'synthetic-image')
})

test('Flow result URLs survive navigation while transient and executable URLs are excluded', () => {
    const { studio, store } = makeStudio()
    studio.history.value = [
        {
            provider: 'flow',
            kind: 'image',
            status: 'completed',
            started: Date.now(),
            title: 'A saved result',
            body: { private: 'raw material' },
            text: 'long response',
            outputs: [
                { kind: 'image', url: '/tmp/result.png' },
                { kind: 'image', url: 'blob:temporary' },
                { kind: 'image', url: 'javascript:alert(1)' },
            ],
        },
    ]
    studio.persistHistory()
    const saved = JSON.parse(store.get('lay4flow_studio_receipts'))
    assert.equal(saved[0].body, undefined)
    assert.equal(saved[0].text, undefined)
    assert.deepEqual(saved[0].outputs, [{ url: 'http://fixture.local/tmp/result.png', originalURL: '', kind: 'image' }])
    studio.restoreHistory()
    assert.equal(studio.history.value[0].outputs[0].url, 'http://fixture.local/tmp/result.png')
})

test('Flow temporary results archive once, retain their original source and restore the stable content URL', async () => {
    const { studio, archiveRequests, store } = makeStudio()
    studio.history.value = [
        {
            key: 'archive-old',
            provider: 'flow',
            kind: 'image',
            status: 'completed',
            started: Date.now(),
            title: 'Historical Flow result',
            outputs: [
                {
                    kind: 'image',
                    url: 'http://fixture.local/tmp/result.png',
                    sourceURL: 'http://fixture.local/tmp/result.png',
                },
            ],
        },
    ]
    const entry = studio.history.value[0],
        output = entry.outputs[0]
    await Promise.all([studio.archiveFlowOutput(output, entry), studio.archiveFlowOutput(output, entry)])
    assert.equal(archiveRequests.length, 1, 'concurrent visible/read paths share a single archive request')
    assert.match(output.url, /^\/api\/admin\/studio\/media\/[a-f0-9]{64}\/content$/)
    assert.equal(output.sourceURL, output.url)
    assert.equal(output.originalURL, 'http://fixture.local/tmp/result.png')
    studio.persistHistory()
    const saved = JSON.parse(store.get(STUDIO_HISTORY_KEY))
    assert.match(saved[0].outputs[0].url, /^http:\/\/fixture\.local\/api\/admin\/studio\/media\//)
    studio.restoreHistory()
    const restored = studio.history.value[0]
    await studio.archiveFlowOutput(restored.outputs[0], restored)
    assert.equal(archiveRequests.length, 1, 'stable archive content is not archived again after navigation')
})

test('session history restores 300 recent requests plus older unresolved Vids receipts without raw payloads', () => {
    const { studio, store } = makeStudio()
    const receipt = 'vids_' + '7'.repeat(64)
    studio.history.value = Array.from({ length: 330 }, (_, index) => ({
        provider: 'flow',
        kind: 'image',
        status: 'completed',
        started: Date.now() - index,
        title: `Result ${index}`,
        outputs: [{ kind: 'image', url: `/tmp/result-${index}.png` }],
        progressLog: 'x'.repeat(20000),
        text: 'large response',
        body: { base64: 'PRIVATE' },
    }))
    studio.history.value.push({
        provider: 'vids',
        kind: 'image',
        status: 'unknown',
        receipt,
        signature: 'pending-original',
        started: 1,
        outputs: [],
        title: 'Old pending receipt',
    })
    studio.persistHistory()
    const saved = JSON.parse(store.get(STUDIO_HISTORY_KEY))
    assert.equal(saved.length, 301)
    assert.equal(saved[300].receipt, receipt)
    assert.ok(saved.every((entry) => !('body' in entry) && !('text' in entry) && !('progressLog' in entry)))
    assert.match(studio.historyStorageMessage.value, /300/)
    studio.restoreHistory()
    assert.equal(studio.history.value.length, 301)
    assert.equal(studio.history.value[299].title, 'Result 299')
    assert.equal(studio.history.value[300].signature, 'pending-original')
})

test('backend-owned jobs can submit when local history storage is unavailable', async () => {
    const { studio, requests } = makeStudio({
        storageFailure: true,
        responder: async (path, options) =>
            new Response(JSON.stringify({ job: fixtureJob(JSON.parse(options.body), 1) }), { status: 202 }),
    })
    studio.prompt.value = 'Synthetic storage failure fixture'
    await studio.generate()
    assert.equal(requests.filter((request) => request.options.method === 'POST').length, 1)
    assert.equal(studio.history.value[0].status, 'queued')
    assert.match(studio.historyStorageMessage.value, /尚未保存/)
    assert.equal(studio.submitting.value, false)
})

test('unsettled Flow backend jobs survive the history window and resume after reload', () => {
    const { studio } = makeStudio()
    const job = fixtureJob({ provider: 'flow', count: 1, request_id: 'fixture' }, 1)
    job.items[0].status = 'generating'
    studio.applyGenerationJob(job)
    studio.history.value.unshift(
        ...Array.from({ length: 330 }, (_, index) => ({
            provider: 'flow',
            kind: 'image',
            status: 'completed',
            started: Date.now() + index,
            outputs: [],
            title: `new-${index}`,
        })),
    )
    studio.persistHistory()
    studio.restoreHistory()
    const restored = studio.history.value.find((entry) => entry.jobId === job.id)
    assert.ok(restored)
    assert.equal(restored.status, 'generating')
    studio.applyGenerationJob(job)
    assert.equal(studio.history.value.filter((entry) => entry.jobId === job.id).length, 1)
})

test('a typed recovery receipt stays bound to that ID, not a different server response', async () => {
    const requested = 'vids_' + 'd'.repeat(64),
        returned = 'vids_' + 'e'.repeat(64)
    const { studio } = makeStudio({
        responder: async () => new Response(JSON.stringify({ id: returned, kind: 'image', status: 'unknown' })),
    })
    studio.receiptInput.value = requested
    await studio.recoverReceipt()
    assert.equal(studio.history.value.length, 0)
    assert.equal(studio.task.value, null)
    assert.match(studio.error.value, /原请求不匹配/)
})

test('download recovery never POSTs an unknown receipt and invalid output indexes do not alter its status', async () => {
    const { studio, requests } = makeStudio()
    const receipt = 'vids_' + 'f'.repeat(64)
    const current = { receipt, kind: 'image', status: 'unknown', outputs: [] }
    await studio.inspect(current, true)
    assert.equal(requests.length, 0)
    assert.equal(current.status, 'unknown')
    assert.throws(() =>
        studio.applyReceipt(current, {
            id: receipt,
            kind: 'image',
            status: 'completed',
            outputs: [{ index: 0, content_url: `/api/admin/vids/v1/vids/generations/${receipt}/content?index=1` }],
        }),
    )
    assert.equal(current.status, 'unknown')
})

test('Flow and Vids uploads enforce their distinct byte ceilings before reading', async () => {
    const { studio } = makeStudio()
    studio.kind.value = 'image'
    studio.selectedId.value = 'gemini-3.0-pro-image'
    studio.selectModel()
    await flush()
    await studio.addImage({ raw: { name: 'flow-large.png', type: 'image/png', size: 15 * 1024 ** 2 } })
    assert.equal(studio.materials.value.length, 1, 'Flow accepts a 15 MiB image within its 20 MiB per-image ceiling')
    await studio.addImage({ raw: { name: 'flow-too-large.png', type: 'image/png', size: 21 * 1024 ** 2 } })
    assert.equal(studio.materials.value.length, 1)
    assert.match(studio.error.value, /20 MiB/)
    studio.provider.value = 'vids'
    studio.selectedId.value = 'vids-image'
    studio.selectModel()
    await flush()
    studio.params.mode = 'edit'
    await flush()
    await studio.addImage({ raw: { name: 'vids-too-large.png', type: 'image/png', size: 11 * 1024 ** 2 } })
    assert.equal(studio.materials.value.length, 0)
    assert.match(studio.error.value, /10 MiB/)
})

test('Vids availability accepts only positive safe integer account counts', async () => {
    const { studio } = makeStudio({ routeQuery: { platform: 'vids' } })
    studio.vidsStatus.value = { configured: true, available: { image: '1' }, recoverable: { image: -1 } }
    assert.equal(studio.available.value, false)
    studio.vidsStatus.value = { configured: true, available: { image: 0 }, recoverable: { image: 1 } }
    assert.equal(studio.available.value, true)
})

test('keyboard media tabs wrap, move focus after updates, and ignore actions during generation', async () => {
    const { studio } = makeStudio()
    let focused = '',
        prevented = 0
    const tablist = {
        querySelector: (selector) => ({
            focus: () => {
                focused = selector
            },
        }),
    }
    const event = { key: 'ArrowRight', preventDefault: () => prevented++, currentTarget: { parentElement: tablist } }
    await studio.navigateKind(event)
    assert.equal(studio.kind.value, 'video')
    assert.equal(focused, '[data-kind="video"]')
    event.key = 'End'
    await studio.navigateKind(event)
    assert.equal(studio.kind.value, 'document')
    event.key = 'ArrowRight'
    await studio.navigateKind(event)
    assert.equal(studio.kind.value, 'image')
    studio.busy.value = true
    await studio.navigateKind(event)
    assert.equal(studio.kind.value, 'image')
    assert.equal(prevented, 3)
})

test('a material-dependent mode reveals the complete upload controls', async () => {
    const { studio } = makeStudio()
    studio.kind.value = 'video'
    studio.selectedId.value = 'veo_3_1'
    studio.selectModel()
    await flush()
    studio.parameterSections.value = []
    studio.params.mode = 'first_last_frame'
    await flush()
    assert.ok(studio.parameterSections.value.includes('materials'))
    assert.equal(studio.modeLimits.value.minImages, 2)
})

test('the workspace Chinese copy survives source encoding and retains readable media labels', () => {
    const { descriptor } = parse(source)
    assert.doesNotMatch(descriptor.template.content, /\?{3,}/)
    for (const label of [
        '下一幅喜欢的作品，从这里开始',
        '选一个模型，写下描述。生成的图片、视频与声音会出现在这里。',
        '画面设定',
        '声线设置',
        '参考图片与素材',
        '图片预览',
        '视频播放',
        '音频试听',
    ])
        assert.ok(descriptor.template.content.includes(label), `missing Chinese label: ${label}`)
})

test('a job accepted after navigation remains on the server and is restored without reposting', async () => {
    let finishRequest
    const { studio, requests, store, unmount } = makeStudio({
        responder: async (path, options) =>
            new Promise((resolve) => {
                finishRequest = () =>
                    resolve(
                        new Response(JSON.stringify({ job: fixtureJob(JSON.parse(options.body), 1) }), { status: 202 }),
                    )
            }),
    })
    studio.prompt.value = 'Synthetic late acknowledgement'
    const pending = studio.generate()
    while (!finishRequest) await flush()
    unmount()
    finishRequest()
    await pending
    assert.equal(requests.length, 1)
    assert.equal(studio.task.value.status, 'queued')
    assert.equal(studio.task.value.outputs.length, 0)
})

test('a preview response arriving after unmount creates no Blob URL', async () => {
    let resolveContent
    const { studio, unmount } = makeStudio({
        responder: async () =>
            new Promise((resolve) => {
                resolveContent = resolve
            }),
    })
    const receipt = 'vids_' + '9'.repeat(64)
    const current = {
        receipt,
        kind: 'image',
        status: 'completed',
        outputs: [{ path: `/api/admin/vids/v1/vids/generations/${receipt}/content`, kind: 'image' }],
    }
    const pending = studio.preview(current)
    while (!resolveContent) await flush()
    unmount()
    resolveContent(new Response(new Blob(['fixture'], { type: 'image/jpeg' })))
    await pending
    assert.equal(current.outputs[0].url, undefined)
})

test('persistent history restores Vids as authenticated stable media without a manual read or generation POST', () => {
    const { studio, store, localStore, requests } = makeStudio()
    const receipt = 'vids_' + 'd'.repeat(64)
    const path = `/api/admin/vids/v1/vids/generations/${receipt}/content`
    studio.history.value = [
        {
            provider: 'vids',
            kind: 'video',
            status: 'completed',
            started: 1,
            receipt,
            outputs: [{ path, kind: 'video', url: 'blob:transient' }],
        },
    ]
    assert.equal(studio.persistHistory(), true)
    assert.ok(localStore.has(studioHistoryKey('creator')))
    store.clear()
    studio.restoreHistory()
    assert.equal(studio.history.value[0].outputs[0].url, path)
    assert.equal(studio.history.value[0].outputs[0].path, path)
    assert.equal(requests.length, 0)
})

test('full prompt and safe parameter snapshot survive storage and reuse without persisting reference materials', async () => {
    const { studio, localStore } = makeStudio()
    const fullPrompt = 'A complete prompt with details, camera direction and composition. '.repeat(20)
    const entry = {
        provider: 'flow',
        kind: 'image',
        model: 'nano-banana-2',
        status: 'completed',
        started: 1,
        title: fullPrompt.slice(0, 38),
        description: fullPrompt,
        settings: { aspectRatio: '9:16', imageSize: '2k', mode: 'edit', injected_base64: 'SECRET' },
        outputs: [],
    }
    studio.history.value = [entry]
    studio.persistHistory()
    const saved = JSON.parse(localStore.get(studioHistoryKey('creator')))[0]
    assert.equal(saved.description, fullPrompt)
    assert.equal(saved.settings.injected_base64, undefined)
    studio.restoreHistory()
    await studio.reuseEntry(studio.history.value[0])
    assert.equal(studio.prompt.value, fullPrompt)
    assert.equal(studio.params.aspectRatio, '9:16')
    assert.equal(studio.params.imageSize, '2k')
    assert.equal(studio.params.mode, 'edit')
})

test('deleting a failed card persists immediately and keeps unresolved Vids receipts', async () => {
    const { studio, localStore } = makeStudio()
    const failure = { provider: 'flow', kind: 'image', status: 'failed', started: 1, outputs: [] }
    const unknown = {
        provider: 'vids',
        kind: 'image',
        status: 'unknown',
        started: 2,
        receipt: 'vids_' + 'e'.repeat(64),
        outputs: [],
    }
    studio.history.value = [failure, unknown]
    await studio.removeEntry(studio.history.value[0])
    assert.equal(studio.history.value.length, 1)
    assert.equal(JSON.parse(localStore.get(studioHistoryKey('creator')))[0].receipt, unknown.receipt)
})

test('automatic Vids recovery queries original receipts with two workers and never reposts generation or downloads', async () => {
    const responders = []
    const { studio, requests } = makeStudio({
        responder: async (path, options) => new Promise((resolve) => responders.push({ path, options, resolve })),
    })
    const receipts = ['a', 'b', 'c'].map((value) => 'vids_' + value.repeat(64))
    studio.history.value = receipts.map((receipt, index) => ({
        provider: 'vids',
        kind: 'video',
        status: index ? 'unknown' : 'completed',
        started: index + 1,
        receipt,
        outputs: [],
    }))
    studio.restoreVidsReceipts()
    await flush()
    assert.equal(requests.length, 2)
    assert.ok(requests.every((request) => !request.options.method && !request.path.endsWith('/content')))
    responders[0].resolve(
        new Response(
            JSON.stringify({
                id: receipts[0],
                kind: 'video',
                status: 'completed',
                content_url: `/api/admin/vids/v1/vids/generations/${receipts[0]}/content`,
            }),
        ),
    )
    await flush()
    assert.equal(requests.length, 3)
    assert.equal(studio.history.value[0].outputs[0].url, `/api/admin/vids/v1/vids/generations/${receipts[0]}/content`)
    for (let index = 1; index < 3; index++)
        responders[index].resolve(
            new Response(JSON.stringify({ id: receipts[index], kind: 'video', status: 'unknown' })),
        )
    await flush()
    assert.ok(requests.every((request) => request.options.method !== 'POST'))
})

test('a receipt response after navigation persists the restored metadata without creating transient preview URLs', async () => {
    let resolveReceipt
    const { studio, localStore, requests, unmount } = makeStudio({
        responder: async () =>
            new Promise((resolve) => {
                resolveReceipt = resolve
            }),
    })
    const receipt = 'vids_' + 'f'.repeat(64)
    studio.history.value = [{ provider: 'vids', kind: 'video', status: 'unknown', started: 1, receipt, outputs: [] }]
    const previous = studio.history.value[0]
    studio.persistHistory()
    studio.queueReceiptRestore(previous)
    await flush()
    unmount()
    resolveReceipt(
        new Response(
            JSON.stringify({
                id: receipt,
                kind: 'video',
                status: 'completed',
                content_url: `/api/admin/vids/v1/vids/generations/${receipt}/content`,
            }),
        ),
    )
    await flush()
    assert.equal(previous.status, 'unknown')
    const persisted = JSON.parse(localStore.get(studioHistoryKey('creator')))[0]
    assert.equal(persisted.status, 'completed')
    assert.ok(persisted.outputs[0].path.endsWith('/content'))
    assert.equal(requests.length, 1)
    assert.equal(persisted.outputs[0].url, undefined)
})

test('a late previous-page receipt update preserves newer records and does not revive a removed work', async () => {
    let resolveReceipt
    const { studio, localStore, unmount } = makeStudio({
        responder: async () =>
            new Promise((resolve) => {
                resolveReceipt = resolve
            }),
    })
    const receipt = 'vids_' + '8'.repeat(64)
    studio.history.value = [{ provider: 'vids', kind: 'video', status: 'unknown', started: 1, receipt, outputs: [] }]
    studio.persistHistory()
    studio.queueReceiptRestore(studio.history.value[0])
    await flush()
    unmount()
    const newer = {
        provider: 'flow',
        kind: 'image',
        status: 'completed',
        started: 2,
        key: 'new-tab-result',
        outputs: [{ kind: 'image', url: 'http://fixture.local/new.png' }],
    }
    // The replacement view deleted the old receipt and generated a new work.
    localStore.set(studioHistoryKey('creator'), JSON.stringify([newer]))
    resolveReceipt(
        new Response(
            JSON.stringify({
                id: receipt,
                kind: 'video',
                status: 'completed',
                content_url: `/api/admin/vids/v1/vids/generations/${receipt}/content`,
            }),
        ),
    )
    await flush()
    const persisted = JSON.parse(localStore.get(studioHistoryKey('creator')))
    assert.equal(persisted.length, 1)
    assert.equal(persisted[0].key, 'new-tab-result')
    assert.equal(persisted[0].receipt, undefined)
})

test('Flow and Vids image and video batches use the chosen count once', async () => {
    const { studio, requests } = makeStudio({
        responder: async (path, options) =>
            new Response(JSON.stringify({ job: fixtureJob(JSON.parse(options.body), requests.length) }), {
                status: 202,
            }),
    })
    for (const provider of ['flow', 'vids']) {
        for (const kind of ['image', 'video']) {
            studio.provider.value = provider
            studio.kind.value = kind
            await flush()
            studio.selectModel()
            await flush()
            studio.vidsStatus.value = { configured: true, available: { image: 1, video: 1 } }
            studio.params.count = 8
            studio.prompt.value = `Synthetic ${provider} ${kind} batch`
            await studio.generate()
            const submitted = JSON.parse(requests.at(-1).options.body)
            assert.equal(submitted.count, 8)
            assert.equal(submitted.provider, provider)
            if (provider === 'vids' && kind === 'image') assert.equal(submitted.body.n, 1)
            assert.equal(studio.submitting.value, false)
        }
    }
    assert.equal(requests.filter((entry) => entry.options.method === 'POST').length, 4)
    assert.equal(studio.history.value.length, 32)
    studio.params.count = 21
    await studio.generate()
    assert.equal(requests.filter((entry) => entry.options.method === 'POST').length, 4)
})

test('a lost acknowledgement is reconciled from the server request ID before retrying', async () => {
    let accepted
    const { studio, requests } = makeStudio({
        responder: async (path, options) => {
            if (options.method === 'POST') {
                accepted = fixtureJob(JSON.parse(options.body), 1)
                throw new TypeError('synthetic lost acknowledgement')
            }
            return new Response(JSON.stringify({ jobs: [accepted] }))
        },
    })
    studio.prompt.value = 'Synthetic acknowledged background batch'
    studio.params.count = 4
    await studio.generate()
    assert.equal(studio.history.value.length, 4)
    assert.equal(studio.submissionRequest.value, null)
    assert.equal(studio.error.value, '')
    assert.equal(requests.filter((entry) => entry.options.method === 'POST').length, 1)
})

test('a removed backend result stays removed when polling and reloading', async () => {
    const { studio } = makeStudio()
    const job = fixtureJob({ provider: 'flow', count: 1, request_id: 'remove-fixture' }, 1)
    job.state = 'failed'
    job.items[0].status = 'failed'
    studio.applyGenerationJob(job)
    await studio.removeEntry(studio.history.value[0])
    studio.applyGenerationJob(job)
    assert.equal(studio.history.value.length, 0)
    studio.restoreHistory()
    studio.applyGenerationJob(job)
    assert.equal(studio.history.value.length, 0)
})

test('failed background retries remain visible in request details and after reload', async () => {
    const fixture = makeStudio()
    const job = fixtureJob({ provider: 'flow', count: 1, request_id: 'failed-retries' }, 1)
    job.items[0].status = 'failed'
    job.items[0].retry_trace = {
        max_attempts: 3, attempts: 3,
        history: [{ attempt: 1, success: false, error: 'HTTP Error 403' }],
    }
    fixture.studio.applyGenerationJob(job)
    const entry = fixture.studio.history.value[0]
    fixture.studio.task.value = entry
    assert.equal(JSON.parse(fixture.studio.taskDetails.value).retry.attempts, 3)
    fixture.studio.persistHistory()
    fixture.studio.restoreHistory()
    assert.equal(fixture.studio.history.value[0].retryTrace.max_attempts, 3)
})

test('an earlier polling response never hides a newly accepted batch', async () => {
    let finishPoll
    const { studio } = makeStudio({
        responder: async () => new Promise((resolve) => { finishPoll = resolve }),
    })
    const polling = studio.refreshGenerationJobs()
    while (!finishPoll) await flush()
    const job = fixtureJob({ provider: 'flow', count: 1, request_id: 'new-batch' }, 1)
    studio.applyGenerationJob(job, { description: 'Synthetic new batch', settings: {} })
    finishPoll(new Response(JSON.stringify({ jobs: [] })))
    await polling
    assert.equal(studio.history.value[0].status, 'queued')
    assert.equal(studio.activeGenerationJobs.value.length, 1)
})
