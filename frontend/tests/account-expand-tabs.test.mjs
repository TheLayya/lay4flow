import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import { computed, reactive } from 'vue'

const parseDateSource = readFileSync(new URL('../src/lib/api.js', import.meta.url), 'utf8').split('export function parseDate(value)')[1]
const parseDate = new Function('value', parseDateSource.slice(parseDateSource.indexOf('{') + 1, parseDateSource.lastIndexOf('}')))

const source = readFileSync(new URL('../src/components/AccountProxyBinding.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
function binding(overrides = {}) {
    const props = reactive({ account: { id: 7, email: 'demo@example.test', user_paygate_tier: 'PAYGATE_TIER_ONE', credits: 120,
        refresh_status: 'ready' }, draft: {}, proxies: [], activeTab: 'account', ...overrides })
    const code = compileScript(descriptor, { id: 'account-proxy' }).content
        .replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"];?\s*$/gm, '').replace('export default', 'const component =') + '\ncomponent'
    const events = []
    const state = runInNewContext(code, { computed, URL, parseDate }).setup(props, { expose() {}, emit: (...args) => events.push(args) })
    return { props, events, state }
}

test('expanded information reflects account metadata but has no common-setting or cadence editor', () => {
    const { state, props } = binding()
    assert.equal(state.information.value.find(row => row.label === 'Flow 套餐').value, 'Pro')
    assert.equal(state.information.value.find(row => row.label === 'Flow 积分').value, '120')
    props.account.credits = 320
    assert.equal(state.information.value.find(row => row.label === 'Flow 积分').value, '320')
    assert.match(descriptor.template.content, /role="tablist"/)
    assert.match(descriptor.template.content, /role="tabpanel"/)
    assert.doesNotMatch(descriptor.template.content, /draft\.(remark|image_enabled|video_enabled|auto_refresh_enabled|refresh_interval_minutes)/)
    assert.doesNotMatch(descriptor.template.content, /保存配置|常用配置|el-input-number/)
    assert.match(descriptor.template.content, /settings\?section=generation&account_id=/)
})

test('account information normalizes stored SQLite UTC timestamps consistently', () => {
    const { state } = binding({ account: { id: 7, last_used_at: '2026-11-10 02:00:00' } })
    assert.equal(state.information.value.find(row => row.label === '最近使用').value,
        new Date('2026-11-10T02:00:00Z').toLocaleString('zh-CN', { hour12: false }))
})

test('arrow, home and end keys select tabs and move focus without saving', () => {
    const { state, events } = binding()
    const focus = []
    let prevented = 0
    const event = key => ({ key, preventDefault() { prevented++ }, currentTarget: {
        parentElement: { querySelectorAll: () => [0, 1].map(id => ({ focus: () => focus.push(id) })) },
    } })
    state.navigateTabs(event('ArrowRight'), 0)
    state.navigateTabs(event('ArrowLeft'), 1)
    state.navigateTabs(event('End'), 0)
    state.navigateTabs(event('Home'), 1)
    assert.deepEqual(events, [['update:activeTab', 'proxy'], ['update:activeTab', 'account'], ['update:activeTab', 'proxy'], ['update:activeTab', 'account']])
    assert.deepEqual(focus, [1, 0, 1, 0])
    assert.equal(prevented, 4)
    assert.equal(events.some(([type]) => type === 'save'), false)
})
