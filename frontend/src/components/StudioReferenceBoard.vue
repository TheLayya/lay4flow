<script setup>
import { ref } from 'vue'
import { Picture, Plus, Delete, Edit, CopyDocument, ArrowLeft, ArrowRight } from '@element-plus/icons-vue'

const props = defineProps({
    materials: { type: Array, default: () => [] },
    maximum: { type: Number, default: 1 },
    disabled: Boolean,
    busy: Boolean,
    firstLast: Boolean,
    provider: { type: String, default: 'flow' },
})
const emit = defineEmits(['files', 'clipboard', 'remove', 'clear', 'edit', 'move', 'media'])
const input = ref(null)
const dragging = ref(false)
let dragDepth = 0
function onFiles(files) {
    if (!props.disabled && files?.length) emit('files', [...files])
}
function choose(event) {
    onFiles(event.target.files)
    event.target.value = ''
}
function enter(event) {
    if (props.disabled) return
    if ([...event.dataTransfer.types].some((type) => ['Files', 'application/x-lay4flow-media'].includes(type))) {
        event.preventDefault()
        dragDepth++
        dragging.value = true
    }
}
function leave() {
    dragDepth = Math.max(0, dragDepth - 1)
    if (!dragDepth) dragging.value = false
}
function drop(event) {
    dragging.value = false
    dragDepth = 0
    if (props.disabled) return
    const media = event.dataTransfer.getData('application/x-lay4flow-media')
    if (media) {
        try {
            emit('media', JSON.parse(media))
        } catch {
            /* Ignore unrelated drag data. */
        }
    } else onFiles(event.dataTransfer.files)
}
function bytes(value) {
    return value >= 1024 ** 2 ? `${(value / 1024 ** 2).toFixed(1)} MB` : `${Math.max(1, Math.round(value / 1024))} KB`
}
</script>

<template>
    <section
        class="reference-board"
        :class="{ 'is-dragging': dragging, 'has-materials': materials.length }"
        aria-label="参考图片素材区"
        @dragenter="enter"
        @dragleave="leave"
        @dragover.prevent
        @drop.prevent.stop="drop"
    >
        <header class="reference-heading">
            <div>
                <el-icon><Picture /></el-icon>
                <strong>{{ firstLast ? '首帧与尾帧' : '参考图片' }}</strong>
                <span>{{ materials.length }} / {{ maximum }}</span>
            </div>
            <el-button v-if="materials.length" text size="small" :disabled="disabled" @click="emit('clear')">
                清空
            </el-button>
        </header>
        <div v-if="materials.length" class="reference-list">
            <article v-for="(material, index) in materials" :key="`${material.name}-${index}`" class="reference-item">
                <el-image
                    :src="material.data"
                    fit="cover"
                    :preview-src-list="materials.map((item) => item.data)"
                    :initial-index="index"
                    preview-teleported
                    :alt="material.name || `参考图 ${index + 1}`"
                />
                <div class="reference-number">{{ firstLast ? (index === 0 ? '首帧' : '尾帧') : index + 1 }}</div>
                <div class="reference-item-info">
                    <strong :title="material.name">{{ material.name }}</strong>
                    <span>{{ bytes(material.bytes || 0) }}</span>
                </div>
                <div class="reference-item-tools">
                    <el-button :icon="Edit" text size="small" :disabled="disabled" @click="emit('edit', index)">
                        编辑
                    </el-button>
                    <el-button
                        :icon="ArrowLeft"
                        text
                        size="small"
                        :disabled="disabled || index === 0"
                        :aria-label="`参考图 ${index + 1} 向前移动`"
                        @click="emit('move', index, -1)"
                    />
                    <el-button
                        :icon="ArrowRight"
                        text
                        size="small"
                        :disabled="disabled || index === materials.length - 1"
                        :aria-label="`参考图 ${index + 1} 向后移动`"
                        @click="emit('move', index, 1)"
                    />
                    <el-button
                        :icon="Delete"
                        text
                        size="small"
                        type="danger"
                        :disabled="disabled"
                        :aria-label="`移除参考图 ${index + 1}`"
                        @click="emit('remove', index)"
                    />
                </div>
            </article>
        </div>
        <button
            v-if="!materials.length"
            type="button"
            class="reference-empty"
            :disabled="disabled"
            @click="input?.click()"
        >
            <el-icon><Plus /></el-icon>
            <strong>拖入图片，或点击选择</strong>
            <span>也可以直接 Ctrl + V 粘贴截图、拖入右侧作品</span>
        </button>
        <footer class="reference-footer">
            <div class="reference-actions">
                <el-button
                    :icon="Plus"
                    size="small"
                    :disabled="disabled || materials.length >= maximum"
                    :loading="busy"
                    @click="input?.click()"
                >
                    添加图片
                </el-button>
                <el-button
                    :icon="CopyDocument"
                    size="small"
                    :disabled="disabled || materials.length >= maximum"
                    @click="emit('clipboard')"
                >
                    粘贴图片
                </el-button>
            </div>
            <small>PNG / JPEG / WebP · {{ provider === 'vids' ? '总量 10 MB' : '单张 20 MB' }}</small>
        </footer>
        <input
            ref="input"
            type="file"
            accept="image/png,image/jpeg,image/webp"
            :multiple="maximum > 1"
            hidden
            :disabled="disabled"
            @change="choose"
        />
        <div v-if="dragging" class="reference-drop-hint">松开，加入参考图片</div>
    </section>
</template>

<style scoped>
.reference-board {
    position: relative;
    border: 1px solid var(--line);
    border-radius: 12px;
    padding: 12px;
    margin: 18px 0;
    background: var(--surface);
    transition:
        border-color 0.18s,
        background 0.18s;
}
.reference-board.is-dragging {
    border-color: var(--el-color-primary);
    background: var(--pink-soft);
}
.reference-heading,
.reference-heading > div,
.reference-footer,
.reference-actions {
    display: flex;
    align-items: center;
    gap: 8px;
}
.reference-heading {
    justify-content: space-between;
    margin-bottom: 10px;
    min-height: 24px;
}
.reference-heading strong {
    font-size: 13px;
    font-weight: 600;
}
.reference-heading .el-icon {
    color: var(--el-color-primary);
}
.reference-heading span {
    font-size: 11px;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
}
.reference-empty {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 7px;
    min-height: 108px;
    width: 100%;
    border: 1px dashed var(--line);
    border-radius: 8px;
    padding: 13px 8px;
    background: var(--surface-soft);
    color: var(--muted);
    cursor: pointer;
}
.reference-empty:hover {
    border-color: var(--el-color-primary-light-5);
    color: var(--el-color-primary);
}
.reference-empty:focus-visible {
    outline: 2px solid var(--el-color-primary);
    outline-offset: 2px;
}
.reference-empty .el-icon {
    font-size: 20px;
}
.reference-empty strong {
    font-size: 12px;
    font-weight: 500;
}
.reference-empty span {
    font-size: 11px;
    line-height: 1.6;
}
.reference-list {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 9px;
    max-height: 390px;
    overflow: auto;
}
.reference-item {
    min-width: 0;
    position: relative;
    border: 1px solid var(--line);
    border-radius: 8px;
    overflow: hidden;
}
.reference-item > .el-image {
    width: 100%;
    height: 104px;
    display: block;
    background: var(--surface-soft);
}
.reference-number {
    position: absolute;
    top: 6px;
    left: 6px;
    min-width: 20px;
    padding: 2px 5px;
    border-radius: 4px;
    background: rgba(40, 51, 72, 0.72);
    color: #fff;
    font-size: 11px;
    text-align: center;
}
.reference-item-info {
    padding: 6px 7px 0;
    display: flex;
    gap: 6px;
    align-items: center;
}
.reference-item-info strong {
    font-size: 11px;
    font-weight: 400;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    flex: 1;
}
.reference-item-info span {
    color: var(--muted);
    font-size: 10px;
    flex-shrink: 0;
}
.reference-item-tools {
    display: flex;
    justify-content: space-between;
    padding: 2px;
}
.reference-item-tools .el-button {
    margin: 0;
    padding: 5px;
}
.reference-footer {
    flex-wrap: wrap;
    justify-content: space-between;
    margin-top: 10px;
    gap: 7px;
}
.reference-actions .el-button {
    margin: 0;
}
.reference-footer small {
    font-size: 10px;
    color: var(--muted);
    line-height: 1.5;
}
.reference-drop-hint {
    position: absolute;
    inset: 0;
    display: grid;
    place-content: center;
    z-index: 2;
    border-radius: inherit;
    background: var(--pink-soft);
    color: var(--el-color-primary);
    font-size: 15px;
    font-weight: 600;
    pointer-events: none;
}
button:disabled {
    cursor: default;
    opacity: 0.65;
}
</style>
