<script setup>
import { computed, nextTick, onMounted, onUnmounted, reactive, ref, watch } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import {
    Picture,
    VideoCamera,
    Microphone,
    Document,
    MagicStick,
    UploadFilled,
    Delete,
    Download,
    Refresh,
    CopyDocument,
    Timer,
    View,
    Connection,
    ArrowRight,
    Check,
    InfoFilled,
} from '@element-plus/icons-vue'
import { api, notifyError } from '../lib/api'
import {
    STUDIO_HISTORY_KEY,
    STUDIO_HISTORY_OWNER_KEY,
    readStudioHistory,
    selectStudioHistory,
    studioHistoryKey,
    writeStudioHistory,
} from '../lib/studio-history'
import { studioMediaNamespace, getStudioMedia, putStudioMedia, deleteStudioMedia } from '../lib/studio-cache'
import Mascot from '../components/Mascot.vue'
import StudioGallery from '../components/StudioGallery.vue'
import StudioReferenceBoard from '../components/StudioReferenceBoard.vue'
import StudioImageEditor from '../components/StudioImageEditor.vue'
import fallbackSource from '../../../static/js/model_catalog.js?raw'

const fallback = JSON.parse(fallbackSource.slice(fallbackSource.indexOf('{'), fallbackSource.lastIndexOf('}') + 1))
const route = useRoute(),
    router = useRouter(),
    origin = window.location.origin
const provider = ref(route.query.platform === 'vids' ? 'vids' : 'flow'),
    kind = ref('image'),
    selectedId = ref(''),
    prompt = ref('')
const loading = ref(false),
    busy = ref(false),
    materialBusy = ref(false),
    error = ref(''),
    catalogMessage = ref('内置能力目录')
const captchaConfigured = ref(null)
const captchaRequired = computed(() => provider.value === 'flow' && captchaConfigured.value === false)
async function refreshCaptchaReadiness() {
    try {
        const health = await api.get('/health')
        if (typeof health.remote_browser_configured === 'boolean')
            captchaConfigured.value = health.remote_browser_configured
    } catch {}
}
function configureCaptcha() {
    router.push('/settings?section=captcha')
}
const details = ref(false),
    documentDrawer = ref(false),
    receiptInput = ref('')
const parameterSections = ref(window.matchMedia?.('(max-width: 850px)').matches ? [] : ['settings'])
const flowModels = ref(Object.entries(fallback).map(([id, entry]) => ({ id, ...entry })))
const vidsModels = ref([]),
    vidsStatus = ref({ configured: false, available: {}, recoverable: {}, quota: {} })
const materials = ref([]),
    sourceVideo = ref(null),
    task = ref(null),
    history = ref([]),
    clock = ref(Date.now())
const historyStorageMessage = ref('')
const mediaNamespace = studioMediaNamespace()
const imageEditorOpen = ref(false),
    imageEditorSource = ref(null),
    editorLoading = ref(false)
const editorRequest = ref(null)
let materialRevision = 0
let pageActive = true
const params = reactive({
    mode: 'text',
    aspectRatio: '16:9',
    imageSize: '1k',
    quality: 'fast',
    priority: 'standard',
    durationSeconds: 8,
    resolution: '720p',
    style: 'auto',
    count: 1,
    voice: 'Puck',
    shotMode: 'auto',
    trimStartMs: 0,
    trimEndMs: 10000,
    mediaId: '',
})
const kinds = [
    { value: 'image', label: '绘梦 · 图片', icon: Picture },
    { value: 'video', label: '映像 · 视频', icon: VideoCamera },
    { value: 'speech', label: '声线 · 语音', icon: Microphone },
    { value: 'document', label: '文档 · 素材', icon: Document },
]
const modeNames = {
    text: '文字生成',
    text_to_video: '文字生成',
    image_to_video: '首帧生视频',
    first_last_frame: '首尾帧',
    reference_to_video: '参考图视频',
    animate: '单图动画',
    ingredients: '多图参考',
    edit: '编辑素材',
    extend: '视频续写',
    upscale: '放大至 1080p',
}
const qualityNames = { fast: '快速', quality: '高质量', lite: '轻量' },
    styleNames = {
        auto: '自动',
        photography: '摄影',
        background: '背景',
        vector: '矢量插画',
        sketch: '素描',
        watercolor: '水彩',
        cyberpunk: '赛博朋克',
    }
const fallbackVoices = [
    'Achernar',
    'Achird',
    'Algenib',
    'Algieba',
    'Alnilam',
    'Aoede',
    'Autonoe',
    'Callirrhoe',
    'Charon',
    'Despina',
    'Enceladus',
    'Erinome',
    'Fenrir',
    'Gacrux',
    'Iapetus',
    'Kore',
    'Laomedeia',
    'Leda',
    'Orus',
    'Puck',
    'Pulcherrima',
    'Rasalgethi',
    'Sadachbia',
    'Sadaltager',
    'Schedar',
    'Sulafat',
    'Umbriel',
    'Vindemiatrix',
    'Zephyr',
    'Zubenelgenubi',
]
const vidsFallback = {
    image: {
        modes: ['text', 'edit'],
        counts: [1, 2, 3, 4],
        aspect_ratios: ['1:1', '16:9', '9:16'],
        styles: Object.keys(styleNames),
        max_input_images: 1,
    },
    video: {
        modes: ['text', 'animate', 'ingredients', 'edit', 'extend', 'upscale'],
        seconds: [3, 4, 5, 6, 7, 8, 9, 10],
        source_sizes: ['1280x720', '720x1280', '1920x1080', '1080x1920'],
        max_reference_images: 7,
        max_edit_reference_images: 3,
        shot_modes: ['auto', 'long_take', 'montage'],
    },
    speech: { voices: fallbackVoices, verified_voices: ['Puck', 'Gacrux', 'Algenib'] },
}
const models = computed(() =>
    provider.value === 'flow'
        ? flowModels.value.filter((item) => item.type === kind.value)
        : vidsModels.value.filter((item) => item.kind === kind.value),
)
const model = computed(() => models.value.find((item) => item.id === selectedId.value) || models.value[0])
const capabilities = computed(
    () => model.value?.capabilities || (provider.value === 'vids' ? vidsFallback[kind.value] || {} : {}),
)
const modes = computed(() =>
    provider.value === 'flow'
        ? kind.value === 'image'
            ? ['text', 'edit']
            : Object.keys(capabilities.value.modes || {})
        : capabilities.value.modes || [],
)
const imageEditing = computed(() => kind.value === 'image' && params.mode === 'edit')
const modeLimits = computed(() => {
    if (provider.value === 'flow')
        return kind.value === 'image'
            ? { ...capabilities.value, minImages: imageEditing.value ? 1 : 0 }
            : capabilities.value.modes?.[params.mode] || {}
    if (kind.value === 'image')
        return {
            minImages: imageEditing.value ? 1 : 0,
            maxImages: imageEditing.value ? capabilities.value.max_input_images || 1 : 0,
        }
    return {
        minImages: ['animate', 'ingredients'].includes(params.mode) ? 1 : 0,
        maxImages:
            params.mode === 'animate'
                ? 1
                : params.mode === 'ingredients'
                  ? capabilities.value.max_reference_images || 7
                  : params.mode === 'edit'
                    ? capabilities.value.max_edit_reference_images || 3
                    : 0,
        videoInput: ['edit', 'extend', 'upscale'].includes(params.mode),
    }
})
const flowModeRows = computed(() => (capabilities.value.combinations || []).filter((row) => row.mode === params.mode))
const priorities = computed(() => unique(flowModeRows.value.map((row) => row.priority || 'standard')))
const qualities = computed(() =>
    unique(
        flowModeRows.value
            .filter((row) => (row.priority || 'standard') === params.priority)
            .map((row) => row.quality)
            .filter(Boolean),
    ),
)
const combinations = computed(() =>
    flowModeRows.value.filter(
        (row) =>
            (row.priority || 'standard') === params.priority &&
            (row.quality || '') === (qualities.value.length ? params.quality : ''),
    ),
)
const durations = computed(() =>
    provider.value === 'flow'
        ? unique(combinations.value.flatMap((row) => row.durations))
        : capabilities.value.seconds || [],
)
const resolutions = computed(() =>
    provider.value === 'flow' ? unique(combinations.value.flatMap((row) => row.resolutions)) : ['720p', '1080p'],
)
const aspects = computed(() =>
    provider.value === 'flow'
        ? capabilities.value.aspectRatios || []
        : kind.value === 'image'
          ? capabilities.value.aspect_ratios || []
          : ['16:9', '9:16'],
)
const voiceOptions = computed(
    () =>
        capabilities.value.voice_options ||
        (capabilities.value.voices || fallbackVoices).map((id) => ({ id, name: id, description: '' })),
)
const selectedVoice = computed(() => voiceOptions.value.find((item) => item.id === params.voice))
const specificationSummary = computed(() =>
    kind.value === 'speech'
        ? selectedVoice.value?.name || params.voice
        : [
              modeNames[params.mode] || params.mode,
              !(imageEditing.value && provider.value === 'vids') && params.mode !== 'upscale' ? params.aspectRatio : '',
              kind.value === 'image'
                  ? provider.value === 'flow'
                      ? params.imageSize.toUpperCase()
                      : imageEditing.value
                        ? ''
                        : `${params.count} 张`
                  : params.mode === 'upscale'
                    ? '1080P'
                    : `${params.durationSeconds} 秒 · ${params.resolution.toUpperCase()}`,
          ]
              .filter(Boolean)
              .join(' · '),
)
const readyCount = computed(() => positiveCount(vidsStatus.value.available?.[kind.value]))
const recoverableCount = computed(() => positiveCount(vidsStatus.value.recoverable?.[kind.value]))
const available = computed(() =>
    provider.value === 'flow'
        ? Boolean(model.value)
        : vidsStatus.value.configured && (readyCount.value > 0 || recoverableCount.value > 0),
)
const elapsed = computed(() =>
    task.value ? Math.max(0, Math.floor(((task.value.ended || clock.value) - task.value.started) / 1000)) : 0,
)
const taskStatusNames = {
    preparing: '准备请求',
    generating: '生成进行中',
    queued: '等待调度',
    in_progress: '生成进行中',
    received: '成品正在准备',
    completed: '作品已完成',
    download_failed: '下载等待恢复',
    unknown: '等待确认原请求',
    failed: '请求未完成',
    no_result: '上游未返回成品',
    specification_mismatch: '输出规格校验未通过',
}
const pendingStates = ['generating', 'queued', 'in_progress', 'received', 'unknown', 'download_failed']
const examples = computed(() =>
    kind.value === 'image'
        ? [
              {
                  label: '晴空与少女',
                  text: '一位穿着浅蓝裙子的成年少女，抱着白猫站在夏日海边，清澈天空、细腻日系动画插画、自然光，干净构图。',
              },
              {
                  label: '水彩小宇宙',
                  text: '漂浮在云海中的小小书店，粉蓝水彩、柔和纸张纹理、温暖窗光，精致的日系绘本场景。',
              },
              {
                  label: '角色设定',
                  text: '成年原创角色设定图，粉色短发、蓝白机能外套、星形发饰，全身正面站姿，纯色背景，精致动漫画风。',
              },
          ]
        : kind.value === 'video'
          ? [
                {
                    label: '风吹樱花',
                    text: '日系动画场景，春日校园外的樱花树，花瓣随轻风缓缓飘落，镜头缓慢向前移动，柔和午后光线。',
                },
                { label: '海边日落', text: '海边日落，波光缓缓闪烁，镜头沿沙滩平稳移动，蓝粉色天空，电影感光影。' },
            ]
          : [
                {
                    label: '英文欢迎语',
                    text: 'Welcome to our little creative studio. Let your imagination bloom, one lovely idea at a time.',
                },
                {
                    label: '温柔旁白',
                    text: 'Under a sky full of stars, a small blue boat carried a dream across the quiet sea.',
                },
            ],
)
const validation = computed(() => {
    if (kind.value === 'document') return ''
    const min = modeLimits.value.minImages || 0,
        max = modeLimits.value.maxImages || 0
    if (materials.value.length < min) return `当前方式请添加至少 ${min} 张图片。`
    if (materials.value.length > max) return `当前方式最多接受 ${max} 张图片。`
    if (modeLimits.value.videoInput && !sourceVideo.value) return '请添加本次创作要使用的原视频。'
    if (provider.value === 'flow' && kind.value === 'video') {
        if (modeLimits.value.mediaId && !modeLimits.value.videoInput && !params.mediaId.trim())
            return '续写需要已有 Flow 视频的媒体 ID。'
        if (
            !combinations.value.some(
                (row) =>
                    row.resolutions.includes(params.resolution) &&
                    (params.mode === 'edit' || row.durations.includes(params.durationSeconds)),
            )
        )
            return '请选择模型支持的时长与清晰度组合。'
        if (
            params.mode === 'edit' &&
            (!Number.isInteger(params.trimStartMs) ||
                !Number.isInteger(params.trimEndMs) ||
                params.trimStartMs < 0 ||
                params.trimEndMs <= params.trimStartMs ||
                params.trimEndMs - params.trimStartMs > 10000)
        )
            return '编辑范围请使用整数毫秒，单段最长 10 秒。'
        if (
            params.mode === 'edit' &&
            sourceVideo.value?.duration &&
            params.trimEndMs > sourceVideo.value.duration * 1000
        )
            return '片段结束时间超过原视频时长。'
    }
    if (sourceVideo.value && provider.value === 'vids') {
        const maximum =
            capabilities.value.max_input_video_seconds_by_mode?.[params.mode] || (params.mode === 'upscale' ? 10 : 60)
        if (sourceVideo.value.duration && (sourceVideo.value.duration < 3 || sourceVideo.value.duration > maximum))
            return `当前视频请使用 3–${maximum} 秒的 MP4。`
    }
    return ''
})

function unique(values) {
    return [...new Set(values)]
}
function positiveCount(value) {
    return Number.isSafeInteger(value) && value > 0 ? value : 0
}
function resetMaterials() {
    materialRevision++
    materials.value = []
    sourceVideo.value = null
}
function selectModel() {
    selectedId.value = model.value?.id || (provider.value === 'vids' ? `vids-${kind.value}` : '')
    Object.assign(
        params,
        {
            mode: kind.value === 'video' && provider.value === 'flow' ? 'text_to_video' : 'text',
            aspectRatio: provider.value === 'vids' && kind.value === 'image' ? '1:1' : '16:9',
            imageSize: '1k',
            quality: 'fast',
            priority: 'standard',
            durationSeconds: provider.value === 'vids' ? 3 : 8,
            resolution: '720p',
            style: 'auto',
            count: 1,
            voice: 'Puck',
            shotMode: 'auto',
            trimStartMs: 0,
            trimEndMs: 10000,
            mediaId: '',
        },
        model.value?.defaults || {},
    )
    if (params.mode === 'auto') params.mode = 'text_to_video'
    resetMaterials()
    error.value = ''
    normalizeOptions()
}
function normalizeOptions() {
    if (provider.value !== 'flow' || kind.value !== 'video') return
    if (!priorities.value.includes(params.priority)) params.priority = priorities.value[0] || 'standard'
    if (!qualities.value.includes(params.quality)) params.quality = qualities.value[0] || ''
    if (!durations.value.includes(params.durationSeconds)) params.durationSeconds = durations.value[0] || 8
    if (!resolutions.value.includes(params.resolution)) params.resolution = resolutions.value[0] || '720p'
    if (
        !combinations.value.some(
            (row) =>
                row.resolutions.includes(params.resolution) &&
                (params.mode === 'edit' || row.durations.includes(params.durationSeconds)),
        )
    ) {
        const row =
            combinations.value.find((item) => item.resolutions.includes(params.resolution)) || combinations.value[0]
        if (row?.durations.length) params.durationSeconds = row.durations[0]
    }
}
watch([provider, kind], () => {
    if (kind.value === 'speech') provider.value = 'vids'
    selectedId.value = models.value[0]?.id || ''
    selectModel()
})
watch(selectedId, selectModel)
watch(() => [params.mode, params.priority, params.quality], normalizeOptions)
watch(
    () => params.mode,
    () => {
        resetMaterials()
        error.value = ''
    },
)
watch(
    () => [params.mode, kind.value, provider.value],
    () => {
        if (modeLimits.value.minImages > 0 || modeLimits.value.videoInput)
            parameterSections.value = unique([...parameterSections.value, 'materials'])
    },
)
async function navigateKind(event) {
    if (busy.value || materialBusy.value || !['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const tablist = event.currentTarget?.parentElement
    const index = kinds.findIndex((item) => item.value === kind.value)
    const target =
        event.key === 'Home'
            ? 0
            : event.key === 'End'
              ? kinds.length - 1
              : (index + (event.key === 'ArrowRight' ? 1 : -1) + kinds.length) % kinds.length
    kind.value = kinds[target].value
    await nextTick()
    tablist?.querySelector(`[data-kind="${kind.value}"]`)?.focus()
}

let catalogRevision = 0
async function loadCatalog() {
    const revision = ++catalogRevision,
        requestedProvider = provider.value
    if (requestedProvider === 'flow') refreshCaptchaReadiness()
    loading.value = true
    try {
        if (requestedProvider === 'flow') {
            const response = await api.raw('/api/admin/studio/models', { redirect: 'error' })
            const data = await readJSON(response)
            if (revision !== catalogRevision || requestedProvider !== provider.value) return
            const entries = (data.data || []).filter(
                (item) =>
                    !item.legacy &&
                    item.id &&
                    item.capabilities &&
                    item.defaults &&
                    ['image', 'video'].includes(item.type),
            )
            if (!entries.length) throw new Error('服务端返回的公开能力目录为空。')
            flowModels.value = entries
            catalogMessage.value = `服务端目录 · ${entries.length} 个模型`
        } else {
            const settled = await Promise.allSettled([
                api.get('/api/admin/vids/status'),
                api.get('/api/admin/vids/v1/models'),
            ])
            if (revision !== catalogRevision || requestedProvider !== provider.value) return
            if (settled[0].status === 'fulfilled') vidsStatus.value = settled[0].value
            else {
                vidsStatus.value = { configured: false, available: {}, recoverable: {}, quota: {} }
                throw settled[0].reason
            }
            if (settled[1].status === 'fulfilled') vidsModels.value = settled[1].value.data || []
            catalogMessage.value = `${readyCount.value} 个就绪账号 · ${recoverableCount.value} 个可恢复会话`
        }
        if (!models.value.some((item) => item.id === selectedId.value)) {
            selectedId.value = models.value[0]?.id || ''
            selectModel()
        }
    } catch (cause) {
        if (revision === catalogRevision) error.value = cause.message || '模型目录读取失败，请重新连接。'
    } finally {
        if (revision === catalogRevision) loading.value = false
    }
}
watch(provider, loadCatalog)
watch(
    () => route.query.platform,
    (value) => {
        if (!busy.value && !materialBusy.value) provider.value = value === 'vids' ? 'vids' : 'flow'
    },
)
async function readJSON(response) {
    const data = await response
        .json()
        .catch(() => ({ error: { message: `服务端响应格式异常（HTTP ${response.status}）` } }))
    if (!response.ok)
        throw Object.assign(new Error(data.error?.message || data.detail || `HTTP ${response.status}`), {
            data,
            status: response.status,
        })
    return data
}
function fileData(file) {
    return new Promise((resolve, reject) => {
        const reader = new FileReader()
        reader.onload = () => resolve(String(reader.result))
        reader.onerror = () => reject(new Error('文件读取失败，请重新选择。'))
        reader.readAsDataURL(file)
    })
}
let imageQueue = Promise.resolve()
function addImage(uploadFile) {
    imageQueue = imageQueue.then(() => readImage(uploadFile))
    return imageQueue
}
async function readImage(uploadFile) {
    const file = uploadFile.raw
    if (!file) return
    const revision = materialRevision
    materialBusy.value = true
    error.value = ''
    try {
        if (!['image/png', 'image/jpeg', 'image/webp'].includes(file.type))
            throw new Error('请选择 PNG、JPEG 或 WebP 图片。')
        if (materials.value.length >= (modeLimits.value.maxImages || 0))
            throw new Error(`当前方式最多接受 ${modeLimits.value.maxImages || 0} 张图片。`)
        const singleLimit = provider.value === 'vids' ? 10 : 20,
            totalLimit = provider.value === 'vids' ? 10 : 100
        if (
            file.size > singleLimit * 1024 ** 2 ||
            materials.value.reduce((sum, item) => sum + item.bytes, file.size) > totalLimit * 1024 ** 2
        )
            throw new Error(`图片单张最多 ${singleLimit} MiB，总量最多 ${totalLimit} MiB。`)
        const data = await fileData(file)
        if (revision !== materialRevision || !pageActive) return
        if (materials.value.length < modeLimits.value.maxImages)
            materials.value.push({ name: file.name, bytes: file.size, data })
    } catch (cause) {
        error.value = cause.message
    } finally {
        materialBusy.value = false
    }
}
async function ensureReferenceMode() {
    if (kind.value === 'speech' || kind.value === 'document') {
        error.value = '请先切换到图片或视频创作，再添加参考图。'
        return false
    }
    if (!(modeLimits.value.maxImages > 0)) {
        const preferred = kind.value === 'image' ? 'edit' : provider.value === 'vids' ? 'animate' : 'image_to_video'
        if (!modes.value.includes(preferred)) {
            error.value = '此模型当前创作方式没有图片输入，请选择支持参考图片的模型。'
            return false
        }
        params.mode = preferred
        await nextTick()
    }
    return true
}
async function addReferenceFiles(files) {
    if (busy.value || materialBusy.value || !(await ensureReferenceMode())) return
    const images = [...files].filter((file) => file.type.startsWith('image/'))
    if (!images.length) {
        error.value = '请添加 PNG、JPEG 或 WebP 图片。'
        return
    }
    for (const file of images) await addImage({ raw: file })
}
async function pasteImages(event) {
    if (imageEditorOpen.value || busy.value || materialBusy.value) return
    const files = [...(event.clipboardData?.files || [])].filter((file) => file.type.startsWith('image/'))
    if (!files.length) return
    event.preventDefault()
    await addReferenceFiles(files)
}
async function readClipboardImages() {
    try {
        if (!navigator.clipboard?.read) throw new Error('请在页面按 Ctrl + V 粘贴图片。')
        const files = []
        for (const item of await navigator.clipboard.read()) {
            const type = item.types.find((value) => ['image/png', 'image/jpeg', 'image/webp'].includes(value))
            if (type)
                files.push(
                    new File([await item.getType(type)], `粘贴图片-${Date.now()}.${type.split('/')[1]}`, { type }),
                )
        }
        if (!files.length) throw new Error('剪贴板里还没有图片。复制截图后再粘贴。')
        await addReferenceFiles(files)
    } catch (cause) {
        error.value =
            cause.name === 'NotAllowedError' ? '请允许读取剪贴板，或在页面按 Ctrl + V 粘贴图片。' : cause.message
    }
}
function moveReference(index, direction) {
    const next = index + direction
    if (busy.value || materialBusy.value || next < 0 || next >= materials.value.length) return
    const [material] = materials.value.splice(index, 1)
    materials.value.splice(next, 0, material)
}
async function useDraggedMedia(data) {
    if (busy.value || materialBusy.value) return
    const entry = history.value.find((item) => String(item.key || item.receipt || item.started) === data.entryKey)
    const index = Number(data.outputIndex)
    const output = entry?.outputs?.[index]
    if (!entry || !Number.isInteger(index) || output?.kind !== 'image') return
    try {
        const blob = await mediaBlob(output, entry, index)
        if (!blob) throw new Error('原图片尚未载入，请先重新读取预览。')
        await addReferenceFiles([
            new File([blob], `作品参考-${index + 1}.${blob.type.split('/')[1] || 'png'}`, { type: blob.type }),
        ])
    } catch (cause) {
        error.value = cause.message
    }
}
async function referenceResult({ entry, output, index = 0 }, animate = false) {
    if (busy.value || materialBusy.value) return
    try {
        const blob = await mediaBlob(output, entry, index)
        if (!blob) throw new Error('原图片尚未载入，请先重新读取预览。')
        if (animate) {
            kind.value = 'video'
            await nextTick()
            params.mode = provider.value === 'vids' ? 'animate' : 'image_to_video'
            await nextTick()
            resetMaterials()
        }
        await addReferenceFiles([
            new File([blob], `作品参考-${index + 1}.${blob.type.split('/')[1] || 'png'}`, { type: blob.type }),
        ])
        if (!error.value) ElMessage.success(animate ? '原图已设为视频首帧，写下动态描述后即可生成' : '作品已加入参考区')
    } catch (cause) {
        error.value = cause.message
    }
}
function dropPrompt(event) {
    const value = event.dataTransfer?.getData('application/x-lay4flow-prompt')
    if (value) {
        event.preventDefault()
        prompt.value = value.slice(0, 32000)
    }
}
async function openImageEditor({ entry, output, index = 0 }) {
    if (busy.value || editorLoading.value) return
    editorRequest.value = { entry, output, index }
    imageEditorSource.value = {
        url: '',
        title: entry.title || '图片作品',
        prompt: entry.description || entry.title || '',
    }
    imageEditorOpen.value = true
    editorLoading.value = true
    try {
        const blob = await mediaBlob(output, entry, index)
        if (!imageEditorOpen.value) return
        if (!blob) throw new Error('原图片尚未载入，请先重新读取预览。')
        imageEditorSource.value = {
            file: blob,
            title: entry.title || '图片作品',
            prompt: entry.description || entry.title || '',
        }
    } catch (cause) {
        imageEditorSource.value = { error: cause.message, title: entry.title || '图片作品' }
    } finally {
        editorLoading.value = false
    }
}
function editReference(index) {
    const material = materials.value[index]
    if (!material) return
    editorRequest.value = null
    imageEditorSource.value = {
        url: material.data,
        title: material.name,
        prompt: prompt.value,
        materialIndex: index,
        revision: materialRevision,
    }
    imageEditorOpen.value = true
}
async function applyImageEdit({ file, prompt: editPrompt, mode }) {
    if (busy.value || materialBusy.value) return
    error.value = ''
    const singleLimit = (provider.value === 'vids' ? 10 : 20) * 1024 ** 2
    if (!(file instanceof File) || !['image/png', 'image/jpeg', 'image/webp'].includes(file.type)) {
        error.value = '请导出 PNG、JPEG 或 WebP 图片。'
        ElMessage.warning(error.value)
        return
    }
    if (file.size > singleLimit) {
        error.value = `编辑后的图片超过 ${provider.value === 'vids' ? 10 : 20} MB，请裁剪后再使用。`
        ElMessage.warning(error.value)
        return
    }
    const source = imageEditorSource.value
    if (mode === 'edit') {
        kind.value = 'image'
        await nextTick()
        params.mode = 'edit'
        await nextTick()
        if (editPrompt?.trim()) prompt.value = editPrompt
    }
    if (!(await ensureReferenceMode())) return
    const replace = source?.materialIndex
    if (Number.isInteger(replace) && source.revision === materialRevision && materials.value[replace]) {
        const total = materials.value.reduce(
            (sum, item, index) => sum + (index === replace ? file.size : item.bytes),
            0,
        )
        if (total > (provider.value === 'vids' ? 10 : 100) * 1024 ** 2) {
            error.value = '参考图片总量已超出当前模型限制，请减少素材后再使用。'
            ElMessage.warning(error.value)
            return
        }
        materials.value[replace] = { name: file.name, data: await fileData(file), bytes: file.size }
    } else {
        if (mode === 'edit') materials.value = []
        await addImage({ raw: file })
    }
    if (error.value) return
    imageEditorOpen.value = false
    ElMessage.success(mode === 'edit' ? '编辑后的图片已设为原图，修改描述后点击开始绘梦' : '编辑后的图片已加入参考区')
}
function studioShortcuts(event) {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter' && !imageEditorOpen.value) {
        event.preventDefault()
        if (!busy.value && !materialBusy.value && prompt.value.trim() && available.value && !validation.value)
            generate()
    }
}
async function addVideo(uploadFile) {
    const file = uploadFile.raw
    if (!file) return
    materialBusy.value = true
    error.value = ''
    try {
        const limit = provider.value === 'vids' ? 50 : 100
        if (
            !(provider.value === 'vids' ? ['video/mp4'] : ['video/mp4', 'video/webm', 'video/quicktime']).includes(
                file.type,
            )
        )
            throw new Error(
                provider.value === 'vids' ? '请选择 MP4 原视频。' : '请选择 MP4、WebM 或 QuickTime 原视频。',
            )
        if (file.size > limit * 1024 ** 2) throw new Error(`视频大小请控制在 ${limit} MiB 内。`)
        sourceVideo.value = { name: file.name, bytes: file.size, data: await fileData(file), duration: null }
    } catch (cause) {
        error.value = cause.message
    } finally {
        materialBusy.value = false
    }
}
function videoMetadata(event) {
    const duration = event.target.duration
    if (sourceVideo.value && Number.isFinite(duration)) {
        sourceVideo.value.duration = duration
        params.trimEndMs = Math.min(10000, Math.round(duration * 1000))
    }
}
function buildBody() {
    if (validation.value) throw new Error(validation.value)
    if (!prompt.value.trim()) throw new Error('先写下这次创作的画面或朗读文本。')
    if (!available.value)
        throw new Error(
            provider.value === 'flow' ? '当前没有可用模型，请重新同步能力。' : '请先在账号页完成 Vids 接入。',
        )
    if (provider.value === 'vids') {
        const body = { kind: kind.value, prompt: prompt.value }
        if (kind.value === 'speech') return { ...body, voice: params.voice }
        body.mode = params.mode
        if (kind.value === 'image')
            return imageEditing.value
                ? { ...body, image_base64: materials.value[0].data.split(',')[1] }
                : { ...body, n: params.count, aspect_ratio: params.aspectRatio, style: params.style }
        if (params.mode !== 'upscale') Object.assign(body, { seconds: params.durationSeconds, size: vidsSize() })
        if (['text', 'ingredients', 'edit'].includes(params.mode)) body.shot_mode = params.shotMode
        if (sourceVideo.value && modeLimits.value.videoInput) body.video_base64 = sourceVideo.value.data.split(',')[1]
        if (params.mode === 'animate') body.image_base64 = materials.value[0].data.split(',')[1]
        else if (['ingredients', 'edit'].includes(params.mode) && materials.value.length)
            body.images_base64 = materials.value.map((item) => item.data.split(',')[1])
        return body
    }
    const content = [
        { type: 'text', text: prompt.value },
        ...materials.value.map((item) => ({ type: 'image_url', image_url: { url: item.data } })),
    ]
    const generationConfig = {}
    if (kind.value === 'image')
        generationConfig.imageConfig = { aspectRatio: params.aspectRatio, imageSize: params.imageSize }
    else {
        const videoConfig = { mode: params.mode, aspectRatio: params.aspectRatio, resolution: params.resolution }
        if (qualities.value.length) videoConfig.quality = params.quality
        if (capabilities.value.priorities?.length) videoConfig.priority = params.priority
        if (params.mode === 'edit')
            Object.assign(videoConfig, { trimStartMs: params.trimStartMs, trimEndMs: params.trimEndMs })
        else videoConfig.durationSeconds = params.durationSeconds
        generationConfig.videoConfig = videoConfig
        if (modeLimits.value.videoInput) content.push({ type: 'video_url', video_url: { url: sourceVideo.value.data } })
        if (modeLimits.value.mediaId && !modeLimits.value.videoInput) {
            const id = params.mediaId.trim().replace(/^extend:\/\//, '')
            if (/\s/.test(id)) throw new Error('媒体 ID 请移除空白字符。')
            content.push({ type: 'video_url', video_url: { url: `extend://${id}` } })
        }
    }
    return {
        model: selectedId.value,
        messages: [{ role: 'user', content: content.length === 1 ? prompt.value : content }],
        generationConfig,
        stream: true,
    }
}
function vidsSize() {
    return params.aspectRatio === '9:16'
        ? params.resolution === '1080p'
            ? '1080x1920'
            : '720x1280'
        : params.resolution === '1080p'
          ? '1920x1080'
          : '1280x720'
}
function fingerprint(body) {
    let hash = 2166136261
    const text = JSON.stringify(body)
    for (let index = 0; index < text.length; index++) hash = Math.imul(hash ^ text.charCodeAt(index), 16777619)
    return `${(hash >>> 0).toString(16)}:${text.length}`
}
function redactBody(body) {
    return JSON.parse(
        JSON.stringify(body, (key, value) =>
            key.endsWith('base64')
                ? Array.isArray(value)
                    ? value.map(() => '[本地素材已省略]')
                    : '[本地素材已省略]'
                : key === 'url' && typeof value === 'string' && value.startsWith('data:')
                  ? '[本地素材已省略]'
                  : value,
        ),
    )
}
function savedMediaURL(value) {
    if (typeof value !== 'string' || value.length > 8192) return ''
    try {
        const url = new URL(value, location.origin)
        return ['http:', 'https:'].includes(url.protocol) && !url.username && !url.password ? url.href : ''
    } catch {
        return ''
    }
}
function savedSettings(value) {
    const snapshot = {}
    if (!value || typeof value !== 'object') return snapshot
    for (const name of Object.keys(params)) {
        const field = value[name]
        if (typeof field === 'string' && field.length <= 256) snapshot[name] = field
        else if (typeof field === 'number' && Number.isFinite(field)) snapshot[name] = field
        else if (typeof field === 'boolean') snapshot[name] = field
    }
    return snapshot
}
function persistHistory() {
    try {
        const saved = history.value.map((entry) => ({
            provider: entry.provider,
            kind: entry.kind,
            model: String(entry.model || '').slice(0, 128),
            status: entry.status,
            started: entry.started,
            ended: entry.ended,
            key: String(entry.key || '').slice(0, 128),
            receipt: entry.receipt || '',
            signature: String(entry.signature || '').slice(0, 128),
            title: String(entry.title || '').slice(0, 100),
            description: String(entry.description || '').slice(0, 32000),
            settings: savedSettings(entry.settings),
            message: String(entry.message || '').slice(0, 1000),
            outputs: (entry.outputs || []).flatMap((output) =>
                entry.provider === 'vids'
                    ? output.path
                        ? [{ path: output.path, kind: output.kind }]
                        : []
                    : savedMediaURL(output.sourceURL || output.url)
                      ? [
                            {
                                url: savedMediaURL(output.sourceURL || output.url),
                                originalURL: savedMediaURL(output.originalURL),
                                kind: output.kind,
                            },
                        ]
                      : [],
            ),
        }))
        const resultEntries = pageActive ? saved : mergeInactiveHistory(saved)
        let result = writeStudioHistory(localStorage, resultEntries, studioHistoryKey(mediaNamespace))
        const durableSaved = result.saved
        const mirror = writeStudioHistory(sessionStorage, resultEntries)
        try {
            sessionStorage.setItem(STUDIO_HISTORY_OWNER_KEY, mediaNamespace)
        } catch {
            /* best-effort legacy mirror */
        }
        if (!result.saved) result = mirror
        historyStorageMessage.value = !result.saved
            ? '浏览器存储已满或被禁用，本次新记录尚未保存。请检查本地存储设置；Vids 新请求会暂停，已有收据仍可查询。'
            : !durableSaved
              ? '本机长期存储暂不可用，记录已临时保存在当前浏览器会话；请检查本地存储权限并下载重要作品。'
              : result.reduced
                ? `本机存储空间不足，已保存 ${result.retained} 条请求，所有待确认 Vids 收据均保留。请及时下载作品。`
                : result.omitted
                  ? '本机保留最近 300 条请求及所有待确认 Vids 收据；更早作品请及时下载保存。'
                  : ''
        return result.saved
    } catch {
        historyStorageMessage.value =
            '浏览器存储被禁用，本次新记录尚未保存。请检查本地存储设置；Vids 新请求会暂停，已有收据仍可查询。'
        return false
    }
}
function mergeInactiveHistory(saved) {
    const current = readStudioHistory(localStorage, sessionStorage, mediaNamespace)
    if (!Array.isArray(current)) return []
    const identity = (entry) => entry?.receipt || entry?.key || `${entry?.provider}:${entry?.started}`
    const updates = new Map(saved.map((entry) => [identity(entry), entry]))
    return current.map((entry) => {
        const update = updates.get(identity(entry))
        if (!update) return entry
        // A newer instance may already have completed/recovered this request.
        // Only merge unfinished records; deleted entries remain deleted.
        return (entry.status === 'completed' && !entry.outputs?.length) ||
            ['preparing', 'generating', 'queued', 'in_progress', 'received', 'unknown', 'download_failed'].includes(
                entry.status,
            )
            ? update
            : entry
    })
}
async function receiptFor(key) {
    if (!crypto.subtle) return ''
    const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(key))
    return 'vids_' + [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, '0')).join('')
} // taste-skill-ignore: SHA-256 hex bytes require two-character zero padding.
async function generate() {
    if (busy.value || materialBusy.value) return
    error.value = ''
    if (captchaRequired.value) {
        error.value = 'Flow 创作需要先申请打码服务，并在设置中保存 API 密钥。'
        return
    }
    let body
    try {
        body = buildBody()
    } catch (cause) {
        error.value = cause.message
        return
    }
    const signature = fingerprint(body)
    const duplicate =
        provider.value === 'vids' &&
        history.value.find(
            (item) => item.provider === 'vids' && item.signature === signature && pendingStates.includes(item.status),
        )
    if (duplicate) {
        task.value = duplicate
        error.value = '相同内容的原请求等待确认，请在作品区查询原收据。'
        return
    }
    busy.value = true
    const key = crypto.randomUUID?.() || `studio-${Date.now()}-${Math.random().toString(36).slice(2)}`
    const current = reactive({
        provider: provider.value,
        kind: kind.value,
        model: selectedId.value,
        status: 'preparing',
        started: Date.now(),
        key,
        receipt: '',
        signature,
        title: prompt.value.slice(0, 38),
        description: prompt.value,
        settings: savedSettings(params),
        text: '',
        progressLog: '',
        outputs: [],
        body: redactBody(body),
        message: '准备素材与生成请求…',
    })
    task.value = current
    history.value.unshift(current)
    try {
        if (current.provider === 'vids') {
            current.receipt = await receiptFor(key)
            current.status = 'generating'
            current.message = '请求已发出，正在等待原生成收据。'
            if (!persistHistory()) {
                const cause = new Error('请先释放浏览器会话存储或允许本地存储，再提交新的 Vids 请求。')
                cause.status = 400
                throw cause
            }
            const response = await api.raw('/api/admin/vids/v1/vids/generations', {
                method: 'POST',
                redirect: 'error',
                headers: { 'Content-Type': 'application/json', 'Idempotency-Key': key },
                body: JSON.stringify(body),
            })
            const data = await readJSON(response)
            applyReceipt(current, data)
            if (current.status === 'completed') await preview(current)
        } else await generateFlow(current, body)
    } catch (cause) {
        if (cause.data?.vids_receipt) {
            try {
                applyReceipt(current, cause.data.vids_receipt)
            } catch {
                current.status = 'unknown'
            }
        } else if (current.status !== 'completed')
            current.status =
                current.provider === 'vids' && (!cause.status || cause.status >= 500) ? 'unknown' : 'failed'
        current.message =
            current.status === 'completed'
                ? '成品生成已完成，预览读取失败。请点击「读取成品」重试。'
                : current.status === 'unknown'
                  ? '服务器接受状态等待确认。原收据与幂等键已保留，请查询原任务。'
                  : cause.message
        error.value = cause.message
    } finally {
        current.ended = Date.now()
        busy.value = false
        persistHistory()
    }
}
async function generateFlow(current, body) {
    current.status = 'generating'
    current.message = '已连接生成接口，等待服务端事件。'
    const response = await api.raw('/api/admin/studio/chat/completions', {
        method: 'POST',
        redirect: 'error',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
    })
    if (!response.ok) await readJSON(response)
    if (!response.body) throw new Error('浏览器未收到可读取的响应流。')
    const reader = response.body.getReader(),
        decoder = new TextDecoder()
    let buffer = '',
        failed = ''
    function consume(line) {
        if (!line.startsWith('data:')) return
        const value = line.slice(5).trim()
        if (!value || value === '[DONE]') return
        let data
        try {
            data = JSON.parse(value)
        } catch {
            failed = '服务端响应流包含未完成的事件，请检查原请求记录。'
            return
        }
        if (data.error) {
            failed = data.error.message || '生成接口返回错误'
            return
        }
        const delta = data.choices?.[0]?.delta || {},
            chunk = delta.content || '',
            progress = delta.reasoning_content || ''
        current.text += chunk
        current.progressLog += progress
        if (progress || chunk) current.message = (progress || chunk).trim().slice(-180) || current.message
    }
    while (true) {
        const { value, done } = await reader.read()
        if (done) break
        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''
        lines.forEach(consume)
    }
    buffer += decoder.decode()
    if (buffer) consume(buffer)
    if (failed) throw new Error(failed)
    for (const match of current.text.matchAll(/!\[[^\]]*\]\(([^)]+)\)/g)) {
        const url = mediaURL(match[1])
        if (url) current.outputs.push({ kind: 'image', url, sourceURL: url })
    }
    for (const match of current.text.matchAll(/<video[^>]+src=['"]([^'"]+)['"]/gi)) {
        const url = mediaURL(match[1])
        if (url) current.outputs.push({ kind: 'video', url, sourceURL: url })
    }
    if (!current.text.trim() && !current.outputs.length) throw new Error('响应已结束，服务端未返回作品内容。')
    current.status = 'completed'
    current.message = current.outputs.length
        ? '作品已生成，打开预览或下载保存吧。'
        : '已收到服务端文本响应，详细内容见请求记录。'
    if (pageActive && current.outputs.length) void cacheFlowImages(current)
}
function mediaURL(value) {
    try {
        const url = new URL(value, location.origin)
        return ['http:', 'https:', 'blob:'].includes(url.protocol) || /^data:(image|video|audio)\//.test(value)
            ? url.href
            : ''
    } catch {
        return ''
    }
}
function contentPath(value, receipt) {
    const url = new URL(value, location.origin),
        prefix = '/api/admin/vids/v1/vids/generations/'
    if (
        url.origin !== location.origin ||
        url.pathname !== `${prefix}${receipt}/content` ||
        url.hash ||
        [...url.searchParams.keys()].some((key) => key !== 'index') ||
        url.searchParams.getAll('index').length > 1 ||
        !/^[0-3]$/.test(url.searchParams.get('index') || '0')
    )
        throw new Error('成品地址与当前收据不匹配。')
    return url.pathname + url.search
}
function applyReceipt(current, data) {
    if (!/^vids_[a-f0-9]{64}$/.test(data.id || '') || (current.receipt && current.receipt !== data.id))
        throw new Error('服务器返回收据与原请求不匹配。')
    if (data.kind && data.kind !== current.kind) throw new Error('服务器返回的媒体类型与原请求不匹配。')
    const status = data.vids_status || data.status
    if (!Object.keys(taskStatusNames).includes(status)) throw new Error('服务器返回了未知任务状态。')
    if (
        data.outputs !== undefined &&
        (!Array.isArray(data.outputs) ||
            data.outputs.length > 4 ||
            data.outputs.some((item, index) => !item || item.index !== index || typeof item.content_url !== 'string'))
    )
        throw new Error('服务器返回的成品列表格式异常。')
    const previous = current.outputs || []
    const outputs = (
        data.outputs?.length ? data.outputs : data.content_url ? [{ content_url: data.content_url }] : []
    ).map((item, index) => {
        const path = contentPath(item.content_url, data.id)
        if (Number(new URL(path, origin).searchParams.get('index') || 0) !== index)
            throw new Error('成品索引与当前收据顺序不匹配。')
        return {
            ...previous.find((output) => output.path === path),
            path,
            sourceURL: path,
            kind: current.kind === 'speech' ? 'audio' : current.kind,
        }
    })
    current.receipt = data.id
    current.status = status
    current.outputs = outputs
    current.message = data.error
        ? `服务端状态：${status} · ${typeof data.error === 'string' ? data.error : data.error.code || ''}`
        : status === 'completed'
          ? '作品生成完成，正在读取成品。'
          : status === 'unknown'
            ? '原请求接受状态等待确认，请保留收据并查询。'
            : taskStatusNames[status]
}
const blobURLs = new Set()
const archiveRequests = new WeakMap()
function needsFlowArchive(output, entry) {
    if (entry?.provider !== 'flow' || entry.status !== 'completed') return false
    try {
        const url = new URL(output.sourceURL || output.url, origin)
        return ['http:', 'https:'].includes(url.protocol) && (url.origin !== origin || url.pathname.startsWith('/tmp/'))
    } catch {
        return false
    }
}
async function archiveFlowOutput(output, entry) {
    if (!needsFlowArchive(output, entry)) return
    if (archiveRequests.has(output)) return archiveRequests.get(output)
    const pending = (async () => {
        output.archiving = true
        const original = output.sourceURL || output.url
        try {
            const data = await api.post('/api/admin/studio/media/archive', { url: original })
            const url = new URL(data.url, origin)
            if (
                url.origin !== origin ||
                !/^\/api\/admin\/studio\/media\/[a-f0-9]{64}(?:\.(?:jpg|jpeg|png|webp|gif|avif|bmp|mp4|webm|mov|m4v|mkv))?\/content$/.test(
                    url.pathname,
                ) ||
                url.search ||
                url.hash
            )
                throw new Error('作品存档地址格式异常，请重新读取。')
            if (removedEntries.has(entry)) return
            output.originalURL ||= original
            output.sourceURL = url.pathname
            output.url = url.pathname
            output.previewError = ''
            output.archiveError = ''
            persistHistory()
        } catch (cause) {
            output.archiveError = cause.message || '原成品恢复未完成，请稍后重新读取。'
        } finally {
            output.archiving = false
        }
    })()
    archiveRequests.set(output, pending)
    try {
        await pending
    } finally {
        archiveRequests.delete(output)
    }
}
const removedEntries = new WeakSet()
function revokeOutputURL(output) {
    if (output?.url?.startsWith?.('blob:')) {
        URL.revokeObjectURL(output.url)
        blobURLs.delete(output.url)
        output.url = ''
    }
}
async function deleteEntryMedia(entry) {
    removedEntries.add(entry)
    ;(entry?.outputs || []).forEach(revokeOutputURL)
    return deleteStudioMedia(mediaNamespace, entry)
}
async function mediaBlob(output, current = task.value, index = 0) {
    if (!current || !output) return null
    const cached = await getStudioMedia(mediaNamespace, current, index)
    if (cached?.blob instanceof Blob) return cached.blob
    await archiveFlowOutput(output, current)
    if (output.url?.startsWith?.('blob:')) {
        const response = await fetch(output.url)
        return response.blob()
    }
    if (output.url?.startsWith?.('data:')) {
        const response = await fetch(output.url)
        return response.blob()
    }
    const path = output.path || output.sourceURL || output.url
    if (!path) return null
    const url = new URL(path, origin)
    const response =
        current.provider === 'vids' || url.origin === origin
            ? await api.raw(current.provider === 'vids' ? contentPath(path, current.receipt) : path, {
                  redirect: 'error',
              })
            : await fetch(url.href, { credentials: 'omit', redirect: 'error' })
    if (!response.ok) await readJSON(response)
    const blob = await response.blob()
    if (!blob.size) throw new Error('成品响应为空，请稍后重新读取')
    if (!removedEntries.has(current)) await putStudioMedia(mediaNamespace, current, index, blob, { kind: output.kind })
    return blob
}
async function cacheFlowImages(current) {
    if (current.provider !== 'flow') return
    for (const [index, output] of current.outputs.entries()) {
        if (!pageActive || !savedMediaURL(output.sourceURL || output.url)) continue
        await archiveFlowOutput(output, current)
        if (output.kind !== 'image') continue
        if (new URL(output.sourceURL || output.url, origin).origin !== origin) continue
        try {
            await mediaBlob(output, current, index)
        } catch {
            /* The stable result remains viewable if browser cache is full. */
        }
    }
}
async function preview(current, force = false) {
    if (current.status !== 'completed' || !pageActive) return
    for (const [index, output] of current.outputs.entries()) {
        if (output.url && !force) continue
        output.loading = true
        output.previewError = ''
        let blob
        try {
            blob = await mediaBlob(output, current, index)
        } catch (cause) {
            output.previewError =
                output.archiveError ||
                (cause.status === 404 ? '临时成品已过期，正在从原生成记录恢复；请稍后重新读取。' : cause.message) ||
                '成品读取失败'
            output.loading = false
            throw cause
        }
        if (!pageActive || removedEntries.has(current)) {
            output.loading = false
            return
        }
        revokeOutputURL(output)
        output.mime = blob.type
        output.url = URL.createObjectURL(blob)
        blobURLs.add(output.url)
        output.loading = false
    }
    current.message = '作品已生成，打开预览或下载保存吧。'
}
async function inspect(current = task.value, recover = false) {
    if (!/^vids_[a-f0-9]{64}$/.test(current?.receipt || '') || busy.value) return
    if (recover && !['received', 'download_failed'].includes(current.status)) {
        error.value = '当前原收据状态请先查询；下载恢复仅处理已接受结果。'
        return
    }
    busy.value = true
    error.value = ''
    try {
        const path = `/api/admin/vids/v1/vids/generations/${current.receipt}${recover ? '/download' : ''}`
        const response = await api.raw(path, { method: recover ? 'POST' : 'GET', redirect: 'error' })
        applyReceipt(current, await readJSON(response))
        if (current.status === 'completed') await preview(current)
        task.value = current
    } catch (cause) {
        error.value = cause.message
    } finally {
        busy.value = false
        persistHistory()
    }
}
async function recoverReceipt() {
    if (!/^vids_[a-f0-9]{64}$/.test(receiptInput.value.trim())) {
        error.value = '请填写 vids_ 开头、64 位十六进制摘要的完整收据 ID。'
        return
    }
    const existing = history.value.find((item) => item.receipt === receiptInput.value.trim())
    if (existing) {
        await inspect(existing)
        return
    }
    busy.value = true
    try {
        const requestedReceipt = receiptInput.value.trim()
        const response = await api.raw(`/api/admin/vids/v1/vids/generations/${requestedReceipt}`, {
                redirect: 'error',
            }),
            data = await readJSON(response)
        if (!['image', 'video', 'speech'].includes(data.kind)) throw new Error('返回收据缺少有效媒体类型。')
        const current = reactive({
            provider: 'vids',
            kind: data.kind,
            started: Date.now(),
            title: '从收据恢复的作品',
            key: '',
            receipt: requestedReceipt,
            status: 'unknown',
            outputs: [],
            text: '',
        })
        applyReceipt(current, data)
        if (current.status === 'completed') await preview(current)
        history.value.unshift(current)
        task.value = current
        receiptInput.value = ''
        persistHistory()
    } catch (cause) {
        error.value = cause.message
    } finally {
        busy.value = false
    }
}
async function readContent() {
    if (!task.value || busy.value) return
    busy.value = true
    try {
        await preview(task.value, true)
    } catch (cause) {
        error.value = cause.message
    } finally {
        busy.value = false
    }
}
async function download(output, index, current = task.value) {
    try {
        let url = output.url
        if (!url) {
            await preview(current)
            url = output.url
        }
        if (!url) throw new Error('请先读取成品预览。')
        if (!url.startsWith('blob:') && !url.startsWith('data:')) {
            const response = await fetch(url, {
                credentials: new URL(url, location.origin).origin === location.origin ? 'include' : 'omit',
            })
            if (!response.ok) throw new Error(`下载响应 HTTP ${response.status}`)
            const blob = await response.blob()
            output.mime = blob.type
            url = URL.createObjectURL(blob)
            blobURLs.add(url)
        }
        const extension =
            output.kind === 'video'
                ? 'mp4'
                : output.kind === 'audio'
                  ? 'wav'
                  : output.mime === 'image/png' || /\.png(?:\?|$)/i.test(output.url || '')
                    ? 'png'
                    : output.mime === 'image/webp' || /\.webp(?:\?|$)/i.test(output.url || '')
                      ? 'webp'
                      : 'jpg'
        const anchor = document.createElement('a')
        anchor.href = url
        anchor.download = `lay4flow-${current?.receipt || current?.started}-${index + 1}.${extension}`
        anchor.click()
    } catch (cause) {
        notifyError(cause)
    }
}
async function copy(value) {
    try {
        await navigator.clipboard.writeText(value)
        ElMessage.success('已复制')
    } catch (cause) {
        notifyError(cause)
    }
}
function showDetails(entry = task.value) {
    task.value = entry
    details.value = true
}
async function readEntry(entry) {
    task.value = entry
    await readContent()
}
const hydrationQueue = []
const hydratedEntries = new Set()
const receiptRestorations = new Set()
let hydrationWorkers = 0
function hydrateEntryMedia(entry) {
    if (!entry) return
    if (entry.provider === 'vids' && (!entry.outputs?.length || entry.status !== 'completed'))
        queueReceiptRestore(entry)
    if (hydratedEntries.has(entry)) return
    hydratedEntries.add(entry)
    hydrationQueue.push(entry)
    runHydrationQueue()
}
const receiptRestoreQueue = []
let receiptRestoreWorkers = 0
function restoreVidsReceipts() {
    history.value
        .filter((entry) => entry.provider === 'vids' && entry.status === 'completed' && !entry.outputs?.length)
        .forEach(queueReceiptRestore)
    history.value
        .filter(
            (entry) => entry.provider === 'vids' && ['unknown', 'received', 'download_failed'].includes(entry.status),
        )
        .slice(0, 12)
        .forEach(queueReceiptRestore)
}
function queueReceiptRestore(entry) {
    if (
        !pageActive ||
        receiptRestorations.has(entry) ||
        entry?.provider !== 'vids' ||
        !/^vids_[a-f0-9]{64}$/.test(entry.receipt || '') ||
        ![
            'completed',
            'unknown',
            'received',
            'download_failed',
            'preparing',
            'generating',
            'queued',
            'in_progress',
        ].includes(entry.status) ||
        (entry.status === 'completed' && entry.outputs?.length)
    )
        return
    receiptRestorations.add(entry)
    receiptRestoreQueue.push(entry)
    runReceiptRestoreQueue()
}
function runReceiptRestoreQueue() {
    while (pageActive && receiptRestoreWorkers < 2 && receiptRestoreQueue.length) {
        const entry = receiptRestoreQueue.shift()
        receiptRestoreWorkers++
        restoreOriginalReceipt(entry).finally(() => {
            receiptRestoreWorkers--
            runReceiptRestoreQueue()
        })
    }
}
async function restoreOriginalReceipt(entry) {
    try {
        const response = await api.raw(`/api/admin/vids/v1/vids/generations/${entry.receipt}`, { redirect: 'error' })
        const data = await readJSON(response)
        const target = pageActive ? entry : { ...entry, outputs: [...(entry.outputs || [])] }
        applyReceipt(target, data)
        if (target.status === 'completed') {
            for (const output of target.outputs) {
                if (!output.url) output.url = contentPath(output.path, entry.receipt)
            }
            target.message = '原成品已自动恢复，切换页面和刷新后仍可继续查看。'
        }
        if (!pageActive) {
            const index = history.value.indexOf(entry)
            if (index >= 0) history.value.splice(index, 1, target)
        }
        persistHistory()
    } catch (cause) {
        if (pageActive) entry.message = `原收据自动查询未完成：${cause.message}；可点击查询原任务重试。`
    }
}
function runHydrationQueue() {
    while (pageActive && hydrationWorkers < 2 && hydrationQueue.length) {
        const entry = hydrationQueue.shift()
        hydrationWorkers++
        hydrateCachedEntry(entry).finally(() => {
            hydrationWorkers--
            runHydrationQueue()
        })
    }
}
async function hydrateCachedEntry(entry) {
    if (!entry || entry.status !== 'completed') return
    for (const [index, output] of (entry.outputs || []).entries()) {
        if (output.url?.startsWith?.('blob:')) continue
        const cached = await getStudioMedia(mediaNamespace, entry, index)
        if (!(cached?.blob instanceof Blob) || !pageActive) {
            if (pageActive) await archiveFlowOutput(output, entry)
            continue
        }
        const previous = output.url
        output.url = URL.createObjectURL(cached.blob)
        output.mime = cached.mime || cached.blob.type
        blobURLs.add(output.url)
        if (previous?.startsWith?.('blob:')) {
            URL.revokeObjectURL(previous)
            blobURLs.delete(previous)
        }
    }
}
async function retryPreview({ entry }) {
    task.value = entry
    try {
        await preview(entry, true)
    } catch (cause) {
        for (const output of entry.outputs || [])
            if (output.previewError) output.previewError = output.archiveError || cause.message
    }
}
async function removeEntry(entry) {
    if (!['completed', 'failed', 'no_result', 'specification_mismatch'].includes(entry?.status)) {
        ElMessage.info('原任务仍待确认，先查询收据后再清理记录')
        return
    }
    const index = history.value.indexOf(entry)
    if (index < 0) return
    history.value.splice(index, 1)
    if (task.value === entry) task.value = history.value[0] || null
    persistHistory()
    await deleteEntryMedia(entry)
}
async function removeMany(entries = []) {
    const removable = entries.filter((entry) =>
        ['completed', 'failed', 'no_result', 'specification_mismatch'].includes(entry.status),
    )
    if (!removable.length) return
    if (removable.some((entry) => entry.status === 'completed')) {
        try {
            await ElMessageBox.confirm(
                `删除所选 ${removable.length} 条本机创作记录及缓存？已下载文件与服务器原任务会保留。`,
                '清理创作记录',
                { type: 'warning', confirmButtonText: '删除记录', cancelButtonText: '取消' },
            )
        } catch {
            return
        }
    }
    for (const entry of removable) await removeEntry(entry)
}
async function downloadMany(entries = []) {
    for (const entry of entries) {
        for (const [index, output] of (entry.outputs || []).entries()) await download(output, index, entry)
    }
}
async function reuseEntry(entry) {
    if (busy.value) return
    provider.value = entry.provider === 'vids' ? 'vids' : 'flow'
    kind.value = entry.kind || 'image'
    await nextTick()
    if (provider.value === 'vids' && !models.value.some((item) => item.id === entry.model)) await loadCatalog()
    selectedId.value = entry.model || ''
    await nextTick()
    selectModel()
    prompt.value = entry.description || entry.title || ''
    for (const [name, value] of Object.entries(entry.settings || {})) if (name in params) params[name] = value
    await nextTick()
    window.scrollTo?.({ top: 0, behavior: 'smooth' })
    ElMessage.success('已带入原提示词与生成参数，可直接修改后重新创作')
}
async function recoverEntry(entry) {
    await inspect(entry, true)
}
const taskDetails = computed(() =>
    task.value
        ? JSON.stringify(
              {
                  provider: task.value.provider,
                  model: task.value.model,
                  status: task.value.status,
                  receipt: task.value.receipt,
                  idempotency_key: task.value.key,
                  request: task.value.body,
                  progress: task.value.progressLog,
                  response: task.value.text,
                  message: task.value.message,
              },
              null,
              2,
          )
        : '当前还没有创作请求。',
)
function restoreHistory() {
    try {
        const saved = readStudioHistory(localStorage, sessionStorage, mediaNamespace)
        history.value = []
        if (!Array.isArray(saved)) return
        for (const entry of selectStudioHistory(saved)) {
            if (
                !entry ||
                !['flow', 'vids'].includes(entry.provider) ||
                !['image', 'video', 'speech'].includes(entry.kind) ||
                !Object.hasOwn(taskStatusNames, entry.status) ||
                !Number.isFinite(entry.started) ||
                !Array.isArray(entry.outputs) ||
                (entry.provider === 'vids' && entry.outputs.length > 4) ||
                (entry.receipt && !/^vids_[a-f0-9]{64}$/.test(entry.receipt))
            )
                continue
            const outputs = entry.outputs.flatMap((output) => {
                const outputKind = entry.kind === 'speech' ? 'audio' : entry.kind
                if (entry.provider === 'flow')
                    return savedMediaURL(output?.url)
                        ? [
                              {
                                  url: savedMediaURL(output.url),
                                  sourceURL: savedMediaURL(output.url),
                                  originalURL: savedMediaURL(output.originalURL),
                                  kind: outputKind,
                              },
                          ]
                        : []
                try {
                    const path = contentPath(output?.path, entry.receipt)
                    return [{ path, sourceURL: path, url: path, kind: outputKind }]
                } catch {
                    return []
                }
            })
            const interrupted = entry.provider === 'flow' && ['preparing', 'generating'].includes(entry.status)
            history.value.push({
                provider: entry.provider,
                kind: entry.kind,
                model: String(entry.model || '').slice(0, 128),
                started: entry.started,
                ended: Number.isFinite(entry.ended) ? entry.ended : undefined,
                key: String(entry.key || '').slice(0, 128),
                receipt: entry.receipt || '',
                signature: String(entry.signature || '').slice(0, 128),
                text: '',
                progressLog: '',
                title: String(entry.title || '').slice(0, 100),
                description: String(entry.description || '').slice(0, 32000),
                settings: savedSettings(entry.settings),
                message: interrupted
                    ? '页面曾在生成时离开，请先查看请求日志确认结果。'
                    : String(entry.message || '').slice(0, 1000),
                status: interrupted ? 'unknown' : entry.status,
                outputs,
            })
        }
        persistHistory()
        historyStorageMessage.value =
            history.value.length >= 300 ? '本机保留最近 300 条请求及所有待确认 Vids 收据；更早作品请及时下载保存。' : ''
    } catch {
        history.value = []
        historyStorageMessage.value = '本机创作记录读取异常，请检查本地存储设置；已有服务端收据仍可手动恢复。'
    }
}
let interval
onMounted(() => {
    document.addEventListener('paste', pasteImages)
    document.addEventListener('keydown', studioShortcuts)
    localStorage.removeItem('studio_api_key')
    restoreHistory()
    restoreVidsReceipts()
    selectedId.value = models.value[0]?.id || ''
    selectModel()
    loadCatalog()
    interval = setInterval(() => {
        clock.value = Date.now()
    }, 1000)
})
onUnmounted(() => {
    document.removeEventListener('paste', pasteImages)
    document.removeEventListener('keydown', studioShortcuts)
    pageActive = false
    clearInterval(interval)
    blobURLs.forEach((url) => URL.revokeObjectURL(url))
    blobURLs.clear()
})
</script>

<template>
    <div class="studio-view">
        <header class="page-heading">
            <div>
                <h1>把脑海里的世界，画出来。</h1>
                <p>图片、动态与声音，在同一个工作台慢慢成形。</p>
            </div>
            <div class="page-actions">
                <el-button :icon="Refresh" :loading="loading" @click="loadCatalog">同步能力</el-button>
            </div>
        </header>
        <div class="studio-nav surface">
            <el-segmented
                v-model="provider"
                :options="[
                    { label: 'Flow 创作', value: 'flow' },
                    { label: 'Vids 媒体', value: 'vids' },
                ]"
                :disabled="busy || materialBusy || kind === 'speech'"
            />
            <div class="studio-kind-tabs" role="tablist" aria-label="创作类型" @keydown="navigateKind">
                <button
                    v-for="item in kinds"
                    :id="`studio-tab-${item.value}`"
                    :key="item.value"
                    :data-kind="item.value"
                    role="tab"
                    :aria-controls="`studio-panel-${item.value}`"
                    :aria-selected="kind === item.value"
                    :tabindex="kind === item.value ? 0 : -1"
                    :class="{ active: kind === item.value }"
                    :disabled="busy || materialBusy"
                    @click="kind = item.value"
                >
                    <el-icon><component :is="item.icon" /></el-icon>
                    {{ item.label }}
                </button>
            </div>
            <el-tag :type="captchaRequired ? 'warning' : available ? 'success' : 'info'" effect="light" round>
                <span class="status-dot" />
                {{
                    captchaRequired
                        ? '请先填写打码密钥'
                        : available
                          ? provider === 'flow'
                              ? '已连接工作台'
                              : `${readyCount} 个账号就绪`
                          : provider === 'flow'
                            ? '等待同步模型'
                            : '等待接入账号'
                }}
            </el-tag>
        </div>

        <section
            v-if="kind === 'document'"
            id="studio-panel-document"
            role="tabpanel"
            aria-labelledby="studio-tab-document"
            class="document-workspace surface"
        >
            <div class="document-illustration">
                <el-icon><Document /></el-icon>
                <span>Vids / MATERIAL LIBRARY</span>
            </div>
            <div>
                <h2>把创作放进你的 Vids 文档</h2>
                <p>
                    现有文档接口支持读取已保存场景、图片插入与核验。这里为你保留完整文档入口，查看场景
                    ID、素材规则与历史操作说明。
                </p>
                <el-alert
                    title="文档操作属于既有兼容与诊断能力；当前创作主流程使用本次上传的素材。"
                    type="info"
                    :closable="false"
                    show-icon
                />
                <div class="document-actions">
                    <el-button type="primary" :icon="Document" @click="documentDrawer = true">
                        打开完整素材说明
                    </el-button>
                    <el-button tag="a" href="/legacy/vids" target="_blank" :icon="ArrowRight">
                        完整 Vids 控制台
                    </el-button>
                </div>
            </div>
        </section>
        <div
            v-else
            :id="`studio-panel-${kind}`"
            role="tabpanel"
            :aria-labelledby="`studio-tab-${kind}`"
            class="studio-grid"
        >
            <section class="studio-editor surface" aria-label="创作编辑器">
                <div class="section-heading">
                    <div>
                        <h2>{{ kind === 'speech' ? '让文字拥有声音' : '今天，想创造什么？' }}</h2>
                    </div>
                    <el-tag effect="plain">{{ provider === 'flow' ? 'FLOW' : 'VIDS' }}</el-tag>
                </div>
                <el-form label-position="top" :disabled="busy || materialBusy">
                    <el-form-item label="生成模型" class="model-field">
                        <el-select v-model="selectedId" filterable placeholder="选择模型" :loading="loading">
                            <el-option
                                v-for="entry in models"
                                :key="entry.id"
                                :label="entry.displayName || entry.description || entry.id"
                                :value="entry.id"
                            >
                                <span>{{ entry.displayName || entry.description || entry.id }}</span>
                                <small class="model-option-id">{{ entry.id }}</small>
                            </el-option>
                            <el-option
                                v-if="!models.length && provider === 'vids'"
                                :label="`Vids ${kind === 'image' ? 'Image' : kind === 'video' ? 'Video' : 'Speech'}`"
                                :value="`vids-${kind}`"
                            />
                        </el-select>
                        <div class="field-hint">
                            {{ catalogMessage }}
                            <el-tooltip content="目录来自服务端能力声明；未连接时显示仓库自带公开模型。">
                                <el-icon><InfoFilled /></el-icon>
                            </el-tooltip>
                        </div>
                    </el-form-item>
                    <el-form-item :label="kind === 'speech' ? '朗读文本' : '画面描述'">
                        <el-input
                            v-model="prompt"
                            class="studio-prompt-input"
                            @dragover.prevent
                            @drop="dropPrompt"
                            type="textarea"
                            :autosize="{ minRows: 3, maxRows: 8 }"
                            :maxlength="kind === 'speech' ? 4096 : 32000"
                            show-word-limit
                            :placeholder="
                                kind === 'speech'
                                    ? '写下你想听见的文字…当前 Vids 语音请使用英文等受支持语言。'
                                    : '描述主角、场景、光线和画风。越具体，越接近你的想象…'
                            "
                        />
                    </el-form-item>
                    <div class="prompt-suggestions">
                        <span>灵感小签</span>
                        <el-button
                            v-for="example in examples"
                            :key="example.label"
                            size="small"
                            round
                            @click="prompt = example.text"
                        >
                            {{ example.label }}
                        </el-button>
                    </div>
                    <StudioReferenceBoard
                        v-if="kind !== 'speech'"
                        :materials="materials"
                        :maximum="modeLimits.maxImages || 1"
                        :disabled="busy || materialBusy"
                        :busy="materialBusy"
                        :first-last="params.mode === 'first_last_frame'"
                        :provider="provider"
                        @files="addReferenceFiles"
                        @clipboard="readClipboardImages"
                        @remove="materials.splice($event, 1)"
                        @clear="materials = []"
                        @edit="editReference"
                        @move="moveReference"
                        @media="useDraggedMedia"
                    />
                    <el-alert
                        v-if="error || validation"
                        :title="error || validation"
                        type="warning"
                        :closable="false"
                        show-icon
                        class="studio-error"
                    />
                    <div class="generate-area">
                        <p v-if="captchaRequired" class="studio-captcha-setup" role="alert">
                            Flow 创作需要打码密钥，
                            <el-button link type="primary" @click="configureCaptcha">现在填写</el-button>
                        </p>
                        <el-button
                            type="primary"
                            size="large"
                            :icon="MagicStick"
                            :loading="busy || materialBusy"
                            :disabled="!prompt.trim() || !available || !!validation || captchaRequired"
                            @click="generate"
                        >
                            {{
                                busy
                                    ? '正在把灵感变成作品…'
                                    : materialBusy
                                      ? '正在读取素材…'
                                      : kind === 'image'
                                        ? '开始绘梦'
                                        : kind === 'video'
                                          ? '开始生成映像'
                                          : '生成我的声线'
                            }}
                        </el-button>
                        <span>
                            {{ provider === 'vids' ? '使用对应媒体额度生成' : '已登录，直接开始创作' }} · Ctrl + Enter
                        </span>
                    </div>
                    <div v-if="!available" class="connection-help">
                        <el-icon><Connection /></el-icon>
                        <span>
                            {{ provider === 'flow' ? '请重新同步模型能力。' : '先接入 Vids 账号，再开始创作。' }}
                        </span>
                        <el-button v-if="provider === 'flow'" link type="primary" @click="loadCatalog">
                            重新同步
                        </el-button>
                        <el-button v-else link type="primary" @click="router.push('/accounts?platform=vids')">
                            账号接入
                        </el-button>
                    </div>

                    <el-collapse v-model="parameterSections" class="studio-settings">
                        <el-collapse-item name="settings">
                            <template #title>
                                <div class="settings-toggle">
                                    <span>{{ kind === 'speech' ? '声线设置' : '画面设定' }}</span>
                                    <small>{{ specificationSummary }}</small>
                                </div>
                            </template>
                            <div class="parameter-grid">
                                <el-form-item v-if="kind !== 'speech'" label="创作方式">
                                    <el-select v-model="params.mode">
                                        <el-option
                                            v-for="mode in modes"
                                            :key="mode"
                                            :value="mode"
                                            :label="modeNames[mode] || mode"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item
                                    v-if="
                                        kind !== 'speech' &&
                                        !(imageEditing && provider === 'vids') &&
                                        params.mode !== 'upscale'
                                    "
                                    label="画面比例"
                                >
                                    <el-select v-model="params.aspectRatio">
                                        <el-option
                                            v-for="aspect in aspects"
                                            :key="aspect"
                                            :label="`${aspect} · ${aspect === '16:9' ? '横向' : aspect === '9:16' ? '竖向' : aspect === '1:1' ? '方形' : '画幅'}`"
                                            :value="aspect"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item v-if="kind === 'image' && provider === 'flow'" label="输出精度">
                                    <el-select v-model="params.imageSize">
                                        <el-option
                                            v-for="size in capabilities.imageSizes"
                                            :key="size"
                                            :value="size"
                                            :label="size === '1k' ? '1K · 原生' : size.toUpperCase()"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item
                                    v-if="kind === 'image' && provider === 'vids' && !imageEditing"
                                    label="画面风格"
                                >
                                    <el-select v-model="params.style">
                                        <el-option
                                            v-for="style in capabilities.styles"
                                            :key="style"
                                            :value="style"
                                            :label="styleNames[style] || style"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item
                                    v-if="kind === 'image' && provider === 'vids' && !imageEditing"
                                    label="生成数量"
                                >
                                    <el-input-number
                                        v-model="params.count"
                                        :min="1"
                                        :max="Math.max(...(capabilities.counts || [1]))"
                                        controls-position="right"
                                    />
                                </el-form-item>
                                <el-form-item v-if="kind === 'video' && params.mode !== 'upscale'" label="清晰度">
                                    <el-select v-model="params.resolution">
                                        <el-option
                                            v-for="resolution in resolutions"
                                            :key="resolution"
                                            :value="resolution"
                                            :label="resolution.toUpperCase()"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item
                                    v-if="
                                        kind === 'video' &&
                                        params.mode !== 'upscale' &&
                                        !(provider === 'flow' && params.mode === 'edit')
                                    "
                                    :label="params.mode === 'extend' ? '追加时长' : '输出时长'"
                                >
                                    <el-select v-model="params.durationSeconds">
                                        <el-option
                                            v-for="seconds in durations"
                                            :key="seconds"
                                            :value="seconds"
                                            :label="`${seconds} 秒`"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item
                                    v-if="kind === 'video' && provider === 'flow' && qualities.length"
                                    label="生成档位"
                                >
                                    <el-select v-model="params.quality">
                                        <el-option
                                            v-for="quality in qualities"
                                            :key="quality"
                                            :value="quality"
                                            :label="qualityNames[quality] || quality"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item
                                    v-if="kind === 'video' && provider === 'flow' && capabilities.priorities?.length"
                                    label="执行模式"
                                >
                                    <el-select v-model="params.priority">
                                        <el-option
                                            v-for="priority in priorities"
                                            :key="priority"
                                            :value="priority"
                                            :label="priority === 'relaxed' ? 'Relaxed · Ultra 账号' : '标准'"
                                        />
                                    </el-select>
                                </el-form-item>
                                <el-form-item
                                    v-if="
                                        kind === 'video' &&
                                        provider === 'vids' &&
                                        ['text', 'ingredients', 'edit'].includes(params.mode)
                                    "
                                    label="镜头语言"
                                >
                                    <el-select v-model="params.shotMode">
                                        <el-option label="自动" value="auto" />
                                        <el-option label="长镜头" value="long_take" />
                                        <el-option label="蒙太奇" value="montage" />
                                    </el-select>
                                </el-form-item>
                                <el-form-item v-if="kind === 'speech'" label="选择声线" class="span-two">
                                    <el-select v-model="params.voice" filterable>
                                        <el-option
                                            v-for="voice in voiceOptions"
                                            :key="voice.id"
                                            :label="`${voice.name}${voice.description ? ` · ${voice.description}` : ''}`"
                                            :value="voice.id"
                                        />
                                    </el-select>
                                    <p class="field-hint">
                                        {{ selectedVoice?.description || '30 个官方预设 · WAV 音频' }}
                                        <el-tag
                                            v-if="capabilities.verified_voices?.includes(params.voice)"
                                            size="small"
                                            type="success"
                                        >
                                            短英文已验收
                                        </el-tag>
                                    </p>
                                </el-form-item>
                            </div>
                            <el-alert
                                v-if="imageEditing && provider === 'vids'"
                                title="编辑成品由上游返回实际尺寸；构图与风格请直接写进描述。"
                                type="info"
                                :closable="false"
                                show-icon
                            />
                            <el-alert
                                v-if="params.mode === 'upscale'"
                                title="保留原视频时长与方向，放大至 1080p。上传 3–10 秒 MP4。"
                                type="info"
                                :closable="false"
                                show-icon
                            />
                            <el-form-item
                                v-if="provider === 'flow' && modeLimits.mediaId && !modeLimits.videoInput"
                                label="源视频媒体 ID"
                            >
                                <el-input v-model="params.mediaId" placeholder="填写 Flow 生成返回的 media ID" />
                            </el-form-item>
                            <div v-if="provider === 'flow' && params.mode === 'edit'" class="parameter-grid">
                                <el-form-item label="片段起点（毫秒）">
                                    <el-input-number
                                        v-model="params.trimStartMs"
                                        :min="0"
                                        :step="1000"
                                        controls-position="right"
                                    />
                                </el-form-item>
                                <el-form-item label="片段终点（毫秒）">
                                    <el-input-number
                                        v-model="params.trimEndMs"
                                        :min="1"
                                        :step="1000"
                                        controls-position="right"
                                    />
                                </el-form-item>
                            </div>
                        </el-collapse-item>
                        <el-collapse-item v-if="modeLimits.videoInput" name="materials">
                            <template #title>
                                <div class="settings-toggle">
                                    <span>参考图片与素材</span>
                                    <small>
                                        {{
                                            sourceVideo
                                                ? '已添加原视频'
                                                : materials.length
                                                  ? `已添加 ${materials.length} 张图片`
                                                  : modeLimits.videoInput
                                                    ? '上传原视频'
                                                    : modeLimits.minImages
                                                      ? `至少 ${modeLimits.minImages} 张图片`
                                                      : '可选 · 上传图片或拖入素材'
                                        }}
                                    </small>
                                </div>
                            </template>
                            <div v-if="modeLimits.videoInput" class="material-section">
                                <div class="section-heading small">
                                    <h3>原视频</h3>
                                    <span class="muted">
                                        {{ provider === 'vids' ? 'MP4 · 50 MiB' : 'MP4 / WebM / MOV · 100 MiB' }}
                                    </span>
                                </div>
                                <el-upload
                                    v-if="!sourceVideo"
                                    drag
                                    :auto-upload="false"
                                    :show-file-list="false"
                                    :limit="1"
                                    :accept="provider === 'vids' ? 'video/mp4' : 'video/mp4,video/webm,video/quicktime'"
                                    :on-change="addVideo"
                                    :disabled="busy || materialBusy"
                                >
                                    <el-icon class="upload-icon"><VideoCamera /></el-icon>
                                    <div>
                                        拖入原视频，或
                                        <strong>点击选择</strong>
                                    </div>
                                    <small>素材随本次请求提交</small>
                                </el-upload>
                                <div v-else class="source-video">
                                    <video
                                        :src="sourceVideo.data"
                                        controls
                                        preload="metadata"
                                        @loadedmetadata="videoMetadata"
                                    />
                                    <div>
                                        <span>{{ sourceVideo.name }}</span>
                                        <el-button text type="danger" :icon="Delete" @click="sourceVideo = null">
                                            移除
                                        </el-button>
                                    </div>
                                </div>
                            </div>
                        </el-collapse-item>
                    </el-collapse>
                </el-form>
            </section>

            <section class="studio-canvas surface" aria-label="作品与任务状态" tabindex="0">
                <div class="section-heading">
                    <div>
                        <h2>我的作品</h2>
                        <p class="gallery-heading-copy">每一次灵感，都在这里接着生长。</p>
                    </div>
                    <el-button text :icon="View" @click="details = true">请求记录</el-button>
                </div>
                <div v-if="!history.length" class="canvas-empty">
                    <div class="canvas-companion"><Mascot pose="welcome" mood="happy" /></div>
                    <div class="canvas-welcome-copy">
                        <h3>下一幅喜欢的作品，从这里开始</h3>
                        <p>选一个模型，写下描述。生成的图片、视频与声音会出现在这里。</p>
                    </div>
                    <div class="empty-formats">
                        <span>
                            <el-icon><Picture /></el-icon>
                            图片预览
                        </span>
                        <span>
                            <el-icon><VideoCamera /></el-icon>
                            视频播放
                        </span>
                        <span>
                            <el-icon><Microphone /></el-icon>
                            音频试听
                        </span>
                    </div>
                </div>
                <template v-else>
                    <div v-if="task" class="task-status">
                        <el-tag
                            :type="
                                task.status === 'completed'
                                    ? 'success'
                                    : ['failed', 'unknown', 'specification_mismatch', 'no_result'].includes(task.status)
                                      ? 'warning'
                                      : 'primary'
                            "
                            round
                        >
                            {{ taskStatusNames[task.status] || task.status }}
                        </el-tag>
                        <span class="task-summary">{{ task.title }}</span>
                        <span>
                            <el-icon><Timer /></el-icon>
                            {{ elapsed }} 秒 · {{ task.provider === 'vids' ? 'Vids' : 'Flow' }}
                        </span>
                    </div>
                    <StudioGallery
                        class="studio-results"
                        :entries="history"
                        :selected="task"
                        :busy="busy"
                        :status-names="taskStatusNames"
                        @select="task = $event"
                        @details="showDetails"
                        @download="download"
                        @read="readEntry"
                        @inspect="inspect"
                        @recover="recoverEntry"
                        @copy="copy"
                        @edit-image="openImageEditor"
                        @reference-image="referenceResult($event)"
                        @animate-image="referenceResult($event, true)"
                        @remove="removeEntry"
                        @remove-many="removeMany"
                        @download-many="downloadMany"
                        @retry-preview="retryPreview"
                        @reuse="reuseEntry"
                        @hydrate="hydrateEntryMedia"
                    />
                </template>
                <el-alert
                    v-if="historyStorageMessage"
                    :title="historyStorageMessage"
                    type="warning"
                    show-icon
                    :closable="false"
                    class="history-storage-notice"
                />
                <div class="canvas-note">
                    <el-icon><Check /></el-icon>
                    原比例展示 · 点击图片看大图 ·
                    本机保存作品记录，切换页面与刷新后自动恢复；清理浏览器数据会移除本机缓存
                </div>
                <div class="studio-receipt-recovery">
                    <el-collapse>
                        <el-collapse-item title="恢复已有 Vids 收据" name="receipt">
                            <div class="receipt-recovery">
                                <el-input
                                    v-model="receiptInput"
                                    placeholder="粘贴 Vids 收据 ID，读取原任务与成品"
                                    :prefix-icon="Document"
                                    clearable
                                />
                                <el-button
                                    :icon="Refresh"
                                    :loading="busy"
                                    :disabled="!receiptInput.trim()"
                                    @click="recoverReceipt"
                                >
                                    恢复历史收据
                                </el-button>
                            </div>
                            <p class="history-note">查询原任务、恢复下载；生成请求不会重复提交。</p>
                        </el-collapse-item>
                    </el-collapse>
                </div>
            </section>
        </div>

        <StudioImageEditor
            v-model="imageEditorOpen"
            :source="imageEditorSource"
            :loading="editorLoading"
            @apply="applyImageEdit"
            @reload="editorRequest && openImageEditor(editorRequest)"
        />
        <el-drawer v-model="details" title="创作请求记录" size="min(650px, 100vw)">
            <el-alert
                title="原始素材已从记录中省略。Vids 收据与幂等键用于查询同一请求。"
                type="info"
                :closable="false"
                show-icon
            />
            <el-button :icon="CopyDocument" class="detail-copy" @click="copy(taskDetails)">复制记录</el-button>
            <pre class="studio-json">{{ taskDetails }}</pre>
        </el-drawer>
        <el-drawer v-model="documentDrawer" title="Vids 文档与素材接口" size="90%" class="legacy-doc-drawer">
            <iframe
                src="/legacy/vids-api-guide?embedded=1#console-document"
                title="Vids 完整文档与素材接口"
                class="legacy-doc-frame"
            />
        </el-drawer>
    </div>
</template>

<style scoped>
.studio-view {
    --studio-pink: var(--accent);
    --studio-blue: var(--blue);
}
@media (min-width: 1051px) {
    .studio-view {
        display: flex;
        flex: 1;
        flex-direction: column;
        min-height: 0;
    }
    .studio-view .page-heading,
    .studio-nav {
        flex-shrink: 0;
    }
    .studio-grid {
        flex: 1;
        min-height: 0;
        align-items: stretch;
    }
    .studio-grid > .studio-editor,
    .studio-grid > .studio-canvas {
        min-height: 0;
        height: 100%;
        overflow-y: auto;
        overscroll-behavior-y: contain;
        scrollbar-gutter: stable;
        scrollbar-width: thin;
    }
    .studio-grid > .studio-editor {
        position: static;
    }
    .studio-grid > .studio-canvas {
        display: block;
    }
    .studio-canvas > .section-heading {
        position: sticky;
        top: -24px;
        z-index: 2;
        padding: 12px 0;
        margin-top: -12px;
        background: var(--surface);
    }
    .studio-canvas .canvas-empty {
        min-height: min(500px, 58svh);
    }
    .studio-results {
        min-height: calc(100% - 180px);
    }
}
.history-storage-notice {
    margin: 16px 0;
}
.studio-captcha-setup {
    margin: 0 0 10px;
    color: var(--el-color-warning);
    font-size: 13px;
    line-height: 1.7;
}

.studio-view .page-heading h1 {
    font-size: clamp(24px, 2.5vw, 35px);
    letter-spacing: normal;
}
.studio-nav {
    display: flex;
    align-items: center;
    gap: 24px;
    padding: 15px 20px;
    margin-bottom: 22px;
}
.studio-nav > .el-tag {
    margin-left: auto;
}
.studio-kind-tabs {
    display: flex;
    gap: 4px;
    flex-wrap: wrap;
}
.studio-kind-tabs button {
    display: flex;
    align-items: center;
    gap: 7px;
    border: 0;
    background: transparent;
    padding: 11px 13px;
    border-radius: 10px;
    color: var(--muted);
    font: inherit;
    font-size: 12px;
    font-weight: 650;
    cursor: pointer;
    transition:
        background 0.2s,
        color 0.2s;
}
.studio-kind-tabs button.active {
    background: var(--pink-soft);
    color: var(--accent);
}
.studio-kind-tabs button:hover {
    background: var(--ground);
}
.studio-kind-tabs button:focus-visible {
    outline: 2px solid var(--accent);
    outline-offset: 2px;
}
.studio-kind-tabs button:disabled {
    opacity: 0.5;
    cursor: wait;
}
.studio-grid {
    display: grid;
    grid-template-columns: minmax(350px, 400px) minmax(0, 1fr);
    gap: 22px;
    align-items: start;
}
.studio-editor {
    padding: 23px;
    min-width: 0;
    position: sticky;
    top: 22px;
}
.studio-editor :deep(.el-form-item) {
    margin-bottom: 16px;
}
.studio-editor :deep(.el-form-item__label) {
    font-size: 12px;
    font-weight: 650;
    color: var(--ink);
    padding-bottom: 7px;
}
.studio-editor :deep(.el-select),
.parameter-grid :deep(.el-input-number) {
    width: 100%;
}
.studio-editor :deep(.el-textarea__inner) {
    padding: 15px;
    font-size: 14px;
    line-height: 1.8;
    border-radius: 11px;
    background: var(--surface);
}
.studio-editor :deep(.el-input__count) {
    bottom: 7px;
    right: 12px;
    font-size: 12px;
    background: transparent;
}
.studio-view .section-heading {
    margin-bottom: 24px;
    align-items: center;
}
.studio-view .section-heading h2 {
    font-size: 18px;
    letter-spacing: normal;
    margin: 0;
}
.studio-view .section-heading.small {
    margin: 20px 0 15px;
}
.studio-view .section-heading.small h3 {
    font-size: 13px;
    margin: 0;
}
.studio-view .section-heading.small .muted {
    font-size: 12px;
}
.model-option-id {
    float: right;
    color: var(--muted);
    margin-left: 16px;
    font-size: 12px;
}
.field-hint {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 12px;
    color: var(--muted);
    margin: 8px 0 0;
    line-height: 1.6;
}
.prompt-suggestions {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-wrap: wrap;
    margin-top: -4px;
}
.prompt-suggestions > span {
    font-size: 12px;
    color: var(--muted);
    margin-right: 3px;
}
.prompt-suggestions :deep(.el-button) {
    margin: 0;
    font-size: 12px;
    padding: 5px 9px;
    color: var(--muted);
    border-color: var(--line);
    background: var(--surface);
}

.parameter-grid {
    display: grid;
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 0 15px;
}
.span-two {
    grid-column: 1 / -1;
}
.material-section {
    margin: 14px 0 22px;
}
.material-section :deep(.el-upload),
.material-section :deep(.el-upload-dragger) {
    width: 100%;
}
.material-section :deep(.el-upload-dragger) {
    background: var(--ground);
    border-color: var(--line);
    border-radius: 12px;
    padding: 23px 15px;
    font-size: 12px;
    color: var(--ink);
}
.material-section :deep(.el-upload-dragger:hover) {
    background: var(--pink-soft);
    border-color: var(--studio-pink);
}
.upload-icon {
    display: block;
    font-size: 27px;
    color: var(--blue);
    margin: 0 auto 9px;
}
.material-section strong {
    color: var(--studio-pink);
    font-weight: 600;
}
.material-section small {
    display: block;
    font-size: 12px;
    color: var(--muted);
    margin-top: 7px;
}
.material-previews {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    margin-top: 12px;
}
.material-previews figure {
    position: relative;
    min-width: 0;
    margin: 0;
}
.material-previews :deep(.el-image) {
    display: block;
    height: 76px;
    width: 100%;
    border-radius: 8px;
}
.material-previews :deep(.el-button) {
    position: absolute;
    top: 3px;
    right: 3px;
    width: 23px;
    height: 23px;
    background: var(--surface);
}
.material-previews figcaption {
    color: var(--muted);
    font-size: 12px;
    text-align: center;
    margin-top: 4px;
}
.source-video {
    border-radius: 12px;
    overflow: hidden;
    background: var(--ground);
}
.source-video video {
    display: block;
    width: 100%;
    max-height: 230px;
}
.source-video > div {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 5px 12px;
    font-size: 12px;
}
.source-video span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}
.studio-error {
    margin: 16px 0;
}
.generate-area {
    margin: 20px 0 22px;
    display: grid;
    gap: 9px;
}
.generate-area .el-button {
    width: 100%;
    min-height: 47px;
    font-size: 14px;
    font-weight: 700;
    letter-spacing: normal;
}
.generate-area > span {
    color: var(--muted);
    text-align: center;
    font-size: 12px;
}
.connection-help {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-top: 17px;
    padding-top: 14px;
    font-size: 12px;
    color: var(--muted);
}
.connection-help .el-button {
    margin-left: auto;
    font-size: 12px;
}
.studio-canvas {
    padding: 24px;
    min-width: 0;
    min-height: 650px;
    display: flex;
    flex-direction: column;
}
.gallery-heading-copy {
    margin: 7px 0 0;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.7;
}
.canvas-empty {
    flex: 1;
    display: flex;
    flex-direction: column;
    justify-content: center;
    align-items: center;
    text-align: center;
    padding: 0 20px 26px;
}
.canvas-companion {
    width: min(100%, 375px);
    height: 350px;
    overflow: hidden;
    margin: 0 auto 4px;
    position: relative;
}
.canvas-companion :deep(.mascot) {
    width: 100%;
    height: 100%;
    background: transparent;
    border-radius: 0;
}
.canvas-companion :deep(.mascot img) {
    object-fit: contain;
    object-position: center bottom;
}
.canvas-welcome-copy {
    position: relative;
    max-width: 390px;
}
.canvas-empty h3 {
    color: var(--ink);
    font-size: 22px;
    letter-spacing: normal;
    margin: 8px 0 14px;
}
.canvas-empty p {
    font-size: 14px;
    line-height: 1.9;
    color: var(--muted);
    margin: 0;
    max-width: 370px;
    overflow-wrap: anywhere;
}
.empty-formats {
    display: flex;
    gap: 21px;
    margin-top: 27px;
    font-size: 12px;
    color: var(--muted);
}
.empty-formats span {
    display: flex;
    gap: 5px;
    align-items: center;
}
.canvas-note {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 6px;
    color: var(--muted);
    font-size: 12px;
    line-height: 1.7;
    padding-top: 20px;
    margin-top: auto;
}
.task-status {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 10px;
    padding: 12px;
    margin: -3px 0 15px;
    background: var(--surface-soft);
    border-radius: 9px;
}
.task-status > span {
    display: flex;
    align-items: center;
    gap: 5px;
    font-size: 12px;
    color: var(--muted);
}
.task-status .task-summary {
    flex: 1;
    min-width: 0;
    display: block;
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
}
.task-status > span:last-child {
    flex-shrink: 0;
}
.studio-receipt-recovery {
    border-top: 1px solid var(--line);
    margin-top: 20px;
}
.studio-receipt-recovery :deep(.el-collapse) {
    --el-collapse-border-color: transparent;
    --el-collapse-header-bg-color: transparent;
    --el-collapse-content-bg-color: transparent;
}
.studio-receipt-recovery :deep(.el-collapse-item__header) {
    color: var(--muted);
    font-size: 12px;
    font-weight: 500;
}
.studio-receipt-recovery :deep(.el-collapse-item__content) {
    padding-bottom: 8px;
}
.receipt-recovery {
    display: flex;
    gap: 10px;
}
.receipt-recovery .el-input {
    flex: 1;
}
.history-note {
    font-size: 12px;
    color: var(--muted);
    line-height: 1.8;
    margin: 10px 0 0;
}
.detail-copy {
    margin: 20px 0 12px;
}
.studio-json {
    padding: 20px;
    border-radius: 12px;
    color: var(--ink);
    font-size: 13px;
    line-height: 1.85;
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    background: var(--surface-soft);
}
.document-workspace {
    padding: 55px;
    display: grid;
    grid-template-columns: 240px 1fr;
    gap: 55px;
    align-items: center;
}
.document-illustration {
    height: 250px;
    display: grid;
    place-content: center;
    justify-items: center;
    gap: 24px;
    background: var(--blue-soft);
    border-radius: 24px;
    color: #b09bc9;
}
.document-illustration > .el-icon {
    font-size: 75px;
}
.document-illustration > span {
    font-size: 12px;
    letter-spacing: normal;
}
.document-workspace h2 {
    font-size: 26px;
    margin: 12px 0;
    letter-spacing: normal;
}
.document-workspace p {
    color: var(--muted);
    line-height: 1.9;
    font-size: 13px;
    margin-bottom: 25px;
}
.document-actions {
    display: flex;
    gap: 10px;
    margin-top: 26px;
}
.document-actions .el-button {
    margin: 0;
}
.legacy-doc-frame {
    width: 100%;
    height: calc(100vh - 120px);
    border: 0;
    border-radius: 12px;
}
.studio-settings {
    --el-collapse-border-color: transparent;
    --el-collapse-header-bg-color: transparent;
    --el-collapse-content-bg-color: transparent;
    margin: 6px -3px 0;
}
.studio-settings :deep(.el-collapse-item__header) {
    min-height: 53px;
    height: auto;
    padding: 11px 3px;
    font-weight: 600;
}
.studio-settings :deep(.el-collapse-item__content) {
    padding: 14px 3px 2px;
}
.studio-settings :deep(.el-collapse-item__arrow) {
    color: var(--muted);
}
.settings-toggle {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 12px;
    width: 100%;
    text-align: left;
    line-height: 1.6;
}
.settings-toggle > span {
    color: var(--ink);
    font-size: 14px;
    flex-shrink: 0;
}
.settings-toggle > small {
    color: var(--muted);
    font-size: 12px;
    font-weight: 400;
    text-align: right;
}
@media (prefers-reduced-motion: reduce) {
    .studio-view :deep(.mascot img) {
        animation: none;
    }
}
@media (min-width: 1700px) {
    .studio-grid {
        grid-template-columns: 410px minmax(0, 1fr);
    }
    .studio-canvas {
        min-height: 735px;
    }
    .canvas-companion {
        width: 420px;
        height: 425px;
    }
}
@media (max-width: 1250px) {
    .studio-nav {
        gap: 13px;
        flex-wrap: wrap;
    }
    .studio-nav > .el-tag {
        margin-left: 0;
    }
    .studio-kind-tabs {
        order: 3;
        width: 100%;
        padding-top: 9px;
    }
    .studio-grid {
        grid-template-columns: minmax(310px, 350px) minmax(0, 1fr);
        gap: 16px;
    }
    .studio-editor,
    .studio-canvas {
        padding: 20px;
    }
}
@media (max-width: 1050px) {
    .studio-grid {
        grid-template-columns: 1fr;
    }
    .studio-editor {
        position: static;
    }
    .studio-canvas {
        min-height: 530px;
    }
    .canvas-companion {
        height: 315px;
    }
    .studio-kind-tabs {
        justify-content: space-between;
    }
    .studio-kind-tabs button {
        padding: 10px;
    }
    .document-workspace {
        grid-template-columns: 1fr;
        padding: 25px;
        gap: 25px;
    }
    .document-illustration {
        height: 160px;
    }
    .document-actions {
        flex-wrap: wrap;
    }
}
@media (max-width: 520px) {
    .studio-nav {
        padding: 14px;
    }
    .studio-kind-tabs button {
        font-size: 12px;
        padding: 9px 4px;
        gap: 4px;
    }
    .studio-kind-tabs button .el-icon {
        display: none;
    }
    .studio-editor,
    .studio-canvas {
        padding: 19px 14px;
    }
    .canvas-empty {
        padding: 0 5px 20px;
    }
    .canvas-companion {
        height: 285px;
    }
    .canvas-empty h3 {
        font-size: 20px;
    }
    .empty-formats {
        gap: 12px;
    }
    .receipt-recovery {
        flex-direction: column;
    }
    .studio-view .page-actions .el-button {
        font-size: 12px;
    }
    .canvas-note {
        align-items: flex-start;
        font-size: 11px;
    }
    .canvas-note > .el-icon {
        flex-shrink: 0;
        margin-top: 3px;
    }
    .document-actions > .el-button {
        width: 100%;
    }
    .task-status {
        flex-wrap: wrap;
        gap: 9px;
    }
    .task-status .task-summary {
        display: none;
    }
}
</style>
