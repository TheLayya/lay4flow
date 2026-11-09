<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { RouterLink, useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
    Key,
    MagicStick,
    FolderOpened,
    Refresh,
    CopyDocument,
    Monitor,
    Tools,
    Search,
    Document,
} from '@element-plus/icons-vue'
import { api, notifyError, parseDate } from '../lib/api'
import { CAPTCHA_SERVICE_URL } from '../lib/captcha-service'

const group = ref('general')
const route = useRoute()
const captchaKeyInput = ref(null)
const settingsQuery = ref('')
const groups = [
    { id: 'general', label: '基础与访问', sub: '账号 · API 密钥', icon: Key },
    { id: 'generation', label: '生成与调度', sub: '超时 · 轮询 · 刷新', icon: MagicStick },
    { id: 'rules', label: '自定义规则', sub: 'Flow · Vids 维护与保护', icon: Document },
    { id: 'captcha', label: '远程有头打码', sub: '服务地址 · 密钥 · 超时', icon: Monitor },
    { id: 'cache', label: '媒体缓存', sub: '保存时间 · 访问地址', icon: FolderOpened },
    { id: 'integration', label: '运行诊断', sub: '更新记录 · 调试日志', icon: Tools },
]
const settingCards = [
    { id: 'api', section: 'general', terms: 'API 访问密钥 当前 API 密钥 新 API 密钥 key 外部客户端' },
    { id: 'login', section: 'general', terms: '控制台登录 管理员名称 当前密码 新密码 确认新密码 账号' },
    { id: 'timeouts', section: 'generation', terms: '任务等待时间 图片生成超时 视频生成超时 最大重试次数 秒' },
    { id: 'dispatch', section: 'generation', terms: '账号调度 默认调度 轮询调度 并发' },
    {
        id: 'errorPolicy',
        section: 'rules',
        terms: '生成错误与账号保护 自定义规则 错误关键字 限速 配额 连续失败 冷却 换号 重试 LIMIT DEACTIVE RETRY DISABLE DELAY PUBLIC_ERROR_USER_QUOTA_REACHED RESOURCE_EXHAUSTED',
    },
    {
        id: 'refresh',
        section: 'generation',
        terms: 'Profile 登录态维护 每账号 自动维护 会话维护间隔 分钟 Google Cookie 后台浏览器',
    },
    {
        id: 'vidsConfig',
        section: 'rules',
        terms: 'Vids 会话维护 账号保护 保活 维护间隔 修复退避 连续失败 阈值 冷却 自定义规则 LIMIT DEACTIVE DISABLE',
    },
    {
        id: 'captcha',
        section: 'captcha',
        terms: '远程有头打码 验证码 远程服务地址 API 密钥 申请 请求超时 remote browser',
    },
    { id: 'cache', section: 'cache', terms: '作品媒体缓存 缓存保留时间 秒 公开访问地址 当前生效地址 图片 视频' },
    { id: 'release', section: 'integration', terms: '更新记录 当前版本 版本日志 Docker Windows 安装 更新' },
    { id: 'debug', section: 'integration', terms: '运行诊断 启用详细调试日志 logs.txt' },
]
const matchingCards = computed(() =>
    settingCards.filter((card) => {
        const query = settingsQuery.value.trim().toLowerCase()
        return (
            !query ||
            `${groups.find((item) => item.id === card.section)?.label} ${card.terms}`.toLowerCase().includes(query)
        )
    }),
)
const cardVisible = (id) => matchingCards.value.some((card) => card.id === id)
const sectionVisible = (id) =>
    (settingsQuery.value.trim() || group.value === id) && matchingCards.value.some((card) => card.section === id)
function jumpToSection(id) {
    settingsQuery.value = ''
    group.value = groups.some((item) => item.id === id) ? id : 'general'
}
const loading = ref(true)
const failures = ref([])
const loaded = reactive(new Set())
const snapshots = reactive({})
const busy = reactive(new Set())
const savedAt = reactive({})
const admin = reactive({ admin_username: '', api_key: '', debug_enabled: false })
const password = reactive({ username: '', old_password: '', new_password: '', confirm_password: '' })
const newKey = ref('')
const generation = reactive({ image_timeout: 300, video_timeout: 1500, max_retries: 3 })
const defaultAccountErrorRules = [
    'PUBLIC_ERROR_MODEL_ACCESS_DENIED|LIMIT|0s',
    'PUBLIC_ERROR_USER_QUOTA_REACHED|LIMIT|24h',
    'Resource has been exhausted (e.g. check quota).|LIMIT|1s',
    'PUBLIC_ERROR_USER_REQUESTS_THROTTLED|LIMIT|3s',
    'RESOURCE_EXHAUSTED|LIMIT|5s',
].join('\n')
const accountErrorPolicy = reactive({
    rules: defaultAccountErrorRules,
    consecutive_failure_threshold: 3,
    failure_cooldown_seconds: 300,
})
const accountErrorPolicyError = ref('')
const vidsConfig = reactive({
    keepalive_enabled: true,
    keepalive_interval_minutes: 10,
    repair_cooldown_seconds: 300,
    consecutive_failure_threshold: 3,
    failure_cooldown_seconds: 300,
    rules: '',
})
const vidsConfigError = ref('')
const callLogic = reactive({ call_mode: 'default' })
const cache = reactive({ enabled: false, timeout: 7200, base_url: '', effective_base_url: '' })
const captcha = reactive({
    captcha_method: 'remote_browser',
    remote_browser_base_url: CAPTCHA_SERVICE_URL,
    remote_browser_api_key: '',
    remote_browser_timeout: 60,
})
const release = reactive({
    project: 'lay4flow-remote',
    version: '—',
    release_date: '',
    entries: [],
    release_page: 'https://github.com/TheLayya/lay4flow/releases',
    update_docs: { windows: '/docs/WINDOWS_INSTALL.md', docker: '/docs/DOCKER_UPDATE.md' },
})
const releaseCheck = reactive({ loading: false, checked_at: '', status: '', message: '' })
let loadRequest
const configSources = [
    {
        id: 'admin',
        path: '/api/admin/config',
        target: admin,
        readOnly: true,
        map: (data) => ({
            admin_username: data.admin_username,
            api_key: data.api_key,
            debug_enabled: data.debug_enabled,
        }),
    },
    { id: 'generation', path: '/api/config/generation', target: generation, wrap: true },
    {
        id: 'errorPolicy',
        path: '/api/account-error-policy',
        target: accountErrorPolicy,
        map: (data) => accountErrorPolicyPayload({ ...data.config, rules: data.config.rules ?? defaultAccountErrorRules }),
    },
    {
        id: 'vidsConfig',
        path: '/api/admin/vids/config',
        target: vidsConfig,
        map: (data) => vidsConfigPayload({ ...vidsConfig, ...data.config }),
    },
    { id: 'callLogic', path: '/api/call-logic/config', target: callLogic, wrap: true },
    { id: 'cache', path: '/api/cache/config', target: cache, wrap: true },
    {
        id: 'captcha',
        path: '/api/captcha/config',
        target: captcha,
        map: (data) => ({
            captcha_method: 'remote_browser',
            remote_browser_base_url: CAPTCHA_SERVICE_URL,
            remote_browser_api_key: data.remote_browser_api_key || '',
            remote_browser_timeout: data.remote_browser_timeout || 60,
        }),
    },
]
const string = (value) => JSON.stringify(value)
function accountErrorPolicyPayload(value = accountErrorPolicy) {
    return {
        rules: value.rules,
        consecutive_failure_threshold: value.consecutive_failure_threshold,
        failure_cooldown_seconds: value.failure_cooldown_seconds,
    }
}
function fillAccountRuleExamples() {
    accountErrorPolicy.rules = defaultAccountErrorRules
    accountErrorPolicyError.value = ''
}
function vidsConfigPayload(value = vidsConfig) {
    return {
        keepalive_enabled: value.keepalive_enabled,
        keepalive_interval_minutes: value.keepalive_interval_minutes,
        repair_cooldown_seconds: value.repair_cooldown_seconds,
        consecutive_failure_threshold: value.consecutive_failure_threshold,
        failure_cooldown_seconds: value.failure_cooldown_seconds,
        rules: value.rules,
    }
}
const tracked = (id, value) => id === 'errorPolicy' ? accountErrorPolicyPayload(value) : id === 'vidsConfig' ? vidsConfigPayload(value) : value
function markSnapshot(id, value) {
    snapshots[id] = string(tracked(id, value))
}
function dirty(id, value) {
    return snapshots[id] !== undefined && snapshots[id] !== string(tracked(id, value))
}
const dirtyCount = computed(
    () =>
        configSources.filter((item) => !item.readOnly && dirty(item.id, item.target)).length +
        Number(dirty('debug', { enabled: admin.debug_enabled })) +
        Number(!!newKey.value.trim()) +
        Number(
            Object.entries(password).some(([key, value]) =>
                key === 'username' ? !!value && value !== admin.admin_username : !!value,
            ),
        ),
)
async function reloadSettings() {
    if (dirtyCount.value) {
        try {
            await ElMessageBox.confirm('重新读取会覆盖本页尚未保存的修改。', '重新读取设置', {
                confirmButtonText: '重新读取',
                cancelButtonText: '继续编辑',
                type: 'warning',
            })
        } catch {
            return
        }
    }
    await load()
}
function ensure(result) {
    if (result?.success === false) throw new Error(result.message || '配置保存未完成')
    return result
}
const date = (value) => (value ? parseDate(value).toLocaleString('zh-CN', { hour12: false }) : '尚未检测')
async function load() {
    if (loadRequest) return loadRequest
    loadRequest = readSettings().finally(() => {
        loadRequest = null
    })
    return loadRequest
}
async function readSettings() {
    loading.value = true
    failures.value = []
    loaded.clear()
    const results = await Promise.allSettled(configSources.map((item) => api.get(item.path)))
    results.forEach((result, index) => {
        const item = configSources[index]
        if (result.status === 'fulfilled') {
            Object.assign(
                item.target,
                item.map ? item.map(result.value) : item.wrap ? result.value.config : result.value,
            )
            markSnapshot(item.id, item.target)
            loaded.add(item.id)
        } else
            failures.value.push(
                `${groups.find((row) => row.id === item.id)?.label || item.id}: ${result.reason.message}`,
            )
    })
    password.username = admin.admin_username
    if (loaded.has('admin')) {
        loaded.add('debug')
        markSnapshot('debug', { enabled: admin.debug_enabled })
    }
    loading.value = false
    await loadRelease()
}

async function loadRelease() {
    try {
        Object.assign(release, await api.get('/api/admin/release'))
    } catch (error) {
        // Older servers may not have the release endpoint yet.
        releaseCheck.message = '当前服务未提供版本记录接口，请查看仓库更新记录。'
    }
}

async function checkRelease() {
    if (releaseCheck.loading) return
    releaseCheck.loading = true
    try {
        const result = await api.get('/api/admin/release/check')
        Object.assign(releaseCheck, result)
        if (result.status === 'update_available') ElMessage.warning(result.message)
        else ElMessage.success(result.message || '版本检查完成')
    } catch (error) {
        releaseCheck.message = '线上版本暂时不可查询，已保留本地更新记录。'
        notifyError(error)
    } finally {
        releaseCheck.loading = false
    }
}
function openReleaseHistory() {
    window.dispatchEvent(new Event('open-release-history'))
}
async function save(id, path, body, target = body) {
    if (busy.has(id)) return
    if (!loaded.has(id)) return ElMessage.warning('请先成功读取此组配置，再保存修改')
    const submittedBody = JSON.parse(JSON.stringify(body))
    const submittedTarget = JSON.parse(JSON.stringify(target))
    busy.add(id)
    try {
        const result = ensure(await api.post(path, submittedBody))
        markSnapshot(id, submittedTarget)
        savedAt[id] = new Date().toLocaleTimeString('zh-CN', { hour12: false })
        ElMessage.success('这组配置已保存')
        return result
    } catch (error) {
        notifyError(error)
    } finally {
        busy.delete(id)
    }
}
async function savePassword() {
    if (!password.old_password || password.new_password.length < 4)
        return ElMessage.warning('请填写当前密码，新密码至少 4 个字符')
    if (password.new_password !== password.confirm_password) return ElMessage.warning('两次新密码需要一致')
    busy.add('password')
    try {
        ensure(
            await api.post('/api/admin/password', {
                username: password.username.trim() || undefined,
                old_password: password.old_password,
                new_password: password.new_password,
            }),
        )
        localStorage.removeItem('token')
        ElMessage.success('登录信息已更新，请重新登录')
        setTimeout(() => {
            location.href = '/login'
        }, 1200)
    } catch (error) {
        notifyError(error)
    } finally {
        busy.delete('password')
    }
}
async function saveKey() {
    if (busy.has('apiKey')) return
    if (newKey.value.trim().length < 6) return ElMessage.warning('API 密钥至少 6 个字符')
    try {
        await ElMessageBox.confirm('更新后，调用 API 的客户端需要使用新密钥。', '更新 API 密钥', {
            confirmButtonText: '更新密钥',
            cancelButtonText: '取消',
            type: 'warning',
        })
    } catch {
        return
    }
    const submittedKey = newKey.value.trim()
    busy.add('apiKey')
    try {
        ensure(await api.post('/api/admin/apikey', { new_api_key: submittedKey }))
        admin.api_key = submittedKey
        if (newKey.value.trim() === submittedKey) newKey.value = ''
        ElMessage.success('API 密钥已更新')
    } catch (error) {
        notifyError(error)
    } finally {
        busy.delete('apiKey')
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
async function saveCache() {
    if (cache.base_url && !/^https?:\/\//.test(cache.base_url))
        return ElMessage.warning('访问地址需要以 http:// 或 https:// 开头')
    const submitted = { ...cache }
    const result = await save(
        'cache',
        '/api/cache/config',
        { enabled: submitted.enabled, timeout: submitted.timeout, base_url: submitted.base_url.trim() },
        submitted,
    )
    if (result) {
        cache.effective_base_url = submitted.base_url.trim() || 'http://127.0.0.1:8000'
        markSnapshot('cache', { ...submitted, effective_base_url: cache.effective_base_url })
    }
}
async function saveCaptcha() {
    if (!captcha.remote_browser_api_key.trim()) return ElMessage.warning('请先申请打码并填写 API 密钥')
    const submitted = {
        ...captcha,
        remote_browser_base_url: CAPTCHA_SERVICE_URL,
        remote_browser_api_key: captcha.remote_browser_api_key.trim(),
    }
    const result = await save('captcha', '/api/captcha/config', submitted, submitted)
    if (result) {
        if (captcha.remote_browser_api_key.trim() === submitted.remote_browser_api_key)
            captcha.remote_browser_api_key = submitted.remote_browser_api_key
        window.dispatchEvent(new Event('captcha-config-saved'))
    }
}
function validateAccountErrorPolicy(value) {
    const bounds = [
        ['consecutive_failure_threshold', 1, 100, '连续失败保护阈值'],
        ['failure_cooldown_seconds', 1, 86400, '连续失败冷却时间'],
    ]
    for (const [field, min, max, label] of bounds) {
        if (!Number.isInteger(value[field]) || value[field] < min || value[field] > max)
            return `${label}需为 ${min}–${max} 之间的整数`
    }
    return ''
}
async function saveAccountErrorPolicy() {
    if (busy.has('errorPolicy')) return
    if (!loaded.has('errorPolicy')) return ElMessage.warning('请先成功读取此组配置，再保存修改')
    const submitted = accountErrorPolicyPayload()
    accountErrorPolicyError.value = validateAccountErrorPolicy(submitted)
    if (accountErrorPolicyError.value) return
    busy.add('errorPolicy')
    try {
        ensure(await api.post('/api/account-error-policy', submitted))
        markSnapshot('errorPolicy', submitted)
        savedAt.errorPolicy = new Date().toLocaleTimeString('zh-CN', { hour12: false })
        ElMessage.success('错误规则与账号保护已保存')
    } catch (error) {
        accountErrorPolicyError.value = error.message || '规则保存失败，请检查后重试'
        notifyError(error)
    } finally {
        busy.delete('errorPolicy')
    }
}
async function saveVidsConfig() {
    if (busy.has('vidsConfig')) return
    if (!loaded.has('vidsConfig')) return ElMessage.warning('请先成功读取 Vids 配置，再保存修改')
    const submitted = vidsConfigPayload()
    const bounds = [
        ['keepalive_interval_minutes', 1, 1440, 'Vids 维护间隔'],
        ['repair_cooldown_seconds', 30, 86400, 'Vids 修复退避时间'],
        ['consecutive_failure_threshold', 1, 100, 'Vids 连续失败保护阈值'],
        ['failure_cooldown_seconds', 1, 86400, 'Vids 连续失败冷却时间'],
    ]
    vidsConfigError.value = ''
    for (const [field, min, max, label] of bounds) {
        if (!Number.isInteger(submitted[field]) || submitted[field] < min || submitted[field] > max) {
            vidsConfigError.value = `${label}需为 ${min}–${max} 之间的整数`
            return
        }
    }
    busy.add('vidsConfig')
    try {
        ensure(await api.post('/api/admin/vids/config', submitted))
        markSnapshot('vidsConfig', submitted)
        savedAt.vidsConfig = new Date().toLocaleTimeString('zh-CN', { hour12: false })
        ElMessage.success('Vids 会话维护与账号保护已保存')
    } catch (error) {
        vidsConfigError.value = error.message || 'Vids 配置保存失败，请检查后重试'
        notifyError(error)
    } finally {
        busy.delete('vidsConfig')
    }
}
watch(
    [() => route.query.section, loading],
    async ([section, isLoading]) => {
        if (section !== 'captcha' || isLoading) return
        settingsQuery.value = ''
        group.value = 'captcha'
        await nextTick()
        captchaKeyInput.value?.focus()
        document.getElementById('settings-captcha')?.scrollIntoView({ block: 'start' })
    },
    { immediate: true },
)
onMounted(load)
</script>

<template>
    <div class="settings-view">
        <header class="page-heading">
            <div>
                <h1>工作室设置</h1>
            </div>
            <div class="page-actions">
                <el-tag v-if="dirtyCount" type="warning" effect="light">{{ dirtyCount }} 组待保存</el-tag>
                <el-button :icon="Refresh" :loading="loading" @click="reloadSettings">重新读取</el-button>
            </div>
        </header>
        <el-alert
            v-if="failures.length"
            title="部分配置读取失败，请重新读取后再保存对应配置"
            :description="failures.join('；')"
            type="warning"
            show-icon
            :closable="false"
            class="settings-alert"
        />
        <div class="settings-toolbar">
            <el-input
                v-model="settingsQuery"
                class="settings-search"
                :prefix-icon="Search"
                clearable
                placeholder="搜索设置、字段或服务，如：超时、代理、密钥"
                aria-label="搜索设置"
            />
            <nav class="settings-nav" aria-label="设置分区" role="tablist">
                <button
                    v-for="item in groups"
                    :key="item.id"
                    role="tab"
                    :aria-selected="group === item.id && !settingsQuery.trim()"
                    :aria-controls="`settings-${item.id}`"
                    :class="{ active: group === item.id }"
                    @click="jumpToSection(item.id)"
                >
                    <el-icon><component :is="item.icon" /></el-icon>
                    {{ item.label }}
                </button>
            </nav>
            <span class="settings-match-count">
                {{ settingsQuery.trim() ? `${matchingCards.length} 项匹配` : '' }}
            </span>
        </div>
        <div class="settings-layout">
            <main class="settings-main">
                <el-skeleton v-if="loading" :rows="10" animated class="surface settings-skeleton" />
                <template v-else>
                    <el-empty
                        v-if="!matchingCards.length"
                        description="没有匹配设置，试试其他字段或服务名称"
                        :image-size="64"
                    />
                    <section
                        v-show="sectionVisible('general')"
                        id="settings-general"
                        class="settings-section"
                        data-section="general"
                    >
                        <header class="settings-section-title">
                            <el-icon><component :is="groups.find((item) => item.id === 'general').icon" /></el-icon>
                            <h2>{{ groups.find((item) => item.id === 'general').label }}</h2>
                            <p>{{ groups.find((item) => item.id === 'general').sub }}</p>
                        </header>
                        <div class="settings-grid">
                            <section v-show="cardVisible('api')" data-setting-card="api" class="surface settings-card">
                                <div class="card-heading">
                                    <div>
                                        <h3>API 访问密钥</h3>
                                        <p>外部客户端在这里设置密钥；本工作台登录后可直接创作。</p>
                                    </div>
                                    <el-tag type="info" size="small" effect="plain">访问凭证</el-tag>
                                </div>
                                <el-form label-position="top">
                                    <el-form-item label="当前 API 密钥">
                                        <div class="input-with-action">
                                            <el-input
                                                :model-value="admin.api_key"
                                                type="password"
                                                show-password
                                                readonly
                                                autocomplete="off"
                                            />
                                            <el-tooltip content="复制当前密钥">
                                                <el-button
                                                    :icon="CopyDocument"
                                                    aria-label="复制当前 API 密钥"
                                                    @click="copy(admin.api_key)"
                                                    :disabled="!admin.api_key"
                                                />
                                            </el-tooltip>
                                        </div>
                                    </el-form-item>
                                    <el-form-item label="新 API 密钥">
                                        <el-input
                                            v-model="newKey"
                                            type="password"
                                            show-password
                                            autocomplete="new-password"
                                            placeholder="填写新的 API 密钥，至少 6 个字符"
                                        />
                                    </el-form-item>
                                </el-form>
                                <div class="save-row">
                                    <span>密钥默认隐藏，点眼睛查看</span>
                                    <el-button
                                        type="primary"
                                        :loading="busy.has('apiKey')"
                                        :disabled="!loaded.has('admin') || !newKey"
                                        @click="saveKey"
                                    >
                                        更新访问密钥
                                    </el-button>
                                </div>
                            </section>
                            <section
                                v-show="cardVisible('login')"
                                data-setting-card="login"
                                class="surface settings-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>控制台登录</h3>
                                        <p>更新管理员名称与登录密码</p>
                                    </div>
                                </div>
                                <el-form label-position="top">
                                    <el-form-item label="管理员名称">
                                        <el-input v-model="password.username" autocomplete="username" />
                                    </el-form-item>
                                    <el-form-item label="当前密码">
                                        <el-input
                                            v-model="password.old_password"
                                            type="password"
                                            show-password
                                            autocomplete="current-password"
                                        />
                                    </el-form-item>
                                    <div class="settings-form-grid">
                                        <el-form-item label="新密码">
                                            <el-input
                                                v-model="password.new_password"
                                                type="password"
                                                show-password
                                                autocomplete="new-password"
                                                placeholder="至少 4 个字符"
                                            />
                                        </el-form-item>
                                        <el-form-item label="确认新密码">
                                            <el-input
                                                v-model="password.confirm_password"
                                                type="password"
                                                show-password
                                                autocomplete="new-password"
                                            />
                                        </el-form-item>
                                    </div>
                                </el-form>
                                <div class="save-row">
                                    <span>更新登录信息后需要重新登录</span>
                                    <el-button :loading="busy.has('password')" @click="savePassword">
                                        更新登录信息
                                    </el-button>
                                </div>
                            </section>
                        </div>
                    </section>
                    <section
                        v-show="sectionVisible('generation')"
                        id="settings-generation"
                        class="settings-section"
                        data-section="generation"
                    >
                        <header class="settings-section-title">
                            <el-icon><component :is="groups.find((item) => item.id === 'generation').icon" /></el-icon>
                            <h2>{{ groups.find((item) => item.id === 'generation').label }}</h2>
                            <p>{{ groups.find((item) => item.id === 'generation').sub }}</p>
                        </header>
                        <div class="settings-grid">
                            <section
                                v-show="cardVisible('timeouts')"
                                data-setting-card="timeouts"
                                class="surface settings-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>任务等待时间</h3>
                                        <p>为每一种创作保留合适的完成时间</p>
                                    </div>
                                    <el-tag v-if="dirty('generation', generation)" type="warning" size="small">
                                        未保存
                                    </el-tag>
                                </div>
                                <el-form label-position="top">
                                    <div class="settings-form-grid">
                                        <el-form-item label="图片生成超时（秒）">
                                            <el-input-number
                                                v-model="generation.image_timeout"
                                                :min="60"
                                                :max="3600"
                                                :step="30"
                                                controls-position="right"
                                            />
                                            <span class="field-note">建议 300 秒，支持 60–3600 秒</span>
                                        </el-form-item>
                                        <el-form-item label="视频生成超时（秒）">
                                            <el-input-number
                                                v-model="generation.video_timeout"
                                                :min="60"
                                                :max="7200"
                                                :step="60"
                                                controls-position="right"
                                            />
                                            <span class="field-note">建议 1500 秒，支持 60–7200 秒</span>
                                        </el-form-item>
                                    </div>
                                    <el-form-item label="最大尝试次数（含首次）">
                                        <el-input-number
                                            v-model="generation.max_retries"
                                            :min="1"
                                            :max="20"
                                            controls-position="right"
                                        />
                                        <span class="field-note">
                                            每份作品的统一总上限，包含首次、验证码失败、同账号重试和换号尝试；保存后对新任务生效
                                        </span>
                                    </el-form-item>
                                </el-form>
                                <div class="save-row">
                                    <span>
                                        {{
                                            savedAt.generation ? '已保存于 ' + savedAt.generation : '调整后对新任务生效'
                                        }}
                                    </span>
                                    <el-button
                                        type="primary"
                                        :loading="busy.has('generation')"
                                        :disabled="!loaded.has('generation')"
                                        @click="
                                            save('generation', '/api/config/generation', { ...generation }, generation)
                                        "
                                    >
                                        保存生成配置
                                    </el-button>
                                </div>
                            </section>
                            <section
                                v-show="cardVisible('dispatch')"
                                data-setting-card="dispatch"
                                class="surface settings-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>账号调度</h3>
                                        <p>选择每个创作请求分配账号的方式</p>
                                    </div>
                                    <el-tag v-if="dirty('callLogic', callLogic)" type="warning" size="small">
                                        未保存
                                    </el-tag>
                                </div>
                                <el-radio-group v-model="callLogic.call_mode" class="mode-options">
                                    <el-radio value="default" border>
                                        <strong>默认调度</strong>
                                        <span>优先低负载；相同负载按会话就绪、剩余槽位与最久未使用排序</span>
                                    </el-radio>
                                    <el-radio value="polling" border>
                                        <strong>轮询调度</strong>
                                        <span>按顺序轮换可用的创作账号</span>
                                    </el-radio>
                                </el-radio-group>
                                <div class="save-row">
                                    <span>
                                        {{
                                            savedAt.callLogic
                                                ? '已保存于 ' + savedAt.callLogic
                                                : '两种模式均跳过冷却、异常、停用及本次已失败的账号'
                                        }}
                                    </span>
                                    <el-button
                                        type="primary"
                                        :loading="busy.has('callLogic')"
                                        :disabled="!loaded.has('callLogic')"
                                        @click="
                                            save(
                                                'callLogic',
                                                '/api/call-logic/config',
                                                { call_mode: callLogic.call_mode },
                                                callLogic,
                                            )
                                        "
                                    >
                                        保存调度方式
                                    </el-button>
                                </div>
                            </section>
                            <section
                                v-show="cardVisible('refresh')"
                                data-setting-card="refresh"
                                class="surface settings-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>Profile 登录态维护</h3>
                                        <p>按每个账号的自动维护开关和会话维护间隔执行，已有账号沿用各自设置。</p>
                                    </div>
                                </div>
                                <p class="field-note">新建 Profile 账号默认每 30 分钟维护一次；已有账号按各自保存的间隔执行。后台每分钟扫描到期账号。</p>
                                <p class="field-note">日常优先通过 HTTP 校验会话；到维护时间后由后台无头浏览器访问 Flow 续期并正常关闭。Google 登录已失效时需重新登录。</p>
                                <div class="save-row">
                                    <span>在账号管理中修改单个账号的维护开关和间隔。</span>
                                    <RouterLink to="/accounts" class="el-button">管理账号维护设置</RouterLink>
                                </div>
                            </section>
                        </div>
                    </section>
                    <section
                        v-show="sectionVisible('rules')"
                        id="settings-rules"
                        class="settings-section"
                        data-section="rules"
                    >
                        <div class="settings-grid">
                            <section
                                v-show="cardVisible('errorPolicy')"
                                data-setting-card="errorPolicy"
                                class="surface settings-card error-policy-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>Flow 自定义规则</h3>
                                        <p>每行一条规则，按顺序匹配；修改、删除或清空后保存即可生效。</p>
                                    </div>
                                    <el-tag v-if="dirty('errorPolicy', accountErrorPolicy)" type="warning" size="small">
                                        未保存
                                    </el-tag>
                                </div>
                                <el-form label-position="top" @submit.prevent="saveAccountErrorPolicy">
                                    <el-form-item label="自定义错误规则" :error="accountErrorPolicyError">
                                        <el-input
                                            v-model="accountErrorPolicy.rules"
                                            type="textarea"
                                            :rows="6"
                                            :maxlength="65536"
                                            placeholder="每行一条：报错关键字|操作|时间，例如 PUBLIC_ERROR_USER_QUOTA_REACHED|LIMIT|24h"
                                            class="error-policy-input" spellcheck="false"
                                            aria-label="自定义错误规则"
                                            :disabled="!loaded.has('errorPolicy')"
                                            @input="accountErrorPolicyError = ''"
                                        />
                                        <span class="field-note">
                                            格式：关键字|操作|时间。不区分大小写，首条匹配生效；清空后不使用关键词规则。
                                        </span>
                                    </el-form-item>
                                    <div class="error-policy-actions" aria-label="错误规则操作说明">
                                        <div><code>LIMIT|24h</code><span>账号冷却指定时间并换号；<code>LIMIT|0s</code> 只在本次请求中跳过该账号。</span></div>
                                        <div><code>DEACTIVE</code><span>标记账号异常并换号，需要处理异常后恢复。</span></div>
                                        <div><code>RETRY</code><span>不修改账号状态，直接重试。</span></div>
                                        <div><code>DISABLE</code><span>停用账号并换号，需要手动启用。</span></div>
                                        <div><code>DELAY|10s</code><span>等待指定时间后重试本次请求，最多 300 秒。</span></div>
                                    </div>
                                    <p class="field-note error-policy-units">时间使用 s、m、h，例如 3s、5m、24h；LIMIT 最长 7 天。</p>
                                    <div class="error-policy-example-row">
                                        <span class="field-note">示例只是可编辑内容，不会在删除后隐式生效。</span>
                                        <el-button text :disabled="!loaded.has('errorPolicy')" @click="fillAccountRuleExamples">填入示例规则</el-button>
                                    </div>
                                    <div class="settings-form-grid error-policy-limits">
                                        <el-form-item label="连续失败保护阈值">
                                            <el-input-number
                                                v-model="accountErrorPolicy.consecutive_failure_threshold"
                                                :min="1"
                                                :max="100"
                                                controls-position="right"
                                            />
                                            <span class="field-note">同一账号连续失败达到此次数后暂停分配；成功会清零。</span>
                                        </el-form-item>
                                        <el-form-item label="连续失败冷却时间（秒）">
                                            <el-input-number
                                                v-model="accountErrorPolicy.failure_cooldown_seconds"
                                                :min="1"
                                                :max="86400"
                                                controls-position="right"
                                            />
                                            <span class="field-note">保护冷却到期后可重新参与调度。</span>
                                        </el-form-item>
                                    </div>
                                </el-form>
                                <p class="field-note error-policy-safety">所有账号共用「生成与调度」中的最大尝试次数，不再单独限制账号分配轮数。仅对明确未受理的请求自动重试；已受理或提交结果未知时不重复生成。没有可用账号时会提前结束并显示原因。</p>
                                <div class="save-row">
                                    <span>{{ savedAt.errorPolicy ? '已保存于 ' + savedAt.errorPolicy : '保存后对新请求生效' }}</span>
                                    <el-button
                                        type="primary"
                                        :loading="busy.has('errorPolicy')"
                                        :disabled="!loaded.has('errorPolicy')"
                                        @click="saveAccountErrorPolicy"
                                    >
                                        保存错误规则
                                    </el-button>
                                </div>
                            </section>
                            <section
                                v-show="cardVisible('vidsConfig')"
                                data-setting-card="vidsConfig"
                                class="surface settings-card error-policy-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>Vids 会话维护与账号保护</h3>
                                        <p>独立维护已绑定的 Vids 账号，保护状态和统计单独记录。</p>
                                    </div>
                                    <el-tag v-if="dirty('vidsConfig', vidsConfig)" type="warning" size="small">未保存</el-tag>
                                </div>
                                <el-form label-position="top" :disabled="!loaded.has('vidsConfig')" @submit.prevent="saveVidsConfig">
                                    <el-form-item label="后台维护已绑定 Vids 会话">
                                        <el-switch v-model="vidsConfig.keepalive_enabled" aria-label="后台维护已绑定 Vids 会话" />
                                        <span class="field-note">空闲且启用的账号会定期校验会话；会话失效时按退避时间尝试修复。</span>
                                    </el-form-item>
                                    <div class="settings-form-grid error-policy-limits">
                                        <el-form-item label="Vids 维护间隔（分钟）">
                                            <el-input-number v-model="vidsConfig.keepalive_interval_minutes" :min="1" :max="1440" controls-position="right" />
                                        </el-form-item>
                                        <el-form-item label="Vids 修复退避时间（秒）">
                                            <el-input-number v-model="vidsConfig.repair_cooldown_seconds" :min="30" :max="86400" controls-position="right" />
                                            <span class="field-note">连续修复失败时逐步延长等待，避免频繁打开登录会话。</span>
                                        </el-form-item>
                                        <el-form-item label="Vids 连续失败保护阈值">
                                            <el-input-number v-model="vidsConfig.consecutive_failure_threshold" :min="1" :max="100" controls-position="right" />
                                            <span class="field-note">明确属于账号的生成失败达到阈值后暂停分配；生成成功会清零。</span>
                                        </el-form-item>
                                        <el-form-item label="Vids 连续失败冷却时间（秒）">
                                            <el-input-number v-model="vidsConfig.failure_cooldown_seconds" :min="1" :max="86400" controls-position="right" />
                                            <span class="field-note">冷却到期后可重新分配，也可在账号详情中手动解除保护。</span>
                                        </el-form-item>
                                    </div>
                                    <el-form-item label="Vids 自定义错误规则" :error="vidsConfigError">
                                        <el-input
                                            v-model="vidsConfig.rules"
                                            type="textarea" :rows="4" :maxlength="65536"
                                            placeholder="每行一条：报错关键字|操作|时间，例如 upstream_http_429|LIMIT|5m"
                                            class="error-policy-input" spellcheck="false"
                                            aria-label="Vids 自定义错误规则"
                                            @input="vidsConfigError = ''"
                                        />
                                        <span class="field-note">按顺序匹配，清空后不使用关键词规则。仅支持 LIMIT、DEACTIVE、DISABLE。</span>
                                    </el-form-item>
                                    <div class="error-policy-actions" aria-label="Vids 错误规则操作说明">
                                        <div><code>LIMIT|5m</code><span>暂停该账号的 Vids 新任务，冷却到期后恢复；时间可用 s、m、h，最长 7 天。</span></div>
                                        <div><code>DEACTIVE</code><span>标记 Vids 账号异常，处理后手动解除保护。</span></div>
                                        <div><code>DISABLE</code><span>停用 Vids 新任务，手动解除保护后恢复。</span></div>
                                    </div>
                                </el-form>
                                <p class="field-note error-policy-safety">已受理、结果未知和下载失败会保留原任务记录；维护、查询和下载异常不计入账号连续失败。</p>
                                <div class="save-row">
                                    <span>{{ savedAt.vidsConfig ? '已保存于 ' + savedAt.vidsConfig : '保存后对后续维护和新请求生效' }}</span>
                                    <el-button type="primary" :loading="busy.has('vidsConfig')" :disabled="!loaded.has('vidsConfig')" @click="saveVidsConfig">保存 Vids 配置</el-button>
                                </div>
                            </section>
                        </div>
                    </section>
                    <section
                        v-show="sectionVisible('captcha')"
                        id="settings-captcha"
                        class="settings-section"
                        data-section="captcha"
                    >
                        <header class="settings-section-title">
                            <el-icon><component :is="groups.find((item) => item.id === 'captcha').icon" /></el-icon>
                            <h2>{{ groups.find((item) => item.id === 'captcha').label }}</h2>
                            <p>{{ groups.find((item) => item.id === 'captcha').sub }}</p>
                        </header>
                        <div class="settings-grid">
                            <section
                                v-show="cardVisible('captcha')"
                                data-setting-card="captcha"
                                class="surface settings-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>远程有头打码</h3>
                                        <p>Flow 图片与视频创作必需；打码出口由远程服务管理。</p>
                                    </div>
                                    <el-tag v-if="dirty('captcha', captcha)" type="warning" size="small">未保存</el-tag>
                                </div>
                                <ol class="captcha-steps" aria-label="打码服务配置指引">
                                    <li>
                                        <a :href="CAPTCHA_SERVICE_URL" target="_blank" rel="noopener noreferrer">
                                            在这里申请打码 ↗
                                        </a>
                                    </li>
                                    <li>在打码服务中复制 API 密钥</li>
                                    <li>填写下方密钥并保存</li>
                                </ol>
                                <el-form label-position="top">
                                    <el-form-item label="远程服务地址">
                                        <div class="captcha-service-address">
                                            <a :href="CAPTCHA_SERVICE_URL" target="_blank" rel="noopener noreferrer">
                                                {{ CAPTCHA_SERVICE_URL }}
                                            </a>
                                            <el-button :icon="CopyDocument" @click="copy(CAPTCHA_SERVICE_URL)">
                                                复制地址
                                            </el-button>
                                        </div>
                                    </el-form-item>
                                    <el-form-item label="远程服务 API 密钥">
                                        <el-input
                                            ref="captchaKeyInput"
                                            v-model="captcha.remote_browser_api_key"
                                            type="text"
                                            placeholder="申请后，将 API 密钥粘贴到这里"
                                            autocomplete="off"
                                        />
                                    </el-form-item>
                                    <el-form-item label="请求超时（秒）">
                                        <el-input-number
                                            v-model="captcha.remote_browser_timeout"
                                            :min="5"
                                            :max="600"
                                            :precision="0"
                                            controls-position="right"
                                        />
                                    </el-form-item>
                                </el-form>
                                <div class="save-row">
                                    <span>
                                        {{
                                            savedAt.captcha
                                                ? '已保存于 ' + savedAt.captcha
                                                : '只有保存后的配置会用于新请求'
                                        }}
                                    </span>
                                    <el-button
                                        type="primary"
                                        :loading="busy.has('captcha')"
                                        :disabled="!loaded.has('captcha')"
                                        @click="saveCaptcha"
                                    >
                                        保存验证码配置
                                    </el-button>
                                </div>
                            </section>
                        </div>
                    </section>
                    <section
                        v-show="sectionVisible('cache')"
                        id="settings-cache"
                        class="settings-section"
                        data-section="cache"
                    >
                        <header class="settings-section-title">
                            <el-icon><component :is="groups.find((item) => item.id === 'cache').icon" /></el-icon>
                            <h2>{{ groups.find((item) => item.id === 'cache').label }}</h2>
                            <p>{{ groups.find((item) => item.id === 'cache').sub }}</p>
                        </header>
                        <div class="settings-grid">
                            <section
                                v-show="cardVisible('cache')"
                                data-setting-card="cache"
                                class="surface settings-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>作品媒体缓存</h3>
                                        <p>Flow 与 Vids 共用缓存保留时间；清理不删除登录资料、生成收据或未确认任务。</p>
                                    </div>
                                    <el-tag v-if="dirty('cache', cache)" type="warning" size="small">未保存</el-tag>
                                </div>
                                <div class="switch-row">
                                    <div>
                                        <strong>启用 Flow 媒体链接缓存</strong>
                                        <p>Flow 生成后缓存媒体并返回链接；Vids 为下载接口保留本地成品，不受此开关影响。</p>
                                    </div>
                                    <el-switch v-model="cache.enabled" />
                                </div>
                                <el-form label-position="top">
                                    <el-form-item label="缓存保留时间（秒）">
                                        <el-input-number
                                            v-model="cache.timeout"
                                            :min="0"
                                            :max="86400"
                                            :step="600"
                                            controls-position="right"
                                        />
                                        <span class="field-note">Flow 缓存与 Vids 已完成媒体均使用此期限；0 表示持续保留，7200 秒等于 2 小时。修改后自动生效，已清理文件不因延长期限恢复。</span>
                                    </el-form-item>
                                    <el-form-item label="公开访问地址">
                                        <el-input
                                            v-model="cache.base_url"
                                            placeholder="https://your-domain，留空使用默认地址"
                                        />
                                        <span class="field-note">客户端通过这个地址读取已生成的媒体作品</span>
                                    </el-form-item>
                                </el-form>
                                <div class="effective-address">
                                    <span>当前生效地址</span>
                                    <code>{{ cache.effective_base_url || 'http://127.0.0.1:8000' }}</code>
                                    <el-button
                                        text
                                        :icon="CopyDocument"
                                        aria-label="复制媒体访问地址"
                                        @click="copy(cache.effective_base_url || 'http://127.0.0.1:8000')"
                                    />
                                </div>
                                <div class="save-row">
                                    <span>
                                        {{ savedAt.cache ? '已保存于 ' + savedAt.cache : '保存后同步到媒体缓存服务' }}
                                    </span>
                                    <el-button
                                        type="primary"
                                        :loading="busy.has('cache')"
                                        :disabled="!loaded.has('cache')"
                                        @click="saveCache"
                                    >
                                        保存媒体缓存
                                    </el-button>
                                </div>
                            </section>
                        </div>
                    </section>
                    <section
                        v-show="sectionVisible('integration')"
                        id="settings-integration"
                        class="settings-section"
                        data-section="integration"
                    >
                        <header class="settings-section-title">
                            <el-icon><component :is="groups.find((item) => item.id === 'integration').icon" /></el-icon>
                            <h2>{{ groups.find((item) => item.id === 'integration').label }}</h2>
                            <p>{{ groups.find((item) => item.id === 'integration').sub }}</p>
                        </header>
                        <div class="settings-grid">
                            <section
                                v-show="cardVisible('release')"
                                data-setting-card="release"
                                class="surface settings-card release-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>版本与更新记录</h3>
                                        <p>当前版本、已发布修复和 Docker / Windows 更新入口集中在这里。</p>
                                    </div>
                                    <el-tag type="info" size="small" effect="plain">{{ release.project }}</el-tag>
                                </div>
                                <div class="release-meta">
                                    <div>
                                        <span>当前版本</span>
                                        <strong>{{ release.version === '—' ? '—' : `v${release.version}` }}</strong>
                                    </div>
                                    <div>
                                        <span>发布日期</span>
                                        <strong>{{ release.release_date || '—' }}</strong>
                                    </div>
                                    <div v-if="releaseCheck.checked_at">
                                        <span>上次检查</span>
                                        <strong>
                                            {{
                                                parseDate(releaseCheck.checked_at).toLocaleString('zh-CN', {
                                                    hour12: false,
                                                })
                                            }}
                                        </strong>
                                    </div>
                                </div>
                                <p v-if="releaseCheck.message" class="release-note">{{ releaseCheck.message }}</p>
                                <div class="release-links">
                                    <el-button :icon="Document" @click="openReleaseHistory">打开更新日志</el-button>
                                    <el-button :loading="releaseCheck.loading" :icon="Refresh" @click="checkRelease">
                                        检查线上版本
                                    </el-button>
                                    <a :href="release.release_page" target="_blank" rel="noreferrer">GitHub 发布页</a>
                                    <a :href="release.update_docs.windows" target="_blank" rel="noreferrer">
                                        Windows 一键安装
                                    </a>
                                    <a :href="release.update_docs.docker" target="_blank" rel="noreferrer">
                                        Docker 热更新
                                    </a>
                                </div>
                            </section>
                            <section
                                v-show="cardVisible('debug')"
                                data-setting-card="debug"
                                class="surface settings-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>运行诊断</h3>
                                        <p>记录详细的上游请求与响应，便于定位生成问题</p>
                                    </div>
                                    <el-tag
                                        v-if="dirty('debug', { enabled: admin.debug_enabled })"
                                        type="warning"
                                        size="small"
                                    >
                                        未保存
                                    </el-tag>
                                    <el-tag v-else type="info" size="small">调试</el-tag>
                                </div>
                                <div class="switch-row">
                                    <div>
                                        <strong>启用详细调试日志</strong>
                                        <p>详细记录写入 logs.txt，会增加日志文件大小</p>
                                    </div>
                                    <el-switch v-model="admin.debug_enabled" />
                                </div>
                                <div class="save-row">
                                    <span>按需要开启，排查完成后关闭</span>
                                    <el-button
                                        :loading="busy.has('debug')"
                                        :disabled="!loaded.has('debug')"
                                        @click="
                                            save(
                                                'debug',
                                                '/api/admin/debug',
                                                { enabled: admin.debug_enabled },
                                                { enabled: admin.debug_enabled },
                                            )
                                        "
                                    >
                                        保存诊断设置
                                    </el-button>
                                </div>
                            </section>
                        </div>
                    </section>
                </template>
            </main>
        </div>
    </div>
</template>

<style scoped>
.settings-view { min-width: 0; }
.settings-alert { margin-bottom: 12px; }
.page-heading { margin-bottom: 14px; }
.settings-toolbar {
    display: flex;
    align-items: center;
    gap: 14px;
    position: sticky;
    top: 72px;
    z-index: 10;
    padding: 8px 12px;
    margin-bottom: 12px;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
}
.settings-nav { order: -1; display: flex; gap: 3px; flex: 1; min-width: 0; overflow-x: auto; }
.settings-nav > button {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    white-space: nowrap;
    padding: 9px 12px;
    border: 0;
    border-radius: 6px;
    color: var(--muted);
    background: transparent;
    cursor: pointer;
    font: inherit;
    font-size: 13px;
}
.settings-nav > button:hover { color: var(--ink); background: var(--surface-soft); }
.settings-nav > button.active { color: var(--accent-strong); background: var(--pink-soft); font-weight: 650; }
.settings-nav > button:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.settings-search { width: 250px; flex: 0 0 auto; }
.settings-match-count { color: var(--muted); font-size: 12px; white-space: nowrap; }
.settings-match-count:empty { display: none; }
.settings-main { min-width: 0; }
.settings-section { margin: 0; scroll-margin-top: 150px; }
.settings-section-title { display: none; }
.settings-grid {
    display: block;
    min-width: 0;
    background: var(--surface);
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
}
.settings-section + .settings-section { margin-top: 12px; }
.settings-card {
    display: block;
    min-width: 0;
    padding: 18px 22px;
    margin: 0;
    border: 0;
    border-radius: 0;
    background: transparent;
    box-shadow: none;
}
.settings-card + .settings-card { border-top: 1px solid var(--line); }
.card-heading { display: flex; justify-content: space-between; gap: 14px; align-items: center; margin-bottom: 14px; }
.card-heading h3 { margin: 0; font-size: 14px; line-height: 1.6; font-weight: 650; }
.card-heading p { margin: 3px 0 0; font-size: 12px; color: var(--muted); line-height: 1.65; }
.settings-form-grid { display: contents; }
.settings-card :deep(.el-form-item) {
    display: grid;
    grid-template-columns: 220px minmax(0, 1fr);
    align-items: start;
    column-gap: 22px;
    margin-bottom: 14px;
}
.settings-card :deep(.el-form-item__label) {
    float: none;
    width: auto;
    height: auto;
    margin: 0;
    padding: 6px 0 0;
    font-size: 13px;
    font-weight: 500;
    line-height: 20px;
    color: var(--ink);
}
.settings-card :deep(.el-form-item__content) { min-width: 0; display: flex; align-items: center; flex-wrap: wrap; line-height: 1.6; }
.settings-card :deep(.el-input), .settings-card :deep(.el-select) { width: 100%; }
.settings-card :deep(.el-input-number) { width: 170px; }
.settings-card :deep(.el-input-number .el-input) { width: 100%; }
.input-with-action { display: flex; width: 100%; gap: 8px; }
.input-with-action > .el-input { min-width: 0; flex: 1; }
.field-note { display: block; flex-basis: 100%; width: 100%; margin-top: 4px; color: var(--muted); font-size: 12px; line-height: 1.7; }
.save-row { display: flex; align-items: center; gap: 16px; padding-top: 2px; margin-top: 0; }
.save-row > span { color: var(--muted); font-size: 12px; line-height: 1.6; margin-right: auto; }
.save-row .el-button { min-width: 104px; }
.switch-row { display: flex; align-items: center; justify-content: space-between; gap: 20px; padding: 2px 0 12px; margin-bottom: 8px; }
.switch-row strong { font-size: 13px; font-weight: 500; }
.switch-row p { margin: 3px 0 0; font-size: 12px; color: var(--muted); line-height: 1.7; }
.mode-options { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 12px; width: 100%; margin-bottom: 12px; }
.mode-options :deep(.el-radio) { height: auto; margin: 0; padding: 10px 12px; border-radius: 6px; }
.mode-options strong { display: block; font-size: 13px; }
.mode-options span { display: block; margin-top: 3px; color: var(--muted); font-size: 12px; line-height: 1.6; white-space: normal; }
.error-policy-card > .card-heading { margin-bottom: 12px; }
.error-policy-card :deep(.el-form-item:first-child) { display: block; }
.error-policy-card :deep(.el-form-item:first-child .el-form-item__label) { display: block; padding: 0; margin-bottom: 7px; }
.error-policy-input { width: 100%; }
.error-policy-input :deep(textarea) { width: 100%; min-height: 150px; font-family: Consolas, 'SFMono-Regular', monospace; font-size: 13px; line-height: 1.8; padding: 11px 13px; }
.error-policy-actions { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 6px 26px; font-size: 12px; line-height: 1.7; }
.error-policy-actions > div { display: grid; grid-template-columns: 106px minmax(0, 1fr); gap: 10px; }
.error-policy-actions code { color: var(--accent-strong); font-family: Consolas, 'SFMono-Regular', monospace; }
.error-policy-example-row { display: flex; align-items: center; gap: 14px; margin: 0 0 14px; padding: 0 0 12px; border-bottom: 1px solid var(--line); }
.error-policy-example-row .field-note { flex: 1; margin: 0; }
.error-policy-limits { display: grid; grid-template-columns: repeat(3, minmax(0, 1fr)); gap: 18px; }
.error-policy-limits :deep(.el-form-item) { display: block; margin-bottom: 8px; }
.error-policy-limits :deep(.el-form-item__label) { display: block; padding: 0; margin-bottom: 7px; }
.error-policy-safety { margin-bottom: 12px; }
.captcha-steps { display: flex; flex-wrap: wrap; gap: 4px 30px; margin: 0 0 14px; padding-left: 20px; font-size: 12px; line-height: 1.8; color: var(--muted); }
.captcha-steps a, .captcha-service-address a, .release-links a { color: var(--accent-strong); }
.captcha-service-address { display: flex; align-items: center; flex-wrap: wrap; gap: 8px 16px; min-height: 32px; }
.captcha-service-address a { overflow-wrap: anywhere; }
.effective-address { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; margin: 0 0 12px; font-size: 12px; color: var(--muted); }
.effective-address code { overflow-wrap: anywhere; flex: 1; color: var(--ink); }
.release-meta { display: flex; flex-wrap: wrap; gap: 14px 35px; margin: 0 0 12px; }
.release-meta > div { display: flex; align-items: baseline; gap: 9px; }
.release-meta span, .release-note { color: var(--muted); font-size: 12px; }
.release-meta strong { font-size: 14px; font-weight: 600; }
.release-note { margin: 0 0 10px; line-height: 1.7; }
.release-links { display: flex; flex-wrap: wrap; align-items: center; gap: 8px 18px; font-size: 12px; }
.settings-skeleton { padding: 22px; }
@media (max-width: 1200px) {
    .settings-toolbar { flex-wrap: wrap; gap: 8px; }
    .settings-nav { flex: 1 1 100%; }
    .settings-search { width: 100%; }
}
@media (max-width: 760px) {
    .settings-toolbar { position: static; padding: 7px; }
    .settings-nav { overflow-x: auto; padding-bottom: 2px; gap: 2px; }
    .settings-nav > button { padding: 8px 10px; font-size: 12px; }
    .settings-nav .el-icon { display: none; }
    .settings-card { padding: 15px 14px; }
    .settings-card :deep(.el-form-item) { display: block; margin-bottom: 13px; }
    .settings-card :deep(.el-form-item__label) { display: block; padding: 0; margin-bottom: 6px; }
    .settings-card :deep(.el-input-number) { width: 160px; }
    .settings-section { scroll-margin-top: 90px; }
    .mode-options, .error-policy-actions, .error-policy-limits { grid-template-columns: minmax(0, 1fr); gap: 8px; }
    .error-policy-actions > div { grid-template-columns: 92px minmax(0, 1fr); }
    .error-policy-example-row { flex-wrap: wrap; gap: 4px; }
    .error-policy-example-row .field-note { flex-basis: 100%; }
    .error-policy-input :deep(textarea) { min-height: 180px; font-size: 12px; }
    .save-row { align-items: start; flex-wrap: wrap; gap: 8px; }
    .save-row > span { flex: 1 1 100%; }
    .save-row .el-button { margin-left: auto; }
    .release-meta > div { flex-wrap: wrap; gap: 4px 8px; }
    .switch-row { align-items: start; }
}
</style>
