import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import * as Vue from 'vue'
import { compileScript, parse } from '@vue/compiler-sfc'

function setupGallery(entries, overrides = {}) {
    const source = readFileSync(new URL('../src/components/StudioGallery.vue', import.meta.url), 'utf8')
    const { descriptor } = parse(source)
    const script = compileScript(descriptor, { id: 'gallery-test' }).content
    const context = vm.createContext({ ...Vue, onMounted() {}, onUnmounted() {} })
    for (const name of [
        'Picture',
        'VideoCamera',
        'Microphone',
        'Download',
        'View',
        'Refresh',
        'CopyDocument',
        'WarningFilled',
        'Loading',
        'Document',
        'Delete',
        'Edit',
        'Search',
        'RefreshLeft',
    ])
        context[name] = {}
    vm.runInContext(
        script
            .replace(/^import[\s\S]*?from\s+['"][^'"]+['"]\s*;?\s*$/gm, '')
            .replace('export default {', 'globalThis.component = {'),
        context,
    )
    const events = []
    const props = Vue.reactive({
        entries,
        selected: null,
        busy: false,
        loadingIds: new Set(),
        statusNames: {},
        ...overrides,
    })
    const gallery = context.component.setup(props, {
        expose() {},
        emit(...args) {
            events.push(args)
        },
    })
    return { gallery, props, events }
}

test('the masonry gallery retains every output, pending request and correct global image preview order across media filters', () => {
    const entries = [
        { key: 'pending', kind: 'image', status: 'generating', outputs: [] },
        {
            key: 'mixed',
            kind: 'image',
            status: 'completed',
            outputs: [
                { kind: 'video', url: 'https://fixture.local/clip.mp4' },
                { kind: 'image', url: 'https://fixture.local/tall.webp' },
                { kind: 'image', url: 'https://fixture.local/wide.webp' },
            ],
        },
        { key: 'speech', kind: 'speech', status: 'completed', outputs: [{ kind: 'audio', url: 'blob:fixture-audio' }] },
        { key: 'restore', kind: 'image', status: 'completed', outputs: [{ kind: 'image', path: '/content' }] },
    ]
    const { gallery } = setupGallery(entries)
    assert.equal(gallery.cards.value.length, 6)
    assert.equal(gallery.finishedCount.value, 4)
    assert.equal(gallery.cards.value[0].output, null)
    assert.equal(gallery.cards.value.at(-1).output.path, '/content')
    assert.equal(gallery.imageIndex(gallery.cards.value[2]), 0)
    assert.equal(gallery.imageIndex(gallery.cards.value[3]), 1)
    gallery.filter.value = 'image'
    assert.equal(gallery.visibleCards.value.length, 4)
    gallery.filter.value = 'audio'
    assert.equal(gallery.visibleCards.value.length, 1)
    assert.equal(gallery.visibleCards.value[0].entry.key, 'speech')
    gallery.filter.value = 'all'
    assert.equal(gallery.visibleCards.value.length, 6)
})

test('gallery filters request states and prompt text before selecting unique completed or failed requests', () => {
    const entries = [
        {
            key: 'done',
            status: 'completed',
            kind: 'image',
            title: '海边 Sakura',
            outputs: [
                { kind: 'image', url: '/first' },
                { kind: 'image', url: '/second' },
            ],
        },
        { key: 'failed', status: 'failed', kind: 'video', title: 'Sea retry', outputs: [] },
        { key: 'waiting', status: 'unknown', kind: 'video', title: 'Pending Sea', outputs: [] },
        { key: 'receipt', status: 'received', kind: 'video', title: 'Sakura receipt', outputs: [] },
    ]
    const { gallery } = setupGallery(entries)
    assert.equal(gallery.cards.value.length, 5)
    assert.equal(gallery.filteredEntries.value.length, 4)
    gallery.search.value = 'SAKURA'
    assert.equal(gallery.visibleCards.value.length, 3)
    assert.equal(gallery.selectableEntries.value.length, 1)
    gallery.toggleAll(true)
    assert.equal(gallery.checkedEntries.value.length, 1, 'multiple outputs share a single request selection')
    assert.equal(gallery.downloadableEntries.value.length, 1)
    assert.equal(gallery.allChecked.value, true)
    gallery.search.value = ''
    assert.equal(gallery.someChecked.value, true)
    gallery.toggleEntry(entries[2], true)
    assert.equal(gallery.checkedEntries.value.length, 1, 'unresolved receipts retain their history')
    gallery.statusFilter.value = 'failed'
    assert.equal(gallery.visibleCards.value.length, 1)
    assert.equal(gallery.failedEntries.value[0].key, 'failed')
    gallery.toggleAll(true)
    assert.equal(gallery.checkedEntries.value.length, 2)
    gallery.statusFilter.value = 'pending'
    assert.equal(gallery.visibleCards.value.length, 2)
    assert.equal(gallery.selectableEntries.value.length, 0)
    gallery.statusFilter.value = 'completed'
    assert.equal(gallery.visibleCards.value.length, 2)
    gallery.toggleAll(false)
    assert.equal(
        gallery.checkedEntries.value.length,
        1,
        'deselecting the current filter preserves other intentional selections',
    )
})

test('preview failures are distinct from request failures and retry emits only a read request', () => {
    const entries = [
        {
            key: 'vids',
            provider: 'vids',
            status: 'completed',
            kind: 'video',
            outputs: [{ kind: 'video', path: '/content/0' }],
        },
        { key: 'flow', status: 'completed', kind: 'image', outputs: [{ kind: 'image', url: '/expired' }] },
    ]
    const { gallery, props, events } = setupGallery(entries)
    const noURL = gallery.cards.value[0],
        image = gallery.cards.value[1]
    assert.equal(gallery.hasPreviewError(noURL), false, 'an unloaded output is not a failed media element')
    props.entries[0].outputs[0].loading = true
    assert.equal(gallery.isLoading(noURL), true)
    props.entries[0].outputs[0].loading = false
    props.entries[0].outputs[0].previewError = '成品缓存暂时不可用'
    assert.equal(gallery.hasPreviewError(noURL), true)
    assert.equal(gallery.previewErrorMessage(noURL), '成品缓存暂时不可用')
    gallery.previewFailed(image)
    assert.equal(gallery.hasPreviewError(image), true)
    assert.equal(image.entry.status, 'completed', 'media errors never relabel a successful generation')
    gallery.retryPreview(image)
    assert.equal(gallery.hasPreviewError(image), false)
    assert.equal(events.length, 1)
    assert.equal(events[0][0], 'retry-preview')
    assert.equal(events[0][1].entry.key, 'flow')
    assert.equal(events[0][1].index, 0)
    assert.equal(gallery.previewVersions[image.key], 1)
    props.entries[1].outputs[0].url = '/fresh'
    assert.equal(gallery.hasPreviewError(gallery.cards.value[1]), false)
})
