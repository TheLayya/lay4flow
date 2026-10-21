<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
    Plus,
    Search,
    Refresh,
    Download,
    Upload,
    MoreFilled,
    Picture,
    VideoCamera,
    Connection,
    ArrowRight,
    Key,
    Monitor,
    MagicStick,
} from '@element-plus/icons-vue'
import { api, notifyError, parseDate } from '../lib/api'
import Mascot from '../components/Mascot.vue'
import AccountProxyBinding from '../components/AccountProxyBinding.vue'

const accounts = ref([])
const vids = ref([])
const candidates = ref([])
const loading = ref(true)
const loadError = ref('')
const vidsError = ref('')
const quotaRefreshing = ref(false)
const search = ref('')
const route = useRoute()
const platform = ref(route.query.platform === 'vids' ? 'vids' : 'all')
const state = ref('all')
const selected = ref([])
const page = ref(1)
const pageSize = ref(10)
const busy = reactive(new Set())
const detailId = ref(null)
const detailOpen = ref(false)
const editorOpen = ref(false)
const editorTab = ref('identity')
const editing = ref(null)
const saving = ref(false)
const formRef = ref()
const importOpen = ref(false)
const importText = ref('')
const importing = ref(false)
const importJobs = ref([])
const importJobsError = ref('')
const importJobsLoading = ref(false)
const importRetrying = reactive(new Set())
const importSubmitted = ref(null)
let importRequest
let importJobTimer
let importJobsPromise
let importJobsVersion = 0
const activeImportJob = (job) => ['queued', 'running'].includes(job.state)
const activeImportJobs = computed(() => importJobs.value.filter(activeImportJob))
const sameImportSubmitted = computed(
    () => importSubmitted.value?.text === importText.value && importSubmitted.value?.proxy === importProxy.value,
)
const importJobState = (job) =>
    ({
        queued: '等待处理',
        running: '正在导入',
        completed: job.failed ? '部分失败' : '导入完成',
        failed: '导入失败',
        cancelled: '任务已中断',
    })[job.state] || job.state
const importStageLabel = (item) => {
    if (item.state === 'succeeded') return '已导入'
    if (item.state === 'failed') return '失败'
    if (item.state === 'cancelled') return '已中断'
    return (
        {
            queued: '等待处理',
            legacy_import: '验证旧版账号',
            open_profile: '打开浏览器',
            restore_state: '恢复登录态',
            oauth_renewal: '续签 Google 会话',
            cold_check_1: '验证持久化会话（1/2）',
            cold_check_2: '验证持久化会话（2/2）',
            save_account: '保存账号',
            prepare_workspace: '准备工作空间',
            finalize: '完成导入',
            completed: '已导入',
        }[item.stage] || (item.state === 'running' ? '正在验证' : '等待处理')
    )
}
function importJobProgress(job) {
    return `${(job.items || []).filter((item) => ['succeeded', 'failed', 'cancelled'].includes(item.state)).length} / ${job.total} 项`
}
function publishImportJobs() {
    window.dispatchEvent(new CustomEvent('lay4flow:profile-import-jobs', { detail: { jobs: importJobs.value } }))
}
function rememberImportJob(job) {
    importJobsVersion++
    importJobs.value = [job, ...importJobs.value.filter((row) => row.id !== job.id)]
    importJobsError.value = ''
    publishImportJobs()
    scheduleImportJobs()
}
function scheduleImportJobs() {
    clearInterval(importJobTimer)
    importJobTimer = undefined
    if (!disposed && activeImportJobs.value.length) {
        importJobTimer = setInterval(() => {
            if (!document.hidden) refreshImportJobs()
        }, 2000)
    }
}
async function refreshImportJobs() {
    if (disposed || importJobsPromise) return importJobsPromise
    importJobsLoading.value = true
    const version = importJobsVersion
    importJobsPromise = (async () => {
        try {
            const result = ensure(await api.get('/api/profile-transfer/jobs'))
            if (disposed || version !== importJobsVersion) return
            const previous = new Map(importJobs.value.map((job) => [job.id, job]))
            importJobs.value = result.jobs || []
            importJobsError.value = ''
            if (importSubmitted.value?.id && !importJobs.value.some((job) => job.id === importSubmitted.value.id)) {
                importSubmitted.value = null
                importRequest = undefined
                importJobsError.value = '任务记录已过期或服务已重启，请重新提交；已导入账号与现有资料保留。'
            }
            publishImportJobs()
            const finished = importJobs.value.filter(
                (job) => previous.has(job.id) && activeImportJob(previous.get(job.id)) && !activeImportJob(job),
            )
            if (finished.length) {
                load(true, true)
                for (const job of finished)
                    ElMessage({
                        type: job.failed || job.state !== 'completed' ? 'warning' : 'success',
                        message: `后台导入结束：新增 ${job.added || 0} 个，更新 ${job.updated || 0} 个，失败 ${job.failed || 0} 个${job.state === 'cancelled' ? '，任务已中断' : ''}`,
                    })
            }
        } catch (error) {
            if (!disposed) importJobsError.value = `任务状态暂未更新：${error.message}。后台处理会继续，可稍后刷新。`
        } finally {
            importJobsLoading.value = false
            importJobsPromise = undefined
            scheduleImportJobs()
        }
    })()
    return importJobsPromise
}
async function retryImportJob(job) {
    if (importRetrying.has(job.id) || activeImportJob(job)) return
    importRetrying.add(job.id)
    try {
        rememberImportJob(
            ensure(await api.post(`/api/profile-transfer/jobs/${encodeURIComponent(job.id)}/retry`, {})).job,
        )
        ElMessage.success('失败项已重新排队，后台继续处理')
    } catch (error) {
        notifyError(error)
    } finally {
        importRetrying.delete(job.id)
    }
}
const importProxy = ref(null)
const proxyPool = ref([])
const proxyPoolError = ref('')
const availableProxies = computed(() => proxyPool.value.filter((row) => row.enabled && row.last_check_ok !== false))
const proxyOptionLabel = (row) =>
    `#${row.id} ${row.proxy_url}${row.note ? ' · ' + row.note : ''} · ${row.bound_account_count || 0} 个账号 · ${row.last_check_ok === false ? '连接异常' : row.last_check_ok ? '连接正常' : '未检测'}`
const currentProxyLabel = computed(() => {
    const row = proxyPool.value.find((row) => row.id === form.proxy_pool_id)
    return row
        ? proxyOptionLabel(row)
        : form.proxy_pool_id
          ? `出口 #${form.proxy_pool_id}`
          : form.proxy_url || '尚未绑定'
})
const expandedAccountIds = ref([])
const quickDrafts = reactive({})
const quickBaselines = reactive({})
const quickErrors = reactive({})
const quickSaved = reactive({})
const quickSavingId = ref(null)
const quickSavingKind = ref('')
let accountMutationVersion = 0
const quickFields = ['remark', 'image_enabled', 'video_enabled', 'auto_refresh_enabled', 'refresh_interval_minutes']
function quickDraft(row) {
    if (quickDrafts[row.id]) return quickDrafts[row.id]
    const draft = {
        proxy_pool_id: row.proxy_pool_id ?? null,
        submission_proxy_mode: row.submission_proxy_mode || 'fixed',
        remark: row.remark || '',
        image_enabled: row.image_enabled !== false,
        video_enabled: row.video_enabled !== false,
        auto_refresh_enabled: row.auto_refresh_enabled !== false,
        refresh_interval_minutes: row.refresh_interval_minutes || 120,
    }
    quickBaselines[row.id] = { ...draft }
    return (quickDrafts[row.id] = draft)
}
watch(accounts, (rows) => {
    for (const row of rows) {
        const draft = quickDrafts[row.id]
        const baseline = quickBaselines[row.id]
        if (!draft || !baseline) continue
        for (const key of ['proxy_pool_id', 'submission_proxy_mode', ...quickFields]) {
            const value = row[key] ?? (key === 'proxy_pool_id' ? null : defaults()[key])
            if (draft[key] === baseline[key]) draft[key] = value
            baseline[key] = value
        }
    }
})
function accountProxy(row) {
    return proxyPool.value.find((proxy) => proxy.id === row.proxy_pool_id)
}
function accountProxySummary(row) {
    if (row.submission_proxy_mode === 'pool_random')
        return `生成 · 代理池随机${row.proxy_pool_id ? ` · 登录 #${row.proxy_pool_id}` : ''}`
    const url = accountProxy(row)?.proxy_url || row.proxy_url
    const prefix = row.proxy_pool_id ? `代理 #${row.proxy_pool_id}` : url ? '固定代理' : '绑定代理'
    if (!url) return prefix
    try {
        const parsed = new URL(url)
        const port = parsed.port || (parsed.protocol === 'https:' ? 443 : parsed.protocol === 'http:' ? 80 : 1080)
        return `${prefix} · ${parsed.protocol.slice(0, -1).toUpperCase()} ${parsed.hostname}:${port}`
    } catch {
        return `${prefix} · ${url}`
    }
}
function toggleQuickAccount(row) {
    quickDraft(row)
    expandedAccountIds.value = expandedAccountIds.value.includes(row.id)
        ? expandedAccountIds.value.filter((id) => id !== row.id)
        : [...expandedAccountIds.value, row.id]
}
function expandQuickAccount(row, expanded) {
    if (expanded.some((item) => item.id === row.id)) quickDraft(row)
    expandedAccountIds.value = expanded.map((item) => item.id)
}
async function reloadQuickProxies() {
    const version = accountMutationVersion
    try {
        const result = ensure(await api.get('/api/proxy/pool'))
        if (version !== accountMutationVersion) return
        proxyPool.value = result.entries || []
        proxyPoolError.value = ''
    } catch (error) {
        proxyPoolError.value = error.message
    }
}
async function saveQuickAccount(row, kind = 'proxy') {
    if (quickSavingId.value !== null || row.vidsOnly || (saving.value && editing.value?.id === row.id)) return
    const draft = quickDraft(row)
    let body
    let selectedProxy
    if (kind === 'proxy') {
        if (draft.proxy_pool_id === row.proxy_pool_id) return
        const target = proxyPool.value.find((proxy) => proxy.id === draft.proxy_pool_id)
        if (!target?.enabled) {
            quickErrors[row.id] = '请选择已启用的代理'
            return
        }
        selectedProxy = { ...target }
        body = { proxy_pool_id: target.id }
    } else if (kind === 'strategy') {
        if (!['fixed', 'pool_random'].includes(draft.submission_proxy_mode)) {
            quickErrors[row.id] = '请选择生成提交出口'
            return
        }
        if (draft.submission_proxy_mode === (row.submission_proxy_mode || 'fixed')) return
        body = { submission_proxy_mode: draft.submission_proxy_mode }
    } else {
        if (!Number.isInteger(draft.refresh_interval_minutes) || draft.refresh_interval_minutes < 1) {
            quickErrors[row.id] = '刷新间隔至少 1 分钟'
            return
        }
        body = Object.fromEntries(
            quickFields.filter((key) => draft[key] !== (row[key] ?? defaults()[key])).map((key) => [key, draft[key]]),
        )
        if (!Object.keys(body).length) return
    }
    quickSavingId.value = row.id
    quickSavingKind.value = kind
    quickErrors[row.id] = ''
    quickSaved[row.id] = ''
    try {
        if (kind === 'proxy' && row.browser_profile_source === 'cloak' && selectedProxy.proxy_url !== row.proxy_url) {
            try {
                await ElMessageBox.confirm(
                    `将 ${row.email || '#' + row.id} 切换到代理 #${selectedProxy.id}。服务端会保留现有 Cookie 和 seed，验证并重新绑定 Profile。请先关闭该账号浏览器；验证期间该账号暂停接收新任务。验证失败保留原出口。`,
                    '确认切换 Cloak 代理',
                    { confirmButtonText: '验证并切换', cancelButtonText: '保留原绑定', type: 'warning' },
                )
            } catch {
                return
            }
        }
        // Let an older poll finish before this write so it cannot restore the previous binding.
        await loadingPromise
        ensure(await api.put(`/api/tokens/${row.id}`, body))
        accountMutationVersion++
        const previousProxy = accounts.value.find((item) => item.id === row.id)?.proxy_pool_id
        const updates = { ...body, ...(selectedProxy ? { proxy_url: selectedProxy.proxy_url } : {}) }
        accounts.value = accounts.value.map((item) => (item.id === row.id ? { ...item, ...updates } : item))
        if (kind === 'proxy') {
            for (const proxy of proxyPool.value) {
                if (proxy.id === previousProxy)
                    proxy.bound_account_count = Math.max(0, (proxy.bound_account_count || 0) - 1)
                if (proxy.id === selectedProxy.id) proxy.bound_account_count = (proxy.bound_account_count || 0) + 1
            }
        }
        quickSaved[row.id] =
            kind === 'proxy'
                ? `已绑定代理 #${body.proxy_pool_id}`
                : kind === 'strategy'
                  ? `生成出口已设置为${body.submission_proxy_mode === 'pool_random' ? '代理池随机' : '账号固定代理'}`
                  : '账号配置已保存'
        ElMessage.success(quickSaved[row.id])
    } catch (error) {
        quickErrors[row.id] = error.message
    } finally {
        quickSavingId.value = null
        quickSavingKind.value = ''
    }
}
const exporting = ref(false)
const convertOpen = ref(false)
const convertST = ref('')
const convertResult = ref(null)
const converting = ref(false)
const protocolRefresh = ref(false)
const refreshSaving = ref(false)
const onboarding = ref(false)
const loginOpen = ref(false)
const loginStarting = ref(false)
const loginFinishing = ref(false)
const loginCanceling = ref(false)
const loginSession = ref(null)
const loginStatus = ref('')
const nativeDesktop = computed(() => window.lay4flowDesktop?.runtime === 'native')
const nativePresentation = ref(window.lay4flowDesktop?.runtime === 'native')
const presentationReady = ref(nativePresentation.value)
async function detectNativePresentation() {
    if (nativeDesktop.value) {
        nativePresentation.value = true
        presentationReady.value = true
        return
    }
    try {
        const health = await api.get('/health')
        nativePresentation.value = health.viewer_presentation === 'native'
    } catch {
        nativePresentation.value = false
    } finally {
        presentationReady.value = true
    }
}
// The native Windows shell owns the headed Cloak window. Keep its controls in
// this page so users never have to open a second noVNC tab just to focus or
// refresh an account.
const nativeBrowser = ref(null)
const nativeBrowserBusy = computed(() => busy.has('native-browser'))
const loginForm = reactive({ email: '', proxy_pool_id: null })
let pollTimer
let loadingPromise
let quotaPromise
let quotaVersion = 0
let disposed = false
const form = reactive({})
const defaults = () => ({
    st: '',
    remark: '',
    project_id: '',
    project_name: '',
    image_enabled: true,
    video_enabled: true,
    image_concurrency: -1,
    video_concurrency: -1,
    protocol_mode: 'session',
    google_cookies: '',
    login_account: '',
    login_password: '',
    proxy_pool_id: null,
    submission_proxy_mode: 'fixed',
    auto_refresh_enabled: true,
    refresh_interval_minutes: 120,
    browser_profile_source: 'cookies',
})
const rules = {
    st: [
        {
            validator: (_, value, done) =>
                !editing.value && !value.trim() ? done(new Error('请填写 Session Token')) : done(),
            trigger: ['blur', 'change'],
        },
    ],
    refresh_interval_minutes: [{ type: 'number', min: 1, message: '刷新间隔至少 1 分钟', trigger: 'change' }],
}
const rows = computed(() => {
    const bound = new Map(vids.value.map((row) => [row.token_id, row]))
    const merged = accounts.value.map((row) => ({ ...row, vids: bound.get(row.id) }))
    for (const item of vids.value)
        if (!merged.some((row) => row.id === item.token_id))
            merged.push({ id: item.token_id, email: item.email, is_active: item.enabled, vids: item, vidsOnly: true })
    return merged
})
const sortedRows = computed(() =>
    [...filtered.value].sort((a, b) =>
        tableSort.order
            ? (Number(a[tableSort.prop] || 0) - Number(b[tableSort.prop] || 0)) *
              (tableSort.order === 'ascending' ? 1 : -1)
            : 0,
    ),
)
const tableSort = reactive({ prop: 'credits', order: null })
const filtered = computed(() =>
    rows.value.filter((row) => {
        const q = search.value.trim().toLowerCase()
        return (
            (!q || `${row.email} ${row.name || ''} ${row.remark || ''} ${row.id}`.toLowerCase().includes(q)) &&
            (platform.value === 'all' || (platform.value === 'vids' ? !!row.vids : !row.vidsOnly)) &&
            (state.value === 'all' ||
                (state.value === 'active'
                    ? row.is_active
                    : state.value === 'inactive'
                      ? !row.is_active
                      : row.at_expired ||
                        ['failed', 'login_required', 'config_error', 'retrying'].includes(row.refresh_status) ||
                        row.refresh_error ||
                        (row.vids?.enabled && (!row.vids.ready || quotaInfo(row).error || quotaInfo(row).stale))))
        )
    }),
)
const paginated = computed(() => sortedRows.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const detail = computed(() => rows.value.find((row) => row.id === detailId.value))
const metrics = computed(() => [
    { label: '账号成员', value: rows.value.length, note: 'Flow · Vids 统一管理', icon: Connection, color: 'pink' },
    {
        label: '在线待命',
        value: rows.value.filter((row) => row.is_active).length,
        note: `${rows.value.filter((row) => !row.is_active).length} 个暂停中`,
        icon: Monitor,
        color: 'green',
    },
    {
        label: 'Flow 总余额',
        value: accounts.value.reduce((sum, row) => sum + Number(row.credits || 0), 0).toLocaleString(),
        note: '可用于图片与视频创作',
        icon: MagicStick,
        color: 'blue',
    },
    {
        label: 'Vids 剩余额度',
        quotas: ['image', 'video'].map((kind) => ({ kind, ...quotaSummary(kind) })),
        note: `${vids.value.filter((row) => row.enabled).length} 个启用账号 · 图片 / 视频分开计量`,
        icon: VideoCamera,
        color: 'violet',
    },
])
const date = (value) => (value ? parseDate(value).toLocaleString('zh-CN', { hour12: false }) : '暂无')
const quotaNumber = (value) =>
    ['number', 'string'].includes(typeof value) &&
    String(value).trim() !== '' &&
    Number.isFinite(Number(value)) &&
    Number(value) >= 0
        ? Number(value)
        : null
const quotaValue = (value) => quotaNumber(value)?.toLocaleString('zh-CN') ?? '—'
const quotaError = (value) =>
    ({
        session_authentication_failed: '登录已过期，请重新连接账号',
        transport_failed: '额度连接暂时中断',
        quota_unavailable: '额度暂未读取成功',
        account_mismatch: '账号身份待核对',
        session_identity_mismatch: '会话身份待核对',
    })[value] || (String(value || '').startsWith('upstream_http_') ? `额度服务返回 ${value.slice(14)}` : value)
function quotaSummary(kind) {
    const active = vids.value.filter((row) => row.enabled && row[`${kind}_enabled`] !== false)
    const known = active.map((row) => row.last_quota?.[kind]).filter((quota) => quotaNumber(quota?.available) !== null)
    return {
        value: known.length ? quotaValue(known.reduce((sum, quota) => sum + Number(quota.available), 0)) : '—',
        partial: known.length < active.length,
        stale: !!vidsError.value || known.some((quota) => quota.stale || quota.error),
    }
}
function quotaInfo(row) {
    const quotas = ['image', 'video'].map((kind) => row.vids?.last_quota?.[kind])
    const known = quotas.filter((quota) => quotaNumber(quota?.available) !== null)
    const error =
        [...new Set(quotas.map((quota) => quotaError(quota?.error)).filter(Boolean))].join('；') || vidsError.value
    const stale = known.some((quota) => quota.stale) || (!!known.length && (!row.vids?.ready || !row.vids?.enabled))
    const times = quotas.map((quota) => quota?.queried_at).filter(Boolean)
    const updated = times.length
        ? times.reduce((oldest, value) => (parseDate(value) < parseDate(oldest) ? value : oldest))
        : null
    return {
        error,
        stale,
        updated,
        label: error
            ? known.length
                ? '更新失败 · 保留上次额度'
                : '额度更新失败'
            : busy.has(`${row.id}:quota`)
              ? '同步中'
              : !row.vids?.enabled
                ? '已暂停自动更新'
                : !row.vids?.ready
                  ? vidsState(row.vids)
                  : quotaRefreshing.value
                    ? '同步中'
                    : stale
                      ? '历史额度 · 等待更新'
                      : !known.length
                        ? '尚未查询额度'
                        : known.length < 2
                          ? '部分额度待更新'
                          : '已自动同步',
    }
}
const sessionLabel = (row) =>
    ({
        refreshing: '正在刷新',
        login_required: '需要重新登录',
        config_error: '配置需要调整',
        retrying: '等待重试',
        failed: '需要关注',
    })[row.refresh_status] ||
    (row.refresh_error || row.browser_profile_error
        ? '需要关注'
        : row.at_expired
          ? '已过期'
          : row.at_expiring_within_1h
            ? '即将刷新'
            : '会话正常')
const tier = (value) =>
    ({ PAYGATE_TIER_NOT_PAID: 'Free', PAYGATE_TIER_ONE: 'Pro', PAYGATE_TIER_TWO: 'Ultra' })[value] || value || 'Free'
const vidsState = (row) =>
    !row
        ? '未接入'
        : row.enabled === false
          ? '已暂停'
          : row.identity_verified === false
            ? '身份待核对'
            : {
                  ready: '会话就绪',
                  authentication_expired: '登录已过期',
                  identity_mismatch: '身份待核对',
                  unavailable: '待重新连接',
              }[row.session_state] || (row.ready ? '会话就绪' : '待重新连接')
const profileLabel = (value) =>
    ({
        ready: '已就绪',
        login_required: '需要登录',
        uninitialized: '未初始化',
        failed: '需要关注',
        error: '需要关注',
        refreshing: '正在刷新',
    })[value] ||
    value ||
    '未初始化'
const flowAttention = (row) =>
    !!(
        row.at_expired ||
        row.refresh_error ||
        row.browser_profile_error ||
        ['failed', 'login_required', 'config_error', 'retrying'].includes(row.refresh_status)
    )
const vidsAttention = (row) =>
    !!(row?.enabled !== false && (!row?.ready || row?.identity_verified === false || row?.runtime_error))
watch([search, platform, state, pageSize], () => {
    page.value = 1
})
watch(
    () => route.query.platform,
    (value) => {
        platform.value = value === 'vids' ? 'vids' : 'all'
    },
)
watch(filtered, (value) => {
    page.value = Math.min(page.value, Math.max(1, Math.ceil(value.length / pageSize.value)))
})
function ensure(result) {
    if (result?.success === false) {
        const detail = result.detail
        const message =
            typeof detail === 'string'
                ? detail
                : detail?.message || detail?.reason || result.message || result.error?.message || '操作未完成'
        throw new Error(message)
    }
    return result
}
async function load(silent = false, fresh = false) {
    if (fresh) quotaVersion++
    if (loadingPromise) {
        const pending = loadingPromise
        await pending
        return fresh ? load(silent) : undefined
    }
    if (!silent) loading.value = true
    const version = quotaVersion
    const accountVersion = accountMutationVersion
    loadingPromise = (async () => {
        const result = await Promise.allSettled([
            api.get('/api/tokens'),
            api.get('/api/admin/vids/accounts/detail'),
            api.get('/api/admin/vids/onboard/candidates'),
            api.get('/api/token-refresh/config'),
            api.get('/api/proxy/pool'),
        ])
        if (result[0].status === 'fulfilled' && accountVersion === accountMutationVersion) {
            accounts.value = result[0].value
            loadError.value = ''
        } else if (result[0].status === 'rejected') loadError.value = result[0].reason.message
        if (result[1].status === 'fulfilled') {
            if (version === quotaVersion) {
                vids.value = result[1].value.accounts || []
                vidsError.value = ''
            }
        } else if (version === quotaVersion) vidsError.value = result[1].reason.message
        if (result[2].status === 'fulfilled') candidates.value = result[2].value.accounts || []
        if (result[3].status === 'fulfilled') protocolRefresh.value = result[3].value.config.protocol_refresh_enabled
        if (result[4].status === 'fulfilled' && accountVersion === accountMutationVersion) {
            proxyPool.value = result[4].value.entries || []
            proxyPoolError.value = ''
        } else if (result[4].status === 'rejected') proxyPoolError.value = result[4].reason.message
    })().finally(() => {
        loading.value = false
        loadingPromise = null
        refreshVidsQuota()
    })
    return loadingPromise
}
async function refreshVidsQuota() {
    if (disposed || quotaPromise || document.hidden || editorOpen.value) return quotaPromise
    quotaRefreshing.value = true
    const version = quotaVersion
    quotaPromise = api
        .get('/api/admin/vids/accounts/detail?refresh=true')
        .then((result) => {
            if (version !== quotaVersion) return
            vids.value = result.accounts || []
            vidsError.value = ''
            quotaVersion++
        })
        .catch((error) => {
            if (version === quotaVersion) vidsError.value = error.message
        })
        .finally(() => {
            quotaRefreshing.value = false
            quotaPromise = null
        })
    return quotaPromise
}
function refreshVisible() {
    if (!document.hidden) refreshImportJobs()
    if (!document.hidden && !editorOpen.value && quickSavingId.value === null) return load(true)
}
function openEditor(row) {
    editing.value = row || null
    Object.assign(
        form,
        defaults(),
        row
            ? {
                  ...row,
                  project_id: row.current_project_id || '',
                  project_name: row.current_project_name || '',
                  login_password: '',
              }
            : {},
    )
    editorTab.value = 'identity'
    editorOpen.value = true
}
function openDetail(row) {
    detailId.value = row.id
    detailOpen.value = true
}
function editDetail() {
    const account = detail.value
    if (!account) return
    detailOpen.value = false
    openEditor(account)
}
function clearFormValidation() {
    formRef.value?.clearValidate()
}
function tableCommand(command) {
    if (command === 'import') importOpen.value = true
    else if (command === 'export') exportAccounts()
    else if (command === 'manual') openEditor()
    else if (command === 'convert') convertOpen.value = true
    else if (command === 'vids') onboardAll()
}
function accountCommand(row, command) {
    if (command === 'edit') openEditor(row)
    else if (command === 'browser') openBrowser(row)
    else action(row, command)
}
function selectMobile(row, checked) {
    selected.value = selected.value.filter((item) => item.id !== row.id)
    if (checked) selected.value.push(row)
}
async function saveAccount() {
    if (saving.value || quickSavingId.value === editing.value?.id) return
    if (!(await formRef.value.validate().catch(() => false))) return
    saving.value = true
    try {
        const body = Object.fromEntries(Object.keys(defaults()).map((key) => [key, form[key]]))
        if (editing.value) {
            if (body.st === editing.value.st) delete body.st
            if (!body.login_password) delete body.login_password
            if (body.proxy_pool_id === (editing.value.proxy_pool_id ?? null)) delete body.proxy_pool_id
            ensure(await api.put(`/api/tokens/${editing.value.id}`, body))
        } else ensure(await api.post('/api/tokens', body))
        ElMessage.success(editing.value ? '账号配置已保存' : '新成员已加入')
        editorOpen.value = false
        await load(true, true)
    } catch (error) {
        notifyError(error)
    } finally {
        saving.value = false
    }
}
async function action(row, type) {
    const key = `${row.id}:${type}`
    if (busy.has(key)) return
    busy.add(key)
    try {
        if (type === 'delete') {
            await ElMessageBox.confirm(
                `删除 ${row.email || '账号 #' + row.id} 后，其会话与配置将一并移除。`,
                '删除账号',
                { confirmButtonText: '确认删除', cancelButtonText: '保留账号', type: 'warning' },
            )
            ensure(await api.delete(`/api/tokens/${row.id}`))
        } else if (type === 'quota') {
            const result = await api.post(`/api/admin/vids/accounts/${row.id}/quota-refresh`, {})
            if (result.account) {
                quotaVersion++
                vids.value = vids.value.map((item) => (item.token_id === row.id ? result.account : item))
                const error = ['image', 'video']
                    .map((kind) => quotaError(result.account.last_quota?.[kind]?.error))
                    .filter(Boolean)
                    .join('；')
                if (error) throw new Error(error)
            }
        } else if (type === 'onboard' || type === 'reconnect')
            await api.post(
                `/api/admin/vids/onboard/${row.id}${type === 'reconnect' ? '/reconnect' : ''}`,
                type === 'onboard' ? { create_blank: true } : {},
            )
        else ensure(await api.post(`/api/tokens/${row.id}/${type}`, {}))
        ElMessage.success(
            {
                delete: '账号已删除',
                enable: '账号已启用',
                disable: '账号已暂停',
                'refresh-at': '会话已刷新',
                'refresh-credits': '余额已更新',
                quota: 'Vids 额度已更新',
                onboard: 'Vids 接入已完成',
                reconnect: 'Vids 会话已重新连接',
            }[type],
        )
        await load(true, true)
    } catch (error) {
        if (error !== 'cancel' && error !== 'close') notifyError(error)
    } finally {
        busy.delete(key)
    }
}
async function bulk(type) {
    if (!selected.value.length || busy.has('bulk')) return
    const targets = selected.value.filter((row) => !row.vidsOnly)
    if (type === 'delete') {
        try {
            await ElMessageBox.confirm(`将删除选中的 ${targets.length} 个账号及其配置。`, '批量删除', {
                confirmButtonText: '确认删除',
                cancelButtonText: '取消',
                type: 'warning',
            })
        } catch {
            return
        }
    }
    busy.add('bulk')
    let ok = 0
    const errors = []
    for (const row of targets) {
        try {
            ensure(
                await (type === 'delete'
                    ? api.delete(`/api/tokens/${row.id}`)
                    : api.post(`/api/tokens/${row.id}/${type}`, {})),
            )
            ok++
        } catch (error) {
            errors.push(`${row.email}: ${error.message}`)
        }
    }
    busy.delete('bulk')
    await load(true, true)
    ElMessage({
        type: errors.length ? 'warning' : 'success',
        message: `已完成 ${ok} / ${targets.length} 个账号${errors.length ? '，失败原因可在账号详情中查看' : ''}`,
    })
    if (errors.length)
        ElMessageBox.alert(errors.join('\n'), '部分账号操作未完成', {
            confirmButtonText: '知道了',
            customClass: 'multiline-message',
        })
}
async function exportAccounts() {
    if (exporting.value) return
    const source = selected.value.length ? selected.value.filter((row) => !row.vidsOnly) : accounts.value
    if (!source.length) return
    exporting.value = true
    const body = []
    const errors = []
    for (const row of source) {
        try {
            if (row.browser_profile_source === 'cloak') {
                const result = ensure(await api.get(`/api/tokens/${row.id}/profile-export`))
                if (result.bundle?.format !== 'lay4flow-cloak-session') throw new Error('Profile 导出格式校验失败')
                body.push(result.bundle)
                continue
            }
            body.push({
                ...Object.fromEntries(
                    Object.keys(defaults())
                        .filter((key) => !['st', 'project_id', 'project_name', 'remark', 'proxy_pool_id'].includes(key))
                        .map((key) => [key, key === 'login_password' ? '' : (row[key] ?? defaults()[key])]),
                ),
                email: row.email,
                session_token: row.st,
                access_token: row.at,
                is_active: row.is_active,
                remark: row.remark || '',
                project_id: row.current_project_id || '',
                project_name: row.current_project_name || '',
            })
        } catch (error) {
            errors.push(`${row.email || row.id}：${error.message}`)
        }
    }
    exporting.value = false
    if (!body.length) return notifyError(new Error(errors.join('；')))
    const envelope = { format: 'lay4flow-profile-bundle', version: 1, accounts: body }
    const url = URL.createObjectURL(new Blob([JSON.stringify(envelope, null, 2)], { type: 'application/json' }))
    const link = document.createElement('a')
    link.href = url
    link.download = `lay4flow-accounts-${new Date().toISOString().slice(0, 10)}.json`
    link.click()
    URL.revokeObjectURL(url)
    ElMessage.success(`已导出 ${body.length} 个账号`)
    if (errors.length)
        ElMessageBox.alert(errors.join('\n'), '部分账号导出未完成', {
            confirmButtonText: '知道了',
            customClass: 'multiline-message',
        })
}
async function readImport(file) {
    if (importing.value) return
    try {
        const text = await file.raw.text()
        if (importing.value) return
        importText.value = text
    } catch (error) {
        notifyError(error)
    }
}
function closeImport(done) {
    done()
}
async function importAccounts() {
    if (importing.value || sameImportSubmitted.value) return
    importing.value = true
    const source = importText.value
    const selectedProxy = importProxy.value
    try {
        const input = JSON.parse(source)
        const entries =
            input?.format === 'lay4flow-profile-bundle' && input.version === 1
                ? input.accounts
                : Array.isArray(input)
                  ? input
                  : input?.format === 'lay4flow-cloak-session'
                    ? [input]
                    : null
        if (!Array.isArray(entries) || !entries.length || entries.some((entry) => !entry || typeof entry !== 'object'))
            throw new Error('请选择有效的 Profile JSON，或旧版账号 JSON 数组')
        if (!importRequest || importRequest.text !== source || importRequest.proxy !== selectedProxy) {
            importRequest = {
                text: source,
                proxy: selectedProxy,
                id: globalThis.crypto?.randomUUID?.() || `import-${Date.now()}-${Math.random().toString(36).slice(2)}`,
            }
        }
        let submittedJob
        try {
            const result = ensure(
                await api.post('/api/profile-transfer/jobs', {
                    bundles: entries,
                    ...(selectedProxy ? { proxy_pool_id: selectedProxy } : {}),
                    request_id: importRequest.id,
                }),
            )
            submittedJob = result.job
            rememberImportJob(result.job)
        } catch (error) {
            // A disconnected response can still mean that the server accepted the task.
            await refreshImportJobs()
            const accepted = importJobs.value.find((job) => job.request_id === importRequest.id)
            if (!accepted) throw error
            submittedJob = accepted
        }
        importSubmitted.value = { text: source, proxy: selectedProxy, id: submittedJob.id }
        importOpen.value = false
        ElMessage.success('已提交后台导入，可关闭窗口或继续操作其他页面')
    } catch (error) {
        notifyError(error)
    } finally {
        importing.value = false
    }
}
async function convert() {
    if (!convertST.value.trim()) return ElMessage.warning('请填写 Session Token')
    converting.value = true
    try {
        convertResult.value = ensure(await api.post('/api/tokens/st2at', { st: convertST.value.trim() }))
    } catch (error) {
        notifyError(error)
    } finally {
        converting.value = false
    }
}
async function copy(value) {
    try {
        await navigator.clipboard.writeText(value)
        ElMessage.success('已复制')
    } catch (error) {
        notifyError(error)
    }
}
async function toggleRefresh(value) {
    refreshSaving.value = true
    try {
        ensure(await api.post('/api/token-refresh/enabled', { enabled: value }))
        ElMessage.success('自动刷新设置已保存')
    } catch (error) {
        protocolRefresh.value = !value
        notifyError(error)
    } finally {
        refreshSaving.value = false
    }
}
async function onboardAll() {
    if (onboarding.value) return
    const pending = candidates.value.filter((row) => !row.bound)
    if (!pending.length) return ElMessage.info('当前账号均已接入，或暂时没有可接入会话')
    onboarding.value = true
    let ok = 0
    const errors = []
    for (const row of pending) {
        try {
            await api.post(`/api/admin/vids/onboard/${row.token_id}`, { create_blank: true })
            ok++
        } catch (error) {
            errors.push(`${row.email || row.token_id}: ${error.message}`)
        }
    }
    onboarding.value = false
    await load(true, true)
    ElMessage({ type: errors.length ? 'warning' : 'success', message: `Vids 接入完成 ${ok} / ${pending.length}` })
    if (errors.length) ElMessageBox.alert(errors.join('\n'), 'Vids 接入结果', { confirmButtonText: '知道了' })
}
async function openBrowser(row) {
    const native = nativePresentation.value
    const viewer = native ? null : window.open('about:blank', '_blank')
    if (!native && !viewer) return ElMessage.warning('请允许浏览器打开此网站的新窗口')
    if (viewer) {
        viewer.opener = null
        viewer.document.title = '正在连接账号浏览器'
        viewer.document.body.textContent = '正在连接账号浏览器…'
    }
    busy.add(`${row.id}:browser`)
    let receipt
    try {
        receipt = ensure(await api.post(`/api/tokens/${row.id}/browser`, {}))
        if (native && receipt.presentation !== 'native') throw new Error('本地账号浏览器服务未返回原生窗口状态')
        if (receipt.presentation === 'native') {
            if (!receipt.viewer_id) throw new Error('本地账号浏览器凭据校验失败')
            viewer?.close()
            nativeBrowser.value = {
                tokenId: row.id,
                viewerId: receipt.viewer_id || '',
                email: row.email || `账号 #${row.id}`,
                status: 'ready',
            }
            ElMessage.success('本地 Cloak 窗口已打开，可直接在桌面窗口操作')
            return
        }
        const remoteViewer = viewer || openLoginWindow()
        const url = new URL(receipt.viewer_url, location.origin)
        if (
            url.origin !== location.origin ||
            url.pathname !== `/api/profile-browser/${row.id}` ||
            url.search ||
            url.hash
        )
            throw new Error('账号浏览器地址校验失败')
        if (receipt.viewer_id) url.searchParams.set('viewer_id', receipt.viewer_id)
        if (remoteViewer.closed)
            await api.delete(`/api/tokens/${row.id}/browser?viewer_id=${encodeURIComponent(receipt.viewer_id || '')}`)
        else remoteViewer.location.href = url.href
    } catch (error) {
        viewer?.close()
        if (receipt)
            await api
                .delete(`/api/tokens/${row.id}/browser?viewer_id=${encodeURIComponent(receipt.viewer_id || '')}`)
                .catch(() => {})
        notifyError(error)
    } finally {
        busy.delete(`${row.id}:browser`)
    }
}
function nativeViewerQuery(viewer = nativeBrowser.value) {
    if (!viewer?.tokenId) return ''
    return viewer.viewerId ? `?viewer_id=${encodeURIComponent(viewer.viewerId)}` : ''
}
async function focusNativeBrowser() {
    const viewer = nativeBrowser.value
    if (!viewer || nativeBrowserBusy.value) return
    busy.add('native-browser')
    try {
        const result = ensure(
            await api.post(`/api/tokens/${viewer.tokenId}/browser/focus${nativeViewerQuery(viewer)}`, {}),
        )
        nativeBrowser.value = { ...viewer, status: result.status || 'ready' }
        ElMessage.success('已显示本地 Cloak 窗口')
    } catch (error) {
        notifyError(error)
    } finally {
        busy.delete('native-browser')
    }
}
async function refreshNativeBrowser() {
    const viewer = nativeBrowser.value
    if (!viewer || nativeBrowserBusy.value) return
    busy.add('native-browser')
    try {
        const result = ensure(
            await api.post(`/api/tokens/${viewer.tokenId}/browser/refresh${nativeViewerQuery(viewer)}`, {}),
        )
        nativeBrowser.value = { ...viewer, status: result.status || 'ready' }
        ElMessage.success(result.message || '凭据已刷新，本地窗口保持打开')
    } catch (error) {
        notifyError(error)
    } finally {
        busy.delete('native-browser')
    }
}
async function closeNativeBrowser(refresh = false) {
    const viewer = nativeBrowser.value
    if (!viewer || nativeBrowserBusy.value) return
    busy.add('native-browser')
    try {
        ensure(await api.delete(`/api/tokens/${viewer.tokenId}/browser${nativeViewerQuery(viewer)}`))
        nativeBrowser.value = null
        if (refresh) {
            const result = ensure(await api.post(`/api/tokens/${viewer.tokenId}/refresh-at`, {}))
            ElMessage.success(result.message || '窗口已关闭，凭据刷新已提交')
        } else ElMessage.success('本地 Cloak 窗口已关闭')
    } catch (error) {
        notifyError(error)
    } finally {
        busy.delete('native-browser')
    }
}
const loginStorage = 'lay4flow.profileLogin'
function clearLogin() {
    loginSession.value = null
    sessionStorage.removeItem(loginStorage)
}
function loginViewerUrl(receipt) {
    const url = new URL(receipt.viewer_url, location.origin)
    if (
        url.origin !== location.origin ||
        url.pathname !== `/api/profile-browser/${receipt.token_id}` ||
        url.search ||
        url.hash ||
        !receipt.viewer_id
    )
        throw new Error('账号浏览器地址校验失败')
    url.searchParams.set('viewer_id', receipt.viewer_id)
    return url.href
}
function openLoginWindow() {
    const viewer = window.open('about:blank', '_blank')
    if (!viewer) throw new Error('请允许浏览器打开此网站的新窗口')
    viewer.opener = null
    viewer.document.title = '正在连接登录浏览器'
    viewer.document.body.textContent = '正在连接登录浏览器…'
    return viewer
}
async function startLogin() {
    if (loginStarting.value || loginSession.value) return
    if (!/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(loginForm.email.trim()))
        return ElMessage.warning('请填写要登录的 Google 账号邮箱')
    const native = nativePresentation.value
    let viewer
    let receipt
    try {
        if (!native) viewer = openLoginWindow()
        loginStarting.value = true
        receipt = ensure(
            await api.post('/api/profile-login', {
                email: loginForm.email.trim(),
                ...(loginForm.proxy_pool_id ? { proxy_pool_id: loginForm.proxy_pool_id } : {}),
            }),
        )
        const presentation = receipt.presentation || (native ? 'native' : 'web')
        if (native && presentation !== 'native') throw new Error('本地账号浏览器服务未返回原生窗口状态')
        if (presentation === 'native' && (!receipt.viewer_id || !receipt.session_id || !receipt.token_id))
            throw new Error('本地账号浏览器凭据校验失败')
        const url = presentation === 'native' ? null : loginViewerUrl(receipt)
        loginSession.value = {
            session_id: receipt.session_id,
            token_id: receipt.token_id,
            viewer_id: receipt.viewer_id,
            viewer_url: receipt.viewer_url,
            presentation,
            expires_at: receipt.expires_at,
            email: loginForm.email.trim(),
        }
        sessionStorage.setItem(loginStorage, JSON.stringify(loginSession.value))
        loginStatus.value = presentation === 'native'
            ? '本地 Cloak 窗口已打开，请在桌面窗口完成 Google 登录，再点击“完成登录并添加”。'
            : '请在 Cloak 窗口完成 Google 登录，再点击“完成登录并添加”。'
        if (presentation === 'native') viewer?.close()
        else {
            if (!viewer) viewer = openLoginWindow()
            if (viewer.closed) loginStatus.value = '登录浏览器已就绪，点击“继续登录”重新打开窗口。'
            else viewer.location.href = url
        }
    } catch (error) {
        viewer?.close()
        if (receipt?.session_id && !loginSession.value)
            await api
                .delete(
                    `/api/profile-login/${encodeURIComponent(receipt.session_id)}?viewer_id=${encodeURIComponent(receipt.viewer_id || '')}`,
                )
                .catch(() => {})
        notifyError(error)
    } finally {
        loginStarting.value = false
    }
}
function resumeLogin() {
    if (loginSession.value?.presentation === 'native' || nativePresentation.value) {
        const session = loginSession.value
        if (!session?.token_id) return
        api.post(`/api/tokens/${session.token_id}/browser/focus${session.viewer_id ? `?viewer_id=${encodeURIComponent(session.viewer_id)}` : ''}`, {})
            .then(() => (loginStatus.value = '已显示本地 Cloak 登录窗口，请继续完成 Google 登录。'))
            .catch(notifyError)
        loginOpen.value = true
        return
    }
    let viewer
    try {
        const url = loginViewerUrl(loginSession.value)
        viewer = openLoginWindow()
        viewer.location.href = url
        loginOpen.value = true
    } catch (error) {
        viewer?.close()
        notifyError(error)
    }
}
async function completeLogin() {
    if (!loginSession.value || loginFinishing.value || loginCanceling.value) return
    loginFinishing.value = true
    try {
        const result = ensure(
            await api.post(`/api/profile-login/${encodeURIComponent(loginSession.value.session_id)}/complete`, {
                viewer_id: loginSession.value.viewer_id,
            }),
        )
        clearLogin()
        loginOpen.value = false
        loginStatus.value = `账号已验证并加入：${result.email || loginForm.email}`
        ElMessage.success(
            result.is_active === false ? 'Cloak 登录完成，账号已保存为暂停状态' : 'Cloak 登录完成，账号已加入调度',
        )
        await load(true, true)
    } catch (error) {
        loginStatus.value = error.message
        notifyError(error)
    } finally {
        loginFinishing.value = false
    }
}
async function cancelLogin() {
    if (!loginSession.value || loginFinishing.value || loginCanceling.value) return
    loginCanceling.value = true
    try {
        ensure(
            await api.delete(
                `/api/profile-login/${encodeURIComponent(loginSession.value.session_id)}?viewer_id=${encodeURIComponent(loginSession.value.viewer_id)}`,
            ),
        )
        clearLogin()
        loginStatus.value = ''
        loginOpen.value = false
    } catch (error) {
        notifyError(error)
    } finally {
        loginCanceling.value = false
    }
}
onMounted(() => {
    detectNativePresentation()
    load()
    refreshImportJobs()
    document.addEventListener('visibilitychange', refreshVisible)
    try {
        const stored = JSON.parse(sessionStorage.getItem(loginStorage) || 'null')
        if (
            stored?.session_id &&
            stored.viewer_id &&
            stored.token_id &&
            (!stored.expires_at ||
                new Date(typeof stored.expires_at === 'number' ? stored.expires_at * 1000 : stored.expires_at) >
                    new Date())
        ) {
            if (stored.presentation !== 'native' && !nativePresentation.value) loginViewerUrl(stored)
            loginSession.value = stored
            loginForm.email = stored.email || ''
            loginStatus.value = stored.presentation === 'native' || nativePresentation.value
                ? '有一份尚未完成的本地 Cloak 登录，可继续显示窗口。'
                : '有一份尚未完成的 Cloak 登录，可继续登录或取消。'
        } else clearLogin()
    } catch {
        clearLogin()
    }
    pollTimer = setInterval(() => {
        refreshVisible()
    }, 15000)
})
onUnmounted(() => {
    disposed = true
    quotaVersion++
    clearInterval(pollTimer)
    clearInterval(importJobTimer)
    document.removeEventListener('visibilitychange', refreshVisible)
})
</script>

<template>
    <div class="accounts-view">
        <header class="page-heading">
            <div>
                <h1>账号小队</h1>
                <p>照顾好每一份灵感，从账号、会话到创作额度。</p>
            </div>
            <div class="page-actions">
                <el-button :icon="Refresh" :loading="loading" @click="load()">刷新</el-button>
                <el-button type="primary" :icon="Plus" @click="loginOpen = true">添加账号</el-button>
            </div>
        </header>
        <section
            v-if="importJobs.length || importJobsError"
            id="profile-import-jobs"
            class="surface profile-import-jobs"
            aria-label="后台导入任务"
        >
            <div class="profile-import-heading">
                <div>
                    <strong>后台导入</strong>
                    <span>
                        {{
                            activeImportJobs.length
                                ? `${activeImportJobs.length} 个任务处理中 · 可以继续操作`
                                : '最近任务 · 刷新页面后仍可查看'
                        }}
                    </span>
                </div>
                <el-button size="small" :loading="importJobsLoading" @click="refreshImportJobs">刷新进度</el-button>
            </div>
            <p v-if="importJobsError" class="profile-import-jobs-error" role="status">{{ importJobsError }}</p>
            <p class="field-note profile-import-retention">
                任务记录保留 24 小时；服务重启后可重新提交，已导入账号保留。
            </p>
            <details
                v-for="job in importJobs"
                :key="job.id"
                class="profile-import-job"
                :open="activeImportJob(job) || job.failed > 0 || job.state === 'cancelled'"
            >
                <summary>
                    <strong :class="{ failed: job.failed || job.state === 'cancelled' }">
                        {{ importJobState(job) }}
                    </strong>
                    <span>
                        {{ importJobProgress(job) }} · 新增 {{ job.added || 0 }} · 更新 {{ job.updated || 0 }} · 失败
                        {{ job.failed || 0 }}
                    </span>
                    <span class="profile-import-job-time">{{ Math.floor(job.elapsed_seconds || 0) }} 秒</span>
                    <span class="profile-import-expand">查看详情</span>
                </summary>
                <div class="profile-import-job-detail">
                    <p v-if="activeImportJob(job)" class="field-note">
                        服务器正在处理，可关闭窗口或切换页面；网络较慢时会更久。
                    </p>
                    <ul>
                        <li
                            v-for="(item, index) in job.items"
                            :key="index"
                            :class="{ failed: ['failed', 'cancelled'].includes(item.state) }"
                        >
                            <span>{{ item.email }}</span>
                            <strong>{{ importStageLabel(item) }}</strong>
                            <small v-if="item.message">{{ item.message }}</small>
                        </li>
                    </ul>
                    <el-button
                        v-if="job.can_retry"
                        size="small"
                        type="primary"
                        :loading="importRetrying.has(job.id)"
                        @click="retryImportJob(job)"
                    >
                        重试失败项
                    </el-button>
                </div>
            </details>
        </section>
        <div class="crew-metrics">
            <article v-for="metric in metrics" :key="metric.label" class="crew-metric" :class="metric.color">
                <div class="metric-top">
                    <span>{{ metric.label }}</span>
                    <el-icon><component :is="metric.icon" /></el-icon>
                </div>
                <div v-if="metric.quotas" class="metric-quotas">
                    <div v-for="quota in metric.quotas" :key="quota.kind">
                        <span>{{ quota.kind === 'image' ? '图片' : '视频' }}</span>
                        <strong>
                            {{ quota.value }}
                            <small v-if="quota.partial || quota.stale">{{ quota.partial ? '部分' : '历史' }}</small>
                        </strong>
                    </div>
                </div>
                <strong v-else>{{ metric.value }}</strong>
                <p>{{ metric.note }}</p>
            </article>
        </div>
        <el-alert v-if="loadError" :title="loadError" type="error" show-icon :closable="false" class="account-alert">
            <template #default><el-button size="small" @click="load()">重新读取</el-button></template>
        </el-alert>
        <el-alert
            v-if="vidsError"
            title="Vids 状态读取失败，Flow 账号管理仍可继续"
            :description="vidsError"
            type="warning"
            show-icon
            class="account-alert"
        />
        <section class="surface account-surface">
            <div class="section-heading">
                <div>
                    <h2>
                        成员档案
                        <span class="count-label">{{ rows.length }}</span>
                    </h2>
                    <p>Flow 积分与 Vids 剩余额度直接查看 · 每分钟自动同步</p>
                </div>
                <div class="refresh-toggle">
                    <span>自动刷新 ST</span>
                    <el-tooltip content="使用已保存的 Profile 或 Google Cookies 刷新会话；AT 自动刷新始终开启">
                        <el-switch v-model="protocolRefresh" :loading="refreshSaving" @change="toggleRefresh" />
                    </el-tooltip>
                </div>
            </div>
            <div class="account-toolbar">
                <div class="filters">
                    <el-input
                        v-model="search"
                        :prefix-icon="Search"
                        placeholder="搜索邮箱、备注或账号 ID"
                        clearable
                        class="account-search"
                    />
                    <el-select v-model="platform" aria-label="筛选平台">
                        <el-option label="所有平台" value="all" />
                        <el-option label="Flow" value="flow" />
                        <el-option label="Vids" value="vids" />
                    </el-select>
                    <el-select v-model="state" aria-label="筛选状态">
                        <el-option label="所有状态" value="all" />
                        <el-option label="已启用" value="active" />
                        <el-option label="已暂停" value="inactive" />
                        <el-option label="需要关注" value="attention" />
                    </el-select>
                </div>
                <div class="table-tools">
                    <el-tooltip :content="nativePresentation ? '直接打开本机 Cloak 窗口登录 Google，保存持久化 Profile' : '直接在服务器 Cloak 浏览器登录 Google，保存持久化 Profile'">
                        <el-button :icon="Connection" @click="loginOpen = true">
                            {{ loginSession ? '继续登录' : 'Cloak 登录' }}
                        </el-button>
                    </el-tooltip>
                    <el-dropdown @command="tableCommand">
                        <el-button :icon="MoreFilled" aria-label="更多账号操作" />
                        <template #dropdown>
                            <el-dropdown-menu>
                                <el-dropdown-item command="import" :icon="Upload">导入 JSON</el-dropdown-item>
                                <el-dropdown-item
                                    command="export"
                                    :icon="Download"
                                    :disabled="!accounts.length || exporting"
                                >
                                    {{ selected.length ? '导出所选账号' : '导出全部账号' }}
                                </el-dropdown-item>
                                <el-dropdown-item command="manual" :icon="Key">手动添加 ST / Cookies</el-dropdown-item>
                                <el-dropdown-item command="convert" :icon="Key">ST 转换为 AT</el-dropdown-item>
                                <el-dropdown-item command="vids" :icon="VideoCamera" :disabled="onboarding" divided>
                                    接入未绑定的 Vids 账号
                                </el-dropdown-item>
                            </el-dropdown-menu>
                        </template>
                    </el-dropdown>
                </div>
            </div>
            <div v-if="loginStatus || exporting" class="import-status" aria-live="polite">
                <el-icon><Connection /></el-icon>
                <span>{{ exporting ? '正在读取并导出 Profile，请稍候…' : loginStatus }}</span>
                <el-button v-if="loginSession" text @click="loginOpen = true">查看登录进度</el-button>
            </div>
            <el-alert
                v-if="nativeBrowser"
                class="native-browser-bar"
                type="success"
                :closable="false"
                show-icon
                :title="`${nativeBrowser.email} 的本地 Cloak 窗口已打开`"
            >
                <template #default>
                    <p>直接在桌面窗口完成操作，凭据与窗口管理就在这里。</p>
                    <div class="native-browser-actions">
                        <el-button size="small" :disabled="nativeBrowserBusy" @click="focusNativeBrowser">显示窗口</el-button>
                        <el-button size="small" type="primary" :disabled="nativeBrowserBusy" @click="refreshNativeBrowser">刷新凭据</el-button>
                        <el-button size="small" :disabled="nativeBrowserBusy" @click="closeNativeBrowser(true)">保存并关闭后刷新</el-button>
                        <el-button size="small" type="danger" plain :disabled="nativeBrowserBusy" @click="closeNativeBrowser(false)">关闭窗口</el-button>
                    </div>
                </template>
            </el-alert>
            <div v-if="selected.length" class="selection-bar">
                <span>
                    已选择
                    <strong>{{ selected.length }}</strong>
                    位成员
                </span>
                <el-button size="small" :loading="busy.has('bulk')" @click="bulk('enable')">启用</el-button>
                <el-button size="small" :loading="busy.has('bulk')" @click="bulk('disable')">暂停</el-button>
                <el-button size="small" :loading="busy.has('bulk')" @click="bulk('refresh-at')">刷新会话</el-button>
                <el-button size="small" type="danger" plain :loading="busy.has('bulk')" @click="bulk('delete')">
                    删除
                </el-button>
            </div>
            <div v-if="loading && !rows.length" class="table-skeleton"><el-skeleton :rows="6" animated /></div>
            <el-table
                v-else
                :data="paginated"
                row-key="id"
                :expand-row-keys="expandedAccountIds"
                @expand-change="expandQuickAccount"
                @selection-change="selected = $event"
                @sort-change="Object.assign(tableSort, $event)"
                class="accounts-table"
            >
                <el-table-column type="selection" width="46" :selectable="(row) => !row.vidsOnly" />
                <el-table-column type="expand" width="34">
                    <template #default="{ row }">
                        <AccountProxyBinding
                            v-if="!row.vidsOnly"
                            :account="row"
                            :draft="quickDraft(row)"
                            :proxies="proxyPool"
                            :saving="quickSavingId === row.id"
                            :saving-kind="quickSavingKind"
                            :locked="quickSavingId !== null || (saving && editing?.id === row.id)"
                            :error="quickErrors[row.id]"
                            :saved="quickSaved[row.id]"
                            :pool-error="proxyPoolError"
                            @save="saveQuickAccount(row, $event)"
                            @copy="copy"
                            @reload="reloadQuickProxies"
                        />
                        <p v-else class="account-quick-unavailable">此成员仅接入 Vids，暂无 Flow 账号配置。</p>
                    </template>
                </el-table-column>
                <el-table-column label="账号成员" min-width="230">
                    <template #default="{ row }">
                        <button class="account-identity" @click="openDetail(row)">
                            <span class="account-avatar" :class="'avatar-' + (row.id % 4)">
                                {{ (row.name || row.email || '?').slice(0, 1).toUpperCase() }}
                            </span>
                            <span class="identity-text">
                                <strong>{{ row.email || '账号 #' + row.id }}</strong>
                                <small>
                                    {{ row.name || '成员 #' + row.id }}
                                    <span v-if="row.remark">· {{ row.remark }}</span>
                                </small>
                                <span class="platform-tags">
                                    <el-tag v-if="!row.vidsOnly" size="small" type="info" effect="plain">Flow</el-tag>
                                    <el-tag
                                        v-if="row.vids"
                                        size="small"
                                        :type="row.vids.identity_verified ? 'success' : 'warning'"
                                        effect="plain"
                                    >
                                        Vids
                                    </el-tag>
                                </span>
                            </span>
                        </button>
                        <button
                            v-if="!row.vidsOnly"
                            class="account-proxy-trigger"
                            :aria-expanded="expandedAccountIds.includes(row.id)"
                            :title="
                                accountProxy(row)?.proxy_url || row.proxy_url || '展开后选择代理、复制连接或编辑配置'
                            "
                            @click="toggleQuickAccount(row)"
                        >
                            <el-icon><Connection /></el-icon>
                            <span>{{ accountProxySummary(row) }}</span>
                            <el-icon class="quick-chevron" :class="{ expanded: expandedAccountIds.includes(row.id) }">
                                <ArrowRight />
                            </el-icon>
                        </button>
                    </template>
                </el-table-column>
                <el-table-column label="创作额度" min-width="260" sortable="custom" prop="credits">
                    <template #default="{ row }">
                        <div v-if="!row.vidsOnly" class="flow-quota-line">
                            <span>Flow 积分</span>
                            <strong class="balance-text">{{ quotaValue(row.credits) }}</strong>
                            <el-button
                                text
                                circle
                                size="small"
                                :icon="Refresh"
                                :loading="busy.has(row.id + ':refresh-credits')"
                                :aria-label="`刷新 ${row.email || row.id} 的 Flow 积分`"
                                @click="action(row, 'refresh-credits')"
                            />
                        </div>
                        <div
                            v-if="row.vids"
                            class="vids-quota-cell"
                            :class="{ 'quota-attention': quotaInfo(row).error || quotaInfo(row).stale }"
                        >
                            <div class="vids-quota-heading">
                                <span>Vids 剩余 / 总量</span>
                                <el-button
                                    text
                                    circle
                                    size="small"
                                    :icon="Refresh"
                                    :loading="busy.has(row.id + ':quota')"
                                    :aria-label="`刷新 ${row.email || row.id} 的 Vids 额度`"
                                    @click="action(row, 'quota')"
                                />
                            </div>
                            <div class="vids-quota-values">
                                <div v-for="kind in ['image', 'video']" :key="kind" class="vids-quota-kind">
                                    <span>
                                        <el-icon>
                                            <Picture v-if="kind === 'image'" />
                                            <VideoCamera v-else />
                                        </el-icon>
                                        {{ kind === 'image' ? '图片' : '视频' }}
                                    </span>
                                    <strong
                                        :class="{
                                            'quota-empty': quotaNumber(row.vids.last_quota?.[kind]?.available) === 0,
                                        }"
                                    >
                                        {{ quotaValue(row.vids.last_quota?.[kind]?.available) }}
                                        <small>/ {{ quotaValue(row.vids.last_quota?.[kind]?.total) }}</small>
                                    </strong>
                                </div>
                            </div>
                            <div class="quota-update" aria-live="polite">
                                <span>{{ quotaInfo(row).label }}</span>
                                <time
                                    v-if="quotaInfo(row).updated"
                                    :title="date(quotaInfo(row).updated)"
                                    :datetime="quotaInfo(row).updated"
                                >
                                    {{
                                        parseDate(quotaInfo(row).updated).toLocaleTimeString('zh-CN', { hour12: false })
                                    }}
                                </time>
                            </div>
                            <p v-if="quotaInfo(row).error" class="quota-inline-error" :title="quotaInfo(row).error">
                                {{ quotaInfo(row).error }}
                            </p>
                        </div>
                        <small v-else class="cell-note">{{ tier(row.user_paygate_tier) }} · Vids 未接入</small>
                    </template>
                </el-table-column>
                <el-table-column label="状态" width="90">
                    <template #default="{ row }">
                        <el-tag
                            :type="row.is_active ? 'success' : row.ban_reason === 'refresh_failed' ? 'warning' : 'info'"
                            effect="light"
                            round
                        >
                            {{ row.is_active ? '已启用' : row.ban_reason === 'refresh_failed' ? '待恢复' : '已暂停' }}
                        </el-tag>
                    </template>
                </el-table-column>
                <el-table-column label="创作能力" width="90">
                    <template #default="{ row }">
                        <div class="capability-icons">
                            <el-tooltip :content="row.image_enabled ? '图片生成已开启' : '图片生成已暂停'">
                                <el-icon :class="{ muted: !row.image_enabled }"><Picture /></el-icon>
                            </el-tooltip>
                            <el-tooltip :content="row.video_enabled ? '视频生成已开启' : '视频生成已暂停'">
                                <el-icon :class="{ muted: !row.video_enabled }"><VideoCamera /></el-icon>
                            </el-tooltip>
                        </div>
                    </template>
                </el-table-column>
                <el-table-column label="会话健康" min-width="205">
                    <template #default="{ row }">
                        <div class="account-list-health">
                            <span
                                v-if="!row.vidsOnly"
                                class="session-status"
                                :class="{ attention: flowAttention(row) }"
                            >
                                <i class="status-dot" />
                                <b>Flow</b>
                                {{ sessionLabel(row) }}
                            </span>
                            <span
                                class="session-status"
                                :class="{ attention: row.vids && vidsAttention(row.vids), unlinked: !row.vids }"
                            >
                                <i class="status-dot" />
                                <b>Vids</b>
                                {{ vidsState(row.vids) }}
                            </span>
                            <small
                                v-if="row.vids"
                                class="health-identity"
                                :class="{ attention: row.vids.identity_verified === false }"
                            >
                                {{ row.vids.identity_verified ? '身份已验证' : '身份待验证' }}
                            </small>
                            <small
                                v-if="row.refresh_error || row.browser_profile_error || row.vids?.runtime_error"
                                class="health-inline-error"
                            >
                                {{
                                    row.refresh_error || row.browser_profile_error || quotaError(row.vids.runtime_error)
                                }}
                            </small>
                        </div>
                    </template>
                </el-table-column>
                <el-table-column label="操作" width="180" fixed="right">
                    <template #default="{ row }">
                        <el-button v-if="!row.vidsOnly" text type="primary" @click="toggleQuickAccount(row)">
                            {{ expandedAccountIds.includes(row.id) ? '收起' : '展开编辑' }}
                        </el-button>
                        <el-button v-else text type="primary" @click="openDetail(row)">详情</el-button>
                        <el-button
                            v-if="row.browser_profile_source === 'cloak'"
                            text
                            :loading="busy.has(row.id + ':browser')"
                            @click="openBrowser(row)"
                        >
                            浏览器
                        </el-button>
                        <el-dropdown v-if="!row.vidsOnly" @command="(command) => accountCommand(row, command)">
                            <el-button text :icon="MoreFilled" :aria-label="'操作 ' + row.email" />
                            <template #dropdown>
                                <el-dropdown-menu>
                                    <template v-if="!row.vidsOnly">
                                        <el-dropdown-item command="edit">编辑配置</el-dropdown-item>
                                        <el-dropdown-item
                                            command="refresh-at"
                                            :disabled="busy.has(row.id + ':refresh-at')"
                                        >
                                            刷新 ST / AT
                                        </el-dropdown-item>
                                        <el-dropdown-item command="refresh-credits">刷新余额</el-dropdown-item>
                                        <el-dropdown-item :command="row.is_active ? 'disable' : 'enable'">
                                            {{ row.is_active ? '暂停账号' : '启用账号' }}
                                        </el-dropdown-item>
                                        <el-dropdown-item command="delete" divided>删除账号</el-dropdown-item>
                                    </template>
                                </el-dropdown-menu>
                            </template>
                        </el-dropdown>
                    </template>
                </el-table-column>
                <template #empty>
                    <el-empty
                        :description="
                            search || platform !== 'all' || state !== 'all'
                                ? '没有匹配的成员，试试其他筛选条件'
                                : '小队还没有成员，添加第一个创作账号吧'
                        "
                    >
                        <template #image><Mascot portrait compact mood="sleepy" class="empty-mascot" /></template>
                        <el-button v-if="!accounts.length" type="primary" :icon="Plus" @click="openEditor()">
                            添加账号
                        </el-button>
                    </el-empty>
                </template>
            </el-table>
            <div v-if="!loading || rows.length" class="mobile-accounts">
                <article v-for="row in paginated" :key="row.id" class="mobile-account">
                    <header class="mobile-account-header">
                        <el-checkbox
                            v-if="!row.vidsOnly"
                            :model-value="selected.some((item) => item.id === row.id)"
                            :aria-label="`选择 ${row.email || row.id}`"
                            @change="(checked) => selectMobile(row, checked)"
                        />
                        <button class="account-identity" @click="openDetail(row)">
                            <span class="identity-text">
                                <strong>{{ row.email || '账号 #' + row.id }}</strong>
                                <small>{{ row.remark || row.name || '成员 #' + row.id }}</small>
                            </span>
                        </button>
                        <el-tag :type="row.is_active ? 'success' : 'info'" size="small" effect="light" round>
                            {{ row.is_active ? '已启用' : '已暂停' }}
                        </el-tag>
                    </header>
                    <button
                        v-if="!row.vidsOnly"
                        class="account-proxy-trigger"
                        :aria-expanded="expandedAccountIds.includes(row.id)"
                        @click="toggleQuickAccount(row)"
                    >
                        <el-icon><Connection /></el-icon>
                        <span>{{ accountProxySummary(row) }}</span>
                        <el-icon class="quick-chevron" :class="{ expanded: expandedAccountIds.includes(row.id) }">
                            <ArrowRight />
                        </el-icon>
                    </button>
                    <AccountProxyBinding
                        v-if="!row.vidsOnly && expandedAccountIds.includes(row.id)"
                        :account="row"
                        :draft="quickDraft(row)"
                        :proxies="proxyPool"
                        :saving="quickSavingId === row.id"
                        :saving-kind="quickSavingKind"
                        :locked="quickSavingId !== null || (saving && editing?.id === row.id)"
                        :error="quickErrors[row.id]"
                        :saved="quickSaved[row.id]"
                        :pool-error="proxyPoolError"
                        @save="saveQuickAccount(row, $event)"
                        @copy="copy"
                        @reload="reloadQuickProxies"
                    />
                    <div class="mobile-account-quotas">
                        <div v-if="!row.vidsOnly">
                            <span>Flow 积分</span>
                            <strong>{{ quotaValue(row.credits) }}</strong>
                            <small>{{ tier(row.user_paygate_tier) }}</small>
                        </div>
                        <template v-if="row.vids">
                            <div v-for="kind in ['image', 'video']" :key="kind">
                                <span>Vids {{ kind === 'image' ? '图片' : '视频' }}</span>
                                <strong
                                    :class="{
                                        'quota-empty': quotaNumber(row.vids.last_quota?.[kind]?.available) === 0,
                                    }"
                                >
                                    {{ quotaValue(row.vids.last_quota?.[kind]?.available) }}
                                </strong>
                                <small>/ {{ quotaValue(row.vids.last_quota?.[kind]?.total) }} 总量</small>
                            </div>
                        </template>
                        <div v-else class="mobile-vids-empty">
                            <span>Vids</span>
                            <small>尚未接入</small>
                        </div>
                    </div>
                    <div class="mobile-account-health">
                        <span v-if="!row.vidsOnly" class="session-status" :class="{ attention: flowAttention(row) }">
                            <i class="status-dot" />
                            <b>Flow</b>
                            {{ sessionLabel(row) }}
                        </span>
                        <span
                            class="session-status"
                            :class="{ attention: row.vids && vidsAttention(row.vids), unlinked: !row.vids }"
                        >
                            <i class="status-dot" />
                            <b>Vids</b>
                            {{ vidsState(row.vids) }}
                        </span>
                        <small v-if="row.vids" class="health-identity">
                            {{ row.vids.identity_verified ? '身份已验证' : '身份待验证' }}
                        </small>
                        <p
                            v-if="row.refresh_error || row.browser_profile_error || row.vids?.runtime_error"
                            class="health-inline-error"
                        >
                            {{ row.refresh_error || row.browser_profile_error || quotaError(row.vids.runtime_error) }}
                        </p>
                    </div>
                    <div
                        v-if="row.vids"
                        class="quota-update"
                        :class="{ 'quota-attention': quotaInfo(row).error || quotaInfo(row).stale }"
                        aria-live="polite"
                    >
                        <span>{{ quotaInfo(row).label }}</span>
                        <time
                            v-if="quotaInfo(row).updated"
                            :title="date(quotaInfo(row).updated)"
                            :datetime="quotaInfo(row).updated"
                        >
                            {{ parseDate(quotaInfo(row).updated).toLocaleTimeString('zh-CN', { hour12: false }) }}
                        </time>
                    </div>
                    <p v-if="row.vids && quotaInfo(row).error" class="quota-inline-error">{{ quotaInfo(row).error }}</p>
                    <footer class="mobile-account-actions">
                        <el-button
                            v-if="row.browser_profile_source === 'cloak'"
                            text
                            size="small"
                            :icon="Monitor"
                            :loading="busy.has(row.id + ':browser')"
                            @click="openBrowser(row)"
                        >
                            浏览器
                        </el-button>
                        <el-button
                            v-if="!row.vidsOnly"
                            text
                            size="small"
                            :icon="Refresh"
                            :loading="busy.has(row.id + ':refresh-credits')"
                            @click="action(row, 'refresh-credits')"
                        >
                            刷新积分
                        </el-button>
                        <el-button
                            v-if="row.vids"
                            text
                            size="small"
                            :icon="Refresh"
                            :loading="busy.has(row.id + ':quota')"
                            @click="action(row, 'quota')"
                        >
                            刷新 Vids
                        </el-button>
                        <el-button
                            v-if="!row.vidsOnly"
                            text
                            type="primary"
                            size="small"
                            @click="toggleQuickAccount(row)"
                        >
                            {{ expandedAccountIds.includes(row.id) ? '收起' : '展开编辑' }}
                        </el-button>
                        <el-button v-else text type="primary" size="small" :icon="ArrowRight" @click="openDetail(row)">
                            详情
                        </el-button>
                    </footer>
                </article>
                <el-empty
                    v-if="!paginated.length"
                    :description="
                        search || platform !== 'all' || state !== 'all'
                            ? '没有匹配的成员，试试其他筛选条件'
                            : '小队还没有成员，添加第一个创作账号吧'
                    "
                >
                    <template #image><Mascot portrait compact mood="sleepy" class="empty-mascot" /></template>
                    <el-button v-if="!accounts.length" type="primary" :icon="Plus" @click="loginOpen = true">
                        添加账号
                    </el-button>
                </el-empty>
            </div>
            <footer class="table-footer">
                <span>显示 {{ filtered.length }} 位成员 · 每 15 秒同步状态 · Vids 额度每分钟自动更新</span>
                <el-pagination
                    v-model:current-page="page"
                    v-model:page-size="pageSize"
                    :page-sizes="[10, 20, 50]"
                    :total="filtered.length"
                    layout="sizes, prev, pager, next"
                    small
                />
            </footer>
        </section>
        <aside class="crew-note">
            <el-icon class="note-icon"><Connection /></el-icon>
            <p>
                <strong>小队协作，一起出发。</strong>
                Flow 与 Vids 共享账号开关和创作能力；启用且就绪的 Vids 账号会自动同步额度，离开页面后暂停轮询。
            </p>
            <el-button text :icon="ArrowRight" @click="onboardAll" :loading="onboarding">接入 Vids</el-button>
        </aside>

        <el-drawer
            v-model="detailOpen"
            :size="'min(920px, 100vw)'"
            class="account-drawer"
            :title="detail?.email || '账号详情'"
        >
            <template v-if="detail">
                <div class="detail-header">
                    <span class="account-avatar large" :class="'avatar-' + (detail.id % 4)">
                        {{ (detail.name || detail.email || '?').slice(0, 1).toUpperCase() }}
                    </span>
                    <div>
                        <h2>{{ detail.name || detail.email }}</h2>
                        <p>成员 #{{ detail.id }} · {{ tier(detail.user_paygate_tier) }}</p>
                    </div>
                    <el-tag :type="detail.is_active ? 'success' : 'info'">
                        {{ detail.is_active ? '已启用' : '已暂停' }}
                    </el-tag>
                </div>
                <div class="account-balances" :class="{ 'vids-only': detail.vidsOnly }">
                    <article v-if="!detail.vidsOnly" class="account-balance flow-balance">
                        <span>Flow 积分</span>
                        <strong>{{ quotaValue(detail.credits) }}</strong>
                        <small>图片与视频共用积分</small>
                    </article>
                    <article
                        v-for="kind in ['image', 'video']"
                        :key="kind"
                        class="account-balance"
                        :class="{
                            'balance-stale':
                                detail.vids?.last_quota?.[kind]?.stale || detail.vids?.last_quota?.[kind]?.error,
                        }"
                    >
                        <span>Vids {{ kind === 'image' ? '图片' : '视频' }}额度</span>
                        <strong>
                            {{ quotaValue(detail.vids?.last_quota?.[kind]?.available) }}
                            <small>/ {{ quotaValue(detail.vids?.last_quota?.[kind]?.total) }}</small>
                        </strong>
                        <small v-if="!detail.vids">尚未接入</small>
                        <template v-else>
                            <small>
                                {{
                                    detail.vids.last_quota?.[kind]?.queried_at
                                        ? '更新于 ' + date(detail.vids.last_quota[kind].queried_at)
                                        : '尚未查询额度'
                                }}
                            </small>
                            <el-tag
                                v-if="
                                    quotaNumber(detail.vids.last_quota?.[kind]?.available) !== null &&
                                    (detail.vids.last_quota?.[kind]?.stale ||
                                        detail.vids.last_quota?.[kind]?.error ||
                                        !detail.vids.enabled ||
                                        !detail.vids.ready)
                                "
                                size="small"
                                type="warning"
                                effect="plain"
                            >
                                历史额度
                            </el-tag>
                            <p v-if="detail.vids.last_quota?.[kind]?.error" class="health-inline-error">
                                {{ quotaError(detail.vids.last_quota[kind].error) }}
                            </p>
                        </template>
                    </article>
                </div>
                <div class="account-session-grid">
                    <section
                        v-if="!detail.vidsOnly"
                        class="account-session-summary flow-session-summary"
                        aria-label="Flow 会话"
                    >
                        <div class="session-section-heading">
                            <h3>Flow 会话</h3>
                            <el-tag :type="flowAttention(detail) ? 'warning' : 'success'" size="small">
                                {{ sessionLabel(detail) }}
                            </el-tag>
                        </div>
                        <el-alert
                            v-if="detail.refresh_error || detail.browser_profile_error"
                            :title="detail.refresh_error || detail.browser_profile_error"
                            type="warning"
                            :closable="false"
                            show-icon
                            class="account-alert"
                        />
                        <dl class="session-facts">
                            <div>
                                <dt>AT 到期</dt>
                                <dd>{{ date(detail.at_expires) }}</dd>
                            </div>
                            <div>
                                <dt>下次刷新</dt>
                                <dd>{{ date(detail.next_refresh_at) }}</dd>
                            </div>
                            <div>
                                <dt>浏览器 Profile</dt>
                                <dd>
                                    {{
                                        detail.browser_profile_source === 'cloak'
                                            ? profileLabel(detail.browser_profile_status)
                                            : 'Cookie 会话'
                                    }}
                                </dd>
                            </div>
                            <div>
                                <dt>连续刷新失败</dt>
                                <dd>{{ detail.refresh_failures || 0 }} 次</dd>
                            </div>
                        </dl>
                        <div class="detail-actions">
                            <el-button
                                :icon="Refresh"
                                :loading="busy.has(detail.id + ':refresh-at')"
                                @click="action(detail, 'refresh-at')"
                            >
                                刷新会话
                            </el-button>
                            <el-button
                                :loading="busy.has(detail.id + ':refresh-credits')"
                                @click="action(detail, 'refresh-credits')"
                            >
                                刷新积分
                            </el-button>
                            <el-button
                                v-if="detail.browser_profile_source === 'cloak'"
                                :icon="Monitor"
                                :loading="busy.has(detail.id + ':browser')"
                                @click="openBrowser(detail)"
                            >
                                查看浏览器
                            </el-button>
                        </div>
                    </section>
                    <section class="account-session-summary vids-session-summary" aria-label="Vids 会话">
                        <div class="session-section-heading">
                            <h3>Vids 会话</h3>
                            <el-tag
                                :type="
                                    !detail.vids || detail.vids.enabled === false
                                        ? 'info'
                                        : vidsAttention(detail.vids)
                                          ? 'warning'
                                          : 'success'
                                "
                                size="small"
                            >
                                {{ vidsState(detail.vids) }}
                            </el-tag>
                        </div>
                        <template v-if="detail.vids">
                            <el-alert
                                v-if="detail.vids.runtime_error"
                                :title="quotaError(detail.vids.runtime_error)"
                                type="warning"
                                :closable="false"
                                show-icon
                                class="account-alert"
                            />
                            <dl class="session-facts">
                                <div>
                                    <dt>Vids 身份</dt>
                                    <dd>{{ detail.vids.vids_identity || '尚未读取' }}</dd>
                                </div>
                                <div>
                                    <dt>身份验证</dt>
                                    <dd :class="{ attention: !detail.vids.identity_verified }">
                                        {{ detail.vids.identity_verified ? '身份已验证' : '身份待核对' }}
                                    </dd>
                                </div>
                                <div>
                                    <dt>自动同步</dt>
                                    <dd>{{ quotaInfo(detail).label }}</dd>
                                </div>
                            </dl>
                            <div class="detail-actions">
                                <el-button
                                    type="primary"
                                    :icon="Refresh"
                                    :loading="busy.has(detail.id + ':quota')"
                                    @click="action(detail, 'quota')"
                                >
                                    刷新额度
                                </el-button>
                                <el-button
                                    :loading="busy.has(detail.id + ':reconnect')"
                                    @click="action(detail, 'reconnect')"
                                >
                                    重新连接
                                </el-button>
                            </div>
                        </template>
                        <template v-else>
                            <p class="unlinked-copy">此账号尚未接入 Vids，接入后图片和视频额度会直接显示在上方。</p>
                            <el-button
                                type="primary"
                                :loading="busy.has(detail.id + ':onboard')"
                                @click="action(detail, 'onboard')"
                            >
                                接入此账号
                            </el-button>
                        </template>
                    </section>
                </div>
                <section class="account-overview-metadata">
                    <h3>账号资料与使用记录</h3>
                    <div v-if="!detail.vidsOnly" class="detail-metrics">
                        <div>
                            <span>上次刷新</span>
                            <strong class="metric-date">
                                {{ date(detail.last_refresh_at || detail.last_st_refresh_at) }}
                            </strong>
                        </div>
                        <div>
                            <span>累计图片</span>
                            <strong>{{ detail.image_count || 0 }}</strong>
                        </div>
                        <div>
                            <span>累计视频</span>
                            <strong>{{ detail.video_count || 0 }}</strong>
                        </div>
                        <div>
                            <span>今日错误</span>
                            <strong>{{ detail.today_error_count || 0 }}</strong>
                        </div>
                    </div>
                    <el-descriptions :column="1" border class="detail-descriptions">
                        <el-descriptions-item label="项目名称">
                            {{ detail.current_project_name || '暂无' }}
                        </el-descriptions-item>
                        <el-descriptions-item label="项目 ID">
                            <code>{{ detail.current_project_id || '暂无' }}</code>
                        </el-descriptions-item>
                        <el-descriptions-item label="创建时间">{{ date(detail.created_at) }}</el-descriptions-item>
                        <el-descriptions-item label="最近使用">{{ date(detail.last_used_at) }}</el-descriptions-item>
                        <el-descriptions-item label="备注">{{ detail.remark || '暂无' }}</el-descriptions-item>
                    </el-descriptions>
                </section>
            </template>
            <el-empty v-else description="此账号已移除">
                <template #image><Mascot portrait compact mood="sleepy" class="empty-mascot" /></template>
            </el-empty>
            <template #footer>
                <el-button @click="detailOpen = false">关闭</el-button>
                <el-button v-if="detail && !detail.vidsOnly" type="primary" @click="editDetail">编辑账号配置</el-button>
            </template>
        </el-drawer>

        <el-drawer
            v-model="editorOpen"
            :size="'min(700px, 100vw)'"
            :title="editing ? '编辑成员配置' : '添加创作成员'"
            :close-on-click-modal="!saving"
            @closed="clearFormValidation"
        >
            <p class="editor-intro">配置会话、创作能力和刷新节奏，让每位成员发挥所长。</p>
            <el-form ref="formRef" :model="form" :rules="rules" label-position="top" class="account-form">
                <el-tabs v-model="editorTab">
                    <el-tab-pane label="账号与会话" name="identity">
                        <el-form-item label="Session Token" prop="st">
                            <el-input
                                v-model="form.st"
                                autocomplete="off"
                                placeholder="输入 Google Flow 的 Session Token"
                            />
                            <span class="field-note">
                                {{
                                    editing
                                        ? '仅更新后的 Token 会重新校验账号身份'
                                        : '保存时将自动验证账号并获取 Access Token'
                                }}
                            </span>
                        </el-form-item>
                        <div class="form-grid">
                            <el-form-item label="登录协议">
                                <el-select v-model="form.protocol_mode">
                                    <el-option label="Session Token" value="session" />
                                    <el-option label="Google Cookie 协议" value="protocol" />
                                </el-select>
                            </el-form-item>
                            <el-form-item label="浏览器会话来源">
                                <el-select v-model="form.browser_profile_source">
                                    <el-option label="导入 Cookies" value="cookies" />
                                    <el-option
                                        label="持久化 Cloak Profile"
                                        value="cloak"
                                        :disabled="editing?.browser_profile_source !== 'cloak'"
                                    />
                                </el-select>
                                <span class="field-note">通过 Cloak 登录或导入 Profile JSON 初始化</span>
                            </el-form-item>
                        </div>
                        <el-form-item label="Google Cookies">
                            <el-input
                                v-model="form.google_cookies"
                                type="textarea"
                                :rows="3"
                                autocomplete="off"
                                placeholder="Google Cookies 导出文本"
                            />
                            <span class="field-note">用于协议登录和会话自动刷新</span>
                        </el-form-item>
                        <div class="form-grid">
                            <el-form-item label="登录账号">
                                <el-input
                                    v-model="form.login_account"
                                    placeholder="Google 账号邮箱"
                                    autocomplete="off"
                                />
                            </el-form-item>
                            <el-form-item label="登录密码">
                                <el-input
                                    v-model="form.login_password"
                                    autocomplete="new-password"
                                    :placeholder="editing ? '留空保留原密码' : '选填'"
                                />
                            </el-form-item>
                        </div>
                        <el-form-item label="成员备注">
                            <el-input
                                v-model="form.remark"
                                type="textarea"
                                :rows="3"
                                maxlength="500"
                                show-word-limit
                                placeholder="给这个成员写一句备注吧"
                            />
                        </el-form-item>
                    </el-tab-pane>
                    <el-tab-pane label="创作能力" name="capacity">
                        <div class="capability-setting">
                            <div>
                                <el-icon><Picture /></el-icon>
                                <strong>图片创作</strong>
                                <p>允许此账号参与图片任务</p>
                            </div>
                            <el-switch v-model="form.image_enabled" />
                        </div>
                        <el-form-item label="图片并发上限">
                            <el-input-number
                                v-model="form.image_concurrency"
                                :min="-1"
                                :max="100"
                                controls-position="right"
                            />
                            <span class="field-note">-1 表示不限制，0 表示暂停分配</span>
                        </el-form-item>
                        <div class="capability-setting">
                            <div>
                                <el-icon><VideoCamera /></el-icon>
                                <strong>视频创作</strong>
                                <p>允许此账号参与视频任务</p>
                            </div>
                            <el-switch v-model="form.video_enabled" />
                        </div>
                        <el-form-item label="视频并发上限">
                            <el-input-number
                                v-model="form.video_concurrency"
                                :min="-1"
                                :max="100"
                                controls-position="right"
                            />
                            <span class="field-note">-1 表示不限制，0 表示暂停分配</span>
                        </el-form-item>
                        <div class="form-grid">
                            <el-form-item label="Flow 项目 ID">
                                <el-input v-model="form.project_id" placeholder="留空自动获取" />
                            </el-form-item>
                            <el-form-item label="项目名称">
                                <el-input v-model="form.project_name" placeholder="选填" />
                            </el-form-item>
                        </div>
                    </el-tab-pane>
                    <el-tab-pane label="网络与刷新" name="network">
                        <el-form-item label="生成提交出口">
                            <el-select v-model="form.submission_proxy_mode" aria-label="生成提交出口">
                                <el-option label="账号固定代理" value="fixed" />
                                <el-option label="代理池随机（每次生成取健康出口）" value="pool_random" />
                            </el-select>
                            <span class="field-note">
                                每份 Flow 生成任务使用一个出口；Cloak 登录和自动续期仍使用账号绑定代理。
                            </span>
                        </el-form-item>
                        <el-form-item label="账号代理">
                            <template v-if="editing?.browser_profile_source === 'cloak'">
                                <el-input :model-value="currentProxyLabel" readonly />
                                <span class="field-note">
                                    Cloak 登录和会话刷新使用这个固定出口，保持 Profile 环境一致。
                                </span>
                            </template>
                            <el-select v-else v-model="form.proxy_pool_id" placeholder="请选择固定出口" filterable>
                                <el-option
                                    v-for="row in availableProxies"
                                    :key="row.id"
                                    :value="row.id"
                                    :label="proxyOptionLabel(row)"
                                />
                            </el-select>
                            <router-link to="/proxies">管理代理池</router-link>
                            <span class="field-note">媒体下载在池内独立分配，验证码出口由远程服务管理。</span>
                        </el-form-item>
                        <div class="capability-setting">
                            <div>
                                <strong>自动刷新会话</strong>
                                <p>按照成员自己的节奏更新 Google 登录态</p>
                            </div>
                            <el-switch v-model="form.auto_refresh_enabled" />
                        </div>
                        <el-form-item label="刷新间隔（分钟）" prop="refresh_interval_minutes">
                            <el-input-number
                                v-model="form.refresh_interval_minutes"
                                :min="1"
                                :max="1440"
                                :disabled="!form.auto_refresh_enabled"
                                controls-position="right"
                            />
                        </el-form-item>
                    </el-tab-pane>
                </el-tabs>
            </el-form>
            <template #footer>
                <el-button :disabled="saving" @click="editorOpen = false">取消</el-button>
                <el-button type="primary" :loading="saving" @click="saveAccount">
                    {{ editing ? '保存成员配置' : '验证并添加账号' }}
                </el-button>
            </template>
        </el-drawer>

        <el-dialog
            v-model="loginOpen"
            title="Cloak 登录账号"
            width="min(560px, calc(100vw - 32px))"
            :close-on-click-modal="false"
        >
            <p class="dialog-note">
                {{ nativePresentation
                    ? '登录会直接打开本机 Cloak 窗口；完成 Google 登录后回到这里点击“完成登录并添加”。'
                    : '在服务器浏览器完成 Google 登录，验证身份与 Flow 会话后保存 Profile 并加入账号小队。' }}
            </p>
            <p v-if="!presentationReady" class="field-note" role="status">正在连接账号浏览器服务…</p>
            <el-form label-position="top" @submit.prevent="startLogin">
                <el-form-item label="Google 账号邮箱">
                    <el-input
                        v-model="loginForm.email"
                        type="email"
                        autocomplete="username"
                        placeholder="name@gmail.com"
                        :disabled="!!loginSession || loginStarting"
                    />
                </el-form-item>
                <el-form-item v-if="!loginSession" label="账号代理">
                    <el-select
                        v-model="loginForm.proxy_pool_id"
                        placeholder="自动分配并绑定"
                        clearable
                        filterable
                        :disabled="loginStarting"
                    >
                        <el-option
                            v-for="row in availableProxies"
                            :key="row.id"
                            :value="row.id"
                            :label="proxyOptionLabel(row)"
                        />
                    </el-select>
                    <span class="field-note">登录前从池内分配固定出口，后续会话刷新与 API 保持一致。</span>
                    <router-link to="/proxies">管理代理池</router-link>
                    <span v-if="proxyPoolError" class="field-note">代理池读取失败：{{ proxyPoolError }}</span>
                </el-form-item>
            </el-form>
            <el-alert
                v-if="loginSession"
                :title="loginSession.email"
                :description="loginStatus"
                type="info"
                :closable="false"
                show-icon
            />
            <template #footer>
                <el-button
                    v-if="loginSession"
                    :loading="loginCanceling"
                    :disabled="loginFinishing"
                    @click="cancelLogin"
                >
                    取消本次登录
                </el-button>
                <el-button v-if="loginSession" :disabled="loginFinishing || loginCanceling" @click="resumeLogin">
                    继续登录
                </el-button>
                <el-button
                    v-if="loginSession"
                    type="primary"
                    :loading="loginFinishing"
                    :disabled="loginCanceling"
                    @click="completeLogin"
                >
                    完成登录并添加
                </el-button>
                <template v-else>
                    <el-button :disabled="loginStarting" @click="loginOpen = false">关闭</el-button>
                    <el-button type="primary" :loading="loginStarting" :disabled="!presentationReady" @click="startLogin">
                        {{ nativePresentation ? '打开本地 Cloak 登录' : '打开 Cloak 登录' }}
                    </el-button>
                </template>
            </template>
        </el-dialog>

        <el-dialog
            v-model="importOpen"
            title="导入账号 Profile"
            width="min(620px, calc(100vw - 32px))"
            :before-close="closeImport"
        >
            <p class="dialog-note">
                在后台重建并验证账号，提交后可关闭此窗口、切换页面，刷新后也能查看进度。兼容 Profile JSON
                和旧版账号数组。
            </p>
            <el-upload
                drag
                :auto-upload="false"
                :show-file-list="false"
                accept=".json,application/json"
                :on-change="readImport"
                :disabled="importing"
            >
                <el-icon class="upload-symbol"><Upload /></el-icon>
                <div>
                    拖入 JSON 文件，或
                    <span class="upload-link">点击选择</span>
                </div>
            </el-upload>
            <el-input
                v-model="importText"
                type="textarea"
                :rows="7"
                placeholder="粘贴导出的 Profile JSON"
                class="import-text"
                :disabled="importing"
            />
            <el-form label-position="top">
                <el-form-item label="目标服务器账号代理">
                    <el-select
                        v-model="importProxy"
                        placeholder="自动分配并绑定"
                        clearable
                        filterable
                        :disabled="importing"
                    >
                        <el-option
                            v-for="row in availableProxies"
                            :key="row.id"
                            :value="row.id"
                            :label="proxyOptionLabel(row)"
                        />
                    </el-select>
                    <span v-if="importing" class="field-note">管理代理池</span>
                    <router-link v-else to="/proxies">管理代理池</router-link>
                    <span class="field-note">Profile 文件含登录凭据，请妥善保管；代理密码不随 Profile 导出。</span>
                </el-form-item>
            </el-form>
            <p v-if="sameImportSubmitted" class="dialog-note" role="status">
                这份资料已提交后台，进度与失败重试在账号页的「后台导入」中查看。
            </p>
            <template #footer>
                <el-button @click="importOpen = false">关闭</el-button>
                <el-button
                    type="primary"
                    :loading="importing"
                    :disabled="importing || !importText.trim() || sameImportSubmitted"
                    @click="importAccounts"
                >
                    {{ importing ? '正在提交' : sameImportSubmitted ? '已提交后台' : '后台导入' }}
                </el-button>
            </template>
        </el-dialog>
        <el-dialog v-model="convertOpen" title="ST → AT 会话转换" width="min(560px, calc(100vw - 32px))">
            <p class="dialog-note">转换仅获取会话信息，账号不会自动加入小队。</p>
            <el-input v-model="convertST" placeholder="粘贴 Session Token" autocomplete="off" />
            <el-descriptions v-if="convertResult" :column="1" border class="convert-result">
                <el-descriptions-item label="账号">{{ convertResult.email }}</el-descriptions-item>
                <el-descriptions-item label="有效期">{{ date(convertResult.expires) }}</el-descriptions-item>
                <el-descriptions-item label="Access Token">
                    <el-input :model-value="convertResult.access_token" readonly />
                    <el-button text type="primary" @click="copy(convertResult.access_token)">复制 Token</el-button>
                </el-descriptions-item>
            </el-descriptions>
            <template #footer>
                <el-button @click="convertOpen = false">关闭</el-button>
                <el-button type="primary" :loading="converting" @click="convert">转换会话</el-button>
            </template>
        </el-dialog>
    </div>
</template>

<style scoped>
.crew-metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 16px;
    margin-bottom: 28px;
}
.crew-metric {
    position: relative;
    background: var(--surface);
    border-radius: 19px;
    padding: 20px 22px;
    overflow: hidden;
}
.metric-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 13px;
    color: var(--muted);
    font-weight: 600;
}
.metric-top .el-icon {
    width: 34px;
    height: 34px;
    font-size: 18px;
    border-radius: 11px;
    background: var(--pink-soft);
    color: var(--accent);
}
.crew-metric strong {
    display: block;
    font-size: 31px;
    letter-spacing: -1px;
    margin-top: 7px;
    font-variant-numeric: tabular-nums;
    color: var(--ink);
}
.crew-metric p {
    font-size: 13px;
    color: var(--muted);
    margin: 7px 0 0;
}
.green .metric-top .el-icon {
    background: var(--green-soft);
    color: var(--success);
}
.blue .metric-top .el-icon {
    background: var(--blue-soft);
    color: var(--blue);
}
.violet .metric-top .el-icon {
    background: var(--peach-soft);
    color: var(--peach);
}
.account-surface {
    overflow: hidden;
    padding: 0;
}
.native-browser-bar {
    flex-shrink: 0;
    margin: 0 26px 12px;
    width: auto;
}
.native-browser-bar p {
    margin: 0 0 8px;
    font-size: 13px;
}
.native-browser-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}
.native-browser-actions .el-button {
    margin: 0;
}
.section-heading {
    padding: 24px 26px 16px;
}
.section-heading h2 {
    margin: 0;
    display: flex;
    align-items: center;
    gap: 10px;
}
.section-heading p {
    margin: 7px 0 0;
    font-size: 13px;
    color: var(--muted);
}
.count-label {
    font-size: 13px;
    background: var(--pink-soft);
    color: var(--accent);
    padding: 3px 8px;
    border-radius: 6px;
}
.refresh-toggle {
    display: flex;
    align-items: center;
    gap: 10px;
    font-size: 13px;
    color: var(--muted);
}
.account-toolbar {
    display: flex;
    justify-content: space-between;
    gap: 12px;
    flex-wrap: wrap;
    padding: 10px 26px 22px;
}
.filters,
.table-tools {
    display: flex;
    gap: 9px;
    align-items: center;
}
.account-search {
    width: 260px;
}
.filters .el-select {
    width: 115px;
}
.account-identity {
    display: flex;
    gap: 12px;
    align-items: center;
    border: 0;
    background: transparent;
    text-align: left;
    padding: 0;
    cursor: pointer;
    width: 100%;
    color: var(--ink);
}
.account-avatar {
    width: 39px;
    height: 39px;
    flex-shrink: 0;
    display: grid;
    place-items: center;
    border-radius: 13px;
    font-size: 15px;
    font-weight: 800;
    background: var(--pink-soft);
    color: var(--accent);
}
.avatar-1 {
    background: var(--blue-soft);
    color: var(--blue);
}
.avatar-2 {
    background: var(--peach-soft);
    color: var(--peach);
}
.avatar-3 {
    background: var(--green-soft);
    color: var(--success);
}
.identity-text {
    min-width: 0;
}
.identity-text strong {
    display: block;
    font-size: 14px;
    font-weight: 650;
    max-width: 230px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.identity-text small {
    display: block;
    font-size: 13px;
    color: var(--muted);
    margin-top: 4px;
    max-width: 210px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.platform-tags {
    display: flex;
    gap: 4px;
    margin-top: 8px;
}
.metric-quotas {
    display: flex;
    gap: 25px;
    margin-top: 12px;
}
.metric-quotas > div > span {
    font-size: 12px;
    color: var(--muted);
}
.metric-quotas strong {
    margin-top: 3px;
    font-size: 28px;
    line-height: 1.15;
}
.metric-quotas small {
    font-size: 11px;
    margin-left: 5px;
    color: var(--peach);
    font-weight: 500;
}
.flow-quota-line,
.vids-quota-heading,
.quota-update {
    display: flex;
    align-items: center;
    gap: 8px;
}
.flow-quota-line > span,
.vids-quota-heading > span {
    font-size: 12px;
    color: var(--muted);
}
.flow-quota-line .el-button,
.vids-quota-heading .el-button {
    margin-left: auto;
    min-height: 24px;
}
.vids-quota-cell {
    padding-top: 4px;
}
.flow-quota-line + .vids-quota-cell {
    margin-top: 5px;
    padding-top: 6px;
}
.vids-quota-values {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 16px;
}
.vids-quota-kind > span {
    display: flex;
    align-items: center;
    gap: 4px;
    color: var(--muted);
    font-size: 11px;
    line-height: 1.5;
}
.vids-quota-kind strong {
    display: block;
    font-size: 18px;
    line-height: 1.6;
    font-variant-numeric: tabular-nums;
}
.vids-quota-kind strong small {
    font-size: 12px;
    font-weight: 400;
    color: var(--muted);
}
.quota-update {
    gap: 5px;
    margin-top: 3px;
    font-size: 11px;
    line-height: 1.5;
    color: var(--muted);
    flex-wrap: wrap;
}
.quota-update time {
    font-variant-numeric: tabular-nums;
}
.quota-attention .quota-update {
    color: var(--el-color-warning);
}
.vids-quota-kind .quota-empty {
    color: var(--el-color-danger);
}
.quota-inline-error {
    color: var(--el-color-danger);
    font-size: 11px;
    line-height: 1.5;
    margin: 3px 0 0;
    overflow: hidden;
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow-wrap: anywhere;
}
.mobile-accounts {
    display: none;
}
.balance-text {
    font-size: 14px;
    font-variant-numeric: tabular-nums;
}
.cell-note {
    display: block;
    font-size: 13px;
    color: var(--muted);
    margin-top: 5px;
}
.capability-icons {
    display: flex;
    gap: 13px;
    color: var(--accent);
    font-size: 18px;
}
.capability-icons .muted {
    color: var(--muted);
}
.session-status {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 13px;
}
.session-status .status-dot {
    background: var(--success);
    width: 6px;
    height: 6px;
    border-radius: 50%;
}
.session-status.attention .status-dot {
    background: var(--peach);
}
.table-footer {
    padding: 18px 26px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
}
.table-footer > span {
    font-size: 13px;
    color: var(--muted);
}
.table-skeleton {
    padding: 25px;
}
.selection-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    background: var(--pink-soft);
    padding: 12px 26px;
    font-size: 13px;
}
.selection-bar > span {
    margin-right: auto;
}
.import-status {
    display: flex;
    gap: 10px;
    align-items: center;
    padding: 13px 26px;
    background: var(--blue-soft);
    font-size: 13px;
    color: var(--blue);
}
.crew-note {
    display: flex;
    gap: 15px;
    align-items: center;
    padding: 20px 5px;
    color: var(--muted);
}
.note-icon {
    font-size: 31px;
    color: var(--accent);
}
.crew-note p {
    font-size: 13px;
    line-height: 1.8;
    flex: 1;
}
.crew-note strong {
    color: var(--ink);
    font-weight: 650;
}
.account-alert {
    margin-bottom: 14px;
}
.detail-header {
    display: flex;
    align-items: center;
    gap: 16px;
    margin-bottom: 26px;
}
.detail-header > .el-tag {
    margin-left: auto;
}
.account-avatar.large {
    width: 62px;
    height: 62px;
    font-size: 25px;
    border-radius: 21px;
}
.detail-header h2 {
    font-size: 18px;
    margin: 0;
}
.detail-header p {
    font-size: 13px;
    color: var(--muted);
    margin: 7px 0 0;
}
.account-balances {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 12px;
    margin-bottom: 20px;
}
.account-balances.vids-only {
    grid-template-columns: repeat(2, minmax(0, 1fr));
}
.account-balance {
    min-width: 0;
    padding: 17px;
    background: var(--surface-soft);
    border-radius: 13px;
}
.account-balance.flow-balance {
    background: var(--pink-soft);
}
.account-balance > span {
    color: var(--muted);
    font-size: 13px;
}
.account-balance > strong {
    display: block;
    margin: 10px 0 7px;
    font-size: 29px;
    line-height: 1.25;
    font-variant-numeric: tabular-nums;
}
.account-balance small {
    color: var(--muted);
    font-size: 12px;
    font-weight: 400;
    overflow-wrap: anywhere;
}
.account-balance > .el-tag {
    margin-top: 8px;
}
.account-session-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
}
.account-session-summary {
    min-width: 0;
    padding: 18px;
    border: 1px solid var(--line);
    border-radius: 13px;
}
.session-section-heading {
    display: flex;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px;
    margin-bottom: 15px;
}
.session-section-heading h3,
.account-overview-metadata > h3 {
    font-size: 15px;
    font-weight: 650;
}
.session-facts {
    display: grid;
    gap: 11px;
    margin: 0;
}
.session-facts > div {
    display: grid;
    grid-template-columns: 95px minmax(0, 1fr);
    gap: 9px;
    font-size: 12px;
}
.session-facts dt {
    color: var(--muted);
}
.session-facts dd {
    margin: 0;
    color: var(--ink);
    overflow-wrap: anywhere;
    font-variant-numeric: tabular-nums;
}
.session-facts dd.attention,
.health-identity.attention {
    color: var(--el-color-warning);
}
.account-session-summary .detail-actions {
    gap: 7px;
    margin-top: 18px;
}
.account-session-summary .detail-actions .el-button {
    font-size: 12px;
    margin: 0;
    padding: 8px 10px;
}
.unlinked-copy {
    margin: 10px 0 18px;
    color: var(--muted);
    font-size: 13px;
    line-height: 1.8;
}
.account-overview-metadata {
    margin-top: 25px;
}
.account-overview-metadata > h3 {
    margin-bottom: 14px;
}
.account-overview-metadata .detail-metrics {
    padding: 15px;
    margin-bottom: 14px;
    gap: 12px;
}
.account-overview-metadata .detail-metrics .metric-date {
    font-size: 12px;
    font-weight: 500;
    line-height: 1.6;
}
.account-list-health {
    display: grid;
    gap: 6px;
    padding: 3px 0;
}
.session-status b {
    font-size: 11px;
    color: var(--muted);
    font-weight: 500;
}
.session-status.unlinked .status-dot {
    background: var(--muted);
}
.health-identity {
    color: var(--muted);
    font-size: 11px;
}
.health-inline-error {
    font-size: 12px;
    line-height: 1.6;
    color: var(--el-color-warning);
    white-space: normal;
    overflow-wrap: anywhere;
    margin-top: 4px;
}
.mobile-account-health {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px 16px;
    padding: 13px 0 4px;
}
.mobile-account-health > .health-inline-error {
    width: 100%;
}
.detail-metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    background: var(--surface-soft);
    border-radius: 16px;
    padding: 18px;
    gap: 14px;
    margin-bottom: 25px;
}
.detail-metrics span {
    font-size: 13px;
    color: var(--muted);
}
.detail-metrics strong {
    display: block;
    font-size: 23px;
    margin-top: 9px;
    font-variant-numeric: tabular-nums;
}
.detail-descriptions {
    margin-top: 17px;
}
.detail-descriptions code {
    font-size: 13px;
    overflow-wrap: anywhere;
}
.detail-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    margin-top: 20px;
}
.detail-actions .el-button + .el-button {
    margin-left: 0;
}
.editor-intro {
    font-size: 13px;
    line-height: 1.8;
    color: var(--muted);
    margin: 0 0 20px;
}
@media (max-width: 620px) {
    .account-balances {
        gap: 8px;
        margin-bottom: 14px;
    }
    .account-balance {
        padding: 12px 10px;
        border-radius: 10px;
    }
    .account-balance > span {
        font-size: 11px;
    }
    .account-balance > strong {
        font-size: 22px;
    }
    .account-balance > strong > small {
        font-size: 11px;
    }
    .account-balance > small {
        font-size: 10px;
        line-height: 1.6;
        display: block;
    }
    .account-session-grid {
        grid-template-columns: minmax(0, 1fr);
        gap: 12px;
    }
    .account-session-summary {
        padding: 15px;
    }
    .detail-header {
        gap: 12px;
        margin-bottom: 18px;
    }
    .session-facts > div {
        grid-template-columns: 100px minmax(0, 1fr);
    }
}
.form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 18px;
}
.account-form .el-select {
    width: 100%;
}
.field-note {
    display: block;
    font-size: 13px;
    line-height: 1.7;
    color: var(--muted);
    margin-top: 7px;
}
.capability-setting {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 18px 0;
    margin-bottom: 20px;
}
.capability-setting strong {
    font-size: 13px;
    margin-left: 9px;
}
.capability-setting div > strong:first-child {
    margin-left: 0;
}
.capability-setting p {
    font-size: 13px;
    color: var(--muted);
    margin: 7px 0 0;
}
.dialog-note {
    font-size: 13px;
    line-height: 1.8;
    color: var(--muted);
    margin: 0 0 20px;
}
.upload-symbol {
    font-size: 30px;
    color: var(--accent);
    margin-bottom: 15px;
}
.upload-link {
    color: var(--accent);
}
.import-text,
.convert-result {
    margin-top: 18px;
}
.profile-import-jobs {
    padding: 18px 22px;
    margin-bottom: 22px;
    font-size: 13px;
}
.profile-import-heading,
.profile-import-job summary {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px 14px;
}
.profile-import-heading {
    justify-content: space-between;
    margin-bottom: 8px;
}
.profile-import-heading > div {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 8px 14px;
}
.profile-import-heading span,
.profile-import-job-time,
.profile-import-expand {
    color: var(--muted);
}
.profile-import-job {
    border-top: 1px solid var(--line);
}
.profile-import-job summary {
    cursor: pointer;
    padding: 12px 0;
    list-style: none;
}
.profile-import-job summary::before {
    content: '\203a';
    color: var(--muted);
    font-size: 18px;
}
.profile-import-job[open] summary::before {
    transform: rotate(90deg);
}
.profile-import-job summary::-webkit-details-marker {
    display: none;
}
.profile-import-expand {
    margin-left: auto;
    font-size: 12px;
}
.profile-import-job-detail {
    padding: 0 0 12px 20px;
}
.profile-import-job-detail > p {
    margin: 0;
}
.profile-import-job ul {
    padding: 0;
    margin: 6px 0 12px;
    list-style: none;
    max-height: 280px;
    overflow-y: auto;
}
.profile-import-job li {
    display: grid;
    grid-template-columns: minmax(0, 1fr) auto;
    gap: 3px 12px;
    padding: 8px 0;
}
.profile-import-job li > span,
.profile-import-job li > small {
    overflow-wrap: anywhere;
}
.profile-import-job li > small {
    grid-column: 1 / -1;
    font-size: 12px;
}
.profile-import-job .failed,
.profile-import-jobs-error {
    color: var(--el-color-danger);
}
.accounts-table :deep(.el-table__cell) {
    padding: 16px 0;
}
.accounts-table :deep(.el-table__header th) {
    background: var(--ground);
    font-size: 13px;
    color: var(--muted);
    font-weight: 650;
}
.account-identity:hover strong {
    color: var(--accent);
}
.account-proxy-trigger {
    display: flex;
    align-items: center;
    gap: 6px;
    border: 0;
    border-radius: 4px;
    background: transparent;
    color: var(--accent);
    cursor: pointer;
    padding: 5px 0;
    margin-top: 5px;
    font: inherit;
    font-size: 12px;
    text-align: left;
    max-width: 100%;
}
.account-proxy-trigger > span {
    min-width: 0;
    overflow-wrap: anywhere;
}
.account-proxy-trigger .el-icon {
    flex-shrink: 0;
}
.account-proxy-trigger:hover {
    color: var(--ink);
}
.quick-chevron {
    transition: transform 150ms ease;
}
.quick-chevron.expanded {
    transform: rotate(90deg);
}
.account-quick-unavailable {
    margin: 0;
    padding: 16px 24px;
    color: var(--muted);
}
.account-proxy-trigger:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
}
.accounts-table :deep(.el-table__expanded-cell) {
    padding: 0;
}

@media (max-width: 1100px) {
    .crew-metrics {
        gap: 12px;
    }
    .crew-metric {
        padding: 18px;
    }
    .filters {
        flex: 1;
        flex-wrap: wrap;
    }
    .account-search {
        width: 230px;
    }
    .account-toolbar {
        padding: 10px 20px 18px;
    }
    .section-heading {
        padding: 22px 20px 15px;
    }
    .table-footer {
        padding: 18px 20px;
    }
    .table-tools {
        margin-left: auto;
    }
}
@media (max-width: 700px) {
    .accounts-table {
        display: none;
    }
    .mobile-accounts {
        display: grid;
        gap: 0;
    }
    .mobile-account {
        padding: 16px;
        background: var(--surface);
    }
    .mobile-account-header {
        display: flex;
        align-items: center;
        gap: 8px;
    }
    .mobile-account-header .account-identity {
        min-width: 0;
    }
    .mobile-account-header .identity-text {
        width: 100%;
    }
    .mobile-account-header .identity-text strong,
    .mobile-account-header .identity-text small {
        max-width: 100%;
    }
    .mobile-account-header > .el-tag {
        flex-shrink: 0;
    }
    .mobile-account > .account-proxy-trigger {
        min-height: 44px;
        font-size: 13px;
        margin-top: 4px;
    }
    .mobile-account-quotas {
        display: flex;
        justify-content: space-between;
        gap: 16px;
        padding: 18px 0 10px;
        font-variant-numeric: tabular-nums;
    }
    .mobile-account-quotas span,
    .mobile-account-quotas small {
        display: block;
        font-size: 11px;
        color: var(--muted);
    }
    .mobile-account-quotas strong {
        display: block;
        font-size: 24px;
        font-weight: 700;
        line-height: 1.7;
    }
    .mobile-account-quotas .quota-empty {
        color: var(--el-color-danger);
    }
    .mobile-vids-empty {
        flex: 1;
        text-align: right;
    }
    .mobile-account-actions {
        display: flex;
        align-items: center;
        gap: 6px;
        padding-top: 10px;
        margin-top: 10px;
    }
    .mobile-account-actions .el-button + .el-button {
        margin-left: 0;
    }
    .mobile-account-actions .el-button:last-child {
        margin-left: auto;
    }
    .quota-update.quota-attention {
        color: var(--el-color-warning);
    }
    .crew-metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 10px;
    }
    .crew-metric strong {
        font-size: 26px;
    }
    .crew-metric {
        padding: 16px;
    }
    .metric-top .el-icon {
        width: 29px;
        height: 29px;
    }
    .account-toolbar {
        padding: 10px 16px 18px;
    }
    .filters {
        width: 100%;
        flex: none;
    }
    .account-search {
        width: 100%;
    }
    .filters .el-select {
        width: calc(50% - 5px);
    }
    .table-tools {
        width: 100%;
        margin: 3px 0 0;
    }
    .section-heading {
        padding: 20px 16px;
    }
    .refresh-toggle {
        font-size: 13px;
        gap: 6px;
    }
    .table-footer {
        padding: 15px;
        flex-direction: column;
        align-items: flex-start;
    }
    .selection-bar {
        padding: 12px 16px;
        flex-wrap: wrap;
    }
    .selection-bar > span {
        width: 100%;
        margin-bottom: 6px;
    }
    .crew-note {
        align-items: flex-start;
    }
    .crew-note .el-button {
        display: none;
    }
    .form-grid {
        grid-template-columns: 1fr;
        gap: 0;
    }
    .detail-metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .detail-header {
        flex-wrap: wrap;
    }
    .detail-header h2 {
        max-width: 210px;
        overflow-wrap: anywhere;
    }
}

.identity-text strong {
    font-size: 14px;
}
.identity-text small,
.cell-note {
    font-size: 13px;
}
.metric-top {
    font-size: 13px;
}
.balance-text {
    font-size: 15px;
}
.capability-setting strong {
    font-size: 14px;
}
.account-identity:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 5px;
    border-radius: 10px;
}
.accounts-view .account-surface {
    padding: 0;
}
.accounts-view .section-heading {
    margin-bottom: 0;
}

html.dark .account-avatar {
    background: var(--pink-soft);
    color: var(--accent);
}
html.dark .avatar-1 {
    background: var(--blue-soft);
    color: var(--blue);
}
html.dark .avatar-2 {
    background: var(--peach-soft);
    color: var(--peach);
}
html.dark .avatar-3 {
    background: var(--green-soft);
    color: var(--success);
}
html.dark .violet .metric-top .el-icon {
    background: var(--peach-soft);
    color: var(--peach);
}
html.dark .capability-icons {
    color: var(--accent);
}
html.dark .capability-icons .muted {
    color: var(--muted);
}
html.dark .quota-error {
    color: var(--el-color-danger);
}

.empty-mascot {
    width: 96px;
    height: 96px;
    margin: 0 auto;
    border-radius: 24px;
    background: var(--pink-soft);
}
.empty-mascot :deep(img) {
    animation: none;
}
.accounts-view :deep(.el-empty__description p) {
    font-size: 14px;
}
.accounts-view :deep(.el-empty__image) {
    width: 96px;
}
@media (max-width: 700px) {
    .accounts-view > .page-heading {
        flex-direction: column;
        align-items: stretch;
    }
    .page-actions {
        justify-content: flex-start;
    }
    .selection-bar .el-button + .el-button {
        margin-left: 0;
    }
    .accounts-view .section-heading {
        align-items: flex-start;
        flex-wrap: wrap;
    }
}
@media (min-width: 1051px) {
    .accounts-view {
        display: flex;
        flex-direction: column;
        height: 100%;
        min-height: 0;
    }
    .accounts-view > .page-heading {
        margin-bottom: 12px;
        flex-shrink: 0;
    }
    .accounts-view > .page-heading p,
    .crew-note,
    .crew-metric > p,
    .crew-metric .metric-top .el-icon,
    .account-surface > .section-heading {
        display: none;
    }
    .crew-metrics {
        flex-shrink: 0;
        margin-bottom: 12px;
        gap: 12px;
    }
    .crew-metric {
        padding: 10px 14px;
        border-radius: 12px;
    }
    .crew-metric strong {
        margin-top: 3px;
        font-size: 24px;
        line-height: 1.3;
    }
    .crew-metric.violet {
        padding-block: 8px;
    }
    .crew-metric .vids-quota-total {
        margin-top: 2px;
    }
    .crew-metric .metric-quotas {
        display: flex;
        gap: 16px;
        margin-top: 3px;
    }
    .crew-metric .metric-quotas > div {
        display: flex;
        align-items: baseline;
        gap: 5px;
    }
    .crew-metric .metric-quotas strong {
        font-size: 24px;
        margin: 0;
    }
    .account-surface {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-height: 0;
    }
    .account-toolbar {
        flex-shrink: 0;
        padding: 10px 16px;
        gap: 12px;
    }
    .account-search {
        width: 210px;
    }
    .filters {
        gap: 8px;
        flex-wrap: nowrap;
    }
    .filters > .el-select {
        width: 110px;
    }
    .table-tools .el-button {
        padding-inline: 10px;
    }
    .account-surface > .accounts-table {
        flex: 1;
        min-height: 0;
    }
    .accounts-table :deep(.el-table__inner-wrapper) {
        height: 100%;
    }
    .accounts-table :deep(.el-table__body-wrapper) {
        flex: 1;
        min-height: 0;
    }
    .table-footer {
        flex-shrink: 0;
        padding: 9px 16px;
        gap: 10px;
    }
    .table-footer > span {
        font-size: 11px;
    }
    .profile-import-jobs {
        max-height: 135px;
        overflow: auto;
        flex-shrink: 0;
        margin-bottom: 12px;
        scrollbar-width: thin;
    }
}
</style>
