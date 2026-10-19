<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
    House,
    MagicStick,
    User,
    Document,
    Setting,
    Reading,
    Search,
    Bell,
    ArrowRight,
    Fold,
    Expand,
    Moon,
    Sunny,
    SwitchButton,
    Close,
    FullScreen,
    Monitor,
    Check,
    Refresh,
    Link,
} from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import zhCn from 'element-plus/es/locale/lang/zh-cn'
import { api } from './lib/api'
import { CAPTCHA_SERVICE_URL } from './lib/captcha-service'
import { resetSession } from './router'
import BrandMark from './components/BrandMark.vue'
import Mascot from './components/Mascot.vue'
import SparkIcon from './components/SparkIcon.vue'
import ProfileImportNotice from './components/ProfileImportNotice.vue'

const route = useRoute(),
    router = useRouter()
const isLogin = computed(() => route.path === '/login')
const isProfile = computed(() => /^\/profile\/\d+$/.test(route.path))
const collapsed = ref(localStorage.getItem('sidebar-collapsed') === 'true')
const mobileOpen = ref(false),
    commandOpen = ref(false),
    notificationOpen = ref(false),
    query = ref('')
const isMobile = ref(window.innerWidth < 768)
const theme = ref(localStorage.getItem('theme') || 'light')
const health = ref(null),
    lastChecked = ref(''),
    username = ref(localStorage.getItem('admin_username') || '创作者')
const connectionError = ref(false)
const captchaMissing = computed(() => health.value?.remote_browser_configured === false && !route.meta.public)
const nav = [
    { path: '/dashboard', label: '工坊总览', sub: 'Dashboard', icon: House },
    { path: '/studio', label: '创作工作台', sub: 'Creative studio', icon: MagicStick },
    { path: '/accounts', label: '账号管理', sub: 'Accounts', icon: User },
    { path: '/proxies', label: '代理池', sub: 'Proxy pool', icon: Link },
    { path: '/logs', label: '请求日志', sub: 'Activity', icon: Document },
    { path: '/settings', label: '工坊设置', sub: 'Settings', icon: Setting },
    { path: '/guide', label: 'API 文档', sub: 'API reference', icon: Reading },
]
const matches = computed(() =>
    nav.filter((item) => (item.label + item.sub).toLowerCase().includes(query.value.toLowerCase())),
)
const systemText = computed(() =>
    connectionError.value ? '连接中断' : health.value?.backend_running ? '服务运行中' : '正在连接',
)
const checks = computed(() => [
    {
        label: '后端服务',
        good: health.value?.backend_running,
        note: health.value?.backend_running ? '服务正在响应请求' : '请检查服务连接',
    },
    {
        label: '创作账号',
        good: health.value?.has_active_tokens,
        note: health.value?.has_active_tokens ? '有可用账号' : '添加或启用账号后开始创作',
    },
    {
        label: 'Flow 打码服务',
        good: health.value?.remote_browser_configured,
        note: health.value?.remote_browser_configured ? '已填写远程打码密钥' : '请先申请并填写远程打码 API 密钥',
    },
])
function applyTheme(value) {
    theme.value = value
    document.documentElement.classList.toggle(
        'dark',
        value === 'dark' || (value === 'system' && matchMedia('(prefers-color-scheme: dark)').matches),
    )
    localStorage.setItem('theme', value)
}
const media = matchMedia('(prefers-color-scheme: dark)')
function systemThemeChange() {
    if (theme.value === 'system') applyTheme('system')
}
function toggleSidebar() {
    collapsed.value = !collapsed.value
    localStorage.setItem('sidebar-collapsed', String(collapsed.value))
}
function navigate(path) {
    mobileOpen.value = false
    commandOpen.value = false
    query.value = ''
    router.push(path)
}
function manageAccounts() {
    notificationOpen.value = false
    navigate('/accounts')
}
function resize() {
    isMobile.value = window.innerWidth < 768
    if (!isMobile.value) mobileOpen.value = false
}
function skipToContent() {
    const element = document.getElementById(isProfile.value ? 'screen' : 'main-content')
    if (element) {
        element.setAttribute('tabindex', '-1')
        element.focus()
        element.scrollIntoView({ block: 'start' })
    }
}
async function checkHealth() {
    try {
        health.value = await api.get('/health')
        connectionError.value = false
        lastChecked.value = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    } catch {
        connectionError.value = true
    }
}
async function logout() {
    try {
        await api.post('/api/logout')
    } catch (error) {
        if (error.status !== 401) {
            ElMessage.error('退出未完成，请检查连接后重试')
            return
        }
    }
    localStorage.removeItem('token')
    localStorage.removeItem('studio_api_key')
    resetSession()
    router.push('/login')
}
function expired() {
    resetSession()
    localStorage.removeItem('token')
    if (isProfile.value) {
        location.assign('/login')
        return
    }
    if (route.matched.length && !route.meta.public)
        router.replace({ path: '/login', query: { next: route.fullPath } })
}
function keyboard(event) {
    if (isProfile.value) return
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'k') {
        event.preventDefault()
        commandOpen.value = !commandOpen.value
    }
    if (event.key === 'Escape') mobileOpen.value = false
}
async function fullscreen() {
    try {
        document.fullscreenElement
            ? await document.exitFullscreen()
            : await document.documentElement.requestFullscreen()
    } catch {
        ElMessage.info('请使用浏览器的全屏选项')
    }
}
let timer
watch(
    () => route.path,
    () => {
        mobileOpen.value = false
        username.value = localStorage.getItem('admin_username') || '创作者'
    },
)
onMounted(() => {
    applyTheme(theme.value)
    checkHealth()
    timer = setInterval(checkHealth, 30000)
    window.addEventListener('resize', resize)
    window.addEventListener('keydown', keyboard)
    window.addEventListener('session-expired', expired)
    window.addEventListener('captcha-config-saved', checkHealth)
    media.addEventListener('change', systemThemeChange)
})
onUnmounted(() => {
    clearInterval(timer)
    window.removeEventListener('resize', resize)
    window.removeEventListener('keydown', keyboard)
    window.removeEventListener('session-expired', expired)
    window.removeEventListener('captcha-config-saved', checkHealth)
    media.removeEventListener('change', systemThemeChange)
})
</script>
<template>
    <el-config-provider :locale="zhCn" :button="{ autoInsertSpace: false }">
        <a class="skip-link" href="#main-content" @click.prevent="skipToContent">跳到主要内容</a>
        <router-view v-if="isLogin || isProfile" />
        <div
            v-else
            class="app-shell"
            :class="{ 'rail-collapsed': collapsed, 'studio-shell': route.path === '/studio' }"
        >
            <button v-if="mobileOpen" class="mobile-overlay" aria-label="关闭导航" @click="mobileOpen = false"></button>
            <aside
                class="sidebar"
                :class="{ 'mobile-open': mobileOpen }"
                :inert="isMobile && !mobileOpen"
                :aria-hidden="isMobile && !mobileOpen ? true : undefined"
                aria-label="工坊导航"
            >
                <router-link to="/dashboard" class="sidebar-brand" aria-label="lay4flow-remote 工坊总览">
                    <BrandMark :small="collapsed" />
                </router-link>
                <div class="workspace-label">
                    <span class="workspace-dot"></span>
                    <span>我的创作空间</span>
                    <el-icon><ArrowRight /></el-icon>
                </div>
                <nav class="main-nav">
                    <el-tooltip
                        v-for="item in nav"
                        :key="item.path"
                        :content="item.label"
                        placement="right"
                        :disabled="!collapsed"
                    >
                        <router-link
                            :to="item.path"
                            class="nav-item"
                            :class="{ active: route.path === item.path }"
                            @click="mobileOpen = false"
                        >
                            <el-icon><component :is="item.icon" /></el-icon>
                            <span class="nav-label">{{ item.label }}</span>
                            <span v-if="item.path === '/studio'" class="nav-star"><SparkIcon /></span>
                        </router-link>
                    </el-tooltip>
                </nav>
                <div class="sidebar-bottom">
                    <div class="companion-note">
                        <Mascot compact portrait mood="sleepy" />
                        <div>
                            <strong>lay 在这里等你</strong>
                            <p>lay 在这里等你的新灵感。</p>
                            <router-link to="/studio">
                                打开工作台
                                <el-icon><ArrowRight /></el-icon>
                            </router-link>
                        </div>
                    </div>
                    <el-dropdown
                        trigger="click"
                        placement="top-start"
                        @command="(command) => (command === 'logout' ? logout() : navigate('/settings'))"
                    >
                        <button class="user-menu">
                            <span class="user-avatar">{{ username.slice(0, 1).toUpperCase() }}</span>
                            <span class="user-caption">
                                <strong>{{ username }}</strong>
                                <small>工坊管理员</small>
                            </span>
                            <el-icon><Setting /></el-icon>
                        </button>
                        <template #dropdown>
                            <el-dropdown-menu>
                                <el-dropdown-item command="settings" :icon="Setting">账号与设置</el-dropdown-item>
                                <el-dropdown-item command="logout" :icon="SwitchButton" divided>
                                    退出登录
                                </el-dropdown-item>
                            </el-dropdown-menu>
                        </template>
                    </el-dropdown>
                </div>
            </aside>
            <div class="workspace-main">
                <header class="topbar">
                    <div class="topbar-location">
                        <el-button
                            class="desktop-collapse"
                            text
                            circle
                            :aria-label="collapsed ? '展开侧栏' : '收起侧栏'"
                            @click="toggleSidebar"
                        >
                            <el-icon><component :is="collapsed ? Expand : Fold" /></el-icon>
                        </el-button>
                        <el-button
                            class="mobile-menu-button"
                            text
                            circle
                            aria-label="打开导航"
                            :aria-expanded="mobileOpen"
                            @click="mobileOpen = !mobileOpen"
                        >
                            <el-icon><Expand /></el-icon>
                        </el-button>
                        <el-breadcrumb separator="/">
                            <el-breadcrumb-item>我的工坊</el-breadcrumb-item>
                            <el-breadcrumb-item>{{ route.meta.title }}</el-breadcrumb-item>
                        </el-breadcrumb>
                    </div>
                    <div class="topbar-tools">
                        <button class="command-trigger" @click="commandOpen = true">
                            <el-icon><Search /></el-icon>
                            <span>搜索页面、快速跳转</span>
                            <kbd>Ctrl K</kbd>
                        </button>
                        <el-tooltip :content="systemText">
                            <button class="service-status" @click="notificationOpen = true">
                                <span
                                    class="status-dot"
                                    :class="{ offline: connectionError || !health?.backend_running }"
                                ></span>
                                <span>{{ systemText }}</span>
                            </button>
                        </el-tooltip>
                        <el-tooltip content="全屏工作空间">
                            <el-button
                                text
                                circle
                                aria-label="全屏工作空间"
                                class="fullscreen-button"
                                @click="fullscreen"
                            >
                                <el-icon><FullScreen /></el-icon>
                            </el-button>
                        </el-tooltip>
                        <el-dropdown @command="applyTheme">
                            <el-button text circle aria-label="切换外观">
                                <el-icon><component :is="theme === 'dark' ? Moon : Sunny" /></el-icon>
                            </el-button>
                            <template #dropdown>
                                <el-dropdown-menu>
                                    <el-dropdown-item command="light" :icon="Sunny">樱花日间</el-dropdown-item>
                                    <el-dropdown-item command="dark" :icon="Moon">星空夜间</el-dropdown-item>
                                    <el-dropdown-item command="system" :icon="Monitor">跟随系统</el-dropdown-item>
                                </el-dropdown-menu>
                            </template>
                        </el-dropdown>
                        <el-tooltip content="工坊状态">
                            <el-button text circle aria-label="查看工坊状态" @click="notificationOpen = true">
                                <el-icon><Bell /></el-icon>
                            </el-button>
                        </el-tooltip>
                    </div>
                </header>
                <aside v-if="captchaMissing" class="captcha-setup-notice" role="status" aria-live="polite">
                    <div>
                        <strong>请先配置打码服务</strong>
                        <p>Flow 图片/视频创作需要打码服务。请先申请并填写 API 密钥。</p>
                    </div>
                    <div class="captcha-setup-actions">
                        <a :href="CAPTCHA_SERVICE_URL" target="_blank" rel="noopener noreferrer">在这里申请打码 ↗</a>
                        <el-button type="primary" @click="navigate('/settings?section=captcha')">
                            填写打码密钥
                        </el-button>
                    </div>
                </aside>
                <ProfileImportNotice v-if="route.matched.length && !route.meta.public" />
                <main id="main-content" class="main-content" tabindex="-1">
                    <router-view v-slot="{ Component }">
                        <transition name="page" mode="out-in">
                            <component :is="Component" :key="route.path" />
                        </transition>
                    </router-view>
                </main>
                <footer class="app-footer">
                    <span>lay4flow-remote · 为每一份灵感留个位置</span>
                    <span>
                        Flow & Vids
                        <span class="footer-spark"><SparkIcon /></span>
                        创作连接，无限可能
                    </span>
                </footer>
            </div>
            <el-dialog
                v-model="commandOpen"
                class="command-dialog"
                width="540px"
                :show-close="false"
                align-center
                destroy-on-close
            >
                <template #header><span class="sr-only">快速跳转</span></template>
                <el-input
                    v-model="query"
                    placeholder="想去哪里？搜索页面…"
                    :prefix-icon="Search"
                    clearable
                    size="large"
                    autofocus
                    @keydown.enter="matches[0] && navigate(matches[0].path)"
                />
                <div class="command-results">
                    <button v-for="item in matches" :key="item.path" @click="navigate(item.path)">
                        <el-icon><component :is="item.icon" /></el-icon>
                        <span>
                            <strong>{{ item.label }}</strong>
                            <small>{{ item.sub }}</small>
                        </span>
                        <el-icon><ArrowRight /></el-icon>
                    </button>
                    <el-empty v-if="!matches.length" description="还没有这个页面，试试其他关键词" :image-size="60" />
                </div>
                <p class="command-hint">Enter 打开首项 · Esc 关闭</p>
            </el-dialog>
            <el-drawer v-model="notificationOpen" title="工坊状态" size="min(420px, 100vw)">
                <div class="status-drawer-intro">
                    <Mascot compact />
                    <div>
                        <h3>lay 的工坊巡检</h3>
                        <p class="muted">最近检查 {{ lastChecked || '进行中' }}</p>
                    </div>
                </div>
                <div v-for="check in checks" :key="check.label" class="health-check">
                    <el-icon :class="check.good ? 'good' : 'waiting'">
                        <component :is="check.good ? Check : Bell" />
                    </el-icon>
                    <div>
                        <strong>{{ check.label }}</strong>
                        <p>{{ check.note }}</p>
                    </div>
                </div>
                <el-button :icon="Refresh" @click="checkHealth">重新检查</el-button>
                <el-button type="primary" @click="manageAccounts">管理账号</el-button>
            </el-drawer>
        </div>
    </el-config-provider>
</template>

<style scoped>
.captcha-setup-notice {
    display: flex;
    flex-shrink: 0;
    align-items: center;
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 12px 24px;
    padding: 14px var(--workspace-gutter);
    color: var(--ink);
    background: var(--peach-soft);
}
.captcha-setup-notice strong {
    font-size: 14px;
}
.captcha-setup-notice p {
    margin: 4px 0 0;
    font-size: 13px;
    line-height: 1.6;
}
.captcha-setup-actions {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 12px 20px;
}
.captcha-setup-actions a {
    color: var(--accent-strong);
    font-size: 13px;
    text-decoration: underline;
    text-underline-offset: 3px;
}
@media (max-width: 767px) {
    .captcha-setup-notice {
        padding: 14px 20px;
    }
    .captcha-setup-actions {
        width: 100%;
        justify-content: space-between;
    }
    .captcha-setup-actions > * {
        min-height: 44px;
        display: inline-flex;
        align-items: center;
    }
}
</style>
