<script setup>
import { computed, nextTick, onMounted, reactive, ref } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { api, notifyError } from '../lib/api'
import ProxyPoolPanel from '../components/ProxyPoolPanel.vue'
const busy = reactive(new Set())
const pool = ref([])
const proxyDefaults = () => ({
    protocol: 'socks5h',
    host: '',
    port: 1080,
    username: '',
    password: '',
    note: '',
})
const poolForm = reactive(proxyDefaults())
const poolDialog = ref(false)
const poolEditingId = ref(null)
const poolLoading = ref(false)
const poolBulk = reactive({ running: false, done: 0, total: 0, action: '' })
const poolBulkResult = ref(null)
const poolImportDialog = ref(false)
const poolImportText = ref('')
const poolImporting = ref(false)
const poolImportResult = ref(null)
const poolImportError = ref('')
const poolImportRefreshWarning = ref(false)
const poolImportLines = computed(() => poolImportText.value.split(/\r\n|\n|\r/).filter((line) => line.trim()).length)
const boundAccounts = ref(0)
const expandedId = ref(null)
const inlineEditing = ref(false)
const accountRows = ref(null)
const accountError = ref('')
const accountsLoading = ref(false)
const accountDrafts = reactive({})
const accountSaveErrors = reactive({})
const bindSelections = reactive({})
const bindResults = reactive({})
const bindRefreshWarning = ref(false)
const boundRows = computed(() => (accountRows.value || []).filter((row) => row.proxy_pool_id === expandedId.value))
const selectedAccountIds = computed({
    get: () => bindSelections[expandedId.value] || [],
    set: (value) => (bindSelections[expandedId.value] = value),
})
const bindingOptions = computed(() => {
    const target = pool.value.find((row) => row.id === expandedId.value)
    return (accountRows.value || [])
        .filter((account) => account.proxy_pool_id !== expandedId.value)
        .map((account) => ({
            ...account,
            bindingReason: bindingReason(account, target),
        }))
})
const accountSettings = (account) => ({
    remark: account.remark || '',
    submission_proxy_mode: account.submission_proxy_mode || 'fixed',
    auto_refresh_enabled: account.auto_refresh_enabled !== false,
    image_enabled: account.image_enabled !== false,
    video_enabled: account.video_enabled !== false,
})
const hostPort = computed(
    () => `${poolForm.host.includes(':') ? `[${poolForm.host}]` : poolForm.host}:${poolForm.port}`,
)
const drafts = new Map()
function preserveDraft() {
    if (inlineEditing.value && expandedId.value != null) drafts.set(expandedId.value, { ...poolForm })
}
let accountRequest
async function loadBoundAccounts(force = false) {
    if (busy.size || poolBulk.running || poolImporting.value) return false
    if (accountRequest) return accountRequest
    if (accountRows.value !== null && force !== true) return true
    accountsLoading.value = true
    accountRequest = api
        .get('/api/tokens')
        .then((rows) => {
            if (!Array.isArray(rows)) throw new Error('账号列表格式异常，请重试')
            const previous = new Map((accountRows.value || []).map((account) => [account.id, accountSettings(account)]))
            for (const account of rows) {
                const draft = accountDrafts[account.id]
                if (!draft || Object.entries(draft).every(([key, value]) => previous.get(account.id)?.[key] === value))
                    accountDrafts[account.id] = accountSettings(account)
            }
            accountRows.value = rows
            accountError.value = ''
            return true
        })
        .catch((error) => {
            accountError.value = error.message
            return false
        })
        .finally(() => {
            accountsLoading.value = false
            accountRequest = null
        })
    return accountRequest
}
function bindingReason(account, target) {
    if (!target?.enabled) return '出口已停用，请先启用'
    return ''
}
function sameProxy(first, second) {
    if (String(first || '').trim() === String(second || '').trim()) return true
    try {
        const normalize = (value) => {
            let raw = String(value || '').trim()
            if (/^st5\s+/i.test(raw)) raw = `socks5://${raw.slice(4).trim()}`
            const separator = raw.indexOf('://')
            const protocol = separator < 0 ? 'http' : raw.slice(0, separator).toLowerCase()
            let rest = separator < 0 ? raw : raw.slice(separator + 3)
            const legacy = rest.startsWith('[')
                ? rest.match(/^\[([^\]]+)\]:(\d+):([^:]+):(.+)$/)
                : rest.match(/^([^:]+):(\d+):([^:]+):(.+)$/)
            if (legacy)
                rest = `${encodeURIComponent(legacy[3]).replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`)}:${encodeURIComponent(legacy[4]).replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`)}@${legacy[1].includes(':') ? `[${legacy[1]}]` : legacy[1]}:${legacy[2]}`
            if (!['http', 'https', 'socks5', 'socks5h'].includes(protocol) || !/^.+:\d+$/.test(rest))
                throw new Error('Invalid proxy')
            return `${protocol}://${rest}`
        }
        return normalize(first) === normalize(second)
    } catch {
        return false
    }
}
function accountProxyLabel(account) {
    if (account.proxy_pool_id) return `当前出口 #${account.proxy_pool_id} · ${account.proxy_url || '—'}`
    return account.proxy_url ? `独立代理 ${account.proxy_url}` : '当前直连'
}
function focusBinding(row) {
    expandProxy(row)
    nextTick(() =>
        [...document.querySelectorAll('.inline-binding .el-select input')]
            .find((input) => input.getClientRects().length)
            ?.focus(),
    )
}
function applyAccountBinding(account, target) {
    const previous = pool.value.find((row) => row.id === account.proxy_pool_id)
    if (previous && previous.id !== target.id)
        previous.bound_account_count = Math.max(0, (previous.bound_account_count || 0) - 1)
    if (account.proxy_pool_id !== target.id) target.bound_account_count = (target.bound_account_count || 0) + 1
    account.proxy_pool_id = target.id
    account.proxy_url = target.proxy_url
    if (expandedId.value === target.id) boundAccounts.value = target.bound_account_count
}
async function bindSelectedAccounts(row) {
    if (
        busy.size ||
        accountsLoading.value ||
        poolImporting.value ||
        poolBulk.running ||
        !selectedAccountIds.value.length
    )
        return
    const ids = [...selectedAccountIds.value]
    busy.add('poolBind')
    poolRequestVersion++
    poolLoading.value = false
    bindResults[row.id] = []
    bindRefreshWarning.value = false
    try {
        const switching = (accountRows.value || []).filter(
            (account) =>
                ids.includes(account.id) &&
                account.browser_profile_source === 'cloak' &&
                !sameProxy(account.proxy_url, row.proxy_url),
        )
        if (switching.length) {
            try {
                await ElMessageBox.confirm(
                    `将 ${switching.length} 个 Cloak 账号切换到代理 #${row.id}。服务端会保留现有 Cookie 和 seed，逐个验证并重新绑定 Profile。请先关闭这些账号的浏览器；验证期间对应账号暂停接收新任务。验证失败保留原出口。`,
                    '确认切换 Cloak 代理',
                    { confirmButtonText: '验证并切换', cancelButtonText: '保留原绑定', type: 'warning' },
                )
            } catch {
                return
            }
        }
        for (const id of ids) {
            const account = (accountRows.value || []).find((item) => item.id === id)
            try {
                if (!account) throw new Error('账号已变更，请刷新账号列表')
                const reason = bindingReason(account, row)
                if (reason) throw new Error(reason)
                ensure(await api.put(`/api/tokens/${id}`, { proxy_pool_id: row.id }))
                applyAccountBinding(account, row)
                bindSelections[row.id] = (bindSelections[row.id] || []).filter((value) => value !== id)
                bindResults[row.id].push({ id, email: account.email, success: true, message: '已绑定' })
            } catch (error) {
                bindResults[row.id].push({
                    id,
                    email: account?.email || `#${id}`,
                    success: false,
                    message: error.message,
                })
            }
        }
        bindRefreshWarning.value = !(await loadPool(false))
    } finally {
        busy.delete('poolBind')
    }
}
async function saveBoundAccount(account) {
    if (busy.size || accountsLoading.value || poolImporting.value || poolBulk.running) return
    const key = `accountSave:${account.id}`
    const settings = accountSettings(account)
    const body = Object.fromEntries(
        Object.entries(accountDrafts[account.id]).filter(([name, value]) => settings[name] !== value),
    )
    if (!Object.keys(body).length) return
    busy.add(key)
    delete accountSaveErrors[account.id]
    try {
        ensure(await api.put(`/api/tokens/${account.id}`, body))
        Object.assign(account, body)
        ElMessage.success('账号设置已保存')
    } catch (error) {
        accountSaveErrors[account.id] = error.message
    } finally {
        busy.delete(key)
    }
}
async function copyValue(value, label) {
    try {
        await navigator.clipboard.writeText(String(value ?? ''))
        ElMessage.success(`${label}已复制`)
    } catch {
        ElMessage.error('复制失败，请选择内容后手动复制')
    }
}
function expandProxy(row, expanded = true, edit = false) {
    if (busy.size || poolBulk.running || poolImporting.value) return
    preserveDraft()
    if (!expanded) {
        expandedId.value = null
        inlineEditing.value = false
        return
    }
    if (expandedId.value !== row.id || !inlineEditing.value) openProxy(row, false)
    expandedId.value = row.id
    if (edit) {
        inlineEditing.value = true
        focusInline()
    }
    loadBoundAccounts()
}
function focusInline() {
    nextTick(() =>
        [...document.querySelectorAll('.proxy-inline-editor input:not([readonly]):not([disabled])')]
            .find((input) => input.getClientRects().length)
            ?.focus(),
    )
}
function cancelInline() {
    drafts.delete(expandedId.value)
    inlineEditing.value = false
    const row = pool.value.find((row) => row.id === expandedId.value)
    if (row) openProxy(row, false)
    inlineEditing.value = false
}
function ensure(result) {
    if (result?.success === false) throw new Error(result.message || '配置保存未完成')
    return result
}
let poolRequestVersion = 0
async function loadPool(reportError = true) {
    const version = ++poolRequestVersion
    poolLoading.value = true
    try {
        const entries = ensure(await api.get('/api/proxy/pool')).entries || []
        if (version !== poolRequestVersion) return false
        pool.value = entries
        return true
    } catch (error) {
        if (version === poolRequestVersion && reportError) notifyError(error)
        return false
    } finally {
        if (version === poolRequestVersion) poolLoading.value = false
    }
}
function openProxy(row = null, edit = true) {
    if (busy.size || poolImporting.value || poolBulk.running) return
    preserveDraft()
    let fields = proxyDefaults()
    if (row) {
        try {
            fields = parseProxyFields(row.proxy_url)
        } catch {
            return ElMessage.warning('代理地址格式异常，请检查原始配置')
        }
    }
    poolEditingId.value = row?.id ?? null
    boundAccounts.value = Number(row?.bound_account_count || 0)
    Object.assign(poolForm, fields, { note: row?.note || '' })
    if (row) {
        expandedId.value = row.id
        const draft = drafts.get(row.id)
        if (draft) Object.assign(poolForm, draft)
        inlineEditing.value = edit || !!draft
        poolDialog.value = false
        loadBoundAccounts()
        if (edit) focusInline()
    } else {
        expandedId.value = null
        poolDialog.value = true
    }
}
function parseProxyFields(value) {
    const url = new URL(value)
    if (!['http:', 'https:', 'socks5:', 'socks5h:'].includes(url.protocol)) throw new Error('Invalid proxy protocol')
    return {
        protocol: url.protocol.slice(0, -1),
        host: url.hostname.replace(/^\[|\]$/g, ''),
        port: Number(url.port || (url.protocol === 'https:' ? 443 : url.protocol === 'http:' ? 80 : 1080)),
        username: decodeProxyCredential(url.username),
        password: decodeProxyCredential(url.password),
    }
}
function decodeProxyCredential(value) {
    try {
        return decodeURIComponent(value)
    } catch {
        return value
    }
}
function proxyURLFromFields(form) {
    const host = String(form.host || '')
        .trim()
        .replace(/^\[|\]$/g, '')
    if (!host || /[\s/@?#\\]/.test(host)) throw new Error('请填写有效的 IP 地址或主机名')
    if (!Number.isInteger(form.port) || form.port < 1 || form.port > 65535)
        throw new Error('端口请填写 1–65535 之间的整数')
    if (!['http', 'https', 'socks5', 'socks5h'].includes(form.protocol)) throw new Error('请选择代理协议')
    if (form.password && !form.username) throw new Error('填写密码时也需要填写用户名')
    const authority = `${host.includes(':') ? `[${host}]` : host}:${form.port}`
    try {
        if (!new URL(`http://${authority}`).hostname) throw new Error()
    } catch {
        throw new Error('请填写有效的 IP 地址或主机名')
    }
    const credentials = form.username
        ? `${encodeURIComponent(form.username)}:${encodeURIComponent(form.password || '')}@`
        : ''
    return `${form.protocol}://${credentials}${authority}`
}
function clearProxyForm() {
    if (!poolDialog.value && expandedId.value != null) return
    Object.assign(poolForm, proxyDefaults())
    poolEditingId.value = null
}
async function addProxy() {
    if (busy.size || poolImporting.value || poolBulk.running) return
    let proxyURL
    try {
        proxyURL = proxyURLFromFields(poolForm)
    } catch (error) {
        return ElMessage.warning(error.message)
    }
    busy.add('poolAdd')
    const editingId = poolEditingId.value
    try {
        const body = {
            ...(boundAccounts.value ? {} : { proxy_url: proxyURL }),
            note: poolForm.note.trim(),
        }
        ensure(
            await (poolEditingId.value === null
                ? api.post('/api/proxy/pool', body)
                : api.put(`/api/proxy/pool/${poolEditingId.value}`, body)),
        )
        await loadPool()
        if (editingId !== null) {
            drafts.delete(editingId)
            inlineEditing.value = false
            busy.delete('poolAdd')
            const row = pool.value.find((row) => row.id === editingId)
            if (row) openProxy(row, false)
            inlineEditing.value = false
        } else {
            poolDialog.value = false
            clearProxyForm()
        }
        ElMessage.success(editingId === null ? '代理出口已加入' : '代理出口已更新')
    } catch (error) {
        notifyError(error)
    } finally {
        busy.delete('poolAdd')
    }
}
async function poolAction(row, type) {
    if (poolBulk.running || poolImporting.value || busy.size) return
    if (type === 'delete') {
        try {
            await ElMessageBox.confirm(`移除代理出口 #${row.id}？`, '移除出口', {
                confirmButtonText: '移除',
                cancelButtonText: '保留',
                type: 'warning',
            })
        } catch {
            return
        }
        if (poolBulk.running || poolImporting.value || busy.size) return
    }
    const key = `pool:${row.id}:${type}`
    busy.add(key)
    try {
        const result =
            type === 'delete'
                ? await api.delete(`/api/proxy/pool/${row.id}`)
                : type === 'check'
                  ? await diagnostic(`/api/proxy/pool/${row.id}/check`, {})
                  : await api.post(`/api/proxy/pool/${row.id}/${type}`, {})
        if (type === 'check') ElMessage({ type: result.success ? 'success' : 'warning', message: result.message })
        else ensure(result)
        await loadPool()
    } catch (error) {
        notifyError(error)
    } finally {
        busy.delete(key)
    }
}
async function batchPool({ action, ids }) {
    if (poolBulk.running || poolImporting.value || busy.size || !ids.length) return
    if (action === 'delete') {
        try {
            await ElMessageBox.confirm(`移除所选 ${ids.length} 个代理出口？`, '批量移除代理', {
                confirmButtonText: '移除所选',
                cancelButtonText: '保留',
                type: 'warning',
            })
        } catch {
            return
        }
        if (poolBulk.running || poolImporting.value || busy.size) return
    }
    Object.assign(poolBulk, { running: true, done: 0, total: ids.length, action })
    poolBulkResult.value = null
    try {
        const result = await diagnostic('/api/proxy/pool/batch', { action, ids })
        poolBulk.done = result.results?.length || 0
        poolBulkResult.value = result
        await loadPool()
    } catch (error) {
        notifyError(error)
    } finally {
        poolBulk.running = false
    }
}
function openPoolImport() {
    if (poolBulk.running || poolImporting.value || busy.size) return
    poolImportDialog.value = true
}
function poolImportFailureMessage(item) {
    return (
        {
            invalid_format: '格式有误，请使用 主机:端口:用户名:密码 或完整代理 URL',
            import_failed: '保存失败，请稍后重试',
        }[item?.error_code] || '代理格式或连接信息有误，请检查后重试'
    )
}
async function importPool() {
    if (poolImporting.value || poolBulk.running || busy.size) return
    const text = poolImportText.value
    const lines = text.split(/\r\n|\n|\r/)
    const entries = lines.map((value, index) => ({ value, line: index + 1 })).filter((item) => item.value.trim())
    poolImportError.value = ''
    if (!entries.length) {
        poolImportError.value = '请填写代理地址，每行一个'
        return
    }
    if (text.length > 2100000) {
        poolImportError.value = '导入文本最多 2100000 个字符，请拆分后导入'
        return
    }
    if (entries.length > 500) {
        poolImportError.value = '每次最多导入 500 行代理，请拆分后导入'
        return
    }
    const tooLongIndex = lines.findIndex((line) => line.length > 4096)
    if (tooLongIndex !== -1) {
        poolImportError.value = `第 ${tooLongIndex + 1} 行超过 4096 个字符，请检查后重试`
        return
    }
    poolImporting.value = true
    poolImportResult.value = null
    poolImportRefreshWarning.value = false
    try {
        const result = await api.post('/api/proxy/pool/import', { text })
        if (result?.success !== true || !Array.isArray(result.results)) throw new Error('import_failed')
        poolImportResult.value = {
            total: result.total,
            added: result.added,
            skipped: result.skipped,
            failed: result.failed,
            results: result.results.map((item) => ({
                line: item.line,
                status: item.status,
                message: item.status === 'failed' ? poolImportFailureMessage(item) : '',
            })),
        }
        const completed = new Set(
            result.results.filter((item) => ['added', 'skipped'].includes(item.status)).map((item) => item.line),
        )
        poolImportText.value = lines.filter((line, index) => line.trim() && !completed.has(index + 1)).join('\n')
        poolImportRefreshWarning.value = !(await loadPool(false))
    } catch {
        poolImportError.value = '批量导入未完成，请检查输入或连接后重试；输入已保留'
    } finally {
        poolImporting.value = false
    }
}
async function diagnostic(path, body) {
    const result = await api.raw(path, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
    const payload = await result.json()
    if (!result.ok) throw new Error(payload.detail || payload.message || '诊断请求失败')
    return payload
}
onMounted(loadPool)
</script>
<template>
    <div class="proxy-pool-view">
        <header class="page-heading">
            <div>
                <h1>代理池</h1>
                <p>账号绑定与生成提交出口统一管理；随机调度分散负载，连续失败冷却后自动重探。</p>
            </div>
        </header>
        <section class="surface proxy-pool-surface">
            <ProxyPoolPanel
                :rows="pool"
                :busy="busy"
                :loading="poolLoading"
                :bulk-state="poolBulk"
                :importing="poolImporting"
                :expanded-id="expandedId"
                @reload="loadPool"
                @add="openProxy()"
                @import="openPoolImport"
                @edit="openProxy"
                @bind="focusBinding"
                @expand="expandProxy"
                @copy="copyValue"
                @toggle="poolAction($event, 'toggle')"
                @check="poolAction($event, 'check')"
                @remove="poolAction($event, 'delete')"
                @bulk="batchPool"
            >
                <template #details="{ row }">
                    <section
                        v-if="expandedId === row.id"
                        class="proxy-inline-editor"
                        :aria-label="`出口 ${row.id} 详情`"
                    >
                        <div class="inline-connection">
                            <header class="inline-heading">
                                <strong>
                                    连接信息
                                    <span>#{{ row.id }}</span>
                                </strong>
                                <div class="inline-editor-actions">
                                    <template v-if="inlineEditing">
                                        <el-button size="small" :disabled="busy.size > 0" @click="cancelInline">
                                            取消
                                        </el-button>
                                        <el-button
                                            size="small"
                                            type="primary"
                                            :loading="busy.has('poolAdd')"
                                            :disabled="busy.size > 0 && !busy.has('poolAdd')"
                                            @click="addProxy"
                                        >
                                            保存修改
                                        </el-button>
                                    </template>
                                    <el-button
                                        v-else
                                        text
                                        size="small"
                                        :disabled="busy.size > 0"
                                        @click="openProxy(row)"
                                    >
                                        编辑连接
                                    </el-button>
                                </div>
                            </header>
                            <el-form
                                v-if="inlineEditing"
                                label-position="top"
                                class="inline-fields"
                                :disabled="busy.size > 0"
                            >
                                <el-form-item label="协议">
                                    <el-select
                                        v-model="poolForm.protocol"
                                        aria-label="代理协议"
                                        :disabled="boundAccounts > 0"
                                    >
                                        <el-option
                                            v-for="protocol in ['http', 'https', 'socks5', 'socks5h']"
                                            :key="protocol"
                                            :label="protocol.toUpperCase()"
                                            :value="protocol"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item label="端口">
                                    <el-input-number
                                        v-model="poolForm.port"
                                        :min="1"
                                        :max="65535"
                                        :precision="0"
                                        :disabled="boundAccounts > 0"
                                        controls-position="right"
                                    />
                                </el-form-item>
                                <el-form-item label="IP / 主机" class="inline-field-wide">
                                    <el-input v-model="poolForm.host" :disabled="boundAccounts > 0" />
                                </el-form-item>
                                <el-form-item label="用户名">
                                    <el-input
                                        v-model="poolForm.username"
                                        :disabled="boundAccounts > 0"
                                        autocomplete="off"
                                    />
                                </el-form-item>
                                <el-form-item label="密码">
                                    <el-input
                                        v-model="poolForm.password"
                                        type="text"
                                        :disabled="boundAccounts > 0"
                                        autocomplete="off"
                                    />
                                </el-form-item>
                                <el-form-item label="备注" class="inline-field-wide">
                                    <el-input v-model="poolForm.note" />
                                </el-form-item>
                            </el-form>
                            <dl v-else class="connection-values">
                                <div>
                                    <dt>地址</dt>
                                    <dd>
                                        <span class="protocol-label">{{ poolForm.protocol.toUpperCase() }}</span>
                                        <code>{{ hostPort }}</code>
                                        <el-button text size="small" @click="copyValue(hostPort, '主机与端口')">
                                            复制
                                        </el-button>
                                    </dd>
                                </div>
                                <div>
                                    <dt>用户名</dt>
                                    <dd>
                                        <code>{{ poolForm.username || '无认证' }}</code>
                                        <el-button
                                            v-if="poolForm.username"
                                            text
                                            size="small"
                                            @click="copyValue(poolForm.username, '用户名')"
                                        >
                                            复制
                                        </el-button>
                                    </dd>
                                </div>
                                <div>
                                    <dt>密码</dt>
                                    <dd>
                                        <code>{{ poolForm.password || '—' }}</code>
                                        <el-button
                                            v-if="poolForm.password"
                                            text
                                            size="small"
                                            @click="copyValue(poolForm.password, '密码')"
                                        >
                                            复制
                                        </el-button>
                                    </dd>
                                </div>
                                <div>
                                    <dt>备注</dt>
                                    <dd>{{ poolForm.note || '—' }}</dd>
                                </div>
                            </dl>
                            <div class="inline-full-url">
                                <code>{{ row.proxy_url }}</code>
                                <el-button text size="small" @click="copyValue(row.proxy_url, '完整代理地址')">
                                    复制地址
                                </el-button>
                            </div>
                            <p v-if="boundAccounts && inlineEditing" class="field-note">
                                已绑定账号，连接保持固定，可编辑备注。
                            </p>
                        </div>
                        <div class="inline-accounts">
                            <header class="inline-heading">
                                <strong>
                                    绑定账号
                                    <span>{{ accountRows ? boundRows.length : row.bound_account_count || 0 }} 个</span>
                                </strong>
                                <el-button
                                    text
                                    size="small"
                                    :disabled="accountsLoading || busy.size > 0 || poolBulk.running || poolImporting"
                                    @click="loadBoundAccounts(true)"
                                >
                                    刷新账号
                                </el-button>
                            </header>
                            <div class="inline-binding">
                                <el-select
                                    v-model="selectedAccountIds"
                                    multiple
                                    filterable
                                    collapse-tags
                                    collapse-tags-tooltip
                                    :max-collapse-tags="2"
                                    :loading="accountsLoading"
                                    :disabled="!row.enabled || busy.size > 0 || poolBulk.running || poolImporting"
                                    placeholder="搜索邮箱 / ID，选择账号"
                                    :aria-label="`为出口 ${row.id} 选择账号`"
                                >
                                    <el-option
                                        v-for="account in bindingOptions"
                                        :key="account.id"
                                        :value="account.id"
                                        :label="`${account.email} #${account.id}`"
                                        :disabled="!!account.bindingReason"
                                        style="height: auto; line-height: 20px; padding-block: 6px"
                                    >
                                        <div class="binding-option">
                                            <span>
                                                {{ account.email }}
                                                <small>#{{ account.id }}</small>
                                            </span>
                                            <small>{{ accountProxyLabel(account) }}</small>
                                            <small v-if="account.bindingReason" class="binding-warning">
                                                {{ account.bindingReason }}
                                            </small>
                                        </div>
                                    </el-option>
                                </el-select>
                                <el-button
                                    type="primary"
                                    :loading="busy.has('poolBind')"
                                    :disabled="
                                        !row.enabled ||
                                        !selectedAccountIds.length ||
                                        (busy.size > 0 && !busy.has('poolBind')) ||
                                        accountsLoading ||
                                        poolBulk.running ||
                                        poolImporting
                                    "
                                    @click="bindSelectedAccounts(row)"
                                >
                                    绑定所选 {{ selectedAccountIds.length || '' }} 个
                                </el-button>
                            </div>
                            <p v-if="!row.enabled" class="field-note">出口已停用，启用后可绑定新账号。</p>
                            <p v-else-if="row.last_check_ok === false" class="binding-warning">
                                此出口最近检测异常，请确认连接后绑定。
                            </p>
                            <p v-if="accountsLoading" class="field-note">正在读取账号…</p>
                            <p v-else-if="accountError" class="binding-warning">
                                {{ accountError }}
                                <el-button text size="small" @click="loadBoundAccounts(true)">重试</el-button>
                            </p>
                            <p v-else-if="!boundRows.length" class="binding-empty">
                                尚未绑定账号，在上方选择即可批量绑定。
                            </p>
                            <div v-if="bindResults[row.id]?.length" class="bind-results" role="status">
                                <p
                                    v-for="result in bindResults[row.id]"
                                    :key="result.id"
                                    :class="{ 'binding-warning': !result.success }"
                                >
                                    {{ result.success ? '✓' : '!' }} {{ result.email }} · {{ result.message }}
                                </p>
                            </div>
                            <p v-if="bindRefreshWarning" class="binding-warning">
                                绑定结果已保存，出口列表刷新失败，请点击刷新。
                            </p>
                            <details v-for="account in boundRows" :key="account.id" class="bound-account-row">
                                <summary>
                                    <span class="bound-account-email">
                                        {{ account.email }}
                                        <small>#{{ account.id }}</small>
                                    </span>
                                    <span class="bound-account-status">
                                        {{ account.is_active ? '已启用' : '已停用' }} ·
                                        {{ account.browser_profile_source === 'cloak' ? 'Cloak' : 'Cookie' }}
                                    </span>
                                    <el-button
                                        text
                                        size="small"
                                        @click.prevent.stop="copyValue(account.email, '账号邮箱')"
                                    >
                                        复制
                                    </el-button>
                                    <span class="account-detail-toggle">查看 / 编辑</span>
                                </summary>
                                <div class="bound-account-details">
                                    <div
                                        v-for="[label, value] in [
                                            ['ST', account.st],
                                            ['AT', account.at],
                                            ['登录账号', account.login_account],
                                            ['登录密码', account.login_password],
                                        ]"
                                        :key="label"
                                        class="account-secret"
                                    >
                                        <span>{{ label }}</span>
                                        <code>{{ value || '—' }}</code>
                                        <el-button v-if="value" text size="small" @click="copyValue(value, label)">
                                            复制
                                        </el-button>
                                    </div>
                                    <el-input
                                        v-model="accountDrafts[account.id].remark"
                                        aria-label="账号备注"
                                        placeholder="账号备注"
                                        :disabled="
                                            busy.size > 0 || accountsLoading || poolBulk.running || poolImporting
                                        "
                                    />
                                    <label class="bound-submission-mode">
                                        生成提交出口
                                        <el-select
                                            v-model="accountDrafts[account.id].submission_proxy_mode"
                                            :aria-label="`${account.email} 的生成提交出口`"
                                            :disabled="
                                                busy.size > 0 || accountsLoading || poolBulk.running || poolImporting
                                            "
                                        >
                                            <el-option label="账号固定代理" value="fixed" />
                                            <el-option label="代理池随机（每次生成取健康出口）" value="pool_random" />
                                        </el-select>
                                    </label>
                                    <div class="bound-account-settings">
                                        <el-checkbox
                                            v-model="accountDrafts[account.id].auto_refresh_enabled"
                                            :disabled="
                                                busy.size > 0 || accountsLoading || poolBulk.running || poolImporting
                                            "
                                        >
                                            AT 自动续期
                                        </el-checkbox>
                                        <el-checkbox
                                            v-model="accountDrafts[account.id].image_enabled"
                                            :disabled="
                                                busy.size > 0 || accountsLoading || poolBulk.running || poolImporting
                                            "
                                        >
                                            图片
                                        </el-checkbox>
                                        <el-checkbox
                                            v-model="accountDrafts[account.id].video_enabled"
                                            :disabled="
                                                busy.size > 0 || accountsLoading || poolBulk.running || poolImporting
                                            "
                                        >
                                            视频
                                        </el-checkbox>
                                        <el-button
                                            size="small"
                                            type="primary"
                                            :loading="busy.has(`accountSave:${account.id}`)"
                                            :disabled="
                                                (busy.size > 0 && !busy.has(`accountSave:${account.id}`)) ||
                                                accountsLoading ||
                                                poolBulk.running ||
                                                poolImporting
                                            "
                                            @click="saveBoundAccount(account)"
                                        >
                                            保存设置
                                        </el-button>
                                    </div>
                                    <p v-if="accountSaveErrors[account.id]" class="binding-warning" role="alert">
                                        {{ accountSaveErrors[account.id] }}
                                    </p>
                                </div>
                            </details>
                        </div>
                    </section>
                </template>
            </ProxyPoolPanel>
            <div v-if="poolBulkResult" class="pool-batch-result" role="status">
                <strong>
                    {{ poolBulkResult.succeeded ?? poolBulkResult.results?.filter((item) => item.success).length ?? 0 }}
                    / {{ poolBulkResult.results?.length || 0 }} 个处理成功
                </strong>
                <p v-for="item in (poolBulkResult.results || []).filter((item) => !item.success)" :key="item.id">
                    #{{ item.id }} · {{ item.message || '处理未完成，请重试' }}
                </p>
            </div>
        </section>
        <el-dialog
            v-model="poolImportDialog"
            title="批量导入代理"
            width="min(680px, calc(100vw - 32px))"
            :close-on-click-modal="!poolImporting"
            :close-on-press-escape="!poolImporting"
            :show-close="!poolImporting"
            class="proxy-import-dialog"
        >
            <p class="field-note">
                每行一个代理，主机:端口:用户名:密码 默认使用 SOCKS5（远端 DNS）；也支持显式填写完整代理 URL。 远端 DNS
                兼容 Clash TUN 的 FakeIP，避免将本机虚拟 IP 发送给远端代理。
            </p>
            <el-input
                v-model="poolImportText"
                type="textarea"
                :rows="10"
                :disabled="poolImporting"
                :maxlength="2100000"
                aria-label="批量代理地址，每行一个"
                placeholder="192.0.2.1:6525:demo:demo-pass&#10;socks5h://demo:demo-pass@192.0.2.2:1080"
                autocomplete="off"
                spellcheck="false"
            />
            <p class="field-note pool-import-hint">
                {{ poolImportLines }} / 500 行 · 空行自动忽略 · 单行最多 4096 字符 · 重复代理自动跳过
            </p>
            <el-alert v-if="poolImportError" type="error" :closable="false" :title="poolImportError" role="alert" />
            <div v-if="poolImportResult" class="pool-batch-result" role="status">
                <strong>
                    已添加 {{ poolImportResult.added }} 个 · 跳过重复 {{ poolImportResult.skipped }} 个 · 失败
                    {{ poolImportResult.failed }} 个
                </strong>
                <p v-for="item in poolImportResult.results.filter((item) => item.status === 'failed')" :key="item.line">
                    第 {{ item.line }} 行 · {{ item.message }}
                </p>
                <span v-if="poolImportResult.failed">失败行已保留在输入框，修正后可再次导入。</span>
            </div>
            <el-alert
                v-if="poolImportRefreshWarning"
                type="warning"
                :closable="false"
                title="导入结果已保存，列表刷新失败，请关闭后点击刷新。"
                class="pool-import-hint"
            />
            <template #footer>
                <el-button :disabled="poolImporting" @click="poolImportDialog = false">关闭</el-button>
                <el-button
                    type="primary"
                    :loading="poolImporting"
                    :disabled="!poolImportLines || poolBulk.running"
                    @click="importPool"
                >
                    {{ poolImportResult?.failed ? '重试保留的代理' : '开始导入' }}
                </el-button>
            </template>
        </el-dialog>
        <el-dialog
            v-model="poolDialog"
            :title="poolEditingId === null ? '加入一个代理出口' : '编辑代理出口'"
            width="min(580px, calc(100vw - 32px))"
            :close-on-click-modal="!busy.has('poolAdd')"
            :close-on-press-escape="!busy.has('poolAdd')"
            :show-close="!busy.has('poolAdd')"
            @closed="clearProxyForm"
            class="proxy-editor-dialog"
        >
            <el-form label-position="top" :disabled="busy.has('poolAdd')" class="proxy-fields-form">
                <el-alert
                    v-if="boundAccounts"
                    type="info"
                    :closable="false"
                    class="bound-proxy-note"
                    :title="`已绑定 ${boundAccounts} 个账号，保持现有连接信息；备注可独立调整。`"
                />
                <div class="proxy-host-grid">
                    <el-form-item label="代理协议" required>
                        <el-select v-model="poolForm.protocol" aria-label="代理协议" :disabled="boundAccounts > 0">
                            <el-option
                                v-for="protocol in ['http', 'https', 'socks5', 'socks5h']"
                                :key="protocol"
                                :value="protocol"
                                :label="protocol.toUpperCase()"
                            />
                        </el-select>
                    </el-form-item>
                    <el-form-item label="端口" required>
                        <el-input-number
                            v-model="poolForm.port"
                            :disabled="boundAccounts > 0"
                            :min="1"
                            :max="65535"
                            :precision="0"
                            controls-position="right"
                            aria-label="端口"
                        />
                    </el-form-item>
                </div>
                <el-form-item label="IP / 主机" required>
                    <el-input
                        v-model="poolForm.host"
                        :disabled="boundAccounts > 0"
                        placeholder="输入 IP 地址或主机名"
                        autocomplete="off"
                    />
                </el-form-item>
                <div class="proxy-host-grid">
                    <el-form-item label="用户名">
                        <el-input
                            v-model="poolForm.username"
                            :disabled="boundAccounts > 0"
                            placeholder="可选，无认证时留空"
                            autocomplete="off"
                        />
                    </el-form-item>
                    <el-form-item label="密码">
                        <el-input
                            v-model="poolForm.password"
                            :disabled="boundAccounts > 0"
                            type="text"
                            placeholder="可选，无认证时留空"
                            autocomplete="new-password"
                        />
                    </el-form-item>
                </div>
                <el-form-item label="备注">
                    <el-input v-model="poolForm.note" placeholder="为这个出口起个名字" />
                </el-form-item>
            </el-form>
            <template #footer>
                <el-button :disabled="busy.has('poolAdd')" @click="poolDialog = false">取消</el-button>
                <el-button type="primary" :loading="busy.has('poolAdd')" @click="addProxy">
                    {{ poolEditingId === null ? '添加出口' : '保存代理修改' }}
                </el-button>
            </template>
        </el-dialog>
    </div>
</template>
<style scoped>
.proxy-pool-surface {
    padding: 24px;
}
.proxy-pool-view {
    min-width: 0;
}
.proxy-inline-editor {
    display: grid;
    grid-template-columns: minmax(0, 0.9fr) minmax(0, 1.1fr);
    gap: 28px;
    padding: 18px 24px;
    background: var(--surface);
    border-top: 1px solid var(--line);
    min-width: 0;
}
.inline-heading,
.inline-full-url,
.connection-values dd,
.inline-binding,
.inline-editor-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    min-width: 0;
}
.inline-heading {
    justify-content: space-between;
    min-height: 28px;
    margin-bottom: 8px;
}
.inline-heading strong {
    font-size: 13px;
}
.inline-heading strong span {
    margin-left: 5px;
    color: var(--muted);
    font-size: 12px;
    font-weight: normal;
}
.inline-editor-actions .el-button + .el-button {
    margin-left: 0;
}
.inline-connection,
.inline-accounts {
    min-width: 0;
}
.inline-accounts {
    border-left: 1px solid var(--line);
    padding-left: 28px;
}
.connection-values {
    display: grid;
    gap: 4px;
    margin: 0;
    font-size: 12px;
}
.connection-values > div {
    display: flex;
    gap: 12px;
    align-items: baseline;
    min-height: 25px;
}
.connection-values dt {
    flex: 0 0 44px;
    color: var(--muted);
}
.connection-values dd {
    margin: 0;
    flex-wrap: wrap;
    overflow-wrap: anywhere;
}
.connection-values code,
.inline-full-url code,
.account-secret code {
    font-size: 12px;
    color: var(--ink);
    overflow-wrap: anywhere;
    white-space: normal;
    user-select: text;
}
.protocol-label {
    color: var(--accent);
    font-size: 10px;
    font-weight: 600;
    letter-spacing: 0.04em;
}
.inline-full-url {
    align-items: baseline;
    margin-top: 10px;
    padding-top: 9px;
    border-top: 1px dashed var(--line);
}
.inline-full-url code {
    flex: 1;
    color: var(--muted);
    font-size: 11px;
}
.inline-full-url .el-button,
.connection-values .el-button {
    height: 24px;
    padding: 0 4px;
}
.inline-fields {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0 12px;
}
.inline-fields .el-form-item {
    margin-bottom: 8px;
}
.inline-fields :deep(.el-form-item__label) {
    font-size: 12px;
    padding-bottom: 3px;
    line-height: 18px;
}
.inline-fields :deep(.el-form-item__content),
.inline-fields :deep(.el-input-number),
.inline-fields :deep(.el-select) {
    width: 100%;
    min-width: 0;
}
.inline-field-wide {
    grid-column: 1 / -1;
}
.inline-binding {
    align-items: flex-start;
    margin-bottom: 8px;
}
.inline-binding .el-select {
    flex: 1;
    min-width: 0;
}
.inline-binding > .el-button {
    flex-shrink: 0;
}
.binding-option {
    display: flex;
    flex-direction: column;
}
.binding-option small,
.bound-account-row small {
    font-size: 11px;
    color: var(--muted);
}
.binding-warning {
    color: var(--el-color-warning);
    font-size: 12px;
    overflow-wrap: anywhere;
}
.binding-empty {
    color: var(--muted);
    font-size: 12px;
    padding: 8px 0;
}
.bind-results {
    max-height: 88px;
    overflow-y: auto;
    font-size: 12px;
    line-height: 1.7;
    color: var(--success);
}
.bound-account-row {
    border-top: 1px solid var(--line);
    font-size: 12px;
}
.bound-account-row summary {
    display: flex;
    align-items: center;
    gap: 7px;
    min-height: 36px;
    cursor: pointer;
    list-style: none;
}
.bound-account-row summary::-webkit-details-marker {
    display: none;
}
.bound-account-row summary::before {
    content: '›';
    font-size: 16px;
    color: var(--muted);
}
.bound-account-row[open] summary::before {
    transform: rotate(90deg);
}
.bound-account-email {
    min-width: 0;
    overflow-wrap: anywhere;
    flex: 1;
}
.bound-account-status,
.account-detail-toggle {
    color: var(--muted);
    font-size: 11px;
    flex-shrink: 0;
}
.account-detail-toggle {
    color: var(--accent);
}
.bound-account-details {
    display: grid;
    gap: 8px;
    padding: 0 0 12px 14px;
}
.account-secret {
    display: flex;
    align-items: baseline;
    gap: 8px;
    min-width: 0;
}
.account-secret > span {
    flex: 0 0 52px;
    color: var(--muted);
}
.account-secret code {
    flex: 1;
    max-height: 65px;
    overflow-y: auto;
}
.bound-account-settings {
    display: flex;
    gap: 8px 12px;
    flex-wrap: wrap;
    align-items: center;
}
.bound-submission-mode {
    display: grid;
    grid-template-columns: 92px minmax(0, 1fr);
    align-items: center;
    gap: 12px;
    font-size: 13px;
}
.bound-account-settings .el-checkbox {
    margin-right: 0;
}
.bound-account-settings .el-button {
    margin-left: auto;
}
@media (max-width: 1050px) {
    .proxy-inline-editor {
        gap: 18px;
        padding: 16px;
    }
    .inline-accounts {
        padding-left: 18px;
    }
    .bound-account-status {
        display: none;
    }
}
@media (max-width: 640px) {
    .proxy-inline-editor {
        grid-template-columns: minmax(0, 1fr);
        padding: 14px 0;
    }
    .inline-accounts {
        padding: 14px 0 0;
        border-left: 0;
        border-top: 1px solid var(--line);
    }
    .inline-binding > .el-button {
        padding-inline: 9px;
    }
    .account-detail-toggle {
        display: none;
    }
}
.proxy-routing-note {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 8px 16px;
    margin-bottom: 18px;
    color: var(--muted);
    font-size: 13px;
}
.proxy-routing-note strong {
    color: var(--text);
    font-weight: 500;
}
.bound-proxy-note {
    margin-bottom: 18px;
}
@media (max-width: 640px) {
    .proxy-pool-surface {
        padding: 16px;
    }
}
.proxy-host-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
}
.proxy-fields-form :deep(.el-select),
.proxy-fields-form :deep(.el-input-number) {
    width: 100%;
}
.pool-batch-result {
    max-height: 112px;
    overflow-y: auto;
    margin-top: 12px;
    padding: 10px 12px;
    border-radius: 8px;
    background: var(--surface-soft);
    font-size: 12px;
    line-height: 1.7;
}
.pool-batch-result > p {
    color: var(--el-color-warning);
    overflow-wrap: anywhere;
}
.pool-import-hint {
    margin-top: 10px;
    margin-bottom: 10px;
}
@media (max-width: 640px) {
    .proxy-host-grid {
        grid-template-columns: 1fr;
    }
}
</style>
