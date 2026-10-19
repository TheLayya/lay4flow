<script setup>
import { computed, nextTick, onMounted, reactive, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
    Setting,
    Key,
    MagicStick,
    FolderOpened,
    Refresh,
    CopyDocument,
    Monitor,
    Tools,
    Search,
} from '@element-plus/icons-vue'
import { api, notifyError, parseDate } from '../lib/api'
import { CAPTCHA_SERVICE_URL } from '../lib/captcha-service'
import Mascot from '../components/Mascot.vue'

const group = ref('general')
const route = useRoute()
const captchaKeyInput = ref(null)
const settingsQuery = ref('')
const groups = [
    { id: 'general', label: '基础与访问', sub: '账号 · API 密钥', icon: Key },
    { id: 'generation', label: '生成与调度', sub: '超时 · 轮询 · 刷新', icon: MagicStick },
    { id: 'captcha', label: '远程有头打码', sub: '服务地址 · 密钥 · 超时', icon: Monitor },
    { id: 'cache', label: '媒体缓存', sub: '保存时间 · 访问地址', icon: FolderOpened },
    { id: 'integration', label: '运行诊断', sub: '调试日志', icon: Tools },
]
const settingCards = [
    { id: 'api', section: 'general', terms: 'API 访问密钥 当前 API 密钥 新 API 密钥 key 外部客户端' },
    { id: 'login', section: 'general', terms: '控制台登录 管理员名称 当前密码 新密码 确认新密码 账号' },
    { id: 'health', section: 'general', terms: '账号健康策略 连续错误暂停阈值 自动暂停 失败' },
    { id: 'timeouts', section: 'generation', terms: '任务等待时间 图片生成超时 视频生成超时 最大重试次数 秒' },
    { id: 'dispatch', section: 'generation', terms: '账号调度 默认调度 轮询调度 并发' },
    {
        id: 'refresh',
        section: 'generation',
        terms: '登录态自动刷新 协议 ST 自动刷新 默认刷新间隔 分钟 Google Cookies Access Token',
    },
    {
        id: 'captcha',
        section: 'captcha',
        terms: '远程有头打码 验证码 远程服务地址 API 密钥 申请 请求超时 remote browser',
    },
    { id: 'cache', section: 'cache', terms: '作品媒体缓存 缓存保留时间 秒 公开访问地址 当前生效地址 图片 视频' },
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
const sectionVisible = (id) => matchingCards.value.some((card) => card.section === id)
function jumpToSection(id) {
    settingsQuery.value = ''
    group.value = id
    requestAnimationFrame(() =>
        document.getElementById(`settings-${id}`)?.scrollIntoView({ block: 'start', behavior: 'smooth' }),
    )
}
const loading = ref(true)
const failures = ref([])
const loaded = reactive(new Set())
const snapshots = reactive({})
const busy = reactive(new Set())
const savedAt = reactive({})
const admin = reactive({ admin_username: '', api_key: '', error_ban_threshold: 3, debug_enabled: false })
const password = reactive({ username: '', old_password: '', new_password: '', confirm_password: '' })
const newKey = ref('')
const generation = reactive({ image_timeout: 300, video_timeout: 1500, max_retries: 3 })
const callLogic = reactive({ call_mode: 'default' })
const refreshConfig = reactive({ enabled: false, refresh_interval_minutes: 120 })
const cache = reactive({ enabled: false, timeout: 7200, base_url: '', effective_base_url: '' })
const captcha = reactive({
    captcha_method: 'remote_browser',
    remote_browser_base_url: CAPTCHA_SERVICE_URL,
    remote_browser_api_key: '',
    remote_browser_timeout: 60,
})
let loadRequest
const configSources = [
    { id: 'admin', path: '/api/admin/config', target: admin },
    { id: 'generation', path: '/api/config/generation', target: generation, wrap: true },
    { id: 'callLogic', path: '/api/call-logic/config', target: callLogic, wrap: true },
    {
        id: 'refresh',
        path: '/api/token-refresh/config',
        target: refreshConfig,
        map: (data) => ({
            enabled: data.config.protocol_refresh_enabled,
            refresh_interval_minutes: data.config.refresh_interval_minutes,
        }),
    },
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
const tracked = (id, value) => (id === 'admin' ? { error_ban_threshold: value.error_ban_threshold } : value)
function markSnapshot(id, value) {
    snapshots[id] = string(tracked(id, value))
}
function dirty(id, value) {
    return snapshots[id] !== undefined && snapshots[id] !== string(tracked(id, value))
}
const dirtyCount = computed(
    () =>
        configSources.filter((item) => dirty(item.id, item.target)).length +
        Number(dirty('debug', { enabled: admin.debug_enabled })) +
        Number(!!newKey.value.trim()) +
        Number(
            Object.entries(password).some(([key, value]) =>
                key === 'username' ? !!value && value !== admin.admin_username : !!value,
            ),
        ),
)
const settingsSummary = computed(() => [
    { label: 'API 访问', value: loaded.has('admin') ? (admin.api_key ? '已配置' : '待设置') : '未读取', icon: Key },
    {
        label: '账号调度',
        value: loaded.has('callLogic') ? (callLogic.call_mode === 'polling' ? '轮询调度' : '默认调度') : '未读取',
        icon: MagicStick,
    },
    {
        label: '验证码',
        value: loaded.has('captcha') ? (captcha.remote_browser_api_key.trim() ? '已填写密钥' : '待填写密钥') : '未读取',
        icon: Monitor,
    },
    {
        label: '媒体缓存',
        value: loaded.has('cache')
            ? cache.enabled
                ? cache.timeout
                    ? `${cache.timeout / 60} 分钟`
                    : '持续保留'
                : '已关闭'
            : '未读取',
        icon: FolderOpened,
    },
    { label: '待保存', value: `${dirtyCount.value} 组修改`, icon: Setting },
])
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
                <p>所有配置同页查看，修改后在对应卡片保存。</p>
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
        <div class="settings-summary" aria-label="设置状态总览">
            <div v-for="item in settingsSummary" :key="item.label">
                <el-icon><component :is="item.icon" /></el-icon>
                <span>{{ item.label }}</span>
                <strong>{{ item.value }}</strong>
            </div>
        </div>
        <div class="settings-toolbar">
            <el-input
                v-model="settingsQuery"
                class="settings-search"
                :prefix-icon="Search"
                clearable
                placeholder="搜索设置、字段或服务，如：超时、代理、密钥"
                aria-label="搜索设置"
            />
            <nav class="settings-nav" aria-label="设置分区定位">
                <button
                    v-for="item in groups"
                    :key="item.id"
                    :class="{ active: group === item.id }"
                    @click="jumpToSection(item.id)"
                >
                    <el-icon><component :is="item.icon" /></el-icon>
                    {{ item.label }}
                </button>
            </nav>
            <span class="settings-match-count">
                {{ settingsQuery.trim() ? `${matchingCards.length} 项匹配` : '全部配置已展开' }}
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
                            <section
                                v-show="cardVisible('health')"
                                data-setting-card="health"
                                class="surface settings-card"
                            >
                                <div class="card-heading">
                                    <div>
                                        <h3>账号健康策略</h3>
                                        <p>连续失败达到阈值时自动暂停账号</p>
                                    </div>
                                    <el-tag v-if="dirty('admin', admin)" type="warning" size="small">未保存</el-tag>
                                </div>
                                <el-form label-position="top">
                                    <el-form-item label="连续错误暂停阈值">
                                        <el-input-number
                                            v-model="admin.error_ban_threshold"
                                            :min="1"
                                            :max="100"
                                            controls-position="right"
                                        />
                                        <span class="field-note">
                                            当前设置：连续 {{ admin.error_ban_threshold }} 次错误后暂停分配任务
                                        </span>
                                    </el-form-item>
                                </el-form>
                                <div class="save-row">
                                    <span>
                                        {{
                                            savedAt.admin ? '已保存于 ' + savedAt.admin : '修复会话后可手动重新启用账号'
                                        }}
                                    </span>
                                    <el-button
                                        type="primary"
                                        :loading="busy.has('admin')"
                                        :disabled="!loaded.has('admin')"
                                        @click="
                                            save(
                                                'admin',
                                                '/api/admin/config',
                                                { error_ban_threshold: admin.error_ban_threshold },
                                                admin,
                                            )
                                        "
                                    >
                                        保存健康策略
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
                                    <el-form-item label="最大重试次数">
                                        <el-input-number
                                            v-model="generation.max_retries"
                                            :min="1"
                                            :max="20"
                                            controls-position="right"
                                        />
                                        <span class="field-note">短暂网络中断或上游错误时重新尝试</span>
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
                                        <span>沿用当前可用账号分配逻辑</span>
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
                                                : '两种模式均遵循账号能力与并发设置'
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
                                        <h3>登录态自动刷新</h3>
                                        <p>定期使用保存的 Google Cookies 刷新 Session Token</p>
                                    </div>
                                    <el-tag v-if="dirty('refresh', refreshConfig)" type="warning" size="small">
                                        未保存
                                    </el-tag>
                                </div>
                                <div class="switch-row">
                                    <div>
                                        <strong>启用协议 ST 自动刷新</strong>
                                        <p>Access Token 自动刷新始终开启</p>
                                    </div>
                                    <el-switch v-model="refreshConfig.enabled" />
                                </div>
                                <el-form label-position="top">
                                    <el-form-item label="默认刷新间隔（分钟）">
                                        <el-input-number
                                            v-model="refreshConfig.refresh_interval_minutes"
                                            :min="1"
                                            :max="1440"
                                            :disabled="!refreshConfig.enabled"
                                            controls-position="right"
                                        />
                                        <span class="field-note">账号详情中可以设置独立的刷新间隔</span>
                                    </el-form-item>
                                </el-form>
                                <div class="save-row">
                                    <span>
                                        {{
                                            savedAt.refresh
                                                ? '已保存于 ' + savedAt.refresh
                                                : '登录过期时请重新同步 Google 会话'
                                        }}
                                    </span>
                                    <el-button
                                        type="primary"
                                        :loading="busy.has('refresh')"
                                        :disabled="!loaded.has('refresh')"
                                        @click="
                                            save(
                                                'refresh',
                                                '/api/token-refresh/config',
                                                { ...refreshConfig },
                                                refreshConfig,
                                            )
                                        "
                                    >
                                        保存刷新设置
                                    </el-button>
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
                                        <p>将生成作品保存在服务端，返回可访问的媒体链接</p>
                                    </div>
                                    <el-tag v-if="dirty('cache', cache)" type="warning" size="small">未保存</el-tag>
                                </div>
                                <div class="switch-row">
                                    <div>
                                        <strong>启用媒体缓存</strong>
                                        <p>图片与视频生成完成后自动写入缓存</p>
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
                                            :disabled="!cache.enabled"
                                        />
                                        <span class="field-note">0 表示持续保留；7200 秒等于 2 小时</span>
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
.settings-alert {
    margin-bottom: 20px;
}
.settings-layout {
    display: block;
}
.settings-nav {
    display: flex;
    flex-wrap: wrap;
    gap: 5px;
}
.settings-nav > button {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    padding: 9px 10px;
    margin: 0;
    border: 0;
    border-radius: 8px;
    color: var(--muted);
    background: transparent;
    font: inherit;
    font-size: 12px;
}
.settings-nav > button:hover,
.settings-nav > button.active {
    color: var(--accent);
    background: var(--pink-soft);
}
.settings-summary {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 1px;
    border-radius: 13px;
    overflow: hidden;
    margin-bottom: 16px;
    background: var(--line);
}
.settings-summary > div {
    min-width: 0;
    padding: 14px 16px;
    display: grid;
    grid-template-columns: 18px 1fr;
    gap: 5px 8px;
    background: var(--surface);
}
.settings-summary .el-icon {
    grid-row: 1 / 3;
    color: var(--accent);
    margin-top: 3px;
    font-size: 16px;
}
.settings-summary span {
    font-size: 11px;
    color: var(--muted);
}
.settings-summary strong {
    font-size: 13px;
    color: var(--ink);
    font-weight: 650;
    overflow-wrap: anywhere;
}
.settings-toolbar {
    display: flex;
    gap: 12px;
    align-items: center;
    flex-wrap: wrap;
    padding: 10px 12px;
    border: 1px solid var(--line);
    background: var(--surface);
    border-radius: 12px;
    margin-bottom: 22px;
    position: sticky;
    top: 76px;
    z-index: 10;
}
.settings-search {
    width: min(340px, 100%);
}
.settings-match-count {
    color: var(--muted);
    font-size: 11px;
    margin-left: auto;
    white-space: nowrap;
}
.settings-main {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 24px 20px;
    align-items: start;
}
.settings-section {
    min-width: 0;
    scroll-margin-top: 164px;
    grid-column: 1 / -1;
}
.settings-section[data-section='cache'],
.settings-section[data-section='integration'] {
    grid-column: auto;
}
.settings-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 16px;
    align-items: stretch;
}
.settings-section[data-section='cache'] .settings-grid,
.settings-section[data-section='integration'] .settings-grid {
    grid-template-columns: minmax(0, 1fr);
}
.settings-section[data-section='captcha'] .settings-grid {
    grid-template-columns: minmax(0, 1fr);
}
.settings-section[data-section='captcha'] .settings-card > .el-form {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0 20px;
}
.settings-section[data-section='captcha'] .settings-card > .el-form > .settings-form-grid {
    grid-column: 1 / -1;
}
.captcha-steps {
    display: flex;
    flex-wrap: wrap;
    gap: 8px 32px;
    margin: 0 0 22px;
    padding: 14px 18px 14px 34px;
    color: var(--ink);
    background: var(--surface-soft);
    border-radius: 10px;
    font-size: 13px;
    line-height: 1.7;
}
.captcha-steps a,
.captcha-service-address a {
    color: var(--accent-strong);
    text-decoration: underline;
    text-underline-offset: 3px;
}
.captcha-service-address {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px 16px;
    min-height: 34px;
}
.captcha-service-address a {
    overflow-wrap: anywhere;
}
.settings-card {
    container: settings-card / inline-size;
}
.settings-grid > .settings-card {
    display: flex;
    flex-direction: column;
    height: 100%;
}
.settings-grid > .settings-card > .save-row {
    margin-top: auto;
}
.settings-section-title {
    display: flex;
    align-items: center;
    gap: 9px;
    margin-bottom: 12px;
}
.settings-section-title h2 {
    font-size: 18px;
    margin: 0;
}
.settings-section-title > p {
    font-size: 13px;
    color: var(--muted);
    margin: 0 0 0 5px;
}
.settings-card {
    padding: 12px 14px;
    margin: 0;
}
.card-heading {
    display: flex;
    justify-content: space-between;
    gap: 16px;
    align-items: flex-start;
    margin-bottom: 16px;
}
.card-heading h3 {
    font-size: 15px;
    margin: 0;
    line-height: 1.6;
    font-weight: 700;
}
.card-heading p {
    font-size: 13px;
    color: var(--muted);
    line-height: 1.8;
    margin: 7px 0 0;
}
.settings-form-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
}
.settings-card :deep(.el-form-item__label) {
    font-size: 14px;
    font-weight: 550;
}
.settings-card :deep(.el-form-item) {
    margin-bottom: 15px;
}
.settings-card :deep(.el-input-number) {
    width: 180px;
}
.settings-card :deep(.el-select) {
    width: 100%;
}
.input-with-action {
    display: flex;
    gap: 9px;
    width: 100%;
}
.input-with-action > .el-input {
    flex: 1;
}
.field-note {
    display: block;
    width: 100%;
    font-size: 13px;
    color: var(--muted);
    line-height: 1.8;
    margin-top: 7px;
}
.save-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 15px;
    padding-top: 14px;
    margin-top: 0;
    flex-wrap: wrap;
}
.save-row > span {
    font-size: 13px;
    color: var(--muted);
    line-height: 1.7;
    margin-right: auto;
}
.switch-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 18px;
    padding-bottom: 11px;
    margin-bottom: 13px;
}
.switch-row strong {
    font-size: 14px;
    font-weight: 650;
}
.switch-row p {
    font-size: 13px;
    color: var(--muted);
    line-height: 1.8;
    margin: 7px 0 0;
}
.mode-options {
    width: 100%;
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 14px;
}
.mode-options :deep(.el-radio) {
    height: auto;
    min-height: 62px;
    margin: 0;
    padding: 13px 12px;
    border-radius: 13px;
}
.mode-options strong {
    display: block;
    font-size: 13px;
}
.mode-options span {
    display: block;
    font-size: 13px;
    color: var(--muted);
    margin-top: 7px;
    white-space: normal;
    line-height: 1.6;
}
.effective-address {
    display: flex;
    align-items: center;
    gap: 15px;
    background: var(--surface-soft);
    padding: 14px 16px;
    border-radius: 12px;
    margin-bottom: 24px;
    flex-wrap: wrap;
}
.effective-address > span {
    font-size: 13px;
    color: var(--muted);
}
.effective-address code {
    font-size: 13px;
    overflow-wrap: anywhere;
    flex: 1;
}
.settings-skeleton {
    padding: 30px;
}
.settings-main {
    min-width: 0;
}

@media (min-width: 1600px) {
    .settings-section[data-section='general'] .settings-grid,
    .settings-section[data-section='generation'] .settings-grid {
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .settings-section[data-section='cache'] {
        grid-column: auto;
    }
    .settings-section[data-section='integration'] .settings-grid {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
}
@media (max-width: 1200px) {
    .settings-search {
        width: 100%;
    }
    .settings-summary {
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }
    .settings-toolbar {
        top: 72px;
    }
    .settings-nav {
        flex: 1;
    }
}
@media (min-width: 1100px) and (max-width: 1599px) {
    .settings-grid > [data-setting-card='health'] {
        grid-column: 1 / -1;
        display: grid;
        grid-template-columns: minmax(180px, 1fr) minmax(220px, 1fr) auto;
        gap: 18px;
        align-items: center;
    }
    [data-setting-card='health'] .card-heading,
    [data-setting-card='health'] :deep(.el-form-item) {
        margin: 0;
    }
    [data-setting-card='health'] .save-row {
        padding: 0;
    }
    [data-setting-card='health'] .save-row > span {
        display: none;
    }
}
@media (max-width: 900px) {
    .settings-main {
        grid-template-columns: 1fr;
    }
    .settings-section[data-section='cache'],
    .settings-section[data-section='integration'] {
        grid-column: 1;
    }
    .settings-toolbar {
        position: static;
    }
    .settings-section {
        scroll-margin-top: 90px;
    }
}
@media (max-width: 1100px) {
    .settings-grid {
        grid-template-columns: minmax(0, 1fr);
    }
}
@media (max-width: 650px) {
    .settings-section[data-section='captcha'] .settings-card > .el-form {
        grid-template-columns: minmax(0, 1fr);
        gap: 0;
    }
    .settings-grid,
    .settings-section[data-section='integration'] .settings-grid {
        grid-template-columns: minmax(0, 1fr);
    }
    .settings-card {
        padding: 17px 16px;
    }
    .settings-summary > div {
        padding: 12px 10px;
        grid-template-columns: minmax(0, 1fr);
    }
    .settings-summary .el-icon {
        display: none;
    }
    .settings-search {
        width: 100%;
    }
    .settings-nav {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        width: 100%;
    }
    .settings-nav > button {
        justify-content: center;
        font-size: 11px;
        padding: 9px 6px;
    }
    .settings-match-count {
        width: 100%;
        margin-left: 0;
    }
    .settings-section-title {
        flex-wrap: wrap;
    }
    .settings-section-title > p {
        margin-left: auto;
    }
    .mode-options {
        grid-template-columns: minmax(0, 1fr);
    }
    .card-heading {
        gap: 10px;
    }
    .save-row {
        gap: 10px;
    }
    .save-row > span {
        flex: 1 1 130px;
    }
    .save-row .el-button {
        margin-left: auto;
    }
}
@container settings-card (max-width: 360px) {
    .settings-form-grid {
        grid-template-columns: minmax(0, 1fr);
        gap: 0;
    }
    .mode-options {
        grid-template-columns: minmax(0, 1fr);
    }
}

.settings-nav strong,
.switch-row strong,
.mode-options strong,
.settings-card :deep(.el-form-item__label) {
    font-size: 14px;
}
.settings-card :deep(.el-input__inner) {
    font-size: 14px;
}

html.dark .settings-nav > button:hover {
    background: var(--surface-soft);
}
html.dark .settings-nav > button.active {
    background: var(--pink-soft);
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
.settings-view :deep(.el-empty__description p) {
    font-size: 14px;
}
.settings-view :deep(.el-empty__image) {
    width: 96px;
}
.settings-nav > button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 3px;
}
@media (max-width: 800px) {
    .settings-view > .page-heading {
        flex-direction: column;
        align-items: stretch;
    }
    .page-actions {
        justify-content: flex-start;
    }
}
</style>
