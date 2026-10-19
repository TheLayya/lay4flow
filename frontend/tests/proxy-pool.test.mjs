import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import { computed, reactive, ref, watch, nextTick } from 'vue'

function panel() {
    const { descriptor } = parse(readFileSync(new URL('../src/components/ProxyPoolPanel.vue', import.meta.url), 'utf8'))
    const source = descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
    const props = reactive({
        rows: Array.from({ length: 31 }, (_, index) => ({
            id: index + 1,
            proxy_url: `socks5://user:secret@proxy-${index + 1}.test:1080`,
            note: index < 3 ? 'Tokyo route' : 'Other route',
            enabled: index % 2 === 0,
            last_check_ok: index % 3 === 0 ? true : index % 3 === 1 ? false : null,
        })),
        busy: new Set(),
        loading: false,
        importing: false,
        bulkState: { running: false, done: 0, total: 0 },
    })
    const events = []
    const execute = new Function(
        'computed',
        'ref',
        'watch',
        'defineProps',
        'defineEmits',
        `${source}\nreturn {
        search, stateFilter, healthFilter, currentPage, pageSize, filteredRows, pageRows,
        selected, selectedCount, selectedRows, toggleRow, togglePage, selectFiltered, clearSelection,
        emitBulk, onPageSizeChange, isBusy, enabledCount, healthyCount, failedCount, unknownCount, operationsLocked
    }`,
    )
    const state = execute(
        computed,
        ref,
        watch,
        () => props,
        () =>
            (...args) =>
                events.push(args),
    )
    return { props, state, events }
}

test('proxy pool filters before pagination and keeps a deliberate selection across pages and filters', async () => {
    const { state, events } = panel()
    assert.equal(state.pageRows.value.length, 20)
    state.togglePage(true)
    state.currentPage.value = 2
    assert.equal(state.pageRows.value.length, 11)
    state.toggleRow(state.pageRows.value[0], true)
    assert.equal(state.selectedCount.value, 21)
    state.search.value = 'tokyo'
    await nextTick()
    assert.equal(state.currentPage.value, 1)
    assert.deepEqual(
        state.pageRows.value.map((row) => row.id),
        [1, 2, 3],
    )
    state.emitBulk('check')
    assert.equal(events[0][0], 'bulk')
    assert.equal(events[0][1].action, 'check')
    assert.equal(events[0][1].ids.length, 21)
    state.clearSelection()
    state.selectFiltered()
    assert.equal(state.selectedCount.value, 3)
    state.stateFilter.value = 'disabled'
    state.healthFilter.value = 'failed'
    assert.deepEqual(
        state.filteredRows.value.map((row) => row.id),
        [2],
    )
    assert.equal(state.selectedCount.value, 3)
})

test('proxy bulk running locks selection and duplicate requests while removed rows leave the selection', async () => {
    const { state, props, events } = panel()
    state.selectFiltered()
    assert.equal(state.selectedCount.value, 31)
    props.bulkState.running = true
    state.clearSelection()
    state.togglePage(false)
    state.toggleRow(props.rows[0], false)
    state.emitBulk('delete')
    assert.equal(state.selectedCount.value, 31)
    assert.equal(events.length, 0)
    props.bulkState.running = false
    props.rows = props.rows.filter((row) => row.id !== 1)
    await nextTick()
    assert.equal(state.selectedCount.value, 30)
    state.onPageSizeChange(10)
    assert.equal(state.pageRows.value.length, 10)
    assert.equal(state.currentPage.value, 1)
})

test('proxy pool retains full addresses and exact health totals including unknown results', () => {
    const { state, props } = panel()
    assert.equal(props.rows[0].proxy_url, 'socks5://user:secret@proxy-1.test:1080')
    assert.equal(state.enabledCount.value, 16)
    assert.equal(state.healthyCount.value, 11)
    assert.equal(state.failedCount.value, 10)
    assert.equal(state.unknownCount.value, 10)
    props.busy.add('pool:1:check')
    assert.equal(state.isBusy(props.rows[0], 'check'), true)
    assert.equal(state.isBusy(props.rows[0], 'delete'), false)
})

test('proxy import locks selection and batch actions until the import finishes', () => {
    const { state, props, events } = panel()
    state.toggleRow(props.rows[0], true)
    props.importing = true
    assert.equal(state.operationsLocked.value, true)
    state.togglePage(true)
    state.toggleRow(props.rows[1], true)
    state.selectFiltered()
    state.clearSelection()
    state.emitBulk('delete')
    assert.equal(state.selectedCount.value, 1)
    assert.equal(events.length, 0)
    props.importing = false
    state.emitBulk('disable')
    assert.deepEqual(events, [['bulk', { action: 'disable', ids: [1] }]])
})
