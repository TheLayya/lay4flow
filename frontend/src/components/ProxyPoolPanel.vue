<script setup>
import { computed, ref, watch } from 'vue'
import { Check, Delete, Plus, Refresh, Search } from '@element-plus/icons-vue'

const props = defineProps({
    rows: { type: Array, default: () => [] },
    busy: { type: [Set, Object], default: () => new Set() },
    loading: { type: Boolean, default: false },
    importing: { type: Boolean, default: false },
    expandedId: { type: [Number, String], default: null },
    bulkState: {
        type: Object,
        default: () => ({ running: false, done: 0, total: 0, action: '' }),
    },
})

const emit = defineEmits([
    'reload',
    'add',
    'import',
    'edit',
    'expand',
    'bind',
    'copy',
    'toggle',
    'check',
    'remove',
    'bulk',
])
const search = ref('')
const stateFilter = ref('all')
const healthFilter = ref('all')
const currentPage = ref(1)
const pageSize = ref(20)
const selected = ref(new Set())
const operationsLocked = computed(() => props.bulkState?.running || props.importing || (props.busy?.size || 0) > 0)

const rowId = (row) => row?.id ?? row?.proxy_url
const isBusy = (row, action) => {
    const key = `pool:${rowId(row)}:${action}`
    return typeof props.busy?.has === 'function' ? props.busy.has(key) : Boolean(props.busy?.[key])
}
const health = (row) => (row?.last_check_ok === true ? 'healthy' : row?.last_check_ok === false ? 'failed' : 'unknown')
const healthLabel = (row) => ({ healthy: '连接正常', failed: '连接异常', unknown: '未检测' })[health(row)]
const healthType = (row) => ({ healthy: 'success', failed: 'danger', unknown: 'info' })[health(row)]
const circuitLabel = (row) =>
    row.enabled === false
        ? '已停用'
        : { cooling: '熔断冷却', probing: '恢复探针中', ready: '可分配' }[row.circuit_state]
const circuitType = (row) =>
    row.enabled === false ? 'info' : { cooling: 'danger', probing: 'warning', ready: 'success' }[row.circuit_state]
const hasTraffic = (row) => Number(row.success_count || 0) + Number(row.failure_count || 0) > 0
const successLabel = (row) =>
    hasTraffic(row) && Number.isFinite(row.success_rate) ? `${Math.round(row.success_rate * 100)}%` : '暂无记录'
const probeLabel = (row) => {
    if (!row.cooldown_until) return ''
    const time = new Date(row.cooldown_until)
    if (Number.isNaN(time.getTime())) return ''
    return `最早 ${time.toLocaleTimeString('zh-CN', { hour12: false })} 重探`
}

const filteredRows = computed(() => {
    const query = search.value.trim().toLowerCase()
    return props.rows.filter((row) => {
        const matchesSearch = !query || `${row.proxy_url || ''} ${row.note || ''}`.toLowerCase().includes(query)
        const matchesState = stateFilter.value === 'all' || (stateFilter.value === 'enabled') === Boolean(row.enabled)
        const matchesHealth =
            healthFilter.value === 'all' ||
            (['cooling', 'probing'].includes(healthFilter.value)
                ? row.circuit_state === healthFilter.value
                : health(row) === healthFilter.value)
        return matchesSearch && matchesState && matchesHealth
    })
})
const pageRows = computed(() => {
    const start = (currentPage.value - 1) * pageSize.value
    return filteredRows.value.slice(start, start + pageSize.value)
})
const enabledCount = computed(() => props.rows.filter((row) => row.enabled).length)
const healthyCount = computed(() => props.rows.filter((row) => row.last_check_ok === true).length)
const failedCount = computed(() => props.rows.filter((row) => row.last_check_ok === false).length)
const unknownCount = computed(() => props.rows.filter((row) => row.last_check_ok == null).length)
const pageIds = computed(() => pageRows.value.map(rowId))
const selectedRows = computed(() => props.rows.filter((row) => selected.value.has(rowId(row))))
const allPageSelected = computed(() => pageIds.value.length > 0 && pageIds.value.every((id) => selected.value.has(id)))
const somePageSelected = computed(() => pageIds.value.some((id) => selected.value.has(id)))
const selectedCount = computed(() => selected.value.size)
const pageCountLabel = computed(() => `${filteredRows.value.length} 个出口`)
const checkTimeLabel = (row) => (row.last_check_at ? String(row.last_check_at).replace('T', ' ').slice(0, 19) : '—')

function resetPage() {
    const max = Math.max(1, Math.ceil(filteredRows.value.length / pageSize.value))
    if (currentPage.value > max) currentPage.value = max
}
function toggleRow(row, checked) {
    if (operationsLocked.value) return
    const next = new Set(selected.value)
    const id = rowId(row)
    checked ? next.add(id) : next.delete(id)
    selected.value = next
}
function togglePage(checked) {
    if (operationsLocked.value) return
    const next = new Set(selected.value)
    pageIds.value.forEach((id) => (checked ? next.add(id) : next.delete(id)))
    selected.value = next
}
function clearSelection() {
    if (operationsLocked.value) return
    selected.value = new Set()
}
function selectFiltered() {
    if (operationsLocked.value) return
    selected.value = new Set([...selected.value, ...filteredRows.value.map(rowId)])
}
function emitBulk(action) {
    if (operationsLocked.value) return
    const ids = selectedRows.value.map(rowId).filter(Boolean)
    if (!ids.length) return
    emit('bulk', { action, ids })
}
function onPageSizeChange(value) {
    pageSize.value = Number(value)
    currentPage.value = 1
}

watch([search, stateFilter, healthFilter], () => {
    currentPage.value = 1
})
watch(() => props.rows.length, resetPage)
watch(
    () => props.rows,
    () => {
        const existing = new Set(props.rows.map(rowId))
        selected.value = new Set([...selected.value].filter((id) => existing.has(id)))
    },
)
</script>

<template>
    <section class="proxy-pool-panel" aria-label="统一代理池">
        <header class="pool-panel-heading">
            <dl class="pool-stats" aria-label="代理池统计">
                <div>
                    <dt>全部</dt>
                    <dd>{{ rows.length }}</dd>
                </div>
                <div class="is-enabled">
                    <dt>已启用</dt>
                    <dd>{{ enabledCount }}</dd>
                </div>
                <div class="is-healthy">
                    <dt>健康</dt>
                    <dd>{{ healthyCount }}</dd>
                </div>
                <div class="is-failed">
                    <dt>异常</dt>
                    <dd>{{ failedCount }}</dd>
                </div>
                <div>
                    <dt>未检测</dt>
                    <dd>{{ unknownCount }}</dd>
                </div>
            </dl>
            <div class="pool-heading-actions">
                <el-button
                    size="small"
                    :icon="Refresh"
                    :loading="loading"
                    :disabled="operationsLocked"
                    @click="emit('reload')"
                >
                    刷新
                </el-button>
                <el-button size="small" :disabled="operationsLocked" @click="emit('import')">批量导入</el-button>
                <el-button size="small" type="primary" :icon="Plus" :disabled="operationsLocked" @click="emit('add')">
                    添加出口
                </el-button>
            </div>
        </header>

        <div class="pool-toolbar">
            <el-input v-model="search" size="small" clearable :prefix-icon="Search" placeholder="搜索地址或备注" />
            <el-select v-model="stateFilter" size="small" aria-label="启用状态">
                <el-option label="全部状态" value="all" />
                <el-option label="已启用" value="enabled" />
                <el-option label="已停用" value="disabled" />
            </el-select>
            <el-select v-model="healthFilter" size="small" aria-label="健康状态">
                <el-option label="全部健康" value="all" />
                <el-option label="连接正常" value="healthy" />
                <el-option label="连接异常" value="failed" />
                <el-option label="未检测" value="unknown" />
                <el-option label="熔断冷却" value="cooling" />
                <el-option label="恢复探针中" value="probing" />
            </el-select>
            <span class="pool-result-count">{{ pageCountLabel }}</span>
        </div>

        <div class="pool-selectionbar">
            <el-checkbox
                :model-value="allPageSelected"
                :indeterminate="!allPageSelected && somePageSelected"
                aria-label="选择本页代理"
                :disabled="operationsLocked"
                @change="togglePage"
            >
                选择本页
            </el-checkbox>
            <el-button text size="small" :disabled="!filteredRows.length || operationsLocked" @click="selectFiltered">
                选择全部 {{ filteredRows.length }} 个匹配项
            </el-button>
            <span v-if="selectedCount">已选 {{ selectedCount }} 个（跨页、跨筛选保留）</span>
            <el-button v-if="selectedCount" size="small" text :disabled="operationsLocked" @click="clearSelection">
                清除选择
            </el-button>
            <div v-if="selectedCount || bulkState?.running" class="pool-bulkbar">
                <span v-if="bulkState?.running" class="bulk-progress">正在处理 {{ bulkState.total }} 个出口…</span>
                <template v-else>
                    <el-button
                        size="small"
                        type="primary"
                        :disabled="!selectedCount || operationsLocked"
                        @click="emitBulk('enable')"
                    >
                        批量启用
                    </el-button>
                    <el-button size="small" :disabled="!selectedCount || operationsLocked" @click="emitBulk('disable')">
                        批量停用
                    </el-button>
                    <el-button
                        size="small"
                        :icon="Check"
                        :disabled="!selectedCount || operationsLocked"
                        @click="emitBulk('check')"
                    >
                        批量检测
                    </el-button>
                    <el-button
                        size="small"
                        type="danger"
                        plain
                        :icon="Delete"
                        :disabled="!selectedCount || operationsLocked"
                        @click="emitBulk('delete')"
                    >
                        批量删除
                    </el-button>
                </template>
            </div>
        </div>

        <div class="pool-table-wrap">
            <el-table
                :data="pageRows"
                row-key="id"
                :max-height="expandedId == null ? 640 : undefined"
                :expand-row-keys="expandedId == null ? [] : [expandedId]"
                :scrollbar-tabindex="0"
                scrollbar-always-on
                @expand-change="
                    (row, expanded) =>
                        emit(
                            'expand',
                            row,
                            expanded.some((item) => item.id === row.id),
                        )
                "
                class="pool-table-dense management-table"
                size="small"
            >
                <el-table-column type="expand" width="32">
                    <template #default="{ row }"><slot name="details" :row="row" /></template>
                </el-table-column>
                <el-table-column width="38" align="center">
                    <template #header>
                        <el-checkbox
                            :model-value="allPageSelected"
                            :indeterminate="!allPageSelected && somePageSelected"
                            aria-label="全选本页代理"
                            :disabled="operationsLocked"
                            @change="togglePage"
                        />
                    </template>
                    <template #default="{ row }">
                        <el-checkbox
                            :model-value="selected.has(rowId(row))"
                            :aria-label="'选择出口 ' + row.id"
                            :disabled="operationsLocked"
                            @change="(value) => toggleRow(row, value)"
                        />
                    </template>
                </el-table-column>
                <el-table-column prop="id" label="ID" width="54" />
                <el-table-column prop="proxy_url" label="出口地址" min-width="270" show-overflow-tooltip>
                    <template #default="{ row }">
                        <code class="proxy-address">{{ row.proxy_url }}</code>
                    </template>
                </el-table-column>
                <el-table-column prop="note" label="备注" min-width="120" show-overflow-tooltip>
                    <template #default="{ row }">{{ row.note || '—' }}</template>
                </el-table-column>
                <el-table-column label="启用" width="64" align="center">
                    <template #default="{ row }">
                        <el-switch
                            :model-value="row.enabled"
                            size="small"
                            :aria-label="'启用出口 ' + row.id"
                            :disabled="operationsLocked"
                            :loading="isBusy(row, 'toggle')"
                            @change="emit('toggle', row)"
                        />
                    </template>
                </el-table-column>
                <el-table-column label="绑定账号" width="86" align="right">
                    <template #default="{ row }">
                        <el-button
                            class="pool-bind-trigger"
                            text
                            size="small"
                            :disabled="operationsLocked"
                            :aria-label="`为出口 ${row.id} 绑定账号`"
                            @click="emit('bind', row)"
                        >
                            绑定 {{ row.bound_account_count || 0 }}
                        </el-button>
                    </template>
                </el-table-column>
                <el-table-column label="连接状态" width="98">
                    <template #default="{ row }">
                        <el-tag :type="healthType(row)" size="small" effect="plain">{{ healthLabel(row) }}</el-tag>
                    </template>
                </el-table-column>
                <el-table-column label="最后检测" width="150">
                    <template #default="{ row }">
                        <time class="pool-timestamp" :datetime="row.last_check_at || undefined">
                            {{ checkTimeLabel(row) }}
                        </time>
                    </template>
                </el-table-column>
                <el-table-column label="提交调度" width="110">
                    <template #default="{ row }">
                        <el-tag v-if="row.circuit_state" :type="circuitType(row)" size="small" effect="plain">
                            {{ circuitLabel(row) }}
                        </el-tag>
                        <span v-else class="pool-muted">暂无记录</span>
                    </template>
                </el-table-column>
                <el-table-column label="成功率" width="80" align="right">
                    <template #default="{ row }">
                        <span class="pool-number">{{ successLabel(row) }}</span>
                    </template>
                </el-table-column>
                <el-table-column label="连败" width="58" align="right">
                    <template #default="{ row }">
                        <span class="pool-number">{{ row.consecutive_failures || 0 }}</span>
                    </template>
                </el-table-column>
                <el-table-column label="处理中" width="70" align="right">
                    <template #default="{ row }">
                        <span class="pool-number">{{ row.inflight || 0 }}</span>
                    </template>
                </el-table-column>
                <el-table-column label="重探时间" width="156">
                    <template #default="{ row }">
                        <span class="pool-timestamp">
                            {{ row.circuit_state === 'cooling' ? probeLabel(row) || '—' : '—' }}
                        </span>
                    </template>
                </el-table-column>
                <el-table-column label="操作" width="196" fixed="right">
                    <template #default="{ row }">
                        <div class="row-actions">
                            <el-button text size="small" @click="emit('expand', row, expandedId !== row.id)">
                                {{ expandedId === row.id ? '收起' : '展开' }}
                            </el-button>
                            <el-button text size="small" @click="emit('copy', row.proxy_url, '代理地址')">
                                复制
                            </el-button>
                            <el-button text size="small" :disabled="operationsLocked" @click="emit('edit', row)">
                                编辑
                            </el-button>
                            <el-button
                                text
                                size="small"
                                :disabled="operationsLocked"
                                :loading="isBusy(row, 'check')"
                                @click="emit('check', row)"
                            >
                                检测
                            </el-button>
                            <el-button
                                text
                                size="small"
                                type="danger"
                                :disabled="operationsLocked"
                                :loading="isBusy(row, 'delete')"
                                @click="emit('remove', row)"
                            >
                                删除
                            </el-button>
                        </div>
                    </template>
                </el-table-column>
                <template #empty><el-empty :image-size="48" description="没有匹配的代理出口" /></template>
            </el-table>
        </div>

        <footer class="pool-pagination">
            <span v-if="selectedCount" class="selected-hint">已选 {{ selectedCount }} 个</span>
            <el-pagination
                v-model:current-page="currentPage"
                :page-size="pageSize"
                :page-sizes="[10, 20, 50]"
                :total="filteredRows.length"
                layout="total, sizes, prev, pager, next"
                small
                @size-change="onPageSizeChange"
            />
        </footer>
    </section>
</template>

<style scoped>
.proxy-pool-panel {
    min-width: 0;
    container-type: inline-size;
}
.pool-panel-heading,
.pool-toolbar,
.pool-selectionbar,
.pool-bulkbar,
.pool-pagination,
.pool-heading-actions,
.row-actions {
    display: flex;
    align-items: center;
    gap: 8px;
}
.pool-panel-heading {
    justify-content: space-between;
    flex-wrap: wrap;
    gap: 8px 16px;
    margin-bottom: 8px;
}
.pool-stats {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 6px 16px;
    margin: 0;
    font-size: 12px;
    line-height: 24px;
}
.pool-stats > div {
    display: flex;
    align-items: baseline;
    gap: 5px;
}
.pool-stats dt,
.pool-muted {
    color: var(--muted);
}
.pool-stats dd {
    margin: 0;
    color: var(--ink);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
}
.pool-stats .is-enabled dd,
.pool-stats .is-healthy dd {
    color: var(--success);
}
.pool-stats .is-failed dd {
    color: var(--el-color-danger);
}
.pool-heading-actions {
    margin-left: auto;
}
.pool-heading-actions .el-button + .el-button,
.pool-bulkbar .el-button + .el-button,
.row-actions .el-button + .el-button {
    margin-left: 0;
}
.pool-toolbar {
    flex-wrap: wrap;
    margin-bottom: 6px;
}
.pool-toolbar .el-input {
    width: 240px;
    max-width: 100%;
}
.pool-toolbar .el-select {
    width: 112px;
}
.pool-result-count {
    color: var(--muted);
    font-size: 12px;
    margin-left: auto;
}
.pool-selectionbar {
    flex-wrap: wrap;
    min-height: 28px;
    margin-bottom: 6px;
    gap: 4px 8px;
}
.pool-selectionbar .el-checkbox {
    height: 24px;
    margin-right: 0;
}
.pool-selectionbar > span {
    color: var(--muted);
    font-size: 12px;
}
.pool-bulkbar {
    flex-wrap: wrap;
    margin-left: auto;
    gap: 5px;
}
.bulk-progress,
.selected-hint {
    color: var(--accent);
    font-size: 12px;
}
.pool-table-wrap {
    width: 100%;
    min-width: 0;
    border: 1px solid var(--line);
    border-radius: 4px;
    overflow: hidden;
}
.pool-table-dense :deep(.el-table__inner-wrapper::before) {
    display: none;
}
.pool-table-dense :deep(.el-table__cell) {
    padding: 4px 0;
}
.pool-table-dense :deep(.cell) {
    padding-inline: 7px;
    line-height: 24px;
    white-space: nowrap;
}
.pool-table-dense :deep(.el-checkbox) {
    height: 24px;
}
.pool-table-dense :deep(.el-scrollbar__wrap:focus-visible) {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
}
.proxy-address,
.pool-timestamp {
    font-size: 12px;
    color: var(--ink);
}
.pool-number,
.pool-timestamp {
    font-variant-numeric: tabular-nums;
}
.pool-timestamp {
    color: var(--muted);
    font-size: 11px;
}
.row-actions {
    gap: 1px;
    white-space: nowrap;
}
.row-actions .el-button {
    padding-inline: 4px;
}
.pool-pagination {
    justify-content: flex-end;
    gap: 12px;
    padding-top: 8px;
    flex-wrap: wrap;
}
.pool-pagination :deep(.el-pagination__sizes .el-select) {
    width: 108px;
}
.selected-hint {
    margin-right: auto;
}
@media (max-width: 640px) {
    .pool-stats {
        gap: 3px 12px;
    }
    .pool-toolbar .el-input {
        width: 100%;
    }
    .pool-toolbar .el-select {
        flex: 1;
        min-width: 100px;
    }
    .pool-bulkbar {
        width: 100%;
        margin-left: 0;
    }
    .pool-pagination {
        justify-content: flex-start;
    }
    .pool-pagination :deep(.el-pagination) {
        max-width: 100%;
        flex-wrap: wrap;
        gap: 5px;
    }
    .pool-table-dense :deep(.el-table__cell.el-table-fixed-column--right) {
        position: static !important;
    }
    .pool-table-dense :deep(.el-table__fixed-right-patch),
    .pool-table-dense :deep(.el-table__fixed-right),
    .pool-table-dense :deep(.el-table-fixed-column--right.is-first-column::before) {
        display: none;
    }
}
@media (min-width: 1051px) {
    .proxy-pool-panel {
        display: flex;
        flex-direction: column;
        min-height: 0;
    }
    .pool-panel-heading,
    .pool-toolbar,
    .pool-selectionbar,
    .pool-pagination {
        flex-shrink: 0;
    }
    .pool-table-wrap {
        flex: 1;
        min-height: 0;
        display: flex;
    }
    .pool-table-wrap > .el-table {
        height: 100%;
        max-height: none !important;
        min-height: 0;
        flex: 1;
    }
    .pool-table-wrap :deep(.el-table__inner-wrapper) {
        height: 100%;
    }
    .pool-table-wrap :deep(.el-table__body-wrapper) {
        flex: 1;
        min-height: 0;
    }
}
</style>
