import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { computed, reactive, ref } from 'vue'
import { parse, compileScript } from '@vue/compiler-sfc'

function dashboard(get) {
    const filename = new URL('../src/views/DashboardView.vue', import.meta.url)
    const { descriptor } = parse(readFileSync(filename, 'utf8'))
    const compiled = compileScript(descriptor, { id: 'dashboard' }).content
    const imports = [...compiled.matchAll(/^import[\s\S]*?from\s+['"][^'"]+['"];?\s*$/gm)]
    const context = {
        computed,
        reactive,
        ref,
        api: { get },
        onMounted: () => {},
        onUnmounted: () => {},
        useRouter: () => ({}),
        formatNumber: (value) => (value == null ? '—' : Number(value).toLocaleString('en-US')),
        formatDate: (value) => value,
        parseDate: (value) => new Date(value),
        localStorage: { getItem: () => '' },
        document: { hidden: false, addEventListener() {}, removeEventListener() {} },
        setInterval() {},
        clearInterval() {},
    }
    for (const match of imports) {
        const block = match[0].match(/\{([\s\S]*?)\}/)?.[1]
        if (block)
            for (const name of block
                .split(',')
                .map((item) => item.trim())
                .filter(Boolean))
                if (!(name in context)) context[name] = {}
        const defaultName = match[0].match(/^import\s+([A-Za-z_$][\w$]*)\s+from/)?.[1]
        if (defaultName) context[defaultName] = {}
    }
    const source =
        compiled
            .replace(/^import[\s\S]*?from\s+['"][^'"]+['"];?\s*$/gm, '')
            .replace('export default', 'const component =') + '\ncomponent'
    return runInNewContext(source, context).setup({}, { expose() {} })
}

test('overview automatically refreshes Vids quotas without hiding cached numbers or duplicating requests', async () => {
    let resolve,
        calls = 0
    const pending = new Promise((done) => {
        resolve = done
    })
    const state = dashboard(async (path) => {
        assert.equal(path, '/api/admin/vids/status?refresh=true')
        calls++
        return pending
    })
    state.vidsStatus.value = {
        configured: true,
        configured_count: 1,
        quota: { image: { remaining: 0, total: 20, covered: 1, eligible: 1 } },
    }
    const first = state.refreshVidsQuota(),
        second = state.refreshVidsQuota()
    assert.equal(calls, 1)
    assert.equal(state.vidsQuotas.value[0].remaining, '0')
    assert.equal(state.vidsQuotas.value[1].remaining, '—')
    resolve({
        configured: true,
        quota: {
            image: { remaining: 19, total: 20, covered: 1, eligible: 2 },
            video: { remaining: 427, total: 430, covered: 1, eligible: 1 },
        },
    })
    await Promise.all([first, second])
    assert.equal(state.vidsQuotas.value[0].remaining, '19')
    assert.equal(state.vidsQuotas.value[0].partial, true)
    assert.equal(state.vidsQuotas.value[1].total, '430')
})

test('failed quota synchronization keeps the last overview values and marks their age', async () => {
    const state = dashboard(async () => {
        throw new Error('quota fixture offline')
    })
    state.vidsStatus.value = {
        quota: { image: { remaining: 19, total: 20, covered: 1, eligible: 1, queried_at: '2026-10-15T08:00:00Z' } },
    }
    await state.refreshVidsQuota()
    assert.equal(state.vidsQuotas.value[0].remaining, '19')
    assert.equal(state.vidsQuotas.value[0].stale, true)
    assert.equal(state.quotaError.value, 'quota fixture offline')
})

test('overview excludes paused account observations when the active quota summary is available', () => {
    const state = dashboard(async () => ({}))
    state.vidsStatus.value = {
        quota: {
            image: {
                remaining: 99,
                total: 100,
                covered: 1,
                eligible: 0,
                active_remaining: null,
                active_total: null,
                active_covered: 0,
                active_queried_at: null,
                active_stale: false,
            },
        },
    }
    assert.equal(state.vidsQuotas.value[0].remaining, '—')
    assert.equal(state.vidsQuotas.value[0].covered, 0)
})
