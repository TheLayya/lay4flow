<script setup>
import { computed } from 'vue'

const props = defineProps({
    account: { type: Object, required: true },
    draft: { type: Object, required: true },
    proxies: { type: Array, default: () => [] },
    saving: { type: Boolean, default: false },
    savingKind: { type: String, default: '' },
    locked: { type: Boolean, default: false },
    error: { type: String, default: '' },
    saved: { type: String, default: '' },
    poolError: { type: String, default: '' },
})
const emit = defineEmits(['save', 'copy', 'reload'])
const current = computed(() => props.proxies.find((row) => row.id === props.account.proxy_pool_id))
const picked = computed(() => props.proxies.find((row) => row.id === props.draft.proxy_pool_id))
const proxyChanged = computed(() => props.draft.proxy_pool_id !== (props.account.proxy_pool_id ?? null))
const shownURL = computed(() => picked.value?.proxy_url || props.account.proxy_url || current.value?.proxy_url || '')
const decode = (value) => {
    try {
        return decodeURIComponent(value)
    } catch {
        return value
    }
}
const proxyFields = computed(() => {
    try {
        const parsed = new URL(shownURL.value)
        const port = parsed.port || (parsed.protocol === 'https:' ? 443 : parsed.protocol === 'http:' ? 80 : 1080)
        return [
            { label: '协议', value: parsed.protocol.slice(0, -1).toUpperCase() },
            { label: '主机与端口', value: `${parsed.hostname}:${port}` },
            { label: '用户名', value: decode(parsed.username) },
            { label: '密码', value: decode(parsed.password) },
        ]
    } catch {
        return []
    }
})
const credentials = computed(() => [
    { label: 'Session Token', value: props.account.st },
    { label: 'Access Token', value: props.account.at || props.account.token },
    { label: 'Google Cookies', value: props.account.google_cookies },
    { label: '登录账号', value: props.account.login_account || props.account.email },
    { label: '登录密码', value: props.account.login_password },
])
const options = computed(() => props.proxies.filter((row) => row.enabled || row.id === props.account.proxy_pool_id))
function proxyIdentity(value) {
    let raw = String(value || '').trim()
    if (/^st5\s+/i.test(raw)) raw = `socks5://${raw.slice(4).trim()}`
    const separator = raw.indexOf('://')
    const scheme = separator < 0 ? 'http' : raw.slice(0, separator).toLowerCase()
    let rest = separator < 0 ? raw : raw.slice(separator + 3)
    if (!['http', 'https', 'socks5', 'socks5h'].includes(scheme)) throw new Error('Invalid proxy')
    const legacy = rest.startsWith('[')
        ? rest.match(/^\[([^\]]+)\]:(\d+):([^:]+):(.+)$/)
        : rest.match(/^([^:]+):(\d+):([^:]+):(.+)$/)
    if (legacy)
        rest = `${encodeURIComponent(legacy[3]).replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`)}:${encodeURIComponent(legacy[4]).replace(/[!'()*]/g, (char) => `%${char.charCodeAt(0).toString(16).toUpperCase()}`)}@${legacy[1].includes(':') ? `[${legacy[1]}]` : legacy[1]}:${legacy[2]}`
    if (!/^.+:\d+$/.test(rest)) throw new Error('Invalid proxy')
    return `${scheme}://${rest}`
}
function sameProxy(first, second) {
    if (String(first || '').trim() === String(second || '').trim()) return true
    try {
        return proxyIdentity(first) === proxyIdentity(second)
    } catch {
        return false
    }
}
const fixedExitMismatch = (row) =>
    props.account.browser_profile_source === 'cloak' &&
    !sameProxy(row.proxy_url, props.account.proxy_url || current.value?.proxy_url)
const optionLabel = (row) =>
    `#${row.id} ${row.proxy_url}${row.note ? ' · ' + row.note : ''} · ${row.bound_account_count || 0} 个账号${!row.enabled ? ' · 已停用' : row.last_check_ok === false ? ' · 连接异常' : ''}${fixedExitMismatch(row) ? ' · 切换时验证 Profile' : ''}`
</script>

<template>
    <section
        class="account-quick-panel"
        :data-account-id="account.id"
        :aria-label="`${account.email || account.id} 的代理与配置`"
    >
        <div class="account-quick-grid">
            <section class="quick-proxy-section">
                <h3>
                    账号代理
                    <small>{{ account.proxy_pool_id ? '当前 #' + account.proxy_pool_id : '尚未绑定代理池' }}</small>
                </h3>
                <div class="quick-proxy-picker">
                    <el-select
                        v-model="draft.proxy_pool_id"
                        :aria-label="`为 ${account.email || account.id} 选择代理`"
                        placeholder="搜索地址、用户名或备注"
                        filterable
                        :disabled="locked"
                    >
                        <el-option
                            v-for="proxy in options"
                            :key="proxy.id"
                            :value="proxy.id"
                            :label="optionLabel(proxy)"
                            :disabled="!proxy.enabled"
                        />
                    </el-select>
                    <el-button
                        class="quick-bind-save"
                        type="primary"
                        :loading="saving && savingKind === 'proxy'"
                        :disabled="locked || !proxyChanged || !picked?.enabled"
                        @click="emit('save', 'proxy')"
                    >
                        {{ account.proxy_pool_id ? '保存绑定' : '绑定代理' }}
                    </el-button>
                </div>
                <p v-if="poolError" class="quick-error">
                    代理池读取失败：{{ poolError }}
                    <el-button text size="small" @click="emit('reload')">重试</el-button>
                </p>
                <p v-else-if="!options.length" class="quick-hint">代理池暂无已启用的出口。</p>
                <template v-if="shownURL">
                    <div class="quick-proxy-url">
                        <code>{{ shownURL }}</code>
                        <el-button text size="small" @click="emit('copy', shownURL)">复制整条</el-button>
                    </div>
                    <dl class="quick-proxy-fields">
                        <div v-for="field in proxyFields" :key="field.label">
                            <dt>{{ field.label }}</dt>
                            <dd>
                                <span>{{ field.value || '无' }}</span>
                                <el-button
                                    v-if="field.value"
                                    text
                                    size="small"
                                    :aria-label="'复制代理' + field.label"
                                    @click="emit('copy', field.value)"
                                >
                                    复制
                                </el-button>
                            </dd>
                        </div>
                    </dl>
                </template>
                <p v-if="proxyChanged" class="quick-hint">上方为待绑定出口，保存后生效。</p>
                <p v-if="picked?.last_check_ok === false" class="quick-hint">
                    此出口最近检测连接异常，请确认当前连接状态。
                </p>
                <p v-if="account.browser_profile_source === 'cloak'" class="quick-hint">
                    可主动切换 Cloak 代理，验证通过后重新绑定；验证失败保留原出口。
                </p>
            </section>
            <section class="quick-settings-section">
                <h3>常用配置</h3>
                <label class="quick-remark">
                    备注
                    <el-input
                        v-model="draft.remark"
                        :disabled="locked"
                        maxlength="500"
                        :aria-label="`${account.email || account.id} 的备注`"
                        placeholder="添加账号备注"
                    />
                </label>
                <div class="quick-setting-switches">
                    <label>
                        <span>图片创作</span>
                        <el-switch v-model="draft.image_enabled" :disabled="locked" aria-label="图片创作" />
                    </label>
                    <label>
                        <span>视频创作</span>
                        <el-switch v-model="draft.video_enabled" :disabled="locked" aria-label="视频创作" />
                    </label>
                    <label>
                        <span>自动续期</span>
                        <el-switch v-model="draft.auto_refresh_enabled" :disabled="locked" aria-label="自动续期" />
                    </label>
                </div>
                <div class="quick-refresh-row">
                    <label>
                        刷新间隔
                        <el-input-number
                            v-model="draft.refresh_interval_minutes"
                            :min="1"
                            :max="1440"
                            :disabled="locked || !draft.auto_refresh_enabled"
                            controls-position="right"
                            aria-label="刷新间隔分钟"
                        />
                        <span>分钟</span>
                    </label>
                    <el-button
                        :loading="saving && savingKind === 'settings'"
                        :disabled="locked"
                        @click="emit('save', 'settings')"
                    >
                        保存配置
                    </el-button>
                </div>
                <details class="quick-credentials">
                    <summary>登录凭据 · 展开查看与复制</summary>
                    <dl>
                        <div v-for="field in credentials" :key="field.label">
                            <dt>{{ field.label }}</dt>
                            <dd>
                                <code>{{ field.value || '未填写' }}</code>
                                <el-button v-if="field.value" text size="small" @click="emit('copy', field.value)">
                                    复制
                                </el-button>
                            </dd>
                        </div>
                    </dl>
                </details>
            </section>
        </div>
        <p v-if="error" class="quick-error" role="alert">{{ error }}</p>
        <p v-else-if="saved" class="quick-saved" role="status">{{ saved }}</p>
    </section>
</template>

<style scoped>
.account-quick-panel {
    padding: 18px 24px;
    background: var(--ground);
    color: var(--ink);
}
.account-quick-grid {
    display: grid;
    grid-template-columns: minmax(0, 1.15fr) minmax(320px, 1fr);
    gap: 30px;
}
h3 {
    font-size: 14px;
    font-weight: 650;
    line-height: 1.5;
    margin: 0 0 12px;
}
h3 small {
    font-size: 12px;
    color: var(--muted);
    font-weight: 400;
    margin-left: 8px;
}
.quick-proxy-picker,
.quick-refresh-row,
.quick-proxy-url,
dd {
    display: flex;
    align-items: center;
    gap: 10px;
}
.quick-proxy-picker .el-select {
    flex: 1;
    min-width: 0;
}
.quick-proxy-url {
    margin: 13px 0 8px;
    align-items: flex-start;
}
.quick-proxy-url code,
dd span,
dd code {
    min-width: 0;
    overflow-wrap: anywhere;
    line-height: 1.65;
}
.quick-proxy-url code {
    flex: 1;
}
.quick-proxy-url > .el-button,
dd > .el-button {
    flex-shrink: 0;
    padding: 3px 0;
    height: auto;
}
.quick-proxy-fields {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 8px 24px;
    margin: 0;
}
dt {
    color: var(--muted);
    font-size: 12px;
    line-height: 1.6;
}
dd {
    margin: 0;
    justify-content: space-between;
    font-size: 13px;
    min-height: 24px;
}
.quick-hint {
    color: var(--muted);
    font-size: 12px;
    line-height: 1.65;
    margin: 10px 0 0;
}
.quick-remark {
    display: grid;
    grid-template-columns: 40px 1fr;
    align-items: center;
    gap: 8px;
    font-size: 13px;
}
.quick-setting-switches {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 18px;
    margin: 9px 0;
}
.quick-setting-switches label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
}
.quick-refresh-row {
    justify-content: space-between;
    flex-wrap: wrap;
}
.quick-refresh-row label {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
}
.quick-refresh-row .el-input-number {
    width: 108px;
}
.quick-error {
    color: var(--el-color-danger);
    margin: 12px 0 0;
    line-height: 1.65;
    font-size: 13px;
    overflow-wrap: anywhere;
}
.quick-saved {
    color: var(--success);
    margin: 12px 0 0;
    font-size: 13px;
}
.quick-credentials {
    margin-top: 15px;
    font-size: 13px;
}
.quick-credentials summary {
    cursor: pointer;
    color: var(--muted);
}
.quick-credentials dl {
    margin: 10px 0 0;
    display: grid;
    gap: 8px;
}
.quick-credentials dd {
    align-items: flex-start;
    gap: 16px;
}
.quick-credentials code {
    max-height: 96px;
    overflow: auto;
}
@media (max-width: 1100px) {
    .account-quick-grid {
        grid-template-columns: 1fr;
        gap: 22px;
    }
}
@media (max-width: 700px) {
    .account-quick-panel {
        margin: 12px -8px 0;
        padding: 15px 12px;
        border-radius: 8px;
    }
    .quick-proxy-picker {
        align-items: stretch;
        flex-direction: column;
        gap: 8px;
    }
    .quick-proxy-fields {
        gap: 10px 16px;
    }
    .quick-setting-switches {
        gap: 6px 14px;
    }
    .quick-setting-switches label {
        min-height: 44px;
    }
    .quick-refresh-row {
        gap: 12px;
    }
    .quick-refresh-row .el-button {
        width: 100%;
        margin-left: 0;
        min-height: 40px;
    }
}
</style>
