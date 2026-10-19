<script setup>
import { computed, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
    Refresh,
    Search,
    Delete,
    Download,
    Picture,
    VideoCamera,
    Clock,
    CircleCheck,
    Warning,
    CopyDocument,
    View,
    Link,
    Document,
} from '@element-plus/icons-vue'
import { api, notifyError, parseDate } from '../lib/api'
import Mascot from '../components/Mascot.vue'

const logs = ref([])
const loading = ref(true)
const loadError = ref('')
const search = ref('')
const status = ref('all')
const operation = ref('all')
const timeRange = ref(null)
const page = ref(1)
const pageSize = ref(10)
const tableSort = reactive({ prop: 'duration', order: null })
const autoRefresh = ref(false)
const detailOpen = ref(false)
const detail = ref(null)
const detailLoading = ref(false)
const detailError = ref('')
const activeId = ref(null)
const payloadTab = ref('request')
const previewed = reactive(new Set())
const previewErrors = reactive(new Set())
const clearing = ref(false)
const detailCache = new Map()
let timer
let pendingLoad
let detailSequence = 0
let listVersion = 0
const stages = {
    started: '任务已启动',
    token_selected: '账号已分配',
    token_ready: '准备生成环境',
    project_ready: '项目已就绪',
    uploading_images: '上传参考图',
    solving_image_captcha: '图片验证码验证',
    submitting_image: '提交图片生成',
    image_generated: '图片已生成',
    preparing_video: '准备视频任务',
    submitting_video: '提交视频生成',
    video_submitted: '视频任务已提交',
    video_polling: '视频生成中',
    caching_image: '保存图片缓存',
    caching_video: '保存视频缓存',
    completed: '已完成',
    failed: '生成失败',
    processing: '处理中',
    upsampling_2k: '正在放大至 2K',
    upsampling_4k: '正在放大至 4K',
    upsampling_1080p: '正在放大至 1080p',
}
const classify = (row) =>
    Number(row.status_code) >= 400 || row.status_text === 'failed'
        ? 'failed'
        : Number(row.status_code) === 200 || row.status_text === 'completed'
          ? 'success'
          : Number(row.status_code) === 102
            ? 'processing'
            : 'other'
const statusLabel = (row) =>
    stages[row.status_text] ||
    { failed: '生成失败', success: '已完成', processing: '处理中', other: row.status_text || '已记录' }[classify(row)]
const statusType = (row) =>
    ({ failed: 'danger', success: 'success', processing: 'warning', other: 'info' })[classify(row)]
const operationLabel = (row) =>
    ({
        generate_image: '图片生成',
        generate_video: '视频生成',
        generate_speech: '语音生成',
        chat_completion: '对话请求',
    })[row.operation] ||
    row.operation ||
    'API 请求'
const operationIcon = (row) =>
    String(row.operation).includes('image') ? Picture : String(row.operation).includes('video') ? VideoCamera : Document
const progress = (row) => Math.max(0, Math.min(100, Number(row.progress || 0)))
const date = (value) => (value ? parseDate(value).toLocaleString('zh-CN', { hour12: false }) : '暂无')
const shortDate = (value) =>
    value
        ? parseDate(value).toLocaleString('zh-CN', {
              month: '2-digit',
              day: '2-digit',
              hour: '2-digit',
              minute: '2-digit',
              second: '2-digit',
              hour12: false,
          })
        : '暂无'
const duration = (value) => `${Number(value || 0).toFixed(2)} s`
const operations = computed(() => [...new Set(logs.value.map((row) => row.operation).filter(Boolean))])
const filtered = computed(() =>
    logs.value.filter((row) => {
        const q = search.value.trim().toLowerCase()
        const matchesTime =
            !timeRange.value ||
            (parseDate(row.created_at) >= timeRange.value[0] && parseDate(row.created_at) <= timeRange.value[1])
        return (
            (!q ||
                `${row.id} ${row.token_email || ''} ${row.operation || ''} ${row.error_summary || ''} ${row.status_text || ''}`
                    .toLowerCase()
                    .includes(q)) &&
            (status.value === 'all' || classify(row) === status.value) &&
            (operation.value === 'all' || operation.value === row.operation) &&
            matchesTime
        )
    }),
)
const sortedRows = computed(() =>
    [...filtered.value].sort((a, b) =>
        tableSort.order
            ? (Number(a[tableSort.prop] || 0) - Number(b[tableSort.prop] || 0)) *
              (tableSort.order === 'ascending' ? 1 : -1)
            : 0,
    ),
)
const paginated = computed(() => sortedRows.value.slice((page.value - 1) * pageSize.value, page.value * pageSize.value))
const stats = computed(() => {
    const success = logs.value.filter((row) => classify(row) === 'success')
    return {
        total: logs.value.length,
        processing: logs.value.filter((row) => classify(row) === 'processing').length,
        failed: logs.value.filter((row) => classify(row) === 'failed').length,
        success: success.length,
        average: success.length
            ? duration(success.reduce((sum, row) => sum + Number(row.duration || 0), 0) / success.length)
            : '暂无',
    }
})
watch([search, status, operation, timeRange, pageSize], () => {
    page.value = 1
})
watch(filtered, (value) => {
    page.value = Math.min(page.value, Math.max(1, Math.ceil(value.length / pageSize.value)))
})
watch(autoRefresh, (enabled) => {
    clearInterval(timer)
    if (enabled)
        timer = setInterval(() => {
            if (!document.hidden) load(true)
        }, 5000)
})
function parse(raw) {
    if (typeof raw !== 'string') return raw
    try {
        return JSON.parse(raw)
    } catch {
        return null
    }
}
function pretty(raw) {
    const parsed = parse(raw)
    return parsed
        ? JSON.stringify(
              parsed,
              (_, value) =>
                  typeof value === 'string' && value.length > 8192
                      ? `[大体积内容已折叠，${value.length.toLocaleString()} 个字符；下载原始数据可查看完整内容]`
                      : value,
              2,
          )
        : String(raw || '暂无数据')
}
const response = computed(() => parse(detail.value?.response_body))
const request = computed(() => parse(detail.value?.request_body))
const performance = computed(() => response.value?.performance || response.value?.timings || null)
const requestPrompt = computed(
    () =>
        request.value?.prompt ||
        (Array.isArray(request.value?.messages)
            ? request.value.messages.find((row) => row?.role === 'user')?.content
            : null),
)
function safeUrl(value) {
    if (typeof value !== 'string' || !value.trim()) return ''
    if (/^data:(image\/(png|jpeg|jpg|webp|gif|avif)|video\/(mp4|webm));base64,/i.test(value)) return value
    try {
        const url = new URL(value, location.origin)
        if (!['http:', 'https:'].includes(url.protocol)) return ''
        if (url.pathname.startsWith('/tmp/')) return `${location.origin}${url.pathname}${url.search}`
        return url.href
    } catch {
        return ''
    }
}
const media = computed(() => {
    const body = response.value
    if (!body || typeof body !== 'object') return []
    const found = []
    const add = (label, value) => {
        const url = safeUrl(value)
        if (url && !found.some((item) => item.url === url)) {
            const video =
                /^data:video\//i.test(url) ||
                /\.(mp4|webm|mov|m3u8)(\?|$)/i.test(url) ||
                /\/video\//i.test(url) ||
                String(detail.value.operation).includes('video')
            found.push({ label, url, kind: video ? 'video' : 'image' })
        }
    }
    add('生成结果', body.url)
    for (const [index, item] of (Array.isArray(body.data) ? body.data : []).entries()) {
        add(`作品 ${index + 1}`, item?.url)
        if (item?.b64_json) add(`作品 ${index + 1}`, `data:image/png;base64,${item.b64_json}`)
    }
    const assets = body.generated_assets || {}
    add('最终图片', assets.final_image_url)
    add('最终视频', assets.final_video_url)
    add(
        assets.upscaled_image?.resolution ? assets.upscaled_image.resolution + ' 高清图片' : '高清图片',
        assets.upscaled_image?.local_url || assets.upscaled_image?.url,
    )
    for (const [index, item] of (Array.isArray(assets.images) ? assets.images : []).entries())
        add(`生成图片 ${index + 1}`, typeof item === 'string' ? item : item?.url || item?.local_url)
    return found
})
function outcome(row) {
    return classify(row) === 'failed'
        ? row.error_summary || '上游请求返回错误，点击详情查看响应'
        : classify(row) === 'success'
          ? '创作结果已返回'
          : classify(row) === 'processing'
            ? '灵感正在变成作品…'
            : row.status_text || '请求已记录'
}
async function load(silent = false) {
    if (pendingLoad) return pendingLoad
    if (!silent) loading.value = true
    const version = listVersion
    pendingLoad = (async () => {
        try {
            const result = await api.get('/api/logs?limit=100')
            if (version !== listVersion) return
            logs.value = result
            loadError.value = ''
            for (const [id, cached] of detailCache) {
                const row = logs.value.find((item) => item.id === id)
                if (!row || row.updated_at !== cached.updated_at) detailCache.delete(id)
            }
            if (detailOpen.value && activeId.value) {
                const row = logs.value.find((item) => item.id === activeId.value)
                if (row && detail.value?.updated_at !== row.updated_at) await openDetail(row, true)
            }
        } catch (error) {
            if (version === listVersion) {
                loadError.value = error.message
                if (!silent) notifyError(error)
            }
        } finally {
            loading.value = false
            pendingLoad = null
        }
    })()
    return pendingLoad
}
async function openDetail(row, silent = false) {
    const sequence = ++detailSequence
    activeId.value = row.id
    detailOpen.value = true
    detailError.value = ''
    if (!silent) {
        detail.value = null
        detailLoading.value = true
        previewed.clear()
        previewErrors.clear()
        payloadTab.value = 'request'
    }
    try {
        const cached = detailCache.get(row.id)
        const result = cached && cached.updated_at === row.updated_at ? cached : await api.get(`/api/logs/${row.id}`)
        if (sequence !== detailSequence || activeId.value !== row.id || !detailOpen.value) return
        detail.value = result
        detailCache.set(row.id, result)
    } catch (error) {
        if (sequence === detailSequence) detailError.value = error.message
    } finally {
        if (sequence === detailSequence) detailLoading.value = false
    }
}
function refreshDetail() {
    if (!detail.value) return
    const row = detail.value
    detailCache.delete(row.id)
    openDetail(row)
}
async function clearLogs() {
    try {
        await ElMessageBox.confirm('所有请求日志及详情将被清空，此操作会移除完整历史记录。', '清空请求日志', {
            confirmButtonText: '确认清空',
            cancelButtonText: '保留日志',
            type: 'warning',
        })
    } catch {
        return
    }
    clearing.value = true
    try {
        await api.delete('/api/logs')
        listVersion++
        detailSequence++
        detailCache.clear()
        detailOpen.value = false
        detail.value = null
        logs.value = []
        ElMessage.success('请求日志已清空')
    } catch (error) {
        notifyError(error)
    } finally {
        clearing.value = false
    }
}
function download(value, name) {
    const url = URL.createObjectURL(
        new Blob([typeof value === 'string' ? value : JSON.stringify(value, null, 2)], { type: 'application/json' }),
    )
    const link = document.createElement('a')
    link.href = url
    link.download = name
    link.click()
    URL.revokeObjectURL(url)
}
function exportLogs() {
    download(filtered.value, `lay4flow-logs-${new Date().toISOString().slice(0, 10)}.json`)
    ElMessage.success(`已导出 ${filtered.value.length} 条日志摘要`)
}
async function copy(raw) {
    try {
        await navigator.clipboard.writeText(typeof raw === 'string' ? raw : JSON.stringify(raw, null, 2))
        ElMessage.success('已复制原始数据')
    } catch (error) {
        notifyError(error)
    }
}
onMounted(() => load())
onUnmounted(() => {
    clearInterval(timer)
    detailSequence++
})
</script>

<template>
    <div class="logs-view">
        <header class="page-heading">
            <div>
                <h1>创作记录</h1>
                <p>跟随每一次请求，看看灵感如何成为作品。</p>
            </div>
            <div class="page-actions">
                <el-button :icon="Download" :disabled="!filtered.length" @click="exportLogs">导出记录</el-button>
                <el-button type="primary" :icon="Refresh" :loading="loading" @click="load()">同步日志</el-button>
            </div>
        </header>
        <div class="log-metrics">
            <article>
                <span class="metric-icon pink">
                    <el-icon><Document /></el-icon>
                </span>
                <div>
                    <span>最近请求</span>
                    <strong>{{ stats.total }}</strong>
                </div>
                <small>最近 100 条</small>
            </article>
            <article>
                <span class="metric-icon green">
                    <el-icon><CircleCheck /></el-icon>
                </span>
                <div>
                    <span>已完成</span>
                    <strong>{{ stats.success }}</strong>
                </div>
                <small>{{ stats.total ? ((100 * stats.success) / stats.total).toFixed(1) : '0' }}% 成功率</small>
            </article>
            <article>
                <span class="metric-icon blue">
                    <el-icon><Clock /></el-icon>
                </span>
                <div>
                    <span>处理中</span>
                    <strong>{{ stats.processing }}</strong>
                </div>
                <small>平均 {{ stats.average }}</small>
            </article>
            <article>
                <span class="metric-icon amber">
                    <el-icon><Warning /></el-icon>
                </span>
                <div>
                    <span>需要关注</span>
                    <strong>{{ stats.failed }}</strong>
                </div>
                <small>查看详情排查原因</small>
            </article>
        </div>
        <el-alert v-if="loadError" :title="loadError" type="error" show-icon :closable="false" class="logs-alert" />
        <section class="surface logs-surface">
            <div class="section-heading">
                <div>
                    <h2>请求时间线</h2>
                    <p>筛选与分页应用于最近 100 条请求记录</p>
                </div>
                <div class="logs-options">
                    <span>实时同步</span>
                    <el-switch v-model="autoRefresh" />
                    <el-tooltip content="清空全部历史日志">
                        <el-button
                            text
                            type="danger"
                            :icon="Delete"
                            :loading="clearing"
                            aria-label="清空日志"
                            @click="clearLogs"
                        />
                    </el-tooltip>
                </div>
            </div>
            <div class="log-filters">
                <el-input
                    v-model="search"
                    placeholder="搜索账号、请求 ID 或错误内容"
                    :prefix-icon="Search"
                    clearable
                    class="log-search"
                />
                <el-select v-model="status" aria-label="筛选请求状态">
                    <el-option label="全部状态" value="all" />
                    <el-option label="已完成" value="success" />
                    <el-option label="处理中" value="processing" />
                    <el-option label="生成失败" value="failed" />
                </el-select>
                <el-select v-model="operation" aria-label="筛选请求类型">
                    <el-option label="全部类型" value="all" />
                    <el-option
                        v-for="kind in operations"
                        :key="kind"
                        :label="operationLabel({ operation: kind })"
                        :value="kind"
                    />
                </el-select>
                <el-date-picker
                    v-model="timeRange"
                    type="datetimerange"
                    start-placeholder="开始时间"
                    end-placeholder="结束时间"
                    range-separator="至"
                    format="MM-DD HH:mm"
                    class="date-filter"
                />
            </div>
            <div v-if="loading && !logs.length" class="table-skeleton"><el-skeleton :rows="7" animated /></div>
            <el-table
                v-else
                :data="paginated"
                row-key="id"
                class="logs-table"
                @sort-change="Object.assign(tableSort, $event)"
                @row-dblclick="(row) => openDetail(row)"
            >
                <el-table-column label="创作请求" min-width="180">
                    <template #default="{ row }">
                        <div class="operation-cell">
                            <span class="operation-icon" :class="{ video: String(row.operation).includes('video') }">
                                <el-icon><component :is="operationIcon(row)" /></el-icon>
                            </span>
                            <div>
                                <strong>{{ operationLabel(row) }}</strong>
                                <small>REQUEST #{{ row.id }}</small>
                            </div>
                        </div>
                    </template>
                </el-table-column>
                <el-table-column label="创作成员" min-width="185">
                    <template #default="{ row }">
                        <span class="account-label">{{ row.token_email || row.token_username || '未分配账号' }}</span>
                        <small class="cell-note">{{ row.token_id ? '成员 #' + row.token_id : '暂无' }}</small>
                    </template>
                </el-table-column>
                <el-table-column label="进度与状态" min-width="150">
                    <template #default="{ row }">
                        <el-tag :type="statusType(row)" effect="light" size="small">{{ statusLabel(row) }}</el-tag>
                        <el-progress
                            v-if="classify(row) === 'processing'"
                            :percentage="progress(row)"
                            :stroke-width="4"
                            class="inline-progress"
                        />
                        <span v-else class="status-code">HTTP {{ row.status_code ?? '暂无' }}</span>
                    </template>
                </el-table-column>
                <el-table-column label="结果摘要" min-width="235">
                    <template #default="{ row }">
                        <el-tooltip :content="outcome(row)" placement="top">
                            <span class="outcome-text" :class="{ failed: classify(row) === 'failed' }">
                                {{ outcome(row) }}
                            </span>
                        </el-tooltip>
                    </template>
                </el-table-column>
                <el-table-column label="耗时" width="95" sortable="custom" prop="duration">
                    <template #default="{ row }">
                        <span class="duration">{{ duration(row.duration) }}</span>
                    </template>
                </el-table-column>
                <el-table-column label="开始时间" width="155">
                    <template #default="{ row }">
                        <span class="date-text">{{ shortDate(row.created_at) }}</span>
                    </template>
                </el-table-column>
                <el-table-column label="详情" width="72" fixed="right">
                    <template #default="{ row }">
                        <el-button
                            text
                            type="primary"
                            :icon="View"
                            aria-label="查看请求详情"
                            @click="openDetail(row)"
                        />
                    </template>
                </el-table-column>
                <template #empty>
                    <el-empty
                        :description="
                            search || status !== 'all' || operation !== 'all' || timeRange
                                ? '没有匹配的创作记录，试试其他条件'
                                : '第一份作品，还在等待你的灵感'
                        "
                    >
                        <template #image><Mascot portrait compact mood="sleepy" class="empty-mascot" /></template>
                        <p class="empty-hint">发起生成任务后，可以在这里查看实时进度。</p>
                    </el-empty>
                </template>
            </el-table>
            <footer class="table-footer">
                <span>
                    共 {{ filtered.length }} 条匹配记录
                    <span v-if="autoRefresh">· 每 5 秒同步</span>
                </span>
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
        <p class="logs-footnote">每一次生成都值得记录。列表仅加载轻量摘要，完整请求与响应在详情中按需读取。</p>
        <el-drawer
            v-model="detailOpen"
            :size="'min(790px, 100vw)'"
            :title="'创作请求 #' + (activeId || '')"
            @closed="detailSequence++"
        >
            <el-skeleton v-if="detailLoading" :rows="12" animated />
            <el-alert v-else-if="detailError" :title="detailError" type="error" show-icon :closable="false" />
            <template v-else-if="detail">
                <div class="log-detail-heading">
                    <span class="operation-icon large">
                        <el-icon><component :is="operationIcon(detail)" /></el-icon>
                    </span>
                    <div>
                        <h2>{{ operationLabel(detail) }}</h2>
                        <p>{{ detail.token_email || '未分配账号' }}</p>
                    </div>
                    <el-tag :type="statusType(detail)">{{ statusLabel(detail) }}</el-tag>
                </div>
                <div class="detail-summary">
                    <div>
                        <span>状态码</span>
                        <strong>{{ detail.status_code }}</strong>
                    </div>
                    <div>
                        <span>总耗时</span>
                        <strong>{{ duration(detail.duration) }}</strong>
                    </div>
                    <div>
                        <span>当前进度</span>
                        <strong>{{ progress(detail) }}%</strong>
                    </div>
                </div>
                <el-alert
                    v-if="classify(detail) === 'failed'"
                    :title="detail.error_summary || '请求生成失败'"
                    type="error"
                    :closable="false"
                    show-icon
                    class="detail-alert"
                />
                <el-alert
                    v-else-if="classify(detail) === 'success'"
                    title="创作完成，结果已经返回"
                    type="success"
                    :closable="false"
                    show-icon
                    class="detail-alert"
                />
                <el-timeline class="detail-timeline">
                    <el-timeline-item :timestamp="date(detail.created_at)" placement="top" color="#d792af">
                        <strong>请求已进入工作室</strong>
                        <p>成员 #{{ detail.token_id || '暂无' }} · {{ operationLabel(detail) }}</p>
                    </el-timeline-item>
                    <el-timeline-item
                        :timestamp="date(detail.updated_at || detail.created_at)"
                        placement="top"
                        :type="statusType(detail)"
                    >
                        <strong>{{ statusLabel(detail) }}</strong>
                        <p>{{ outcome(detail) }}</p>
                    </el-timeline-item>
                </el-timeline>
                <section v-if="requestPrompt" class="prompt-preview">
                    <span>这次的灵感</span>
                    <p>{{ typeof requestPrompt === 'string' ? requestPrompt : JSON.stringify(requestPrompt) }}</p>
                </section>
                <section v-if="media.length && classify(detail) === 'success'" class="media-section">
                    <div class="detail-section-heading">
                        <h3>生成作品</h3>
                        <span>{{ media.length }} 份结果</span>
                    </div>
                    <article v-for="(item, index) in media" :key="item.url" class="media-result">
                        <div class="media-result-header">
                            <strong>{{ item.label }}</strong>
                            <el-button
                                v-if="!item.url.startsWith('data:')"
                                tag="a"
                                :href="item.url"
                                target="_blank"
                                rel="noopener noreferrer"
                                text
                                type="primary"
                                :icon="Link"
                            >
                                打开作品
                            </el-button>
                        </div>
                        <div v-if="previewErrors.has(index)" class="preview-error">
                            预览加载失败，可以通过作品链接打开原文件。
                        </div>
                        <div v-else-if="!previewed.has(index)" class="preview-placeholder">
                            <el-icon><component :is="item.kind === 'video' ? VideoCamera : Picture" /></el-icon>
                            <p>点击查看{{ item.kind === 'video' ? '视频' : '图片' }}作品</p>
                            <el-button :icon="View" @click="previewed.add(index)">加载预览</el-button>
                        </div>
                        <video
                            v-else-if="item.kind === 'video'"
                            :src="item.url"
                            controls
                            preload="metadata"
                            @error="previewErrors.add(index)"
                        />
                        <el-image
                            v-else
                            :src="item.url"
                            :preview-src-list="media.filter((row) => row.kind === 'image').map((row) => row.url)"
                            fit="contain"
                            preview-teleported
                            @error="previewErrors.add(index)"
                        />
                    </article>
                </section>
                <section v-if="performance" class="performance-section">
                    <el-collapse>
                        <el-collapse-item title="生成性能与耗时" name="performance">
                            <pre>{{ JSON.stringify(performance, null, 2) }}</pre>
                        </el-collapse-item>
                    </el-collapse>
                </section>
                <section class="payload-section">
                    <el-tabs v-model="payloadTab">
                        <el-tab-pane label="请求数据" name="request" />
                        <el-tab-pane label="响应数据" name="response" />
                    </el-tabs>
                    <div class="payload-actions">
                        <span>JSON · 大体积内容折叠显示</span>
                        <el-button
                            text
                            :icon="CopyDocument"
                            @click="copy(payloadTab === 'request' ? detail.request_body : detail.response_body)"
                        >
                            复制完整数据
                        </el-button>
                        <el-button
                            text
                            :icon="Download"
                            @click="
                                download(
                                    payloadTab === 'request' ? detail.request_body : detail.response_body,
                                    `request-${detail.id}-${payloadTab}.json`,
                                )
                            "
                        >
                            下载
                        </el-button>
                    </div>
                    <pre class="payload-code">{{
                        pretty(payloadTab === 'request' ? detail.request_body : detail.response_body)
                    }}</pre>
                </section>
            </template>
            <template #footer>
                <el-button v-if="detail" :icon="Refresh" :loading="detailLoading" @click="refreshDetail">
                    刷新详情
                </el-button>
                <el-button type="primary" @click="detailOpen = false">完成</el-button>
            </template>
        </el-drawer>
    </div>
</template>

<style scoped>
.log-metrics {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 15px;
    margin-bottom: 26px;
}
.log-metrics article {
    display: flex;
    gap: 14px;
    align-items: center;
    position: relative;
    background: var(--surface);
    border-radius: 18px;
    padding: 20px 18px;
    display: grid;
    grid-template-columns: 39px minmax(0, 1fr);
    column-gap: 14px;
    row-gap: 9px;
}
.metric-icon {
    width: 39px;
    height: 39px;
    display: grid;
    place-items: center;
    border-radius: 12px;
    font-size: 19px;
    flex-shrink: 0;
}
.metric-icon.pink {
    background: var(--pink-soft);
    color: var(--accent);
}
.metric-icon.green {
    background: var(--green-soft);
    color: var(--success);
}
.metric-icon.blue {
    background: var(--blue-soft);
    color: var(--blue);
}
.metric-icon.amber {
    background: var(--peach-soft);
    color: var(--peach);
}
.log-metrics article > div > span {
    font-size: 13px;
    color: var(--muted);
    display: block;
}
.log-metrics strong {
    display: block;
    font-size: 26px;
    letter-spacing: -0.6px;
    margin-top: 5px;
    line-height: 1.1;
    font-variant-numeric: tabular-nums;
}
.log-metrics small {
    position: static;
    grid-column: 1 / -1;
    font-size: 13px;
    color: var(--muted);
}
.logs-surface {
    padding: 0;
    overflow: hidden;
}
.section-heading {
    padding: 23px 26px 15px;
}
.section-heading h2 {
    margin: 0;
    font-size: 16px;
}
.section-heading p {
    font-size: 13px;
    color: var(--muted);
    margin: 7px 0 0;
}
.logs-options {
    display: flex;
    align-items: center;
    gap: 10px;
    color: var(--muted);
    font-size: 13px;
}
.log-filters {
    display: flex;
    flex-wrap: wrap;
    gap: 10px;
    padding: 9px 26px 23px;
}
.log-search {
    width: 260px;
    flex: 1;
    min-width: 210px;
}
.log-filters > .el-select {
    width: 125px;
}
.log-filters > .date-filter {
    width: 255px;
    flex-grow: 0;
}
.operation-cell {
    display: flex;
    align-items: center;
    gap: 12px;
}
.operation-icon {
    width: 34px;
    height: 34px;
    border-radius: 11px;
    display: grid;
    place-items: center;
    background: var(--pink-soft);
    color: var(--accent);
    flex-shrink: 0;
    font-size: 17px;
}
.operation-icon.video {
    background: var(--blue-soft);
    color: var(--blue);
}
.operation-cell strong {
    font-size: 14px;
    font-weight: 600;
}
.operation-cell small {
    font-size: 13px;
    color: var(--muted);
    letter-spacing: 0.5px;
    display: block;
    margin-top: 5px;
}
.account-label {
    font-size: 13px;
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.cell-note {
    font-size: 13px;
    color: var(--muted);
    display: block;
    margin-top: 5px;
}
.status-code {
    font-size: 13px;
    color: var(--muted);
    display: block;
    margin-top: 5px;
    letter-spacing: 0.4px;
}
.inline-progress {
    margin-top: 8px;
    max-width: 125px;
}
.inline-progress :deep(.el-progress__text) {
    font-size: 13px !important;
    min-width: 28px;
}
.outcome-text {
    font-size: 13px;
    color: var(--muted);
    display: -webkit-box;
    -webkit-line-clamp: 2;
    -webkit-box-orient: vertical;
    overflow: hidden;
    line-height: 1.8;
}
.outcome-text.failed {
    color: var(--el-color-danger);
}
.duration {
    font-size: 13px;
    font-variant-numeric: tabular-nums;
    font-weight: 550;
}
.date-text {
    font-size: 13px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
}
.logs-table :deep(.el-table__cell) {
    padding: 16px 0;
}
.logs-table :deep(th) {
    background: var(--ground);
    font-size: 13px;
    color: var(--muted);
    font-weight: 600;
}
.table-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 14px;
    padding: 18px 26px;
}
.table-footer > span {
    font-size: 13px;
    color: var(--muted);
}
.table-skeleton {
    padding: 28px;
}
.logs-alert {
    margin-bottom: 20px;
}
.logs-footnote {
    font-size: 13px;
    line-height: 1.8;
    color: var(--muted);
    padding: 12px 5px;
}

.empty-hint {
    font-size: 13px;
    color: var(--muted);
}
.log-detail-heading {
    display: flex;
    align-items: center;
    gap: 15px;
    margin-bottom: 23px;
}
.operation-icon.large {
    width: 56px;
    height: 56px;
    border-radius: 18px;
    font-size: 25px;
}
.log-detail-heading h2 {
    font-size: 18px;
    margin: 0;
}
.log-detail-heading p {
    font-size: 13px;
    color: var(--muted);
    margin: 7px 0 0;
}
.log-detail-heading > .el-tag {
    margin-left: auto;
}
.detail-summary {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    gap: 20px;
    background: var(--surface-soft);
    padding: 20px 23px;
    border-radius: 15px;
    margin-bottom: 23px;
}
.detail-summary span {
    font-size: 13px;
    color: var(--muted);
}
.detail-summary strong {
    display: block;
    font-size: 24px;
    margin-top: 8px;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.6px;
}
.detail-alert {
    margin-bottom: 26px;
}
.detail-timeline {
    margin: 10px 0 28px;
    padding-left: 5px;
}
.detail-timeline strong {
    font-size: 13px;
}
.detail-timeline p {
    font-size: 13px;
    color: var(--muted);
    line-height: 1.7;
}
.detail-timeline :deep(.el-timeline-item__timestamp) {
    font-size: 13px;
}
.prompt-preview {
    padding: 20px;
    background: var(--pink-soft);
    border-radius: 14px;
    margin-bottom: 25px;
}
.prompt-preview > span {
    font-size: 13px;
    color: var(--accent);
    font-weight: 650;
}
.prompt-preview p {
    font-size: 13px;
    line-height: 1.9;
    margin: 11px 0 0;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
}
.detail-section-heading {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 15px;
}
.detail-section-heading h3 {
    font-size: 13px;
    margin: 0;
}
.detail-section-heading span {
    font-size: 13px;
    color: var(--muted);
}
.media-result {
    border-radius: 15px;
    overflow: hidden;
    margin-bottom: 15px;
}
.media-result-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 10px 17px;
}
.media-result-header strong {
    font-size: 13px;
}
.preview-placeholder {
    min-height: 180px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    background: var(--surface-soft);
    padding: 20px;
}
.preview-placeholder > .el-icon {
    font-size: 28px;
    color: var(--accent);
}
.preview-placeholder p {
    font-size: 13px;
    color: var(--muted);
}
.media-result > .el-image {
    display: block;
    width: 100%;
    max-height: 420px;
    background: var(--surface-soft);
}
.media-result video {
    display: block;
    width: 100%;
    max-height: 430px;
    background: #302838;
}
.preview-error {
    font-size: 13px;
    color: var(--el-color-danger);
    padding: 30px;
    text-align: center;
}
.media-section {
    margin-bottom: 28px;
}
.payload-actions {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-wrap: wrap;
    margin: 0 0 14px;
}
.payload-actions > span {
    font-size: 13px;
    color: var(--muted);
    margin-right: auto;
}
.payload-actions :deep(.el-button) {
    font-size: 13px;
}
.payload-code,
.performance-section pre {
    font-family: Consolas, 'Cascadia Code', monospace;
    background: var(--surface-soft);
    padding: 20px;
    font-size: 13px;
    line-height: 1.85;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    border-radius: 13px;
    max-height: 420px;
    overflow: auto;
    color: var(--ink);
}
.performance-section {
    margin-bottom: 23px;
}
.payload-section {
    margin-top: 25px;
}

@media (max-width: 1100px) {
    .log-metrics {
        gap: 10px;
    }
    .log-metrics article {
        padding: 18px 14px 35px;
        gap: 10px;
    }
    .log-metrics small {
        left: 64px;
    }
    .log-filters {
        padding: 9px 20px 22px;
    }
    .section-heading {
        padding: 22px 20px 15px;
    }
    .log-filters > .date-filter {
        width: 220px;
    }
    .log-search {
        min-width: 200px;
    }
    .table-footer {
        padding: 17px 20px;
    }
}
@media (max-width: 700px) {
    .log-metrics {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .log-metrics article {
        padding: 17px 13px 33px;
    }
    .log-metrics strong {
        font-size: 24px;
    }
    .log-metrics small {
        left: 61px;
        font-size: 13px;
    }
    .metric-icon {
        width: 34px;
        height: 34px;
        font-size: 17px;
    }
    .section-heading {
        padding: 20px 16px;
    }
    .logs-options {
        gap: 7px;
        font-size: 13px;
    }
    .log-filters {
        padding: 9px 16px 20px;
    }
    .log-search {
        width: 100%;
        flex: none;
    }
    .log-filters > .el-select {
        width: calc(50% - 5px);
    }
    .log-filters > .date-filter {
        width: 100%;
    }
    .table-footer {
        flex-direction: column;
        align-items: flex-start;
        padding: 16px;
    }
    .detail-summary {
        gap: 14px;
        padding: 17px;
    }
    .detail-summary strong {
        font-size: 20px;
    }
    .log-detail-heading {
        flex-wrap: wrap;
    }
    .log-detail-heading p {
        overflow-wrap: anywhere;
    }
    .payload-code {
        padding: 15px;
        font-size: 13px;
    }
    .payload-actions > span {
        width: 100%;
    }
}

.operation-cell strong {
    font-size: 14px;
}
.account-label,
.duration,
.date-text,
.outcome-text {
    font-size: 14px;
}
.log-detail-heading p,
.detail-timeline p {
    font-size: 13px;
}
.prompt-preview p {
    font-size: 13px;
}

.logs-view .logs-surface {
    padding: 0;
}
.logs-view .section-heading {
    margin-bottom: 0;
}

html.dark .operation-icon {
    color: var(--accent);
    background: var(--pink-soft);
}
html.dark .operation-icon.video {
    background: var(--blue-soft);
    color: var(--blue);
}
html.dark .outcome-text.failed,
html.dark .preview-error {
    color: var(--el-color-danger);
}
html.dark .metric-icon.pink {
    color: var(--accent);
}
html.dark .metric-icon.blue {
    color: var(--blue);
}
html.dark .metric-icon.green {
    color: var(--success);
}
html.dark .metric-icon.amber {
    color: var(--peach);
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
.logs-view :deep(.el-empty__description p) {
    font-size: 14px;
}
.logs-view :deep(.el-empty__image) {
    width: 96px;
}
@media (max-width: 1100px) {
    .log-metrics article {
        padding: 18px 14px;
    }
}
@media (max-width: 700px) {
    .logs-view > .page-heading {
        flex-direction: column;
        align-items: stretch;
    }
    .page-actions {
        justify-content: flex-start;
    }
    .logs-view .section-heading {
        align-items: flex-start;
        flex-wrap: wrap;
    }
    .log-metrics article {
        grid-template-columns: 34px minmax(0, 1fr);
        padding: 17px 13px;
        column-gap: 10px;
    }
}
</style>
