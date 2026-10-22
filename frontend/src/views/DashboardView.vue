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
    Connection,
    Check,
} from '@element-plus/icons-vue'
import { api, formatNumber, formatDate, parseDate } from '../lib/api'

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
let timer,
    refreshing = false,
    quotaRequest
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
const recentLogs = computed(() => logs.value.slice(0, 10))
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
    <div class="dashboard-page">
        <div class="page-heading">
            <div>
                <h1>工坊总览</h1>
                <p>你的创作日常，一眼就明了。</p>
            </div>
            <div class="page-actions">
                <span class="muted updated-note">{{ refreshed ? `更新于 ${refreshed}` : '正在同步工坊' }}</span>
                <el-button :icon="Refresh" :loading="loading" @click="load">刷新数据</el-button>
                <el-button type="primary" @click="router.push('/studio')">
                    开始新的创作
                    <el-icon><ArrowRight /></el-icon>
                </el-button>
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
        <div class="dashboard-content">
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
                    <button class="text-action" @click="router.push('/accounts')">
                        管理账号
                        <el-icon><ArrowRight /></el-icon>
                    </button>
                </div>
                <div class="credit-value">
                    <span>Flow 可用积分</span>
                    <strong>{{ loading ? '—' : formatNumber(availableCredits) }}</strong>
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
                    <div class="vids-quota-values">
                        <article v-for="quota in vidsQuotas" :key="quota.kind" :class="{ 'quota-stale': quota.stale }">
                            <span>Vids {{ quota.label }}</span>
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
            </section>
          </div>
        </div>
    </div>
</template>
<style scoped>
.dashboard-page > .page-heading {
    margin-bottom: 16px;
}
.dashboard-page > .page-heading p {
    display: none;
}
.page-actions {
    flex-wrap: wrap;
}
.page-actions > .el-button {
    margin-left: 0;
}
.dashboard-metrics {
    gap: 12px;
    margin-bottom: 16px;
}
.dashboard-metrics .metric-card {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 12px;
    padding: 12px 16px;
    border-radius: 12px;
}
.dashboard-metrics .metric-top {
    margin: 0;
}
.dashboard-metrics .metric-icon {
    display: none;
}
.dashboard-metrics .metric-value {
    margin-left: auto;
    font-size: 26px;
}
.dashboard-metrics .metric-card > p {
    width: 100%;
    margin: 0;
    font-size: 12px;
}
/* Two columns share the available width: 1.35fr + 1fr + one 16px gutter. */
.dashboard-content {
    display: grid;
    grid-template-columns: minmax(0, 1.35fr) minmax(0, 1fr);
    align-items: start;
    gap: 16px;
}
.dashboard-middle {
    display: grid;
    grid-template-columns: minmax(0, 1fr);
    gap: 16px;
    margin: 0;
    min-width: 0;
}
.dashboard-content .surface {
    min-width: 0;
    padding: 18px 20px;
    border-radius: 14px;
}
.dashboard-content .section-heading {
    margin-bottom: 14px;
}
.dashboard-content .section-heading h2 {
    font-size: 16px;
}
.dashboard-content .section-heading p {
    display: none;
}
.dashboard-log-table {
    width: 100%;
}
.chart-summary {
    flex-wrap: wrap;
    gap: 8px 12px;
}
.chart-summary > strong {
    font-size: 26px;
}
.chart-legend {
    font-size: 12px;
}
.chart-plot {
    height: 130px;
    margin-top: 14px;
}
.chart-footnote {
    margin-top: 8px;
    font-size: 12px;
    line-height: 1.6;
    color: var(--muted);
}
.energy-panel {
    display: grid;
    grid-template-columns: minmax(90px, .8fr) minmax(0, 1.7fr);
    gap: 0 12px;
}
.energy-panel > .section-heading,
.energy-panel > .energy-notice {
    grid-column: 1 / -1;
}
.energy-panel .provider-row {
    display: none;
}
.credit-value {
    display: flex;
    flex-direction: column;
    align-items: flex-start;
    justify-content: center;
    gap: 8px;
    margin: 0;
    font-size: 24px;
}
.credit-value > span {
    font-size: 13px;
}
.vids-quota-overview {
    width: 100%;
    padding: 0;
}
.vids-quota-values {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 12px;
}
.vids-quota-values > article {
    min-width: 0;
    padding: 8px 10px;
    border-radius: 10px;
    background: var(--surface-soft);
}
.vids-quota-values article > span,
.vids-quota-values small {
    color: var(--muted);
    font-size: 12px;
}
.vids-quota-values p {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 5px;
    margin: 4px 0 6px;
}
.vids-quota-values strong {
    font-size: 22px;
    font-weight: 650;
    font-variant-numeric: tabular-nums;
    line-height: 1.3;
}
.vids-quota-values article > small {
    display: block;
    line-height: 1.6;
}
.vids-quota-values .quota-stale > small,
.quota-update-error {
    color: var(--peach);
}
.quota-update-error {
    margin-top: 10px;
    font-size: 12px;
    line-height: 1.6;
}
.energy-panel .energy-notice {
    margin-top: 12px;
}
.dashboard-empty {
    min-height: 150px;
}
@media (min-width: 1051px) {
    .dashboard-page {
        display: flex;
        flex-direction: column;
        height: 100%;
        min-height: 0;
    }
    .dashboard-page > .page-heading,
    .dashboard-metrics {
        flex-shrink: 0;
    }
    .dashboard-content {
        flex: 1;
        min-height: 0;
        align-items: stretch;
    }
    .recent-activity {
        display: flex;
        flex-direction: column;
        min-height: 0;
    }
    .recent-activity > .section-heading {
        flex-shrink: 0;
    }
    .recent-activity > .dashboard-log-table {
        flex: 1;
        min-height: 0;
    }
    .dashboard-log-table :deep(.el-table__inner-wrapper) {
        height: 100%;
    }
    .dashboard-log-table :deep(.el-table__body-wrapper) {
        flex: 1;
        min-height: 0;
    }
    .dashboard-middle {
        align-content: start;
        overflow: auto;
        scrollbar-width: thin;
    }
    .chart-plot {
        height: clamp(65px, 12vh, 130px);
        margin-top: 10px;
    }
    .dashboard-content .surface {
        padding: 14px 16px;
    }
    .dashboard-content .section-heading {
        margin-bottom: 10px;
    }
    .chart-footnote {
        margin-top: 4px;
        font-size: 11px;
    }
}
@media (max-width: 1200px) {
    .dashboard-content {
        grid-template-columns: minmax(0, 1.1fr) minmax(0, 1fr);
        gap: 12px;
    }
    .dashboard-content .surface {
        padding: 16px;
    }
    .updated-note {
        display: none;
    }
}
@media (max-width: 1050px) {
    .dashboard-content {
        grid-template-columns: minmax(0, 1fr);
    }
    .dashboard-middle {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .dashboard-metrics .metric-value {
        font-size: 24px;
    }
}
@media (max-width: 700px) {
    .dashboard-middle {
        grid-template-columns: minmax(0, 1fr);
    }
    .dashboard-metrics .metric-card {
        padding: 12px;
    }
    .dashboard-metrics .metric-value {
        width: 100%;
        margin-left: 0;
    }
    .dashboard-page .page-actions {
        justify-content: flex-start;
    }
    .page-actions > .el-button {
        min-height: 40px;
    }
    .chart-legend {
        width: 100%;
        margin-left: 0;
    }
    .dashboard-content .section-heading {
        gap: 8px;
    }
    .dashboard-log-table :deep(.cell) {
        white-space: nowrap;
    }
}
@media (min-width: 1051px) and (max-height: 760px) {
    .dashboard-metrics .metric-card > p,
    .chart-footnote,
    .vids-quota-values article > small {
        display: none;
    }
    .dashboard-metrics .metric-card {
        padding-block: 9px;
    }
    .dashboard-content .surface {
        padding: 12px 14px;
    }
    .dashboard-middle {
        gap: 12px;
    }
    .energy-panel {
        display: block;
    }
    .energy-panel .credit-value {
        flex-direction: row;
        align-items: baseline;
        justify-content: space-between;
        gap: 8px;
        margin: 0 0 8px;
    }
    .vids-quota-values p {
        margin-bottom: 0;
    }
    .chart-plot {
        height: 60px;
    }
}
</style>
