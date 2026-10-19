<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'
import { useRoute } from 'vue-router'
import { api } from '../lib/api'

const route = useRoute()
const jobs = ref([])
const stale = ref(false)
const followed = new Set()
const dismissed = ref(new Set())
let timer,
    disposed = false,
    loading = false
let version = 0
const active = (job) => ['queued', 'running'].includes(job.state)
const visible = computed(() =>
    jobs.value.filter((job) => !dismissed.value.has(job.id) && (active(job) || followed.has(job.id))),
)
const count = computed(() => visible.value.reduce((total, job) => total + job.total, 0))
const finished = computed(() =>
    visible.value.reduce(
        (total, job) =>
            total +
            (job.items || []).filter((item) => ['succeeded', 'failed', 'cancelled'].includes(item.state)).length,
        0,
    ),
)
const failures = computed(() => visible.value.reduce((total, job) => total + job.failed, 0))
const running = computed(() => visible.value.some(active))
function update(next) {
    if (!Array.isArray(next) || disposed) return
    for (const job of next) if (active(job)) followed.add(job.id)
    jobs.value = next
    stale.value = false
}
function receive(event) {
    version++
    update(event.detail?.jobs)
}
async function refresh() {
    if (loading || disposed) return
    loading = true
    const requestedVersion = version
    try {
        const response = await api.get('/api/profile-transfer/jobs')
        if (requestedVersion === version && response.success !== false) update(response.jobs)
    } catch {
        stale.value = true
    } finally {
        loading = false
    }
}
function dismiss() {
    dismissed.value = new Set([...dismissed.value, ...visible.value.filter((job) => !active(job)).map((job) => job.id)])
}
onMounted(() => {
    refresh()
    window.addEventListener('lay4flow:profile-import-jobs', receive)
    timer = setInterval(() => {
        if (route.path !== '/accounts' && (running.value || stale.value)) refresh()
    }, 2500)
})
onUnmounted(() => {
    disposed = true
    clearInterval(timer)
    window.removeEventListener('lay4flow:profile-import-jobs', receive)
})
</script>
<template>
    <aside
        v-if="visible.length && route.path !== '/accounts'"
        class="profile-import-notice"
        role="status"
        aria-live="polite"
    >
        <span class="import-dot" :class="{ running }"></span>
        <div>
            <strong>{{ running ? '账号正在后台导入' : '后台导入已结束' }} · {{ finished }} / {{ count }}</strong>
            <span v-if="stale">进度暂未更新，正在重新连接</span>
            <span v-else>
                {{
                    failures
                        ? `${failures} 项失败，可查看原因并重试`
                        : running
                          ? '可继续使用其他页面'
                          : '导入结果已保存'
                }}
            </span>
        </div>
        <router-link to="/accounts">查看导入任务 →</router-link>
        <button v-if="!running" type="button" aria-label="收起导入通知" @click="dismiss">×</button>
    </aside>
</template>
<style scoped>
.profile-import-notice {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 28px;
    background: var(--el-fill-color-light);
    border-bottom: 1px solid var(--el-border-color-light);
    font-size: 13px;
}
.profile-import-notice div {
    display: flex;
    flex-wrap: wrap;
    gap: 6px 16px;
    flex: 1;
}
.profile-import-notice strong {
    font-weight: 600;
    color: var(--el-text-color-primary);
}
.profile-import-notice span {
    color: var(--el-text-color-secondary);
}
.profile-import-notice a {
    color: var(--el-color-primary);
    white-space: nowrap;
    text-decoration: none;
}
.import-dot {
    width: 7px;
    height: 7px;
    flex-shrink: 0;
    border-radius: 50%;
    background: var(--el-color-success);
}
.import-dot.running {
    background: var(--el-color-primary);
}
.profile-import-notice button {
    border: 0;
    background: transparent;
    color: var(--el-text-color-secondary);
    font-size: 20px;
    cursor: pointer;
}
@media (max-width: 767px) {
    .profile-import-notice {
        padding: 12px 18px;
        gap: 8px;
        flex-wrap: wrap;
    }
}
</style>
