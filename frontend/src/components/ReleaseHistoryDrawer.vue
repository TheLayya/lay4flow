<script setup>
import { onMounted, onUnmounted, reactive, ref } from 'vue'
import { Link, Refresh } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'
import { api, parseDate } from '../lib/api'

const open = ref(false)
const drawerSize = ref(window.innerWidth <= 540 ? '100%' : '540px')
const loading = ref(false)
const checking = ref(false)
const updating = ref(false)
const desktopAvailable = ref(Boolean(window.chrome?.webview && window.lay4flowDesktop))
const updateStatus = reactive({ state: '', message: '' })
const loadError = ref('')
const releaseCheck = reactive({ status: '', message: '', checked_at: '' })
const release = reactive({
    project: 'lay4flow-remote',
    version: '—',
    release_date: '',
    entries: [],
    release_page: 'https://github.com/TheLayya/lay4flow/releases',
    update_docs: { windows: '/docs/WINDOWS_INSTALL.md', docker: '/docs/DOCKER_UPDATE.md' },
})

function openDrawer(event) {
    open.value = true
    if (!release.entries.length) loadRelease()
    if (event?.detail?.check) checkRelease()
}

async function loadRelease() {
    if (loading.value) return
    loading.value = true
    loadError.value = ''
    try {
        Object.assign(release, await api.get('/api/release'))
    } catch (error) {
        loadError.value = error?.message || '更新记录暂时无法读取'
    } finally {
        loading.value = false
    }
}

async function checkRelease() {
    if (checking.value) return
    checking.value = true
    try {
        Object.assign(releaseCheck, await api.get('/api/release/check'))
        window.dispatchEvent(new CustomEvent('release-checked', { detail: releaseCheck }))
        if (releaseCheck.status === 'update_available') ElMessage.warning(releaseCheck.message)
        else if (releaseCheck.message) ElMessage.success(releaseCheck.message)
    } catch (error) {
        releaseCheck.message = error?.message || '线上版本暂时不可查询，当前记录仍可查看。'
    } finally {
        checking.value = false
    }
}

function applyUpdate() {
    if (updating.value || !desktopAvailable.value || releaseCheck.status !== 'update_available') return
    updating.value = true
    updateStatus.state = 'running'
    updateStatus.message = '正在准备更新，完成后桌面端会自动重启。'
    try {
        window.chrome.webview.postMessage({ type: 'apply-update', version: releaseCheck.latest?.version })
    } catch (error) {
        updating.value = false
        updateStatus.state = 'failed'
        updateStatus.message = error?.message || '更新启动失败，请重新打开桌面端后重试。'
    }
}
function receiveUpdate(event) {
    const value = event.detail || event.data
    if (!value || (value.type && value.type !== 'desktop-update-status')) return
    if (!['running', 'completed', 'failed'].includes(value.state)) return
    Object.assign(updateStatus, value)
    updating.value = value.state === 'running'
}

function formatDate(value) {
    if (!value) return '—'
    const date = parseDate(value)
    return Number.isNaN(date.getTime())
        ? value
        : date.toLocaleDateString('zh-CN', { year: 'numeric', month: '2-digit', day: '2-digit' })
}
function itemText(value) {
    return String(value).replace(/`([^`]+)`/g, '$1')
}

function resizeDrawer() {
    drawerSize.value = window.innerWidth <= 540 ? '100%' : '540px'
}
onMounted(() => {
    window.addEventListener('open-release-history', openDrawer)
    window.addEventListener('resize', resizeDrawer)
    window.addEventListener('desktop-update-status', receiveUpdate)
    window.chrome?.webview?.addEventListener('message', receiveUpdate)
})
onUnmounted(() => {
    window.removeEventListener('open-release-history', openDrawer)
    window.removeEventListener('resize', resizeDrawer)
    window.removeEventListener('desktop-update-status', receiveUpdate)
    window.chrome?.webview?.removeEventListener('message', receiveUpdate)
})
</script>

<template>
    <el-drawer v-model="open" class="release-history-drawer" title="更新日志" :size="drawerSize">
        <div class="release-drawer-head">
            <div>
                <span class="release-eyebrow">lay4flow-remote</span>
                <h2>当前 v{{ release.version === '—' ? '—' : release.version }}</h2>
                <p v-if="release.release_date">发布于 {{ formatDate(release.release_date) }}</p>
                <p v-else>本地版本记录</p>
            </div>
            <el-button :loading="checking" :icon="Refresh" @click="checkRelease">检查更新</el-button>
            <el-button v-if="desktopAvailable && releaseCheck.status === 'update_available'" type="primary" :loading="updating" @click="applyUpdate">立即更新</el-button>
        </div>
        <el-alert v-if="loadError" type="warning" :closable="false" show-icon :title="loadError" />
        <el-alert v-if="updateStatus.message" class="release-check-message" :type="updateStatus.state === 'failed' ? 'error' : 'info'" :closable="false" show-icon :title="updateStatus.message" />
        <el-alert
            v-if="releaseCheck.message"
            class="release-check-message"
            :type="releaseCheck.status === 'update_available' ? 'warning' : 'info'"
            :closable="false"
            show-icon
            :title="releaseCheck.message"
        />
        <div v-if="loading" class="release-loading">正在读取更新记录…</div>
        <div v-else-if="!release.entries.length && !loadError" class="release-empty">暂无已发布记录</div>
        <div v-else class="release-timeline">
            <article
                v-for="(entry, index) in release.entries"
                :key="`${entry.version}-${entry.date}-${index}`"
                class="release-timeline-entry"
            >
                <div class="release-timeline-marker" aria-hidden="true"></div>
                <div class="release-entry-content">
                    <div class="release-entry-title">
                        <strong>{{ entry.version ? `v${entry.version}` : '历史修复' }}</strong>
                        <time>{{ formatDate(entry.date) }}</time>
                    </div>
                    <ul>
                        <li v-for="(item, itemIndex) in entry.items" :key="`${item}-${itemIndex}`">{{ itemText(item) }}</li>
                    </ul>
                </div>
            </article>
        </div>
        <template #footer>
            <div class="release-drawer-footer">
                <a :href="release.release_page" target="_blank" rel="noreferrer"><el-icon><Link /></el-icon> GitHub 发布页</a>
                <a :href="release.update_docs.windows">Windows 安装</a>
                <a :href="release.update_docs.docker">Docker 更新</a>
            </div>
        </template>
    </el-drawer>
</template>

<style scoped>
@media (max-width: 540px) {
    :global(.release-history-drawer.el-drawer) {
        width: 100vw !important;
        max-width: 100vw !important;
    }
}
.release-drawer-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 18px;
    padding: 3px 0 22px;
    border-bottom: 1px solid var(--line);
}
.release-eyebrow {
    color: var(--accent);
    font-size: 12px;
    font-weight: 700;
    letter-spacing: 0.08em;
    text-transform: uppercase;
}
.release-drawer-head h2 {
    margin: 7px 0 2px;
    color: var(--ink);
    font-size: 25px;
    letter-spacing: -0.04em;
}
.release-drawer-head p {
    margin: 0;
    color: var(--muted);
    font-size: 13px;
}
.release-check-message {
    margin-top: 16px;
}
.release-loading,
.release-empty {
    padding: 48px 0;
    color: var(--muted);
    text-align: center;
}
.release-timeline {
    padding: 24px 0 8px 8px;
}
.release-timeline-entry {
    position: relative;
    display: grid;
    grid-template-columns: 15px minmax(0, 1fr);
    gap: 14px;
    padding-bottom: 26px;
}
.release-timeline-entry:not(:last-child)::before {
    position: absolute;
    top: 14px;
    bottom: 0;
    left: 5px;
    width: 1px;
    background: var(--line);
    content: '';
}
.release-timeline-marker {
    z-index: 1;
    width: 11px;
    height: 11px;
    margin-top: 3px;
    border: 3px solid var(--pink-soft);
    border-radius: 50%;
    background: var(--accent);
}
.release-entry-title {
    display: flex;
    align-items: baseline;
    justify-content: space-between;
    gap: 12px;
}
.release-entry-title strong {
    color: var(--ink);
    font-size: 19px;
}
.release-entry-title time {
    flex-shrink: 0;
    color: var(--muted);
    font-size: 12px;
}
.release-entry-content ul {
    margin: 10px 0 0;
    padding-left: 18px;
    color: var(--muted);
}
.release-entry-content li + li {
    margin-top: 7px;
}
.release-drawer-footer {
    display: flex;
    flex-wrap: wrap;
    gap: 10px 18px;
    align-items: center;
}
.release-drawer-footer a {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    color: var(--accent-strong);
    font-size: 13px;
}
.release-drawer-footer a:hover {
    text-decoration: underline;
    text-underline-offset: 3px;
}
@media (max-width: 480px) {
    .release-drawer-head {
        display: block;
    }
    .release-drawer-head .el-button {
        margin-top: 15px;
    }
}
</style>
