<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import {
    Picture,
    VideoCamera,
    Microphone,
    Download,
    View,
    Refresh,
    CopyDocument,
    WarningFilled,
    Loading,
    Document,
    Delete,
    Edit,
    Search,
    RefreshLeft,
} from '@element-plus/icons-vue'

const props = defineProps({
    entries: { type: Array, default: () => [] },
    selected: { type: Object, default: null },
    busy: Boolean,
    loadingIds: { type: Set, default: () => new Set() },
    statusNames: { type: Object, required: true },
})
const emit = defineEmits([
    'select',
    'details',
    'download',
    'read',
    'inspect',
    'recover',
    'copy',
    'remove',
    'remove-many',
    'reuse',
    'edit-image',
    'retry-preview',
    'download-many',
    'hydrate',
    'reference-image',
    'animate-image',
])
const filter = ref('all')
const statusFilter = ref('all')
const search = ref('')
const checkedKeys = ref(new Set())
const filters = [
    { label: '全部', value: 'all' },
    { label: '图片', value: 'image' },
    { label: '视频', value: 'video' },
    { label: '声音', value: 'audio' },
]
const pending = ['preparing', 'generating', 'queued', 'in_progress', 'received', 'unknown', 'download_failed']
const failures = ['failed', 'no_result', 'specification_mismatch']
const active = ['preparing', 'generating', 'queued', 'in_progress']
const statusFilters = [
    { label: '全部状态', value: 'all' },
    { label: '已完成', value: 'completed' },
    { label: '生成失败', value: 'failed' },
    { label: '待处理', value: 'pending' },
]
function entryKey(entry) {
    return String(entry.key || entry.receipt || entry.started)
}
function canRemove(entry) {
    return entry.status === 'completed' || failures.includes(entry.status)
}
const cards = computed(() =>
    props.entries.flatMap((entry) => {
        const outputs = entry.outputs?.length ? entry.outputs : [null]
        return outputs.map((output, index) => ({
            entry,
            output,
            index,
            kind: output?.kind || (entry.kind === 'speech' ? 'audio' : entry.kind),
            key: `${entryKey(entry)}-${index}`,
        }))
    }),
)
const visibleCards = computed(() =>
    cards.value.filter((card) => {
        if (filter.value !== 'all' && card.kind !== filter.value) return false
        if (statusFilter.value === 'completed' && card.entry.status !== 'completed') return false
        if (statusFilter.value === 'failed' && !failures.includes(card.entry.status)) return false
        if (statusFilter.value === 'pending' && !pending.includes(card.entry.status)) return false
        const query = search.value.trim().toLocaleLowerCase()
        return (
            !query ||
            [card.entry.title, card.entry.description, card.entry.model, card.entry.provider, card.entry.receipt].some(
                (value) =>
                    String(value || '')
                        .toLocaleLowerCase()
                        .includes(query),
            )
        )
    }),
)
const filteredEntries = computed(() => [
    ...new Map(visibleCards.value.map((card) => [entryKey(card.entry), card.entry])).values(),
])
const selectableEntries = computed(() => filteredEntries.value.filter(canRemove))
const checkedEntries = computed(() => props.entries.filter((entry) => checkedKeys.value.has(entryKey(entry))))
const downloadableEntries = computed(() =>
    checkedEntries.value.filter((entry) => entry.outputs?.some((output) => output.url || output.path)),
)
const failedEntries = computed(() => props.entries.filter((entry) => failures.includes(entry.status)))
const allChecked = computed(
    () =>
        selectableEntries.value.length > 0 &&
        selectableEntries.value.every((entry) => checkedKeys.value.has(entryKey(entry))),
)
const someChecked = computed(
    () => !allChecked.value && selectableEntries.value.some((entry) => checkedKeys.value.has(entryKey(entry))),
)
function toggleEntry(entry, checked) {
    if (!canRemove(entry)) return
    const next = new Set(checkedKeys.value)
    if (checked) next.add(entryKey(entry))
    else next.delete(entryKey(entry))
    checkedKeys.value = next
}
function toggleAll(checked) {
    const next = new Set(checkedKeys.value)
    for (const entry of selectableEntries.value) {
        if (checked) next.add(entryKey(entry))
        else next.delete(entryKey(entry))
    }
    checkedKeys.value = next
}
watch(
    () => props.entries.map(entryKey),
    (keys) => {
        checkedKeys.value = new Set([...checkedKeys.value].filter((key) => keys.includes(key)))
    },
)
const images = computed(() =>
    cards.value.filter((card) => card.kind === 'image' && card.output?.url).map((card) => card.output.url),
)
const finishedCount = computed(() => cards.value.filter((card) => card.output?.url).length)
const masonry = ref(null)
const previewErrors = reactive({})
const previewVersions = reactive({})
const hydratedEntries = new Set()
let intersectionObserver
async function observeCards() {
    await nextTick()
    intersectionObserver?.disconnect()
    for (const card of masonry.value?.children || []) intersectionObserver?.observe(card)
}
onMounted(() => {
    intersectionObserver = new IntersectionObserver(
        (items) => {
            for (const item of items) {
                if (!item.isIntersecting) continue
                const key = item.target.dataset.entryKey
                intersectionObserver.unobserve(item.target)
                if (hydratedEntries.has(key)) continue
                hydratedEntries.add(key)
                const entry = props.entries.find((value) => entryKey(value) === key)
                if (entry) emit('hydrate', entry)
            }
        },
        { rootMargin: '300px' },
    )
    observeCards()
})
watch(visibleCards, observeCards, { flush: 'post' })
onUnmounted(() => {
    intersectionObserver?.disconnect()
})
function time(entry) {
    return new Date(entry.started).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' })
}
function imageIndex(card) {
    return Math.max(0, images.value.indexOf(card.output.url))
}
function icon(kind) {
    return kind === 'video' ? VideoCamera : kind === 'audio' ? Microphone : Picture
}
function hasPreviewError(card) {
    return Boolean(card.output?.previewError || (card.output?.url && previewErrors[card.key] === card.output.url))
}
function isLoading(card) {
    return Boolean(card.output?.loading || card.output?.archiving || props.loadingIds?.has(entryKey(card.entry)))
}
function previewErrorMessage(card) {
    return (
        card.output?.archiveError ||
        (typeof card.output?.previewError === 'string'
            ? card.output.previewError
            : '作品已生成，预览地址暂时失效。重新读取会加载原成品。')
    )
}
function previewFailed(card) {
    previewErrors[card.key] = card.output.url
}
function retryPreview(card) {
    delete previewErrors[card.key]
    previewVersions[card.key] = (previewVersions[card.key] || 0) + 1
    emit('retry-preview', { entry: card.entry, output: card.output, index: card.index })
}
function dragPrompt(event, entry) {
    const value = entry.description || entry.title || ''
    event.dataTransfer.effectAllowed = 'copy'
    event.dataTransfer.setData('application/x-lay4flow-prompt', value)
    event.dataTransfer.setData('text/plain', value)
}
function dragImage(event, card) {
    if (event.target.closest('button, input, video, audio')) {
        event.preventDefault()
        return
    }
    if (card.kind !== 'image' || card.entry.status !== 'completed' || hasPreviewError(card)) {
        event.preventDefault()
        return
    }
    dragPrompt(event, card.entry)
    event.dataTransfer.setData(
        'application/x-lay4flow-media',
        JSON.stringify({ entryKey: entryKey(card.entry), outputIndex: card.index }),
    )
}
function previewFromKeyboard(event) {
    event.preventDefault()
    event.currentTarget.querySelector('img')?.click()
}
</script>

<template>
    <div class="studio-gallery">
        <div class="gallery-toolbar">
            <div class="gallery-total">
                <strong>{{ finishedCount }}</strong>
                <span>件作品 · {{ entries.length }} 个请求</span>
            </div>
            <el-segmented v-model="filter" :options="filters" size="small" aria-label="筛选作品类型" />
        </div>
        <div class="gallery-tools">
            <el-input
                v-model="search"
                :prefix-icon="Search"
                placeholder="搜索提示词、模型或收据"
                aria-label="搜索作品"
                clearable
                class="gallery-search"
            />
            <el-select v-model="statusFilter" aria-label="筛选作品状态" class="gallery-status-filter">
                <el-option
                    v-for="option in statusFilters"
                    :key="option.value"
                    :label="option.label"
                    :value="option.value"
                />
            </el-select>
            <el-checkbox
                :model-value="allChecked"
                :indeterminate="someChecked"
                :disabled="!selectableEntries.length"
                @change="toggleAll"
            >
                全选筛选结果
            </el-checkbox>
            <span class="gallery-filter-total">{{ filteredEntries.length }} 条记录</span>
            <el-button
                v-if="failedEntries.length"
                size="small"
                text
                type="danger"
                :icon="Delete"
                @click="emit('remove-many', failedEntries)"
            >
                清理失败 {{ failedEntries.length }}
            </el-button>
        </div>
        <div v-if="checkedEntries.length" class="gallery-batch" role="status">
            <strong>已选 {{ checkedEntries.length }} 条记录</strong>
            <el-button
                size="small"
                :icon="Download"
                :disabled="!downloadableEntries.length"
                @click="emit('download-many', downloadableEntries)"
            >
                下载所选
            </el-button>
            <el-button size="small" type="danger" plain :icon="Delete" @click="emit('remove-many', checkedEntries)">
                删除所选记录
            </el-button>
            <el-button size="small" text @click="checkedKeys = new Set()">取消选择</el-button>
            <span>仅移除创作台记录，原任务与已下载文件保留</span>
        </div>
        <div
            v-if="visibleCards.length"
            ref="masonry"
            class="generated-media gallery-masonry"
            :class="{ 'gallery-pair': visibleCards.length <= 2 }"
            aria-label="作品列表"
        >
            <article
                v-for="card in visibleCards"
                :key="card.key"
                class="gallery-card"
                :class="{
                    'is-selected': selected === card.entry,
                    'has-media': card.output?.url,
                    'is-pending': pending.includes(card.entry.status),
                    'is-failed': failures.includes(card.entry.status),
                    'is-checked': checkedKeys.has(entryKey(card.entry)),
                }"
                :data-media-kind="card.kind"
                :data-entry-key="entryKey(card.entry)"
            >
                <div class="gallery-card-select">
                    <el-checkbox
                        :model-value="checkedKeys.has(entryKey(card.entry))"
                        :disabled="!canRemove(card.entry)"
                        :aria-label="`选择记录：${card.entry.title || entryKey(card.entry)}`"
                        :title="canRemove(card.entry) ? '选择此记录' : '原任务待确认，保留收据后可继续查询'"
                        @change="toggleEntry(card.entry, $event)"
                    />
                </div>
                <figure
                    v-if="card.output?.url && !hasPreviewError(card)"
                    class="gallery-media"
                    :draggable="card.kind === 'image' && card.entry.status === 'completed'"
                    @dragstart="dragImage($event, card)"
                >
                    <el-image
                        v-if="card.kind === 'image'"
                        :key="`${card.key}-${card.output.url}-${previewVersions[card.key] || 0}`"
                        :src="card.output.url"
                        :alt="card.entry.title || '生成的图片作品'"
                        fit="contain"
                        :preview-src-list="images"
                        :initial-index="imageIndex(card)"
                        preview-teleported
                        lazy
                        loading="lazy"
                        tabindex="0"
                        role="button"
                        :aria-label="`查看大图：${card.entry.title || '生成的图片作品'}`"
                        @error="previewFailed(card)"
                        @keydown.enter="previewFromKeyboard"
                        @keydown.space="previewFromKeyboard"
                    />
                    <video
                        v-else-if="card.kind === 'video'"
                        :key="`${card.key}-${card.output.url}-${previewVersions[card.key] || 0}`"
                        :src="card.output.url"
                        controls
                        preload="metadata"
                        :aria-label="card.entry.title || '生成的视频作品'"
                        @error="previewFailed(card)"
                    />
                    <div v-else-if="card.kind === 'audio'" class="audio-art">
                        <el-icon><Microphone /></el-icon>
                        <div class="audio-wave" aria-hidden="true"><i v-for="bar in 24" :key="bar" /></div>
                        <audio
                            :key="`${card.key}-${card.output.url}-${previewVersions[card.key] || 0}`"
                            :src="card.output.url"
                            controls
                            preload="metadata"
                            :aria-label="card.entry.title || '生成的语音作品'"
                            @error="previewFailed(card)"
                        />
                    </div>
                    <figcaption class="media-corner">
                        <el-icon><component :is="icon(card.kind)" /></el-icon>
                        {{ card.kind === 'video' ? '映像' : card.kind === 'audio' ? '声线' : '绘梦' }}
                        {{ card.index + 1 }}
                    </figcaption>
                </figure>
                <div
                    v-else
                    class="gallery-placeholder"
                    :class="{
                        generating: active.includes(card.entry.status) || isLoading(card),
                        'preview-failed': hasPreviewError(card),
                    }"
                >
                    <div class="placeholder-symbol">
                        <el-icon
                            :class="{
                                'is-loading': active.includes(card.entry.status) || isLoading(card),
                            }"
                        >
                            <component
                                :is="
                                    failures.includes(card.entry.status) || hasPreviewError(card)
                                        ? WarningFilled
                                        : active.includes(card.entry.status) || isLoading(card)
                                          ? Loading
                                          : icon(card.kind)
                                "
                            />
                        </el-icon>
                    </div>
                    <strong>
                        {{
                            isLoading(card)
                                ? '正在读取原成品'
                                : hasPreviewError(card)
                                  ? '预览加载失败'
                                  : statusNames[card.entry.status] || card.entry.status
                        }}
                    </strong>
                    <p>
                        {{
                            hasPreviewError(card)
                                ? previewErrorMessage(card)
                                : isLoading(card)
                                  ? '正在恢复预览，不会重新生成。'
                                  : card.entry.message || '作品尚未载入预览。'
                        }}
                    </p>
                    <el-progress
                        v-if="active.includes(card.entry.status) || isLoading(card)"
                        :percentage="100"
                        :show-text="false"
                        indeterminate
                        :duration="3"
                    />
                    <el-button
                        v-if="hasPreviewError(card)"
                        type="primary"
                        size="small"
                        :icon="Refresh"
                        :loading="isLoading(card)"
                        @click="retryPreview(card)"
                    >
                        重新读取预览
                    </el-button>
                    <el-button
                        v-else-if="card.entry.status === 'completed' && card.output?.path"
                        type="primary"
                        size="small"
                        :icon="View"
                        :loading="isLoading(card)"
                        :disabled="busy && !isLoading(card)"
                        @click="emit('read', card.entry)"
                    >
                        读取成品
                    </el-button>
                    <el-button
                        v-else-if="card.entry.text"
                        size="small"
                        :icon="Document"
                        @click="emit('details', card.entry)"
                    >
                        查看文本响应
                    </el-button>
                </div>
                <div class="gallery-card-body">
                    <div class="gallery-card-meta">
                        <span>{{ card.entry.provider === 'vids' ? 'Vids' : 'Flow' }} · {{ time(card.entry) }}</span>
                        <el-tag
                            v-if="hasPreviewError(card) || !card.output?.url || card.entry.status !== 'completed'"
                            size="small"
                            :type="
                                hasPreviewError(card)
                                    ? 'warning'
                                    : failures.includes(card.entry.status)
                                      ? 'danger'
                                      : pending.includes(card.entry.status)
                                        ? 'warning'
                                        : 'success'
                            "
                            effect="light"
                        >
                            {{ hasPreviewError(card) ? '已生成 · 预览失效' : statusNames[card.entry.status] }}
                        </el-tag>
                        <span v-else class="finished-mark">已完成</span>
                    </div>
                    <p
                        class="gallery-prompt"
                        :title="card.entry.description || card.entry.title"
                        draggable="true"
                        @dragstart.stop="dragPrompt($event, card.entry)"
                    >
                        {{ card.entry.title || '从收据恢复的作品' }}
                    </p>
                    <div class="gallery-card-actions">
                        <el-button
                            v-if="card.kind === 'image' && card.output?.url && !hasPreviewError(card)"
                            size="small"
                            type="primary"
                            plain
                            :icon="Edit"
                            @click="emit('edit-image', { entry: card.entry, output: card.output, index: card.index })"
                        >
                            编辑图片
                        </el-button>
                        <el-button
                            v-if="
                                card.kind === 'image' &&
                                card.entry.status === 'completed' &&
                                card.output?.url &&
                                !hasPreviewError(card)
                            "
                            size="small"
                            @click="
                                emit('reference-image', { entry: card.entry, output: card.output, index: card.index })
                            "
                        >
                            作为参考
                        </el-button>
                        <el-button
                            v-if="
                                card.kind === 'image' &&
                                card.entry.status === 'completed' &&
                                card.output?.url &&
                                !hasPreviewError(card)
                            "
                            size="small"
                            :icon="VideoCamera"
                            @click="
                                emit('animate-image', { entry: card.entry, output: card.output, index: card.index })
                            "
                        >
                            生成动画
                        </el-button>
                        <el-button
                            v-if="card.entry.title"
                            size="small"
                            :icon="RefreshLeft"
                            @click="emit('reuse', card.entry)"
                        >
                            重用提示词
                        </el-button>
                        <el-button
                            v-if="card.output?.url"
                            size="small"
                            :icon="Download"
                            @click="emit('download', card.output, card.index, card.entry)"
                        >
                            下载作品
                        </el-button>
                        <el-button
                            v-if="card.entry.provider === 'vids' && pending.includes(card.entry.status)"
                            size="small"
                            :icon="Refresh"
                            :loading="busy"
                            @click="emit('inspect', card.entry)"
                        >
                            查询原收据
                        </el-button>
                        <el-button
                            v-if="
                                card.entry.provider === 'vids' &&
                                ['received', 'download_failed'].includes(card.entry.status)
                            "
                            size="small"
                            type="primary"
                            :icon="Download"
                            :loading="busy"
                            @click="emit('recover', card.entry)"
                        >
                            恢复下载
                        </el-button>
                        <el-button size="small" text :icon="View" @click="emit('details', card.entry)">记录</el-button>
                        <el-button
                            v-if="canRemove(card.entry)"
                            size="small"
                            text
                            type="danger"
                            :icon="Delete"
                            @click="emit('remove', card.entry)"
                        >
                            删除记录
                        </el-button>
                        <el-tooltip v-if="card.entry.receipt" content="复制收据">
                            <el-button
                                size="small"
                                text
                                :icon="CopyDocument"
                                aria-label="复制收据"
                                @click="emit('copy', card.entry.receipt)"
                            />
                        </el-tooltip>
                    </div>
                </div>
            </article>
        </div>
        <el-empty
            v-else
            :description="
                entries.length
                    ? '没有符合筛选条件的记录，试试其他关键词或状态。'
                    : '还没有作品，完成创作后会自动出现在这里。'
            "
            :image-size="70"
        />
    </div>
</template>

<style scoped>
.studio-gallery {
    min-width: 0;
    container: studio-results / inline-size;
}
.gallery-toolbar {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    flex-wrap: wrap;
    padding: 3px 0 20px;
}
.gallery-tools {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    margin: -8px 0 14px;
    padding: 9px 11px;
    border: 1px solid var(--line);
    border-radius: 11px;
    background: color-mix(in srgb, var(--surface-soft) 64%, transparent);
}
.gallery-search {
    width: min(310px, 100%);
}
.gallery-status-filter {
    width: 125px;
}
.gallery-filter-total {
    margin-left: auto;
    color: var(--muted);
    font-size: 12px;
    font-variant-numeric: tabular-nums;
}
.gallery-batch {
    display: flex;
    align-items: center;
    flex-wrap: wrap;
    gap: 8px;
    margin: -5px 0 14px;
    padding: 9px 11px;
    border: 1px solid color-mix(in srgb, var(--accent) 30%, var(--line));
    border-radius: 10px;
    background: var(--pink-soft);
    color: var(--ink);
    font-size: 12px;
}
.gallery-batch > span {
    margin-left: auto;
    color: var(--muted);
}
.gallery-total {
    display: flex;
    align-items: baseline;
    gap: 7px;
    color: var(--muted);
    font-size: 12px;
}
.gallery-total strong {
    color: var(--ink);
    font-size: 24px;
    font-variant-numeric: tabular-nums;
    letter-spacing: -0.05em;
}
.gallery-masonry {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    grid-auto-flow: row;
    gap: 16px;
    align-items: stretch;
}
.gallery-masonry.gallery-pair {
    grid-template-columns: repeat(2, minmax(0, 1fr));
}
.gallery-card {
    container: gallery-card / inline-size;
    position: relative;
    width: 100%;
    min-width: 0;
    display: flex;
    flex-direction: column;
    margin: 0;
    overflow: hidden;
    border: 1px solid var(--line);
    border-radius: 13px;
    background: var(--surface);
    transition:
        border-color 0.2s,
        box-shadow 0.2s;
}
.gallery-card-select {
    position: absolute;
    z-index: 3;
    top: 9px;
    right: 9px;
    display: grid;
    place-items: center;
    width: 28px;
    height: 28px;
    border-radius: 8px;
    background: #fffdfde8;
    box-shadow: 0 2px 9px #2833481c;
}
.gallery-card-select :deep(.el-checkbox) {
    height: 18px;
    margin: 0;
}
.gallery-card.is-checked {
    border-color: var(--accent);
    box-shadow: 0 0 0 2px color-mix(in srgb, var(--accent) 24%, transparent);
}
.gallery-card:hover,
.gallery-card:focus-within {
    border-color: var(--accent);
    box-shadow: 0 6px 22px color-mix(in srgb, var(--accent) 9%, transparent);
}
.gallery-media {
    position: relative;
    margin: 0;
    overflow: hidden;
    background: var(--surface-soft);
    aspect-ratio: 16 / 9;
    flex-shrink: 0;
}
.gallery-media :deep(.el-image),
.gallery-media video {
    display: block;
    width: 100%;
    height: 100%;
    object-fit: contain;
}
.gallery-media :deep(.el-image__inner) {
    display: block;
    width: 100%;
    height: 100%;
    transition: transform 0.35s;
}
.gallery-media :deep(.el-image__placeholder) {
    height: 100%;
}
.gallery-media:hover :deep(.el-image__inner) {
    transform: scale(1.018);
}
.media-corner {
    position: absolute;
    top: 10px;
    left: 10px;
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 5px 8px;
    border-radius: 6px;
    color: #fff;
    background: #283348bf;
    font-size: 11px;
    pointer-events: none;
}
.gallery-card-body {
    padding: 13px;
    display: grid;
    grid-template-rows: 20px 44px 94px;
    gap: 8px;
    min-width: 0;
}
.gallery-card-meta {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 7px;
    color: var(--muted);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
}
.gallery-card-meta :deep(.el-tag) {
    max-width: 60%;
    height: auto;
    min-height: 20px;
    white-space: normal;
}
.finished-mark {
    color: var(--success);
}
.gallery-prompt {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
    margin: 0;
    color: var(--ink);
    font-size: 13px;
    line-height: 1.7;
    overflow-wrap: anywhere;
}
.gallery-card-actions {
    display: grid;
    grid-template-columns: repeat(3, minmax(0, 1fr));
    grid-auto-rows: 28px;
    align-items: start;
    align-content: start;
    gap: 5px;
}
.gallery-card-actions :deep(.el-button) {
    margin: 0;
    min-width: 0;
    width: 100%;
    height: 28px;
    font-size: 11px;
    padding: 5px 3px;
}
.gallery-card-actions :deep(.el-button.is-text) {
    padding: 5px 3px;
}
.gallery-placeholder {
    height: auto;
    min-height: 0;
    aspect-ratio: 16 / 9;
    overflow: auto;
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 12px;
    text-align: center;
    background: var(--surface-soft);
}
.gallery-placeholder.generating {
    background: linear-gradient(125deg, var(--pink-soft), var(--surface-soft) 55%, var(--blue-soft));
}
.placeholder-symbol {
    display: grid;
    place-items: center;
    width: 36px;
    height: 36px;
    min-height: 36px;
    border-radius: 10px;
    background: var(--surface);
    color: var(--accent);
    font-size: 20px;
}
.gallery-placeholder > strong {
    color: var(--ink);
    font-size: 15px;
}
.gallery-placeholder > p {
    max-width: 300px;
    margin: 0;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.8;
    overflow-wrap: anywhere;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
}
.gallery-placeholder :deep(.el-progress) {
    width: 85%;
    max-width: 230px;
}
.is-failed .placeholder-symbol {
    color: var(--el-color-danger);
}
.preview-failed .placeholder-symbol {
    color: var(--warning, #bd7d20);
}
.audio-art {
    min-height: 0;
    height: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 14px 16px;
    background: var(--blue-soft);
}
.audio-art > .el-icon {
    font-size: 34px;
    color: var(--blue);
}
.audio-art audio {
    display: block;
    width: 100%;
    min-width: 0;
    height: 38px;
}
.audio-wave {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 4px;
    width: 100%;
    height: 70px;
    margin: 8px 0 16px;
    overflow: hidden;
}
.audio-wave i {
    width: 4px;
    height: 15px;
    border-radius: 3px;
    background: #b3cde2;
    flex-shrink: 0;
}
.audio-wave i:nth-child(3n) {
    height: 36px;
}
.audio-wave i:nth-child(4n) {
    height: 52px;
}
.audio-wave i:nth-child(5n) {
    height: 26px;
    background: #e6b7cd;
}
@media (max-width: 600px) {
    .gallery-masonry {
        gap: 10px;
    }
    .gallery-card {
        border-radius: 10px;
    }
    .gallery-card-body {
        padding: 10px;
    }
    .gallery-card-meta {
        font-size: 10px;
    }
    .gallery-prompt {
        font-size: 12px;
    }
    .gallery-card-actions :deep(.el-button) {
        min-height: 28px;
        padding: 5px 3px;
        font-size: 11px;
    }
    .gallery-placeholder {
        min-height: 0;
        padding: 12px 10px;
    }
    .gallery-placeholder > p {
        font-size: 11px;
    }
    .gallery-placeholder > strong {
        font-size: 13px;
    }
    .media-corner {
        top: 7px;
        left: 7px;
        font-size: 10px;
        padding: 4px 6px;
    }
    .audio-art {
        padding: 14px 8px;
    }
    .audio-wave {
        gap: 3px;
    }
    .audio-wave i {
        width: 3px;
    }
    .gallery-toolbar {
        gap: 10px;
    }
    .gallery-tools {
        align-items: stretch;
    }
    .gallery-search,
    .gallery-status-filter {
        width: 100%;
    }
    .gallery-filter-total {
        width: 100%;
        margin-left: 0;
    }
    .gallery-batch > span {
        width: 100%;
        margin-left: 0;
    }
    .gallery-toolbar :deep(.el-segmented) {
        width: 100%;
    }
    .gallery-toolbar :deep(.el-segmented__group) {
        display: flex;
    }
    .gallery-toolbar :deep(.el-segmented__item) {
        flex: 1;
    }
}
@container studio-results (min-width: 720px) {
    .gallery-masonry {
        grid-template-columns: repeat(3, minmax(0, 1fr));
    }
}
@container studio-results (min-width: 1040px) {
    .gallery-masonry {
        grid-template-columns: repeat(4, minmax(0, 1fr));
    }
}
@container studio-results (max-width: 599px) {
    .gallery-masonry,
    .gallery-masonry.gallery-pair {
        grid-template-columns: minmax(0, 1fr);
    }
}
@container gallery-card (max-width: 299px) {
    .gallery-card-body {
        grid-template-rows: 20px 44px 127px;
    }
    .gallery-card-actions {
        grid-template-columns: repeat(2, minmax(0, 1fr));
    }
    .gallery-placeholder .placeholder-symbol {
        display: none;
    }
    .gallery-placeholder > p {
        -webkit-line-clamp: 1;
    }
    .gallery-placeholder > strong {
        font-size: 13px;
    }
    .audio-art > .el-icon {
        display: none;
    }
    .audio-wave {
        height: 35px;
        margin: 0 0 8px;
    }
}
@media (prefers-reduced-motion: reduce) {
    .gallery-card,
    .gallery-media :deep(.el-image__inner) {
        transition: none;
    }
    .gallery-placeholder :deep(.is-loading) {
        animation: none;
    }
}
</style>
