<script setup>
import { computed, ref, watch } from 'vue'
import { Check, Delete, Edit, Plus, Refresh, Search } from '@element-plus/icons-vue'

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

const filteredRows = computed(() => {
    const query = search.value.trim().toLowerCase()
    return props.rows.filter((row) => {
        const matchesSearch = !query || `${row.proxy_url || ''} ${row.note || ''}`.toLowerCase().includes(query)
        const matchesState = stateFilter.value === 'all' || (stateFilter.value === 'enabled') === Boolean(row.enabled)
        const matchesHealth = healthFilter.value === 'all' || health(row) === healthFilter.value
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
            <div>
                <div class="pool-title-line">
                    <h3>代理出口</h3>
                    <el-tag size="small" round>{{ rows.length }}</el-tag>
                </div>
                <p>统一管理连接、账号绑定与媒体下载出口</p>
            </div>
            <div class="pool-heading-actions">
                <el-button :icon="Refresh" :loading="loading" :disabled="operationsLocked" @click="emit('reload')">
                    刷新
                </el-button>
                <el-button :disabled="operationsLocked" @click="emit('import')">批量导入</el-button>
                <el-button type="primary" :icon="Plus" :disabled="operationsLocked" @click="emit('add')">
                    添加出口
                </el-button>
            </div>
        </header>

        <div class="pool-stats" aria-label="代理池统计">
            <div>
                <strong>{{ rows.length }}</strong>
                <span>全部</span>
            </div>
            <div class="is-enabled">
                <strong>{{ enabledCount }}</strong>
                <span>已启用</span>
            </div>
            <div class="is-healthy">
                <strong>{{ healthyCount }}</strong>
                <span>健康</span>
            </div>
            <div class="is-failed">
                <strong>{{ failedCount }}</strong>
                <span>异常</span>
            </div>
            <div>
                <strong>{{ unknownCount }}</strong>
                <span>未检测</span>
            </div>
        </div>

        <div class="pool-toolbar">
            <el-input v-model="search" clearable :prefix-icon="Search" placeholder="搜索地址或备注" />
            <el-select v-model="stateFilter" aria-label="启用状态" style="width: 112px">
                <el-option label="全部状态" value="all" />
                <el-option label="已启用" value="enabled" />
                <el-option label="已停用" value="disabled" />
            </el-select>
            <el-select v-model="healthFilter" aria-label="健康状态" style="width: 112px">
                <el-option label="全部健康" value="all" />
                <el-option label="连接正常" value="healthy" />
                <el-option label="连接异常" value="failed" />
                <el-option label="未检测" value="unknown" />
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
        </div>
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

        <div class="pool-table-wrap">
            <el-table
                :data="pageRows"
                row-key="id"
                :max-height="expandedId == null ? 640 : undefined"
                :expand-row-keys="expandedId == null ? [] : [expandedId]"
                @expand-change="
                    (row, expanded) =>
                        emit(
                            'expand',
                            row,
                            expanded.some((item) => item.id === row.id),
                        )
                "
                class="pool-table-dense"
                :header-cell-style="{ height: '38px' }"
            >
                <el-table-column type="expand" width="40">
                    <template #default="{ row }"><slot name="details" :row="row" /></template>
                </el-table-column>
                <el-table-column width="46" align="center">
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
                <el-table-column label="代理出口" min-width="320">
                    <template #default="{ row }">
                        <div class="proxy-cell">
                            <code>{{ row.proxy_url }}</code>
                            <small>{{ row.note || `出口 #${row.id}` }}</small>
                        </div>
                    </template>
                </el-table-column>
                <el-table-column label="启用" min-width="92">
                    <template #default="{ row }">
                        <el-switch
                            :model-value="row.enabled"
                            :aria-label="'启用出口 ' + row.id"
                            :disabled="operationsLocked"
                            :loading="isBusy(row, 'toggle')"
                            @change="emit('toggle', row)"
                        />
                    </template>
                </el-table-column>
                <el-table-column label="绑定账号" min-width="110" align="right">
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
                <el-table-column label="健康 / 最后检测" min-width="170">
                    <template #default="{ row }">
                        <el-tag :type="healthType(row)" size="small">{{ healthLabel(row) }}</el-tag>
                        <span class="last-check">
                            {{
                                row.last_check_at
                                    ? String(row.last_check_at).replace('T', ' ').slice(5, 16)
                                    : '尚未检测'
                            }}
                        </span>
                    </template>
                </el-table-column>
                <el-table-column label="操作" min-width="258" fixed="right">
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
                <template #empty>
                    <el-empty :image-size="48" description="没有匹配的代理出口" />
                </template>
            </el-table>
        </div>

        <div class="pool-mobile-list" :class="{ 'has-expanded': expandedId != null }" aria-label="代理出口列表">
            <article v-for="row in pageRows" :key="rowId(row)" class="pool-mobile-card">
                <div class="mobile-card-top">
                    <el-checkbox
                        :model-value="selected.has(rowId(row))"
                        :aria-label="'选择出口 ' + row.id"
                        :disabled="operationsLocked"
                        @change="(value) => toggleRow(row, value)"
                    />
                    <code>{{ row.proxy_url }}</code>
                    <el-tag :type="healthType(row)" size="small">{{ healthLabel(row) }}</el-tag>
                </div>
                <p>
                    {{ row.note || `出口 #${row.id}` }}
                    <el-button
                        class="pool-bind-trigger"
                        text
                        size="small"
                        :disabled="operationsLocked"
                        :aria-label="`为出口 ${row.id} 绑定账号`"
                        @click="emit('bind', row)"
                    >
                        绑定 {{ row.bound_account_count || 0 }} 个账号
                    </el-button>
                </p>
                <div class="mobile-card-meta">
                    <span>最后检测 {{ row.last_check_at || '—' }}</span>
                    <el-switch
                        :model-value="row.enabled"
                        :aria-label="'启用出口 ' + row.id"
                        :disabled="operationsLocked"
                        :loading="isBusy(row, 'toggle')"
                        @change="emit('toggle', row)"
                    />
                </div>
                <footer class="row-actions">
                    <el-button size="small" @click="emit('expand', row, expandedId !== row.id)">
                        {{ expandedId === row.id ? '收起' : '展开' }}
                    </el-button>
                    <el-button size="small" @click="emit('copy', row.proxy_url, '代理地址')">复制</el-button>
                    <el-button size="small" :disabled="operationsLocked" :icon="Edit" @click="emit('edit', row)">
                        编辑
                    </el-button>
                    <el-button
                        size="small"
                        :disabled="operationsLocked"
                        :icon="Check"
                        :loading="isBusy(row, 'check')"
                        @click="emit('check', row)"
                    >
                        检测
                    </el-button>
                    <el-button
                        size="small"
                        type="danger"
                        plain
                        :icon="Delete"
                        :disabled="operationsLocked"
                        :loading="isBusy(row, 'delete')"
                        @click="emit('remove', row)"
                    >
                        删除
                    </el-button>
                </footer>
                <slot v-if="expandedId === row.id" name="details" :row="row" />
            </article>
            <el-empty v-if="!pageRows.length" :image-size="48" description="没有匹配的代理出口" />
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
    container: proxy-pool / inline-size;
}
.pool-panel-heading,
.pool-toolbar,
.pool-selectionbar,
.pool-bulkbar,
.pool-pagination,
.pool-heading-actions,
.pool-title-line,
.row-actions,
.mobile-card-top,
.mobile-card-meta {
    display: flex;
    align-items: center;
}
.pool-panel-heading {
    justify-content: space-between;
    gap: 16px;
    margin-bottom: 12px;
}
.pool-title-line {
    gap: 8px;
}
.pool-title-line h3 {
    font-size: 15px;
}
.pool-panel-heading p {
    color: var(--muted);
    font-size: 12px;
    margin-top: 2px;
}
.pool-heading-actions,
.pool-toolbar {
    gap: 8px;
}
.pool-stats {
    display: grid;
    grid-template-columns: repeat(5, minmax(0, 1fr));
    gap: 8px;
    margin-bottom: 12px;
}
.pool-stats > div {
    background: var(--surface-soft);
    border-radius: 10px;
    padding: 8px 12px;
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 8px;
}
.pool-stats strong {
    font-size: 18px;
    line-height: 1;
}
.pool-stats span {
    color: var(--muted);
    font-size: 12px;
}
.pool-stats .is-enabled strong,
.pool-stats .is-healthy strong {
    color: var(--success);
}
.pool-stats .is-failed strong {
    color: var(--el-color-danger);
}
.pool-toolbar {
    margin-bottom: 8px;
}
.pool-toolbar .el-input {
    max-width: 280px;
}
.pool-result-count {
    color: var(--muted);
    font-size: 12px;
    margin-left: auto;
}
.pool-selectionbar {
    gap: 10px;
    flex-wrap: wrap;
    margin-bottom: 7px;
}
.pool-selectionbar .el-checkbox {
    margin-right: 0;
}
.pool-selectionbar > span {
    color: var(--accent);
    font-size: 12px;
}
.pool-bulkbar {
    gap: 8px;
    flex-wrap: wrap;
    padding: 8px 10px;
    margin-bottom: 8px;
    background: var(--pink-soft);
    border: 1px solid var(--el-color-primary-light-8);
    border-radius: 10px;
}
.pool-bulkbar .el-checkbox {
    margin-right: 4px;
}
.bulk-progress {
    color: var(--accent);
    font-size: 12px;
    margin-left: auto;
}
.pool-table-wrap {
    border: 1px solid var(--line);
    border-radius: 10px;
    overflow: hidden;
}
.pool-table-dense :deep(.el-table__inner-wrapper::before) {
    display: none;
}
.pool-table-dense :deep(.el-table__cell) {
    padding: 7px 0;
}
.pool-mobile-list.has-expanded {
    max-height: none;
    overflow-y: visible;
}
.pool-table-dense :deep(td:nth-child(3) .cell) {
    overflow: visible;
    text-overflow: clip;
}
.pool-pagination :deep(.el-pagination__sizes .el-select) {
    width: 108px;
}
.proxy-cell {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 1px;
}
.proxy-cell code,
.mobile-card-top code {
    color: var(--ink);
    font-size: 12px;
    overflow-wrap: anywhere;
    white-space: normal;
}
.proxy-cell small {
    color: var(--muted);
    font-size: 11px;
}
.last-check {
    display: block;
    color: var(--muted);
    font-size: 10px;
    white-space: nowrap;
    line-height: 1.5;
    margin-top: 2px;
}
.row-actions {
    gap: 1px;
    white-space: nowrap;
}
.row-actions .el-button + .el-button {
    margin-left: 0;
}
.row-actions .el-button {
    padding-inline: 5px;
}
.pool-mobile-list {
    display: none;
}
.pool-pagination {
    justify-content: flex-end;
    gap: 12px;
    padding-top: 10px;
}
.selected-hint {
    color: var(--accent);
    font-size: 12px;
    margin-right: auto;
}

@media (max-width: 760px) {
    .pool-panel-heading {
        align-items: flex-start;
    }
    .pool-heading-actions .el-button:first-child {
        padding-inline: 8px;
    }
    .pool-heading-actions .el-button:first-child span {
        display: none;
    }
    .pool-stats {
        grid-template-columns: repeat(5, minmax(0, 1fr));
        gap: 4px;
    }
    .pool-stats > div {
        flex-direction: column;
        gap: 5px;
        padding: 9px 6px;
        align-items: center;
    }
    .pool-stats strong {
        font-size: 16px;
    }
    .pool-stats span {
        font-size: 11px;
    }
    .pool-toolbar {
        flex-wrap: wrap;
    }
    .pool-toolbar .el-input {
        width: 100%;
        max-width: none;
    }
    .pool-toolbar .el-select {
        flex: 1;
    }
    .pool-result-count {
        margin-left: 0;
    }
    .pool-table-wrap {
        display: none;
    }
    .pool-mobile-list {
        display: grid;
        gap: 8px;
        max-height: 360px;
        overflow-y: auto;
        padding-right: 2px;
    }
    .pool-mobile-card {
        min-width: 0;
        border: 1px solid var(--line);
        background: var(--surface);
        border-radius: 10px;
        padding: 10px;
    }
    .mobile-card-top {
        gap: 6px;
        min-width: 0;
    }
    .mobile-card-top code {
        flex: 1;
        min-width: 0;
    }
    .pool-mobile-card > p {
        color: var(--muted);
        font-size: 12px;
        margin: 5px 0 8px 24px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .mobile-card-meta {
        justify-content: space-between;
        margin-left: 24px;
        color: var(--muted);
        font-size: 11px;
    }
    .pool-mobile-card footer {
        justify-content: flex-end;
        margin-top: 8px;
        padding-top: 7px;
        border-top: 1px solid var(--line);
    }
    .pool-pagination {
        justify-content: center;
    }
    .pool-pagination .selected-hint {
        display: none;
    }
    .pool-pagination :deep(.el-pagination__sizes) {
        display: none;
    }
}

@container proxy-pool (max-width: 620px) {
    .pool-panel-heading {
        align-items: flex-start;
        flex-wrap: wrap;
        gap: 10px;
    }
    .pool-heading-actions {
        width: 100%;
        justify-content: flex-end;
        gap: 5px;
    }
    .pool-heading-actions .el-button {
        padding-inline: 9px;
    }
    .pool-heading-actions .el-button + .el-button {
        margin-left: 0;
    }
    .pool-stats {
        gap: 4px;
    }
    .pool-stats > div {
        flex-direction: column;
        gap: 5px;
        padding: 9px 6px;
        align-items: center;
    }
    .pool-stats strong {
        font-size: 16px;
    }
    .pool-stats span {
        font-size: 11px;
    }
    .pool-toolbar {
        flex-wrap: wrap;
    }
    .pool-toolbar .el-input {
        width: 100%;
        max-width: none;
    }
    .pool-toolbar .el-select {
        flex: 1;
    }
    .pool-result-count {
        margin-left: 0;
    }
    .pool-table-wrap {
        display: none;
    }
    .pool-mobile-list {
        display: grid;
        gap: 8px;
        max-height: 360px;
        overflow-y: auto;
        padding-right: 2px;
    }
    .pool-mobile-card {
        min-width: 0;
        border: 1px solid var(--line);
        background: var(--surface);
        border-radius: 10px;
        padding: 10px;
    }
    .mobile-card-top {
        gap: 6px;
        min-width: 0;
    }
    .mobile-card-top code {
        flex: 1;
        min-width: 0;
    }
    .pool-mobile-card > p {
        color: var(--muted);
        font-size: 12px;
        margin: 5px 0 8px 24px;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
    }
    .mobile-card-meta {
        justify-content: space-between;
        margin-left: 24px;
        color: var(--muted);
        font-size: 11px;
    }
    .pool-mobile-card footer {
        justify-content: flex-end;
        margin-top: 8px;
        padding-top: 7px;
        border-top: 1px solid var(--line);
    }
    .pool-pagination {
        justify-content: center;
    }
    .pool-pagination .selected-hint {
        display: none;
    }
    .pool-pagination :deep(.el-pagination__sizes) {
        display: none;
    }
}
</style>
