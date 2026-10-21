<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import {
    ArrowRight,
    Picture,
    VideoCamera,
    User,
    Warning,
    Refresh,
    MagicStick,
    Connection,
    Reading,
    Headset,
    Check,
    CircleClose,
    Timer,
    Plus,
    View,
} from '@element-plus/icons-vue'
import { api, formatNumber, formatDate, parseDate } from '../lib/api'
import Mascot from '../components/Mascot.vue'
import SparkIcon from '../components/SparkIcon.vue'
import welcomeArtwork from '../assets/koharu-banner.webp'

const router = useRouter(),
    stats = ref(null),
    health = ref(null),
    accounts = ref([]),
    logs = ref([]),
    vidsStatus = ref(null),
    quotaLoading = ref(false),
    quotaError = ref(''),
    dataErrors = ref([]),
    accountsLoaded = ref(false)
const loading = ref(true),
    error = ref(''),
    range = ref('24h'),
    refreshed = ref('')
const overviewTab = ref('recent')
let timer,
    refreshing = false,
    quotaRequest
const username = localStorage.getItem('admin_username') || '创作者'
const greeting = computed(() => {
    const hour = new Date().getHours()
    return hour < 6 ? '夜深啦' : hour < 12 ? '早上好' : hour < 18 ? '下午好' : '晚上好'
})
const metrics = computed(() => [
    {
        label: '今日图片',
        value: stats.value?.today_images,
        total: `累计 ${formatNumber(stats.value?.total_images)} 张作品`,
        icon: Picture,
        color: 'pink',
    },
    {
        label: '今日视频',
        value: stats.value?.today_videos,
        total: `累计 ${formatNumber(stats.value?.total_videos)} 段视频`,
        icon: VideoCamera,
        color: 'blue',
    },
    {
        label: '活跃账号',
        value: stats.value?.active_tokens,
        total: `共 ${formatNumber(stats.value?.total_tokens)} 个 Flow 账号`,
        icon: User,
        color: 'green',
    },
    {
        label: '今日异常',
        value: stats.value?.today_errors,
        total: `历史 ${formatNumber(stats.value?.total_errors)} 次异常`,
        icon: Warning,
        color: 'peach',
    },
])
const availableCredits = computed(() =>
    accountsLoaded.value
        ? accounts.value.reduce((sum, row) => sum + Number(row.is_active ? row.credits || 0 : 0), 0)
        : null,
)
const vidsQuotas = computed(() =>
    ['image', 'video'].map((kind) => {
        const value = vidsStatus.value?.quota?.[kind] || {}
        const active = Object.hasOwn(value, 'active_remaining')
        const remaining = active ? value.active_remaining : value.remaining
        const total = active ? value.active_total : value.total
        const queriedAt = active ? value.active_queried_at : value.queried_at
        const known = remaining != null && Number.isFinite(Number(remaining))
        const eligible = value.eligible ?? vidsStatus.value?.configured_count ?? 0
        const covered = (active ? value.active_covered : value.covered) ?? (known ? eligible : 0)
        return {
            kind,
            label: kind === 'image' ? '图片' : '视频',
            remaining: known ? formatNumber(remaining) : '—',
            total: total != null ? formatNumber(total) : '—',
            updated: queriedAt ? formatDate(queriedAt) : '',
            stale: Boolean((active ? value.active_stale : value.stale) || quotaError.value),
            partial: eligible > covered,
            covered,
            eligible,
            known,
        }
    }),
)
async function refreshVidsQuota() {
    if (quotaRequest) return quotaRequest
    quotaLoading.value = true
    quotaRequest = api
        .get('/api/admin/vids/status?refresh=true')
        .then((data) => {
            vidsStatus.value = data
            quotaError.value = ''
        })
        .catch((error) => {
            quotaError.value = error.message
        })
        .finally(() => {
            quotaLoading.value = false
            quotaRequest = null
        })
    return quotaRequest
}
const sessionIssues = computed(() =>
    accounts.value.filter((row) => row.is_active && (row.at_expired || row.refresh_error)),
)
const filteredLogs = computed(() => {
    const start = Date.now() - (range.value === '24h' ? 24 : 6) * 3600000
    return logs.value.filter((row) => parseDate(row.created_at).getTime() >= start)
})
const successes = computed(
    () => filteredLogs.value.filter((row) => Number(row.status_code) >= 200 && Number(row.status_code) < 400).length,
)
const successRate = computed(() =>
    filteredLogs.value.length ? Math.round((successes.value / filteredLogs.value.length) * 100) : null,
)
const chart = computed(() => {
    const hours = range.value === '24h' ? 24 : 6,
        now = new Date()
    now.setMinutes(0, 0, 0)
    const buckets = Array.from({ length: hours }, (_, i) => ({
        time: now.getTime() - (hours - 1 - i) * 3600000,
        image: 0,
        video: 0,
        error: 0,
    }))
    for (const row of filteredLogs.value) {
        const time = parseDate(row.created_at).getTime(),
            bucket = buckets.find((item) => time >= item.time && time < item.time + 3600000)
        if (!bucket) continue
        if (Number(row.status_code) >= 400) bucket.error++
        else if (/video|veo|视频/i.test(row.operation)) bucket.video++
        else bucket.image++
    }
    const max = Math.max(1, ...buckets.map((item) => item.image + item.video + item.error))
    return buckets.map((item) => ({
        ...item,
        label: new Date(item.time).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' }),
        height: ((item.image + item.video + item.error) / max) * 100,
    }))
})
const recentLogs = computed(() => logs.value.slice(0, 5))
const status = (row) =>
    Number(row.status_code) >= 400
        ? { label: '需要关注', type: 'danger' }
        : Number(row.status_code) >= 200
          ? { label: '已完成', type: 'success' }
          : { label: row.status_text || '进行中', type: 'info' }
async function load() {
    if (refreshing) return
    refreshing = true
    const results = await Promise.allSettled([
        api.get('/api/stats'),
        api.get('/health'),
        api.get('/api/tokens'),
        api.get('/api/logs?limit=100'),
        api.get('/api/admin/vids/status'),
    ])
    const targets = [stats, health, accounts, logs, vidsStatus]
    results.forEach((result, index) => {
        if (result.status === 'fulfilled') targets[index].value = result.value
    })
    accountsLoaded.value = results[2].status === 'fulfilled' || accountsLoaded.value
    dataErrors.value = results.flatMap((result, index) =>
        result.status === 'rejected' ? [['统计', '服务状态', '账号余额', '创作记录', 'Vids 状态'][index]] : [],
    )
    error.value = results[0].status === 'rejected' ? results[0].reason.message : ''
    refreshed.value = new Date().toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
    loading.value = false
    refreshing = false
    refreshVidsQuota()
}
function refreshVisible() {
    if (!document.hidden) load()
}
onMounted(() => {
    load()
    document.addEventListener('visibilitychange', refreshVisible)
    timer = setInterval(() => {
        if (!document.hidden) load()
    }, 30000)
})
onUnmounted(() => {
    clearInterval(timer)
    document.removeEventListener('visibilitychange', refreshVisible)
})
</script>
<template>
    <div class="dashboard-page" :class="`dashboard-tab-${overviewTab}`">
        <div class="page-heading">
            <div>
                <h1>工坊总览</h1>
                <p>你的创作日常，一眼就明了。</p>
            </div>
            <div class="page-actions">
                <span class="muted updated-note">{{ refreshed ? `更新于 ${refreshed}` : '正在同步工坊' }}</span>
                <el-button :icon="Refresh" :loading="loading" @click="load">刷新数据</el-button>
            </div>
        </div>
        <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="page-alert" />
        <el-alert
            v-else-if="dataErrors.length"
            :title="`${dataErrors.join('、')}暂未同步，点击刷新重新连接。`"
            type="warning"
            show-icon
            :closable="false"
            class="page-alert"
        />
        <section class="welcome-panel">
            <img
                class="welcome-scene"
                :src="welcomeArtwork"
                alt="lay 在樱花树下向你挥手，欢迎开始新的创作"
                width="1920"
                height="1072"
                fetchpriority="high"
            />
            <div class="welcome-copy">
                <div class="welcome-greeting">
                    <span class="greeting-dot"></span>
                    {{ greeting }}，{{ username }}
                </div>
                <h2>今天的灵感，一起实现。</h2>
                <p>
                    从一个小小的想法出发，
                    <br class="welcome-mobile-break" />
                    创造图片、视频与声音里的新世界。
                </p>
                <el-button type="primary" size="large" @click="router.push('/studio')">
                    开始新的创作
                    <el-icon><ArrowRight /></el-icon>
                </el-button>
                <span class="welcome-caption">Flow & Vids · 一个工坊，无限想象</span>
            </div>
            <div class="welcome-mobile-character"><Mascot pose="welcome" mood="sleepy" /></div>
        </section>
        <section class="metric-grid dashboard-metrics" aria-label="创作统计">
            <article v-for="metric in metrics" :key="metric.label" class="metric-card" :class="metric.color">
                <div class="metric-top">
                    <span>{{ metric.label }}</span>
                    <span class="metric-icon">
                        <el-icon><component :is="metric.icon" /></el-icon>
                    </span>
                </div>
                <el-skeleton v-if="loading" :rows="0" animated />
                <strong v-else class="metric-value">
                    {{ formatNumber(metric.value) }}
                    <span v-if="metric.label.includes('图片')">张</span>
                    <span v-else-if="metric.label.includes('视频')">段</span>
                    <span v-else>个</span>
                </strong>
                <p>{{ metric.total }}</p>
            </article>
        </section>
        <nav class="dashboard-overview-tabs" aria-label="总览内容">
            <button :class="{ active: overviewTab === 'recent' }" @click="overviewTab = 'recent'">最近记录</button>
            <button :class="{ active: overviewTab === 'overview' }" @click="overviewTab = 'overview'">
                趋势与额度
            </button>
            <el-button type="primary" @click="router.push('/studio')">
                开始新的创作
                <el-icon><ArrowRight /></el-icon>
            </el-button>
        </nav>
        <div class="dashboard-middle">
            <section class="surface activity-chart">
                <div class="section-heading">
                    <div>
                        <h2>创作的足迹</h2>
                        <p>最近 {{ filteredLogs.length }} 次请求 · {{ range === '24h' ? '24' : '6' }} 小时</p>
                    </div>
                    <el-segmented
                        v-model="range"
                        :options="[
                            { label: '24 小时', value: '24h' },
                            { label: '6 小时', value: '6h' },
                        ]"
                    />
                </div>
                <div class="chart-summary">
                    <strong>
                        {{ filteredLogs.length }}
                        <small>次请求</small>
                    </strong>
                    <span v-if="successRate !== null" class="chart-success">
                        <el-icon><Check /></el-icon>
                        {{ successRate }}% 已完成
                    </span>
                    <span v-else class="muted">等待你的第一份作品</span>
                    <div class="chart-legend">
                        <span>
                            <i class="pink-key"></i>
                            图片 / 其他
                        </span>
                        <span>
                            <i class="blue-key"></i>
                            视频
                        </span>
                        <span>
                            <i class="peach-key"></i>
                            异常
                        </span>
                    </div>
                </div>
                <div
                    class="chart-plot"
                    role="img"
                    :aria-label="`${range === '24h' ? '24' : '6'}小时内共有${filteredLogs.length}次请求，各柱表示每小时请求数量。`"
                >
                    <div class="chart-y">
                        <span>{{ Math.max(1, ...chart.map((b) => b.image + b.video + b.error)) }}</span>
                        <span>0</span>
                    </div>
                    <div class="chart-columns">
                        <el-tooltip
                            v-for="(bucket, i) in chart"
                            :key="bucket.time"
                            :content="`${bucket.label} · 图片/其他 ${bucket.image} · 视频 ${bucket.video} · 异常 ${bucket.error}`"
                            placement="top"
                        >
                            <div class="chart-column">
                                <div
                                    class="chart-bar"
                                    :style="{ height: `${Math.max(bucket.height, 2)}%` }"
                                    :class="{ 'chart-zero': !bucket.height }"
                                >
                                    <i :style="{ flex: bucket.error }" class="error-bar"></i>
                                    <i :style="{ flex: bucket.video }" class="video-bar"></i>
                                    <i :style="{ flex: bucket.image }" class="image-bar"></i>
                                </div>
                                <span v-if="range === '6h' || i % 4 === 0 || i === chart.length - 1" class="chart-tick">
                                    {{ bucket.label }}
                                </span>
                            </div>
                        </el-tooltip>
                    </div>
                </div>
                <p class="chart-footnote">依据最近 100 条请求记录统计；今日创作数以账号累计统计为准。</p>
            </section>
            <section class="surface energy-panel">
                <div class="section-heading">
                    <div>
                        <h2>创作能量站</h2>
                        <p>让每个想法都有续航</p>
                    </div>
                    <span class="energy-icon">
                        <el-icon><Connection /></el-icon>
                    </span>
                </div>
                <div class="credit-value">
                    {{ loading ? '—' : formatNumber(availableCredits) }}
                    <span>Flow 可用积分</span>
                </div>
                <div class="provider-row">
                    <span class="provider-logo flow-logo">F</span>
                    <div>
                        <strong>Flow</strong>
                        <small>{{ stats?.active_tokens ?? '—' }} 个活跃账号</small>
                    </div>
                    <el-tag :type="health?.has_active_tokens ? 'success' : 'info'" effect="light">
                        {{ health?.has_active_tokens ? '已就绪' : '待接入' }}
                    </el-tag>
                </div>
                <section class="vids-quota-overview" aria-label="Vids 剩余额度" aria-live="polite">
                    <div class="vids-quota-title">
                        <strong>Vids 剩余额度</strong>
                        <span>{{ quotaLoading ? '同步中' : '每分钟自动更新' }}</span>
                    </div>
                    <div class="vids-quota-values">
                        <article v-for="quota in vidsQuotas" :key="quota.kind" :class="{ 'quota-stale': quota.stale }">
                            <span>{{ quota.label }}</span>
                            <p>
                                <strong>{{ quota.remaining }}</strong>
                                <small>/ {{ quota.total }}</small>
                            </p>
                            <small>
                                {{
                                    quota.stale
                                        ? '历史额度 · 更新待重试'
                                        : quota.partial
                                          ? `已同步 ${quota.covered}/${quota.eligible} 个账号`
                                          : quota.updated
                                            ? `更新于 ${quota.updated}`
                                            : vidsStatus?.configured
                                              ? '等待额度同步'
                                              : '尚未接入账号'
                                }}
                            </small>
                        </article>
                    </div>
                    <p v-if="quotaError" class="quota-update-error">额度同步暂未完成，已保留上次结果。</p>
                </section>
                <div class="provider-row">
                    <span class="provider-logo vids-logo">V</span>
                    <div>
                        <strong>Google Vids</strong>
                        <small>{{ vidsStatus?.configured_count ?? '—' }} 个已接入账号</small>
                    </div>
                    <el-tag :type="vidsStatus?.configured ? 'success' : 'info'" effect="light">
                        {{ vidsStatus?.configured ? '已连接' : '待接入' }}
                    </el-tag>
                </div>
                <div v-if="sessionIssues.length" class="energy-notice">
                    <el-icon><Warning /></el-icon>
                    {{ sessionIssues.length }} 个账号会话需要关注
                </div>
                <button class="text-action energy-action" @click="router.push('/accounts')">
                    管理创作账号
                    <el-icon><ArrowRight /></el-icon>
                </button>
            </section>
        </div>
        <div class="dashboard-bottom">
            <section class="surface recent-activity">
                <div class="section-heading">
                    <div>
                        <h2>最近的创作记录</h2>
                        <p>每一步想象，都留下回声</p>
                    </div>
                    <el-button text @click="router.push('/logs')">
                        查看全部
                        <el-icon><ArrowRight /></el-icon>
                    </el-button>
                </div>
                <el-skeleton v-if="loading" :rows="4" animated />
                <div v-else-if="!recentLogs.length" class="dashboard-empty">
                    <span class="empty-pictogram">
                        <el-icon><Picture /></el-icon>
                    </span>
                    <div>
                        <h3>这里，等着你的第一份作品</h3>
                        <p>在创作工作台生成作品后，记录会自动出现。</p>
                    </div>
                    <el-button type="primary" plain @click="router.push('/studio')">去创作</el-button>
                </div>
                <el-table v-else :data="recentLogs" class="dashboard-log-table">
                    <el-table-column label="创作任务" min-width="175">
                        <template #default="{ row }">
                            <div class="activity-task">
                                <span class="task-type" :class="/video|veo/i.test(row.operation) ? 'blue' : 'pink'">
                                    <el-icon>
                                        <component :is="/video|veo/i.test(row.operation) ? VideoCamera : Picture" />
                                    </el-icon>
                                </span>
                                <div>
                                    <strong>{{ row.operation || '生成任务' }}</strong>
                                    <small>{{ row.token_email || `账号 #${row.token_id ?? '—'}` }}</small>
                                </div>
                            </div>
                        </template>
                    </el-table-column>
                    <el-table-column label="状态" width="105">
                        <template #default="{ row }">
                            <el-tag :type="status(row).type">{{ status(row).label }}</el-tag>
                        </template>
                    </el-table-column>
                    <el-table-column label="时间" min-width="130">
                        <template #default="{ row }">
                            <span class="muted">{{ formatDate(row.created_at) }}</span>
                        </template>
                    </el-table-column>
                </el-table>
            </section>
            <section class="ideas-panel">
                <div class="section-heading">
                    <div>
                        <h2>灵感快捷入口</h2>
                        <p>让创作，少一点等待</p>
                    </div>
                    <span><SparkIcon /></span>
                </div>
                <button class="idea-link" @click="router.push('/studio')">
                    <span class="idea-icon pink">
                        <el-icon><MagicStick /></el-icon>
                    </span>
                    <span>
                        <strong>打开创作工作台</strong>
                        <small>图片、视频与语音，一起探索</small>
                    </span>
                    <el-icon><ArrowRight /></el-icon>
                </button>
                <button class="idea-link" @click="router.push('/accounts')">
                    <span class="idea-icon blue">
                        <el-icon><Plus /></el-icon>
                    </span>
                    <span>
                        <strong>连接新账号</strong>
                        <small>为工坊补充一点创作能量</small>
                    </span>
                    <el-icon><ArrowRight /></el-icon>
                </button>
                <button class="idea-link" @click="router.push('/guide')">
                    <span class="idea-icon green">
                        <el-icon><Reading /></el-icon>
                    </span>
                    <span>
                        <strong>把灵感接入应用</strong>
                        <small>查看 API 端点与调用示例</small>
                    </span>
                    <el-icon><ArrowRight /></el-icon>
                </button>
                <p class="ideas-note">lay 提醒：记得给你的奇思妙想留点空间。</p>
            </section>
        </div>
    </div>
</template>
<style scoped>
.welcome-panel {
    position: relative;
    min-height: 295px;
    padding: 35px 38px;
    margin-bottom: 23px;
    border-radius: 18px;
    background: var(--pink-soft);
    overflow: hidden;
    display: flex;
    align-items: center;
    isolation: isolate;
}
.dashboard-overview-tabs {
    display: none;
}
.welcome-scene {
    position: absolute;
    inset: 0;
    width: 100%;
    height: 100%;
    object-fit: cover;
    object-position: center 8%;
    z-index: -1;
}
.welcome-mobile-character {
    display: none;
}
.welcome-copy {
    width: 56%;
    padding: 0;
    position: relative;
    z-index: 1;
}
.welcome-greeting {
    display: flex;
    align-items: center;
    gap: 8px;
    color: #93526d;
    font-size: 13px;
    margin-bottom: 14px;
}
.greeting-dot {
    width: 6px;
    height: 6px;
    border-radius: 50%;
    background: var(--accent);
}
.welcome-copy h2 {
    font-size: clamp(26px, 2.2vw, 33px);
    line-height: 1.4;
    color: #30334a;
    font-weight: 730;
    letter-spacing: -0.045em;
    max-width: 560px;
    margin: 0 0 13px;
}
.welcome-copy > p {
    color: #836573;
    font-size: 14px;
    line-height: 1.85;
    max-width: 400px;
    margin: 0 0 22px;
}
.welcome-copy > .el-button {
    height: 41px;
    padding: 0 22px;
    border-radius: 10px;
    font-size: 13px;
    gap: 17px;
}
.welcome-caption {
    display: block;
    margin-top: 16px;
    font-size: 12px;
    color: #8d657a;
}
.welcome-mobile-break {
    display: none;
}
.metric-grid {
    margin-bottom: 23px;
}
.metric-card {
    padding: 19px 23px;
    border-radius: 14px;
}
.metric-value {
    font-size: 32px;
}
.metric-icon {
    width: 31px;
    height: 31px;
    border-radius: 9px;
    font-size: 17px;
}
.metric-card > p {
    font-size: 12px;
}
.dashboard-middle {
    gap: 22px;
    margin-bottom: 22px;
}
.dashboard-bottom {
    gap: 22px;
}
.chart-plot {
    height: 144px;
}
.surface {
    border-radius: 16px;
}
.chart-summary > strong {
    font-size: 29px;
}
.credit-value {
    font-size: 32px;
}
.energy-panel {
    display: flex;
    flex-direction: column;
}
.energy-panel .provider-row {
    flex: 0 0 auto;
}
.vids-quota-overview {
    padding: 17px 0 5px;
    width: 100%;
    order: 1;
}
.energy-panel .energy-action {
    order: 3;
    margin-top: 15px;
}
.energy-panel .energy-notice {
    order: 2;
}
.vids-quota-title {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 8px;
    margin-bottom: 12px;
}
.vids-quota-title > strong {
    font-size: 13px;
    font-weight: 600;
}
.vids-quota-title > span {
    color: var(--muted);
    font-size: 12px;
}
.vids-quota-values {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
}
.vids-quota-values > article {
    padding: 13px 14px;
    border-radius: 12px;
    background: var(--blue-soft);
    min-width: 0;
}
.vids-quota-values > article:first-child {
    background: var(--pink-soft);
}
.vids-quota-values article > span {
    color: var(--muted);
    font-size: 12px;
}
.vids-quota-values p {
    margin: 4px 0 7px;
    display: flex;
    align-items: baseline;
    gap: 5px;
    flex-wrap: wrap;
}
.vids-quota-values strong {
    color: var(--ink);
    font-size: 26px;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    line-height: 1.3;
}
.vids-quota-values small {
    font-size: 11px;
    color: var(--muted);
    line-height: 1.6;
    display: block;
}
.vids-quota-values .quota-stale > small,
.quota-update-error {
    color: var(--peach);
}
.quota-update-error {
    margin-top: 10px;
    font-size: 12px;
    line-height: 1.7;
}
.dashboard-middle {
    align-items: start;
}
.activity-chart {
    min-height: 100%;
}
.dashboard-empty {
    min-height: 155px;
}
.ideas-panel {
    border-radius: 16px;
}
html.dark .welcome-scene {
    opacity: 0.28;
}
html.dark .welcome-copy h2 {
    color: var(--ink);
}
html.dark .welcome-greeting,
html.dark .welcome-copy > p,
html.dark .welcome-caption {
    color: #efd0dd;
}
@media (min-width: 1600px) {
    .welcome-panel {
        min-height: 325px;
        padding: 42px 46px;
    }
    .welcome-copy h2 {
        font-size: 37px;
    }
    .welcome-copy > p {
        font-size: 15px;
    }
}
@media (max-width: 1200px) {
    .welcome-panel {
        padding: 30px;
        min-height: 290px;
    }
    .welcome-copy {
        width: 58%;
    }
    .welcome-copy h2 {
        font-size: 28px;
    }
    .welcome-copy > p {
        max-width: 290px;
        font-size: 13px;
    }
}
@media (max-width: 980px) {
    .welcome-panel {
        min-height: 295px;
        padding: 29px;
    }
    .welcome-copy {
        width: 57%;
    }
    .welcome-copy h2 {
        font-size: 27px;
    }
    .welcome-copy > p {
        max-width: 250px;
    }
    .welcome-caption {
        max-width: 250px;
    }
}
@media (max-width: 767px) {
    .welcome-panel {
        min-height: 318px;
        padding: 25px 24px;
        border-radius: 16px;
    }
    .welcome-scene {
        width: 670px;
        max-width: none;
        left: auto;
        right: -86px;
        object-fit: cover;
        object-position: right top;
        opacity: 0.9;
    }
    .welcome-copy {
        width: 70%;
    }
    .welcome-copy h2 {
        font-size: 25px;
        max-width: 240px;
    }
    .welcome-copy > p {
        font-size: 13px;
        max-width: 220px;
    }
    .welcome-greeting {
        font-size: 12px;
    }
    .welcome-caption {
        font-size: 11px;
        max-width: 190px;
    }
    .welcome-copy > .el-button {
        height: 40px;
        padding: 0 15px;
        font-size: 12px;
    }
    .metric-card {
        padding: 17px 19px;
    }
    .metric-value {
        font-size: 31px;
    }
    .chart-plot {
        height: 130px;
    }
    .dashboard-empty {
        min-height: 175px;
    }
    .surface {
        border-radius: 16px;
    }
    .dashboard-middle,
    .dashboard-bottom {
        gap: 18px;
    }
}
@media (max-width: 430px) {
    .welcome-panel {
        min-height: 310px;
        padding: 24px 21px;
    }
    .welcome-copy {
        width: 70%;
    }
    .welcome-copy h2 {
        font-size: 24px;
        max-width: 235px;
    }
    .welcome-copy > p {
        max-width: 191px;
        font-size: 12px;
    }
    .welcome-scene {
        right: -92px;
        width: 640px;
        object-position: right top;
    }
    .welcome-mobile-break {
        display: block;
    }
}
@media (max-width: 767px) {
    .welcome-panel {
        min-height: 348px;
        padding: 24px;
        display: block;
    }
    .welcome-scene {
        display: none;
    }
    .welcome-copy {
        width: 100%;
    }
    .welcome-copy h2 {
        max-width: 330px;
        font-size: 26px;
        margin-bottom: 17px;
    }
    .welcome-copy > p {
        max-width: 195px;
        font-size: 13px;
        margin-bottom: 22px;
    }
    .welcome-caption {
        max-width: 180px;
        margin-top: 15px;
        font-size: 11px;
    }
    .welcome-mobile-character {
        display: block;
        position: absolute;
        width: 160px;
        height: 217px;
        bottom: -13px;
        right: 1px;
        pointer-events: none;
    }
    .welcome-mobile-character :deep(.mascot) {
        width: 100%;
        height: 100%;
    }
    .welcome-mobile-character :deep(.mascot img) {
        object-fit: contain;
        object-position: center bottom;
    }
}
@media (max-width: 430px) {
    .welcome-panel {
        min-height: 348px;
        padding: 24px 21px;
    }
    .welcome-copy h2 {
        font-size: 25px;
        max-width: 315px;
    }
    .welcome-copy > p {
        max-width: 177px;
        font-size: 12px;
    }
    .welcome-mobile-character {
        width: 150px;
        height: 204px;
        bottom: -12px;
        right: 0;
    }
}
@media (min-width: 1051px) {
    .dashboard-page {
        display: flex;
        flex-direction: column;
        height: 100%;
        min-height: 0;
    }
    .dashboard-page > .page-heading {
        margin-bottom: 12px;
        flex-shrink: 0;
    }
    .dashboard-page > .page-heading p,
    .welcome-panel,
    .ideas-panel,
    .dashboard-metrics .metric-card > p {
        display: none;
    }
    .dashboard-metrics {
        flex-shrink: 0;
        gap: 12px;
        margin-bottom: 12px;
    }
    .dashboard-metrics .metric-card {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 10px 16px;
    }
    .dashboard-metrics .metric-top {
        margin: 0;
    }
    .dashboard-metrics .metric-icon {
        display: none;
    }
    .dashboard-metrics .metric-value {
        font-size: 26px;
    }
    .dashboard-overview-tabs {
        display: flex;
        flex-shrink: 0;
        align-items: center;
        gap: 4px;
        margin-bottom: 12px;
    }
    .dashboard-overview-tabs > button:not(.el-button) {
        padding: 7px 14px;
        border: 0;
        border-radius: 8px;
        color: var(--muted);
        background: transparent;
    }
    .dashboard-overview-tabs > button.active {
        color: var(--accent);
        background: var(--pink-soft);
    }
    .dashboard-overview-tabs > .el-button {
        margin-left: auto;
    }
    .dashboard-bottom,
    .dashboard-middle {
        flex: 1;
        min-height: 0;
        margin: 0;
    }
    .dashboard-tab-recent .dashboard-middle,
    .dashboard-tab-overview .dashboard-bottom {
        display: none;
    }
    .dashboard-bottom {
        display: flex;
    }
    .recent-activity {
        display: flex;
        flex-direction: column;
        flex: 1;
        min-height: 0;
        padding: 14px 18px;
    }
    .recent-activity > .section-heading {
        margin-bottom: 10px;
        flex-shrink: 0;
    }
    .recent-activity .section-heading p {
        display: none;
    }
    .recent-activity > .dashboard-log-table {
        min-height: 0;
        flex: 1;
    }
    .recent-activity > .dashboard-log-table :deep(.el-table__inner-wrapper) {
        height: 100%;
    }
    .recent-activity > .dashboard-log-table :deep(.el-table__body-wrapper) {
        flex: 1;
        min-height: 0;
    }
    .dashboard-middle {
        grid-template-columns: minmax(0, 1.4fr) minmax(280px, 1fr);
        align-items: stretch;
        gap: 14px;
    }
    .dashboard-middle > .surface {
        min-height: 0;
        padding: 14px 18px;
        overflow: auto;
        scrollbar-width: thin;
    }
    .energy-panel .section-heading,
    .activity-chart .section-heading {
        margin-bottom: 10px;
    }
    .energy-panel .section-heading p,
    .energy-panel .provider-row,
    .activity-chart .section-heading p,
    .chart-footnote {
        display: none;
    }
    .credit-value {
        display: flex;
        align-items: baseline;
        gap: 12px;
        margin-bottom: 5px;
        font-size: 26px;
    }
    .credit-value > span {
        font-size: 12px;
    }
    .vids-quota-overview {
        padding-top: 10px;
    }
    .vids-quota-values > article {
        padding: 8px 10px;
    }
    .energy-panel .energy-action {
        margin-top: 10px;
    }
    .chart-summary {
        flex-wrap: wrap;
    }
    .chart-plot {
        height: clamp(90px, 18vh, 170px);
        margin-top: 12px;
    }
    .dashboard-empty {
        flex: 1;
        min-height: 0;
    }
}
</style>
