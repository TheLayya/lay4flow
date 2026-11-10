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
    activeTab: { type: String, default: 'account' },
})
const emit = defineEmits(['save', 'copy', 'reload', 'update:activeTab'])
const tabs = [{ id: 'account', label: '账号信息' }, { id: 'proxy', label: '代理' }]
function navigateTabs(event, index) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const next = event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1
        : (index + (event.key === 'ArrowRight' ? 1 : -1) + tabs.length) % tabs.length
    emit('update:activeTab', tabs[next].id)
    event.currentTarget.parentElement.querySelectorAll('[role="tab"]')[next]?.focus()
}
const cookieDocument = computed(() => {
    const raw = props.account.google_cookies
    if (raw == null || typeof raw === 'string' && !raw.trim()) return { text: '', format: 'empty' }
    let parsed = raw
    if (typeof raw === 'string') {
        try { parsed = JSON.parse(raw) } catch { return { text: raw, format: 'raw' } }
    }
    const wrapper = parsed && !Array.isArray(parsed) && typeof parsed === 'object' &&
        ['refreshCookie', 'cookies'].some(key => Object.hasOwn(parsed, key)) ? parsed : null
    // Older saved jars use refreshCookie/cookies wrappers. Unwrap them using
    // the same priority as the server, retaining the full scoped cookie rows.
    const seen = new Set()
    while (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        const key = ['refreshCookie', 'cookies'].find(name => Object.hasOwn(parsed, name))
        if (!key || seen.has(parsed)) break
        seen.add(parsed)
        parsed = parsed[key]
        if (typeof parsed === 'string') {
            try { parsed = JSON.parse(parsed) } catch { return { text: String(raw), format: 'raw' } }
        }
    }
    if (parsed == null || parsed === '' || typeof parsed === 'object' && !Object.keys(parsed).length)
        return { text: '', format: 'empty' }
    if (typeof parsed !== 'object') return { text: String(raw), format: 'raw' }
    // Keep browser cookie rows intact: a name/value map loses domains, expiry
    // and multiple cookies with the same name. The import route accepts both.
    const document = wrapper ? { ...wrapper, cookies: parsed } : { cookies: parsed }
    delete document.refreshCookie
    const email = props.account.email || props.account.login_account
    if (email) document.email = email
    if (props.account.current_project_id) document.projectId = props.account.current_project_id
    if (props.account.user_paygate_tier) document.userPaygateTier = props.account.user_paygate_tier
    return { text: JSON.stringify(document, null, 2), format: 'json' }
})
function highlightLine(text) {
    const tokens = []
    const pattern = /("(?:\\.|[^"\\])*"\s*:)|("(?:\\.|[^"\\])*")|\b(true|false|null)\b|(-?(?:0|[1-9]\d*)(?:\.\d+)?(?:[eE][+-]?\d+)?)/g
    let cursor = 0
    for (const match of text.matchAll(pattern)) {
        if (match.index > cursor) tokens.push({ text: text.slice(cursor, match.index), kind: 'plain' })
        tokens.push({ text: match[0], kind: match[1] ? 'key' : match[2] ? 'string' : match[3] ? 'literal' : 'number' })
        cursor = match.index + match[0].length
    }
    if (cursor < text.length) tokens.push({ text: text.slice(cursor), kind: 'plain' })
    return tokens
}
const cookieLines = computed(() => cookieDocument.value.text.split('\n').map(line =>
    cookieDocument.value.format === 'json' ? highlightLine(line) : [{ text: line, kind: 'plain' }]))
function copyCookies() {
    if (cookieDocument.value.text) emit('copy', cookieDocument.value.text)
}
const current = computed(() => props.proxies.find((row) => row.id === props.account.proxy_pool_id))
const picked = computed(() => props.proxies.find((row) => row.id === props.draft.proxy_pool_id))
const proxyChanged = computed(() => props.draft.proxy_pool_id !== (props.account.proxy_pool_id ?? null))
const strategyChanged = computed(
    () => props.draft.submission_proxy_mode !== (props.account.submission_proxy_mode || 'fixed'),
)
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
        :aria-label="`${account.email || account.id} 的账号信息与代理`"
    >
        <div class="account-quick-tabs" role="tablist" :aria-label="`${account.email || account.id} 的详情分类`">
            <button v-for="(tab, index) in tabs" :id="`account-${account.id}-tab-${tab.id}`" :key="tab.id"
                type="button" role="tab" :aria-selected="activeTab === tab.id"
                :aria-controls="`account-${account.id}-panel-${tab.id}`" :tabindex="activeTab === tab.id ? 0 : -1"
                @click="emit('update:activeTab', tab.id)" @keydown="navigateTabs($event, index)">{{ tab.label }}</button>
        </div>
        <section v-show="activeTab === 'account'" :id="`account-${account.id}-panel-account`" role="tabpanel"
            :aria-labelledby="`account-${account.id}-tab-account`" class="quick-account-section">
            <div class="quick-cookie-toolbar">
                <strong>{{ cookieDocument.format === 'raw' ? 'Cookie 原文' : 'Cookie JSON' }}</strong>
                <el-button size="small" :disabled="!cookieDocument.text" @click="copyCookies">
                    {{ cookieDocument.format === 'raw' ? '复制 Cookie' : '复制 JSON' }}
                </el-button>
            </div>
            <div v-if="cookieDocument.text" class="quick-cookie-editor" :data-format="cookieDocument.format"
                tabindex="0" role="region" :aria-label="cookieDocument.format === 'raw' ? 'Cookie 原文（只读）' : 'Cookie JSON（只读）'">
                <div class="quick-cookie-gutter" aria-hidden="true"><span v-for="(_, index) in cookieLines" :key="index">{{ index + 1 }}</span></div>
                <pre class="quick-cookie-content"><code><template v-for="(line, index) in cookieLines" :key="index"><span v-for="(token, tokenIndex) in line" :key="tokenIndex" :class="'cookie-' + token.kind">{{ token.text }}</span>{{ index < cookieLines.length - 1 ? '\n' : '' }}</template></code></pre>
            </div>
            <p v-else class="quick-cookie-empty">暂无保存的 Google Cookies。</p>
            <p v-if="cookieDocument.format === 'raw'" class="quick-hint">保存的内容不是 Cookie JSON，按原文展示与复制。</p>
        </section>
        <section v-show="activeTab === 'proxy'" :id="`account-${account.id}-panel-proxy`" role="tabpanel"
            :aria-labelledby="`account-${account.id}-tab-proxy`" class="account-quick-grid">
            <section class="quick-proxy-section">
                <h3>
                    账号代理
                    <small>{{ account.proxy_pool_id ? '当前 #' + account.proxy_pool_id : '尚未绑定代理池' }}</small>
                </h3>
                <label class="quick-strategy-label" :for="`submission-proxy-${account.id}`">生成提交出口</label>
                <div class="quick-proxy-picker quick-submission-picker">
                    <el-select
                        :id="`submission-proxy-${account.id}`"
                        v-model="draft.submission_proxy_mode"
                        :aria-label="`${account.email || account.id} 的生成提交出口`"
                        :disabled="locked"
                    >
                        <el-option label="账号固定代理" value="fixed" />
                        <el-option label="代理池随机（每次生成取健康出口）" value="pool_random" />
                    </el-select>
                    <el-button
                        type="primary"
                        :loading="saving && savingKind === 'strategy'"
                        :disabled="locked || !strategyChanged"
                        @click="emit('save', 'strategy')"
                    >
                        保存策略
                    </el-button>
                </div>
                <p class="quick-hint quick-strategy-hint">
                    {{
                        draft.submission_proxy_mode === 'pool_random'
                            ? '每份新 Flow 任务随机取一个可用出口，整份任务保持同一出口；已开始的任务不变。登录、刷新与 Profile 仍使用下方固定绑定。'
                            : '每份新 Flow 任务使用下方账号绑定的固定出口；已开始的任务不变。登录、刷新与 Profile 绑定不变。'
                    }}
                </p>
                <label class="quick-strategy-label" :for="`account-proxy-${account.id}`">账号绑定出口</label>
                <div class="quick-proxy-picker">
                    <el-select
                        :id="`account-proxy-${account.id}`"
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
                <p v-if="proxyChanged" class="quick-hint">上方为待绑定出口，保存后生效。</p>
                <p v-if="picked?.last_check_ok === false" class="quick-hint">此出口最近检测连接异常，请确认当前连接状态。</p>
                <p v-if="account.browser_profile_source === 'cloak'" class="quick-hint">可主动切换 Cloak 代理，验证通过后重新绑定；验证失败保留原出口。</p>
            </section>
            <section class="quick-proxy-details">
                <h3>出口信息</h3>
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
                <p v-else class="quick-hint">尚未绑定出口。</p>
            </section>
        </section>
        <p v-if="error" class="quick-error" role="alert">{{ error }}</p>
        <p v-else-if="saved" class="quick-saved" role="status">{{ saved }}</p>
    </section>
</template>

<style scoped>
.account-quick-panel {
    box-sizing: border-box;
    padding: 0 20px 16px;
    background: var(--ground);
    color: var(--ink);
}
.account-quick-grid {
    display: grid;
    grid-template-columns: minmax(0, 1fr) minmax(0, 1fr);
    gap: 28px;
}
.account-quick-tabs { display: flex; gap: 24px; border-bottom: 1px solid var(--line); margin-bottom: 16px; }
.account-quick-tabs button { padding: 12px 2px 10px; background: transparent; border: 0; border-bottom: 2px solid transparent; color: var(--muted); cursor: pointer; font-size: 13px; }
.account-quick-tabs button[aria-selected='true'] { color: var(--accent); border-bottom-color: var(--accent); font-weight: 600; }
.account-quick-tabs button:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
.quick-account-section { min-width: 0; }
.quick-cookie-toolbar { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 8px; font-size: 12px; }
.quick-cookie-toolbar strong { font-weight: 550; color: var(--muted); }
.quick-cookie-editor { display: grid; grid-template-columns: 42px minmax(0, 1fr); min-height: 140px; max-height: 320px; overflow: auto; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); }
.quick-cookie-editor:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
.quick-cookie-gutter, .quick-cookie-content { padding: 10px 12px; font: 12px/20px ui-monospace, SFMono-Regular, Consolas, monospace; }
.quick-cookie-gutter { position: sticky; left: 0; z-index: 1; text-align: right; user-select: none; color: var(--muted); background: var(--ground); border-right: 1px solid var(--line); }
.quick-cookie-gutter span { display: block; }
.quick-cookie-content { margin: 0; min-width: max-content; white-space: pre; tab-size: 2; }
.quick-cookie-content code { font: inherit; }
.cookie-key { color: var(--ink); font-weight: 600; }
.cookie-string { color: var(--accent); }
.cookie-number, .cookie-literal { color: var(--muted); }
.quick-cookie-empty { margin: 0; padding: 20px 12px; border: 1px solid var(--line); border-radius: 8px; color: var(--muted); font-size: 13px; }
.quick-proxy-section, .quick-proxy-details { min-width: 0; }
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
.quick-strategy-label {
    display: block;
    color: var(--muted);
    font-size: 12px;
    margin: 14px 0 7px;
}
.quick-strategy-hint {
    margin-bottom: 14px;
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
@media (max-width: 1100px) {
    .account-quick-grid {
        grid-template-columns: 1fr;
        gap: 22px;
    }
}
@media (max-width: 700px) {
    .account-quick-panel {
        padding: 0 12px 14px;
    }
    .quick-proxy-picker {
        align-items: stretch;
        flex-direction: column;
        gap: 8px;
    }
    .quick-proxy-fields {
        gap: 10px 16px;
    }
}
</style>
