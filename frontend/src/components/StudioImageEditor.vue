<script setup>
import { computed, markRaw, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import {
    Download,
    Brush,
    RefreshLeft,
    RefreshRight,
    Crop,
    EditPen,
    FullScreen,
    Minus,
    Plus,
    Refresh,
    Switch,
    Upload,
    Close,
    WarningFilled,
} from '@element-plus/icons-vue'

const props = defineProps({
    modelValue: { type: Boolean, default: false },
    source: { type: Object, default: () => ({ url: '', type: 'image', title: '' }) },
    loading: { type: Boolean, default: false },
})

const emit = defineEmits(['update:modelValue', 'apply', 'reload'])

const canvas = ref(null)
const sourceImage = ref(null)
const notice = ref('')
const prompt = ref('')
const loadingSource = ref(false)
const busy = ref(false)
const zoom = ref(1)
const tool = ref('brush')
const color = ref('#ce3e77')
const brushSize = ref(18)
const crop = ref('original')
const freeWidth = ref(100)
const freeHeight = ref(100)
const rotation = ref(0)
const flipX = ref(false)
const flipY = ref(false)
const strokes = ref([])
const undoStack = ref([])
const redoStack = ref([])
const cropOptions = [
    { value: 'original', label: '原图' },
    { value: 'free', label: '自由' },
    { value: '1:1', label: '1 : 1' },
    { value: '16:9', label: '16 : 9' },
    { value: '9:16', label: '9 : 16' },
]
let objectUrl = ''
let drawing = false
let activeStroke = null
let sourceRevision = 0
let sourceAbort = null

const sourceTitle = computed(() => props.source?.title || '待编辑图片')
const hasImage = computed(() => Boolean(sourceImage.value?.naturalWidth))
const canUndo = computed(() => undoStack.value.length > 0)
const canRedo = computed(() => redoStack.value.length > 0)
const outputRatio = computed(() => {
    const dimensions = sourceDimensions()
    if (crop.value === 'original') return dimensions.width / Math.max(dimensions.height, 1)
    if (crop.value === 'free')
        return (dimensions.width * freeWidth.value) / Math.max(dimensions.height * freeHeight.value, 1)
    const [width, height] = crop.value.split(':').map(Number)
    return width / height
})
const outputLabel = computed(() => {
    const dimensions = cropDimensions()
    return `${Math.round(dimensions.width)} × ${Math.round(dimensions.height)} · PNG`
})

function currentSnapshot() {
    return {
        rotation: rotation.value,
        flipX: flipX.value,
        flipY: flipY.value,
        crop: crop.value,
        freeWidth: freeWidth.value,
        freeHeight: freeHeight.value,
        strokes: strokes.value.map((stroke) => ({
            ...stroke,
            points: stroke.points.map((point) => ({ ...point })),
        })),
    }
}

function restoreSnapshot(snapshot) {
    if (!snapshot) return
    rotation.value = snapshot.rotation
    flipX.value = snapshot.flipX
    flipY.value = snapshot.flipY
    crop.value = snapshot.crop
    freeWidth.value = snapshot.freeWidth
    freeHeight.value = snapshot.freeHeight
    strokes.value = snapshot.strokes.map((stroke) => ({
        ...stroke,
        points: stroke.points.map((point) => ({ ...point })),
    }))
    void nextTick(render)
}

function commit() {
    undoStack.value = [...undoStack.value.slice(-19), currentSnapshot()]
    redoStack.value = []
}

function undo() {
    const previous = undoStack.value.at(-1)
    if (!previous) return
    redoStack.value = [...redoStack.value, currentSnapshot()]
    undoStack.value = undoStack.value.slice(0, -1)
    restoreSnapshot(previous)
}

function redo() {
    const next = redoStack.value.at(-1)
    if (!next) return
    undoStack.value = [...undoStack.value, currentSnapshot()]
    redoStack.value = redoStack.value.slice(0, -1)
    restoreSnapshot(next)
}

function resetEditor() {
    commit()
    rotation.value = 0
    flipX.value = false
    flipY.value = false
    crop.value = 'original'
    freeWidth.value = freeHeight.value = 100
    strokes.value = []
    zoom.value = 1
    notice.value = '已还原到原图'
    void nextTick(render)
}

function clearInk() {
    if (!strokes.value.length) return
    commit()
    strokes.value = []
    void nextTick(render)
}

function close() {
    emit('update:modelValue', false)
}

function sourceDimensions() {
    const image = sourceImage.value
    if (!image) return { width: 1, height: 1 }
    const rotated = Math.abs(rotation.value % 180) === 90
    return {
        width: rotated ? image.naturalHeight : image.naturalWidth,
        height: rotated ? image.naturalWidth : image.naturalHeight,
    }
}

function cropDimensions() {
    const { width, height } = sourceDimensions()
    if (crop.value === 'original') return { width, height }
    if (crop.value === 'free')
        return { width: (width * freeWidth.value) / 100, height: (height * freeHeight.value) / 100 }
    const ratio = outputRatio.value
    if (width / height > ratio) return { width: height * ratio, height }
    return { width, height: width / ratio }
}

function fitCanvasSize() {
    const dimensions = cropDimensions()
    const scale = Math.min(1, 1600 / Math.max(dimensions.width, dimensions.height, 1))
    return {
        width: Math.max(1, Math.round(dimensions.width * scale)),
        height: Math.max(1, Math.round(dimensions.height * scale)),
    }
}

function drawBase(context, width, height) {
    const image = sourceImage.value
    if (!image) return
    const cropSize = cropDimensions()
    const factor = Math.max(width / Math.max(cropSize.width, 1), height / Math.max(cropSize.height, 1))
    context.save()
    context.translate(width / 2, height / 2)
    context.scale((flipX.value ? -1 : 1) * factor, (flipY.value ? -1 : 1) * factor)
    context.rotate((rotation.value * Math.PI) / 180)
    context.drawImage(image, -image.naturalWidth / 2, -image.naturalHeight / 2)
    context.restore()
}

function drawStrokes(context, width, height) {
    for (const stroke of strokes.value) {
        if (!stroke.points.length) continue
        context.save()
        context.globalCompositeOperation = stroke.tool === 'eraser' ? 'destination-out' : 'source-over'
        context.strokeStyle = stroke.color
        context.lineWidth = Math.max(1, stroke.size * (width / 1000))
        context.lineCap = 'round'
        context.lineJoin = 'round'
        context.beginPath()
        stroke.points.forEach((point, index) => {
            const x = point.x * width
            const y = point.y * height
            if (index === 0) context.moveTo(x, y)
            else context.lineTo(x, y)
        })
        if (stroke.points.length === 1)
            context.lineTo(stroke.points[0].x * width + 0.01, stroke.points[0].y * height + 0.01)
        context.stroke()
        context.restore()
    }
}

function render(target = canvas.value, dimensions = fitCanvasSize()) {
    if (!target || !sourceImage.value) return
    target.width = dimensions.width
    target.height = dimensions.height
    const context = target.getContext('2d')
    if (!context) return
    context.clearRect(0, 0, target.width, target.height)
    context.imageSmoothingEnabled = true
    context.imageSmoothingQuality = 'high'
    drawBase(context, target.width, target.height)
    // Keep ink separate: erasing annotations must preserve every source-image pixel.
    const ink = document.createElement('canvas')
    ink.width = target.width
    ink.height = target.height
    const inkContext = ink.getContext('2d')
    if (inkContext) {
        drawStrokes(inkContext, target.width, target.height)
        context.drawImage(ink, 0, 0)
    }
}

function pointerPoint(event) {
    const target = canvas.value
    if (!target) return null
    const rect = target.getBoundingClientRect()
    return {
        x: Math.min(1, Math.max(0, (event.clientX - rect.left) / Math.max(rect.width, 1))),
        y: Math.min(1, Math.max(0, (event.clientY - rect.top) / Math.max(rect.height, 1))),
    }
}

function beginStroke(event) {
    if (!hasImage.value || event.button !== 0 || !['brush', 'eraser'].includes(tool.value)) return
    const point = pointerPoint(event)
    if (!point) return
    commit()
    drawing = true
    activeStroke = { tool: tool.value, color: color.value, size: brushSize.value, points: [point] }
    strokes.value = [...strokes.value, activeStroke]
    canvas.value?.setPointerCapture(event.pointerId)
    render()
}

function continueStroke(event) {
    if (!drawing || !activeStroke) return
    const point = pointerPoint(event)
    if (!point) return
    activeStroke.points.push(point)
    render()
}

function finishStroke() {
    drawing = false
    activeStroke = null
}

function rotateBy(degrees) {
    commit()
    const previous = cropDimensions()
    rotation.value = (rotation.value + degrees + 360) % 360
    const previousFlip = flipX.value
    flipX.value = flipY.value
    flipY.value = previousFlip
    const next = cropDimensions()
    strokes.value = strokes.value.map((stroke) => ({
        ...stroke,
        size: (stroke.size * previous.width) / next.width,
        points: stroke.points.map(({ x, y }) =>
            degrees > 0
                ? {
                      x: 0.5 - ((y - 0.5) * previous.height) / next.width,
                      y: 0.5 + ((x - 0.5) * previous.width) / next.height,
                  }
                : {
                      x: 0.5 + ((y - 0.5) * previous.height) / next.width,
                      y: 0.5 - ((x - 0.5) * previous.width) / next.height,
                  },
        ),
    }))
    void nextTick(render)
}

function setCrop(value) {
    if (crop.value === value) return
    commit()
    const previous = cropDimensions()
    crop.value = value
    remapCrop(previous)
    void nextTick(render)
}

function toggleFlip(axis) {
    commit()
    if (axis === 'x') flipX.value = !flipX.value
    else flipY.value = !flipY.value
    strokes.value = strokes.value.map((stroke) => ({
        ...stroke,
        points: stroke.points.map(({ x, y }) => (axis === 'x' ? { x: 1 - x, y } : { x, y: 1 - y })),
    }))
    void nextTick(render)
}

function remapCrop(previous) {
    const next = cropDimensions()
    strokes.value = strokes.value.map((stroke) => ({
        ...stroke,
        size: (stroke.size * previous.width) / next.width,
        points: stroke.points.map(({ x, y }) => ({
            x: 0.5 + ((x - 0.5) * previous.width) / next.width,
            y: 0.5 + ((y - 0.5) * previous.height) / next.height,
        })),
    }))
}

function changeFreeCrop(axis, value) {
    commit()
    const previous = cropDimensions()
    if (axis === 'width') freeWidth.value = value
    else freeHeight.value = value
    remapCrop(previous)
    void nextTick(render)
}

function setZoom(value) {
    zoom.value = Math.min(2.4, Math.max(0.5, value))
}

function keyboard(event) {
    if (!props.modelValue || event.target?.closest?.('input,textarea,[contenteditable="true"]')) return
    if (event.ctrlKey || event.metaKey) {
        if (event.key.toLowerCase() === 'z') {
            event.preventDefault()
            event.shiftKey ? redo() : undo()
        } else if (event.key.toLowerCase() === 'y') {
            event.preventDefault()
            redo()
        }
    } else if (event.key.toLowerCase() === 'b') tool.value = 'brush'
    else if (event.key.toLowerCase() === 'e') tool.value = 'eraser'
}

function blobFromCanvas(target) {
    return new Promise((resolve, reject) => {
        target.toBlob((blob) => (blob ? resolve(blob) : reject(new Error('PNG 导出失败'))), 'image/png')
    })
}

async function makeFile() {
    if (!sourceImage.value) throw new Error('原图尚未加载')
    const cropSize = cropDimensions()
    const scale = Math.min(1, 4096 / Math.max(cropSize.width, cropSize.height))
    const output = document.createElement('canvas')
    render(output, {
        width: Math.max(1, Math.round(cropSize.width * scale)),
        height: Math.max(1, Math.round(cropSize.height * scale)),
    })
    const blob = await blobFromCanvas(output)
    const basename =
        String(sourceTitle.value)
            .replace(/[^\p{L}\p{N}_-]+/gu, '-')
            .slice(0, 48) || 'lay4flow-edit'
    return new File([blob], `${basename}.png`, { type: 'image/png' })
}

async function apply(mode) {
    if (busy.value || props.loading || !hasImage.value) return
    if (mode === 'edit' && !prompt.value.trim()) {
        notice.value = '写下编辑要求后再提交'
        return
    }
    busy.value = true
    notice.value = ''
    try {
        const file = await makeFile()
        emit('apply', { file, prompt: prompt.value.trim(), mode })
        notice.value = mode === 'reference' ? '已导出，可作为参考图使用' : '已导出，等待 AI 编辑'
    } catch (error) {
        notice.value = error instanceof Error ? error.message : '导出失败，请重试'
    } finally {
        busy.value = false
    }
}

async function download() {
    try {
        const file = await makeFile()
        const url = URL.createObjectURL(file)
        const anchor = document.createElement('a')
        anchor.href = url
        anchor.download = file.name
        anchor.click()
        setTimeout(() => URL.revokeObjectURL(url), 1000)
        notice.value = 'PNG 已下载'
    } catch (error) {
        notice.value = error instanceof Error ? error.message : '下载失败，请重试'
    }
}

function resetObjectUrl() {
    if (objectUrl) URL.revokeObjectURL(objectUrl)
    objectUrl = ''
}

async function loadSource() {
    if (!props.modelValue) return
    const revision = ++sourceRevision
    sourceAbort?.abort()
    sourceAbort = new AbortController()
    loadingSource.value = true
    notice.value = '正在载入原图…'
    resetObjectUrl()
    sourceImage.value = null
    if (!(props.source?.url || props.source?.file)) {
        notice.value = props.source?.error || (props.loading ? '正在读取原成品…' : '原图尚未载入，请重新读取')
        loadingSource.value = false
        return
    }
    try {
        let blob = props.source.file instanceof Blob ? props.source.file : null
        if (!blob) {
            const url = new URL(props.source.url, location.origin)
            if (!['http:', 'https:', 'blob:', 'data:'].includes(url.protocol)) throw new Error('图片地址格式有误')
            const response = await fetch(url.href, {
                credentials: url.origin === location.origin ? 'include' : 'omit',
                signal: sourceAbort.signal,
            })
            if (!response.ok) throw new Error(`原图读取失败（HTTP ${response.status}）`)
            blob = await response.blob()
        }
        if (revision !== sourceRevision || !props.modelValue) return
        objectUrl = URL.createObjectURL(blob)
        await new Promise((resolve, reject) => {
            const image = new Image()
            image.onload = () => {
                if (revision === sourceRevision && props.modelValue) sourceImage.value = markRaw(image)
                resolve()
            }
            image.onerror = () => reject(new Error('图片无法解码，请重试'))
            image.src = objectUrl
        })
        if (revision !== sourceRevision || !props.modelValue) return
        rotation.value = 0
        flipX.value = false
        flipY.value = false
        crop.value = 'original'
        freeWidth.value = freeHeight.value = 100
        strokes.value = []
        undoStack.value = []
        redoStack.value = []
        zoom.value = 1
        prompt.value = String(props.source.prompt || '')
        notice.value = '可以开始裁剪、变换或涂鸦'
        loadingSource.value = false
        await nextTick(render)
    } catch (error) {
        if (revision === sourceRevision && error.name !== 'AbortError') {
            sourceImage.value = null
            notice.value = error instanceof Error ? error.message : '原图读取失败，请重试'
        }
    } finally {
        if (revision === sourceRevision) loadingSource.value = false
    }
}

function retrySource() {
    if (props.source?.file || props.source?.url) void loadSource()
    else emit('reload')
}

watch(
    () => [props.modelValue, props.source?.url, props.source?.file, props.source?.error],
    ([open]) => {
        if (open) {
            window.addEventListener('keydown', keyboard)
            void loadSource()
        } else {
            window.removeEventListener('keydown', keyboard)
            sourceRevision++
            sourceAbort?.abort()
            resetObjectUrl()
            sourceImage.value = null
        }
    },
    { immediate: true },
)
onBeforeUnmount(() => {
    window.removeEventListener('keydown', keyboard)
    sourceRevision++
    sourceAbort?.abort()
    resetObjectUrl()
})
</script>

<template>
    <el-dialog
        :model-value="modelValue"
        class="studio-image-editor-dialog"
        width="min(1120px, 96vw)"
        top="4vh"
        destroy-on-close
        :close-on-click-modal="false"
        :close-on-press-escape="true"
        append-to-body
        @update:model-value="emit('update:modelValue', $event)"
        @opened="render()"
    >
        <template #header>
            <div class="editor-heading">
                <div>
                    <span class="editor-eyebrow">IMAGE LAB · 本地编辑</span>
                    <h2>{{ sourceTitle }}</h2>
                </div>
                <el-tag size="small" effect="plain">{{ outputLabel }}</el-tag>
            </div>
        </template>
        <div class="editor-shell">
            <section class="editor-stage-panel" aria-label="图片预览与编辑画布">
                <div class="editor-toolbar">
                    <el-button-group>
                        <el-button
                            :type="tool === 'brush' ? 'primary' : 'default'"
                            :icon="EditPen"
                            @click="tool = 'brush'"
                        >
                            画笔
                        </el-button>
                        <el-button
                            :type="tool === 'eraser' ? 'primary' : 'default'"
                            :icon="Brush"
                            @click="tool = 'eraser'"
                        >
                            橡皮擦
                        </el-button>
                    </el-button-group>
                    <el-button-group>
                        <el-button :icon="RefreshLeft" title="向左旋转" aria-label="向左旋转" @click="rotateBy(-90)" />
                        <el-button :icon="RefreshRight" title="向右旋转" aria-label="向右旋转" @click="rotateBy(90)" />
                        <el-button :icon="Switch" title="水平翻转" aria-label="水平翻转" @click="toggleFlip('x')" />
                        <el-button
                            :icon="Switch"
                            class="flip-vertical"
                            title="垂直翻转"
                            aria-label="垂直翻转"
                            @click="toggleFlip('y')"
                        />
                    </el-button-group>
                    <el-button :icon="Refresh" @click="resetEditor">还原</el-button>
                </div>
                <div class="editor-canvas-wrap" :class="{ 'is-loading': loadingSource || loading }">
                    <el-skeleton v-if="loadingSource || loading" animated :rows="6" class="editor-skeleton" />
                    <div v-else-if="!hasImage" class="editor-error">
                        <el-icon><WarningFilled /></el-icon>
                        <p>{{ notice || '原图载入失败' }}</p>
                        <el-button :icon="Refresh" @click="retrySource">重新读取</el-button>
                    </div>
                    <canvas
                        v-else
                        ref="canvas"
                        class="editor-canvas"
                        :style="{ transform: `scale(${zoom})` }"
                        role="img"
                        :aria-label="`编辑 ${sourceTitle}`"
                        @pointerdown.prevent="beginStroke"
                        @pointermove.prevent="continueStroke"
                        @pointerup="finishStroke"
                        @pointercancel="finishStroke"
                    />
                </div>
                <div class="editor-zoom">
                    <el-button text :icon="Minus" aria-label="缩小预览" @click="setZoom(zoom - 0.1)" />
                    <el-slider v-model="zoom" :min="0.5" :max="2.4" :step="0.1" :show-tooltip="false" />
                    <el-button text :icon="Plus" aria-label="放大预览" @click="setZoom(zoom + 0.1)" />
                    <span>{{ Math.round(zoom * 100) }}%</span>
                </div>
            </section>
            <aside class="editor-controls" aria-label="图片编辑工具">
                <section class="control-section">
                    <div class="control-title">
                        <el-icon><Crop /></el-icon>
                        <span>裁剪比例</span>
                    </div>
                    <el-segmented
                        :model-value="crop"
                        :options="cropOptions.map((item) => ({ label: item.label, value: item.value }))"
                        @change="setCrop"
                    />
                    <div v-if="crop === 'free'" class="free-crop-controls">
                        <label>
                            保留宽度
                            <el-slider
                                :model-value="freeWidth"
                                :min="10"
                                :max="100"
                                :show-tooltip="false"
                                @update:model-value="changeFreeCrop('width', $event)"
                            />
                            <b>{{ freeWidth }}%</b>
                        </label>
                        <label>
                            保留高度
                            <el-slider
                                :model-value="freeHeight"
                                :min="10"
                                :max="100"
                                :show-tooltip="false"
                                @update:model-value="changeFreeCrop('height', $event)"
                            />
                            <b>{{ freeHeight }}%</b>
                        </label>
                    </div>
                </section>
                <section class="control-section">
                    <div class="control-title">
                        <el-icon><EditPen /></el-icon>
                        <span>涂鸦图层</span>
                        <small>橡皮擦只移除涂鸦</small>
                    </div>
                    <div class="paint-row">
                        <label>
                            颜色
                            <input v-model="color" type="color" aria-label="涂鸦颜色" />
                        </label>
                        <label class="brush-range">
                            粗细
                            <el-slider v-model="brushSize" :min="4" :max="80" :show-tooltip="false" />
                        </label>
                        <b>{{ brushSize }} px</b>
                    </div>
                    <div class="control-actions">
                        <el-button text :disabled="!canUndo" :icon="RefreshLeft" @click="undo">撤销</el-button>
                        <el-button text :disabled="!canRedo" :icon="RefreshRight" @click="redo">重做</el-button>
                        <el-button text @click="clearInk">清除涂鸦</el-button>
                    </div>
                </section>
                <section class="control-section prompt-section">
                    <div class="control-title">
                        <el-icon><EditPen /></el-icon>
                        <span>编辑提示词</span>
                    </div>
                    <el-input
                        v-model="prompt"
                        type="textarea"
                        :rows="4"
                        maxlength="32000"
                        show-word-limit
                        placeholder="可选：描述你想让 AI 编辑的内容"
                    />
                </section>
                <div class="editor-notice" role="status">{{ notice }}</div>
                <div class="editor-bottom-actions">
                    <el-button :icon="Download" @click="download">下载 PNG</el-button>
                    <el-button type="primary" :icon="Upload" :loading="busy || loading" @click="apply('reference')">
                        加入参考图
                    </el-button>
                    <el-button
                        type="primary"
                        plain
                        :icon="FullScreen"
                        :loading="busy || loading"
                        @click="apply('edit')"
                    >
                        使用此图进行 AI 编辑
                    </el-button>
                </div>
            </aside>
        </div>
        <template #footer>
            <div class="editor-footer">
                <span>
                    <el-icon><Close /></el-icon>
                    AI 编辑会生成新作品，原图保持不变
                </span>
                <el-button @click="close">关闭</el-button>
            </div>
        </template>
    </el-dialog>
</template>

<style>
.studio-image-editor-dialog {
    display: flex;
    max-height: 92dvh;
    flex-direction: column;
    padding: 0;
}
.studio-image-editor-dialog .el-dialog__header {
    margin: 0;
    padding: 22px 26px 16px;
    border-bottom: 1px solid var(--line);
}
.studio-image-editor-dialog .el-dialog__body {
    min-height: 0;
    flex: 1;
    padding: 0;
}
.studio-image-editor-dialog .el-dialog__footer {
    flex-shrink: 0;
    padding: 13px 26px;
    border-top: 1px solid var(--line);
}
.editor-heading,
.editor-footer,
.editor-toolbar,
.editor-zoom,
.paint-row,
.control-actions,
.editor-bottom-actions {
    display: flex;
    align-items: center;
}
.editor-heading {
    justify-content: space-between;
    gap: 16px;
}
.editor-eyebrow {
    color: var(--accent);
    font-size: 10px;
    font-weight: 800;
    letter-spacing: 0.14em;
}
.editor-heading h2 {
    margin: 3px 0 0;
    color: var(--ink);
    font-size: 20px;
    line-height: 1.25;
}
.editor-shell {
    display: grid;
    grid-template-columns: minmax(0, 1.35fr) minmax(290px, 0.65fr);
    height: min(690px, 68dvh);
    min-height: 0;
    background: var(--ground);
}
.editor-stage-panel {
    display: flex;
    min-width: 0;
    flex-direction: column;
    padding: 16px;
    background: #202738;
}
.editor-toolbar {
    flex-wrap: wrap;
    gap: 8px;
    padding-bottom: 12px;
}
.editor-toolbar .el-button {
    --el-button-bg-color: #2c3549;
    --el-button-border-color: #47536a;
    --el-button-text-color: #eef1f8;
}
.editor-toolbar .el-button--primary {
    --el-button-bg-color: var(--accent);
    --el-button-border-color: var(--accent);
}
.flip-vertical .el-icon {
    transform: rotate(90deg);
}
.editor-canvas-wrap {
    position: relative;
    display: grid;
    min-height: 0;
    flex: 1;
    place-items: center;
    overflow: auto;
    border: 1px solid #455269;
    border-radius: 14px;
    background: radial-gradient(circle at 30% 20%, #3a4358 0 1px, transparent 1px 13px), #252d40;
    background-size: 14px 14px;
}
.editor-canvas-wrap.is-loading {
    padding: 24px;
}
.editor-skeleton {
    width: min(520px, 80%);
}
.editor-canvas {
    display: block;
    max-width: calc(100% - 28px);
    max-height: calc(100% - 28px);
    object-fit: contain;
    background: #fff;
    box-shadow: 0 18px 50px rgb(0 0 0 / 32%);
    cursor: crosshair;
    transform-origin: center;
    transition: transform 0.18s ease;
    touch-action: none;
}
.editor-error {
    display: grid;
    place-items: center;
    gap: 8px;
    max-width: 360px;
    padding: 42px;
    color: #d8deeb;
    text-align: center;
}
.editor-error .el-icon {
    color: var(--accent);
    font-size: 30px;
}
.editor-error p {
    margin: 0;
}
.editor-zoom {
    gap: 4px;
    padding-top: 8px;
    color: #d8deeb;
}
.editor-zoom .el-slider {
    width: 150px;
    margin: 0 6px;
}
.editor-zoom span {
    width: 44px;
    font-size: 11px;
    text-align: right;
}
.editor-controls {
    display: flex;
    min-width: 0;
    flex-direction: column;
    gap: 14px;
    padding: 18px;
    overflow: auto;
    background: var(--surface);
}
.control-section {
    padding-bottom: 14px;
    border-bottom: 1px solid var(--line);
}
.control-title {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-bottom: 10px;
    color: var(--ink);
    font-size: 13px;
    font-weight: 750;
}
.control-title .el-icon {
    color: var(--accent);
}
.control-title small {
    margin-left: auto;
    color: var(--muted);
    font-size: 10px;
    font-weight: 450;
}
.control-section .el-segmented {
    width: 100%;
}
.free-crop-controls {
    display: grid;
    gap: 5px;
    margin-top: 10px;
}
.free-crop-controls label {
    display: flex;
    align-items: center;
    gap: 9px;
    color: var(--muted);
    font-size: 11px;
}
.free-crop-controls .el-slider {
    flex: 1;
    min-width: 0;
}
.free-crop-controls b {
    min-width: 30px;
    color: var(--ink);
    font-variant-numeric: tabular-nums;
}
.paint-row {
    gap: 8px;
    font-size: 12px;
}
.paint-row label {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    color: var(--muted);
}
.paint-row input[type='color'] {
    width: 28px;
    height: 28px;
    padding: 2px;
    border: 1px solid var(--line);
    border-radius: 7px;
    background: var(--surface);
}
.brush-range {
    min-width: 0;
    flex: 1;
}
.brush-range .el-slider {
    width: 100px;
}
.paint-row b {
    color: var(--ink);
    font-size: 11px;
    font-variant-numeric: tabular-nums;
}
.control-actions {
    flex-wrap: wrap;
    gap: 2px;
    margin-top: 8px;
}
.control-actions .el-button {
    margin: 0;
}
.prompt-section {
    flex: 1;
    min-height: 124px;
}
.editor-notice {
    min-height: 34px;
    padding: 8px 10px;
    border-radius: 8px;
    background: var(--pink-soft);
    color: var(--accent-strong);
    font-size: 11px;
    line-height: 1.5;
}
.editor-bottom-actions {
    flex-wrap: wrap;
    gap: 7px;
}
.editor-bottom-actions .el-button {
    flex: 1 1 135px;
    margin: 0;
}
.editor-bottom-actions .el-button--primary.is-plain:not(:disabled) {
    --el-button-text-color: var(--accent-strong);
    --el-button-bg-color: var(--pink-soft);
    --el-button-border-color: var(--accent);
    --el-button-hover-text-color: var(--surface);
    --el-button-hover-bg-color: var(--accent-strong);
    --el-button-hover-border-color: var(--accent-strong);
    color: var(--accent-strong);
    background: var(--pink-soft);
    border-color: var(--accent);
}
.editor-bottom-actions .el-button--primary.is-plain:not(:disabled):hover {
    color: var(--surface);
    background: var(--accent-strong);
}
.editor-footer {
    justify-content: space-between;
    gap: 12px;
    color: var(--muted);
    font-size: 11px;
}
.editor-footer > span {
    display: inline-flex;
    align-items: center;
    gap: 5px;
}
@media (max-width: 780px) {
    .studio-image-editor-dialog {
        --el-dialog-width: 96vw;
    }
    .editor-shell {
        display: block;
        height: 65dvh;
        overflow: auto;
    }
    .editor-stage-panel {
        min-height: 380px;
        height: 52vh;
    }
    .editor-controls {
        overflow: visible;
    }
}
@media (max-width: 480px) {
    .studio-image-editor-dialog .el-dialog__header {
        padding-inline: 16px;
    }
    .studio-image-editor-dialog .el-dialog__footer {
        padding-inline: 16px;
    }
    .editor-stage-panel {
        min-height: 330px;
        padding: 10px;
    }
    .editor-controls {
        padding: 14px;
    }
    .editor-footer {
        align-items: flex-start;
        flex-direction: column;
    }
}
@media (prefers-reduced-motion: reduce) {
    .editor-canvas {
        transition: none;
    }
}
</style>
