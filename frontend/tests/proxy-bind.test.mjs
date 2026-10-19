import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { parse } from '@vue/compiler-sfc'
import { computed, reactive, ref, nextTick } from 'vue'

function view(api = {}, confirm = async () => {}) {
    const { descriptor } = parse(readFileSync(new URL('../src/views/ProxyPoolView.vue', import.meta.url), 'utf8'))
    const source = descriptor.scriptSetup.content.replace(/^import .*$/gm, '')
    const messages = []
    const message = (value) => messages.push(value)
    for (const kind of ['success', 'warning', 'info', 'error']) message[kind] = message
    const execute = new Function(
        'computed',
        'reactive',
        'ref',
        'nextTick',
        'onMounted',
        'api',
        'ElMessage',
        'ElMessageBox',
        'notifyError',
        'document',
        `${source}\nreturn {
        pool, accountRows, accountError, accountDrafts, accountSaveErrors, expandedId, boundAccounts, boundRows,
        bindSelections, selectedAccountIds, bindingOptions, bindResults, bindRefreshWarning, busy, poolBulk,
        poolImporting, poolImportDialog, poolForm, inlineEditing, loadBoundAccounts, expandProxy,
        bindingReason, bindSelectedAccounts, saveBoundAccount, batchPool, openPoolImport, accountProxyLabel, loadPool, poolLoading
    }`,
    )
    return execute(
        computed,
        reactive,
        ref,
        nextTick,
        () => {},
        {
            get: async () => {
                throw new Error('Unexpected fixture read')
            },
            put: async () => {
                throw new Error('Unexpected fixture write')
            },
            ...api,
        },
        message,
        { confirm },
        (error) => messages.push(error.message),
        { querySelectorAll: () => [] },
    )
}

const target = () => ({
    id: 7,
    proxy_url: 'socks5h://demo:plain-password@target.fixture:1080',
    enabled: true,
    bound_account_count: 0,
})

test('a late proxy refresh cannot replace the newer binding count', async () => {
    const reads = []
    const state = view({ get: () => new Promise((resolve) => reads.push(resolve)) })
    const earlier = state.loadPool()
    const latest = state.loadPool()
    reads[1]({ entries: [{ id: 7, bound_account_count: 2 }] })
    await latest
    reads[0]({ entries: [{ id: 7, bound_account_count: 0 }] })
    await earlier
    assert.equal(state.pool.value[0].bound_account_count, 2)
    assert.equal(state.poolLoading.value, false)
})

test('binding invalidates an earlier pool refresh even if the final refresh fails', async () => {
    let finishRead
    let finishWrite
    let reads = 0
    const row = target()
    const state = view({
        get: async () => {
            reads++
            if (reads === 1) return new Promise((resolve) => { finishRead = resolve })
            throw new Error('fixture refresh failed')
        },
        put: async () => new Promise((resolve) => { finishWrite = resolve }),
    })
    state.pool.value = [row]
    state.accountRows.value = [{ id: 1, email: 'bound@fixture.local' }]
    state.expandedId.value = row.id
    state.selectedAccountIds.value = [1]
    const refreshing = state.loadPool()
    const binding = state.bindSelectedAccounts(row)
    finishRead({ entries: [{ ...row, bound_account_count: 0 }] })
    await refreshing
    finishWrite({ success: true })
    await binding
    assert.equal(state.pool.value[0].bound_account_count, 1)
    assert.equal(state.boundRows.value.length, 1)
    assert.equal(state.boundAccounts.value, 1)
    assert.equal(state.bindRefreshWarning.value, true)
    assert.equal(state.poolLoading.value, false)
})

test('expanding an empty proxy loads every account and retains plain details and unsaved account edits', async () => {
    let fail = true
    let reads = 0
    const account = {
        id: 1,
        email: 'demo@fixture.local',
        proxy_url: '',
        st: 'plain-st',
        at: 'plain-at',
        login_password: 'plain-login-password',
        remark: 'server',
    }
    const state = view({
        get: async () => {
            reads++
            if (fail) throw new Error('fixture offline')
            return [account]
        },
    })
    const row = target()
    state.pool.value = [row]
    state.expandProxy(row)
    await state.loadBoundAccounts()
    assert.equal(state.accountError.value, 'fixture offline')
    fail = false
    await state.loadBoundAccounts(true)
    assert.equal(reads, 2)
    assert.equal(state.accountRows.value[0].login_password, 'plain-login-password')
    assert.equal(state.bindingOptions.value[0].st, 'plain-st')
    state.accountDrafts[1].remark = 'unsaved local edit'
    await state.loadBoundAccounts(true)
    assert.equal(state.accountDrafts[1].remark, 'unsaved local edit')
})

test('binding saves individual pool IDs, retains failures for retry, and distinguishes refresh failure', async () => {
    const writes = []
    const row = target()
    const state = view({
        put: async (path, body) => {
            writes.push({ path, body })
            return path.endsWith('/2') ? { success: false, message: 'fixture save failed' } : { success: true }
        },
        get: async () => {
            throw new Error('fixture refresh failed')
        },
    })
    state.pool.value = [{ id: 3, bound_account_count: 1 }, row]
    state.expandedId.value = row.id
    state.accountRows.value = [
        { id: 1, email: 'first@fixture.local', proxy_pool_id: 3, proxy_url: 'socks5://old.fixture:1080' },
        { id: 2, email: 'retry@fixture.local', proxy_pool_id: null, proxy_url: '' },
    ]
    state.selectedAccountIds.value = [1, 2]
    await state.bindSelectedAccounts(row)
    assert.deepEqual(writes, [
        { path: '/api/tokens/1', body: { proxy_pool_id: 7 } },
        { path: '/api/tokens/2', body: { proxy_pool_id: 7 } },
    ])
    assert.deepEqual([...state.selectedAccountIds.value], [2])
    assert.equal(state.bindResults[7][1].success, false)
    assert.equal(state.bindResults[7][1].message, 'fixture save failed')
    assert.equal(state.bindRefreshWarning.value, true)
    assert.equal(state.boundRows.value[0].email, 'first@fixture.local')
    assert.equal(row.bound_account_count, 1)
    assert.equal(state.pool.value[0].bound_account_count, 0)
    assert.equal(state.accountRows.value[0].proxy_url, row.proxy_url)
})

test('Cloak options preserve fixed exits, show their original proxy, and permit matching pool association', () => {
    const row = target()
    const state = view()
    const cloak = { id: 1, browser_profile_source: 'cloak', proxy_url: 'socks5h://original:user@old.fixture:1080' }
    assert.equal(state.bindingReason(cloak, row), '')
    assert.match(state.accountProxyLabel(cloak), /original:user@old.fixture/)
    assert.equal(state.bindingReason({ ...cloak, proxy_url: row.proxy_url }, row), '')
    assert.equal(
        state.bindingReason(
            { ...cloak, proxy_url: 'st5 target.fixture:1080:demo:plain-password' },
            { ...row, proxy_url: 'socks5://demo:plain-password@target.fixture:1080' },
        ),
        '',
    )
    assert.equal(
        state.bindingReason(
            { ...cloak, proxy_url: 'http://user:p%3Ass@proxy.fixture:80' },
            { ...row, proxy_url: 'http://user:p%3ass@proxy.fixture:80' },
        ),
        '',
    )
    assert.match(state.bindingReason({ browser_profile_source: 'cookies' }, { ...row, enabled: false }), /停用/)
    assert.equal(state.bindingReason({ browser_profile_source: 'cookies' }, { ...row, last_check_ok: false }), '')
})

test('Cloak switching asks once and keeps the original binding and selection after cancellation', async () => {
    let writes = 0
    const confirmations = []
    const row = target()
    const state = view({ put: async () => { writes++ } }, async (...args) => {
        confirmations.push(args)
        throw new Error('cancel')
    })
    const account = { id: 1, email: 'cloak@fixture.local', browser_profile_source: 'cloak', proxy_pool_id: 3, proxy_url: 'socks5://old.fixture:1080' }
    state.pool.value = [row]
    state.accountRows.value = [account]
    state.expandedId.value = row.id
    state.selectedAccountIds.value = [1]
    await state.bindSelectedAccounts(row)
    assert.equal(confirmations.length, 1)
    assert.match(confirmations[0][0], /Cookie.*seed.*Profile/)
    assert.equal(writes, 0)
    assert.equal(account.proxy_pool_id, 3)
    assert.deepEqual([...state.selectedAccountIds.value], [1])
    assert.equal(state.busy.size, 0)
})

test('binding excludes overlapping import and bulk writes while preserving per-proxy selection', async () => {
    let finish
    let calls = 0
    const row = target()
    const state = view({
        put: async () => {
            calls++
            return new Promise((resolve) => {
                finish = resolve
            })
        },
        get: async () => ({ entries: [row] }),
    })
    state.pool.value = [row]
    state.accountRows.value = [{ id: 1, email: 'one@fixture.local' }]
    state.expandedId.value = row.id
    state.selectedAccountIds.value = [1]
    const pending = state.bindSelectedAccounts(row)
    assert.equal(await state.loadBoundAccounts(true), false)
    await state.bindSelectedAccounts(row)
    await state.batchPool({ action: 'delete', ids: [7] })
    state.openPoolImport()
    state.expandProxy({ id: 8, proxy_url: 'socks5://other.fixture:1080' })
    assert.equal(calls, 1)
    assert.equal(state.poolImportDialog.value, false)
    assert.equal(state.expandedId.value, 7)
    finish({ success: true })
    await pending
    assert.equal(state.busy.size, 0)
})

test('inline bound-account settings save only changed editable fields and keep failed drafts', async () => {
    const writes = []
    let fail = true
    const state = view({
        put: async (path, body) => {
            writes.push({ path, body })
            return fail ? { success: false, message: 'fixture error' } : { success: true }
        },
    })
    const account = {
        id: 1,
        email: 'one@fixture.local',
        st: 'plain-st',
        at: 'plain-at',
        remark: 'original',
        auto_refresh_enabled: true,
        image_enabled: true,
        video_enabled: true,
    }
    state.accountDrafts[1] = { remark: 'edited', auto_refresh_enabled: false, image_enabled: true, video_enabled: true }
    await state.saveBoundAccount(account)
    assert.equal(account.remark, 'original')
    assert.equal(state.accountDrafts[1].remark, 'edited')
    assert.equal(state.accountSaveErrors[1], 'fixture error')
    fail = false
    await state.saveBoundAccount(account)
    assert.deepEqual(writes[1], { path: '/api/tokens/1', body: { remark: 'edited', auto_refresh_enabled: false } })
    assert.equal(account.remark, 'edited')
    assert.equal(state.accountSaveErrors[1], undefined)
})
