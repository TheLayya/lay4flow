import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import { computed, reactive, ref, watch } from 'vue'

function logsView() {
    const filename = new URL('../src/views/LogsView.vue', import.meta.url)
    const { descriptor } = parse(readFileSync(filename, 'utf8'))
    const compiled = compileScript(descriptor, { id: 'Logs' }).content
    const imports = []
    for (const match of descriptor.scriptSetup.content.matchAll(/import\s+([\s\S]*?)\s+from\s+['"][^'"]+['"]/g)) {
        const named = match[1].match(/\{([\s\S]*?)\}/)?.[1]
        if (named)
            imports.push(
                ...named
                    .split(',')
                    .map((value) => value.trim())
                    .filter(Boolean),
            )
        const defaultName = match[1]
            .replace(/\{[\s\S]*?\}/, '')
            .replace(/,\s*$/, '')
            .trim()
        if (defaultName) imports.push(defaultName)
    }
    const context = Object.fromEntries(imports.map((name) => [name, {}]))
    Object.assign(context, {
        computed,
        reactive,
        ref,
        watch,
        onMounted() {},
        onUnmounted() {},
        parseDate: (value) => new Date(value),
        setInterval() {},
        clearInterval() {},
        document: { addEventListener() {}, removeEventListener() {} },
    })
    const executable =
        compiled
            .replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"];?\s*$/gm, '')
            .replace('export default', 'const component =') + '\ncomponent'
    return runInNewContext(executable, context).setup({}, { expose() {} })
}
const plain = (value) => JSON.parse(JSON.stringify(value))

test('current attempt history has one total budget while legacy cap remains historical only', () => {
    const source = readFileSync(new URL('../src/views/LogsView.vue', import.meta.url), 'utf8')
    assert.match(source, /attemptHistory\.version === 1 && Number\(attemptHistory\.accounts_limit\)/)
    assert.match(source, /所有账号共用此总次数/)
    const state = logsView()
    state.detail.value = { attempt_history: {
        version: 2, limit: 20, total_attempts: 20, retry_count: 19,
        account_count: 20, account_allocations: 20, attempts: [],
    } }
    assert.equal(state.attemptHistory.value.limit, 20)
    assert.equal('accounts_limit' in state.attemptHistory.value, false)
    assert.deepEqual(plain(state.detailAttempts.value), { attempts: 20, retries: 19, accounts: 20 })
})

test('retry summaries distinguish unknown historical counts from a request with no attempts', () => {
    const s = logsView()
    assert.deepEqual(plain(s.attemptSummary({})), { attempts: null, retries: null, accounts: null })
    assert.deepEqual(plain(s.attemptSummary({ attempt_count: 0, retry_count: 0, account_count: 0 })), {
        attempts: 0,
        retries: 0,
        accounts: 0,
    })
    assert.deepEqual(plain(s.attemptSummary({ attempt_count: 3, account_count: 2 })), {
        attempts: 3,
        retries: 2,
        accounts: 2,
    })
    for (const attempt_count of [-1, '3', NaN, Infinity, 1.5])
        assert.equal(s.attemptSummary({ attempt_count }).attempts, null)
})

test('retry details read structured history and preserve order, errors and same-account decisions', () => {
    const s = logsView()
    const attempts = [
        {
            attempt: 1,
            account_id: 3,
            status: 'failed',
            stage: 'captcha',
            next_action: 'retry_same_account',
            duration_ms: 100.25,
            error_code: 'SYNTHETIC_CAPTCHA',
            error: 'synthetic captcha failure',
        },
        {
            attempt: 2,
            account_id: 3,
            status: 'failed',
            stage: 'submit',
            next_action: 'switch_account',
            duration_ms: 2000,
            rule_action: 'LIMIT',
            rule_keyword: 'synthetic quota',
        },
        { attempt: 3, account_id: 8, status: 'success', stage: 'submit', next_action: 'completed', duration_ms: 1500 },
    ]
    s.detail.value = {
        response_body: JSON.stringify({
            attempt_history: {
                total_attempts: 3,
                retry_count: 2,
                account_count: 2,
                attempts,
            },
        }),
    }
    assert.deepEqual(plain(s.detailAttempts.value), { attempts: 3, retries: 2, accounts: 2 })
    assert.deepEqual(plain(s.attemptRows.value), attempts)
    assert.equal(s.attemptAction(s.attemptRows.value[0]), '使用同一账号重试')
    assert.equal(s.attemptAction(s.attemptRows.value[1]), '跳过该账号，切换账号')
    assert.equal(s.ruleAction(s.attemptRows.value[1]), '限速冷却')
    assert.equal(s.ruleEffect({ rule_action: 'LIMIT', rule_seconds: 86400 }), '账号冷却 24 小时')
    assert.equal(s.ruleEffect({ rule_action: 'LIMIT', rule_seconds: 300 }), '账号冷却 5 分钟')
    assert.equal(s.ruleEffect({ rule_action: 'LIMIT', rule_seconds: 3 }), '账号冷却 3 秒')
    assert.equal(s.ruleEffect({ rule_action: 'LIMIT', rule_seconds: 0 }), '仅在本次请求中跳过该账号')
    assert.equal(s.ruleEffect({ rule_action: 'LIMIT' }), '')
    assert.equal(s.attemptDuration(s.attemptRows.value[0]), '0.10 s')
    assert.equal(s.attemptDuration(s.attemptRows.value[1]), '2.00 s')
})

test('explicit detail history wins over response fallback and malformed history cannot break rendering', () => {
    const s = logsView()
    s.detail.value = {
        attempt_count: 0,
        attempt_history: { total_attempts: 0, attempts: [null, false, 'invalid'] },
        response_body: JSON.stringify({ attempt_history: { total_attempts: 99, attempts: [{ attempt: 99 }] } }),
    }
    assert.equal(s.detailAttempts.value.attempts, 0)
    assert.equal(s.attemptRows.value.length, 0)
    s.detail.value = { response_body: '{invalid' }
    assert.equal(s.detailAttempts.value.attempts, null)
    assert.equal(s.attemptRows.value.length, 0)
    assert.equal(s.attemptDuration({ duration_ms: Infinity }), '耗时未记录')
    assert.equal(s.attemptStage({ stage: 'polling' }), '等待生成结果')
    assert.equal(s.attemptStage({ stage: 'submitting_video' }), '提交视频生成')
    assert.equal(s.attemptStatus({ status: 'in_progress' }), '进行中')
    assert.equal(s.classify({ status_code: 429, status_text: 'retrying_account' }), 'processing')
})

test('attempt diagnostics distinguish missing evidence from recorded zero and validate transport codes', () => {
    const s = logsView()
    assert.deepEqual(plain(s.attemptTimings({})), [])
    assert.equal(s.attemptTransport({}), '')
    assert.equal(s.submissionCount({}), null)
    assert.equal(s.submissionState({}), '')
    assert.equal(s.submissionState({ submission_state: 'rejected' }), '上游明确拒绝')
    assert.equal(s.submissionState({ submission_state: 'unknown' }), '受理情况未知')
    assert.equal(s.submissionState({ submission_state: 'accepted' }), '上游已受理')
    assert.equal(s.submissionState({ submission_state: 'private-session' }), '')
    for (const submission_state of ['__proto__', 'toString', [], {}, true])
        assert.equal(s.submissionState({ submission_state }), '')
    assert.deepEqual(plain(s.attemptTimings({ captcha_ms: 3125, captcha_to_submit_ms: 0, submit_ms: 250 })), [
        { label: '取码', value: '3.13 s' }, { label: '取码→提交', value: '0.00 s' }, { label: '提交响应', value: '0.25 s' },
    ])
    assert.equal(s.attemptTransport({ http_status: 200, rpc_code: 8 }), 'HTTP 200 · RPC 8')
    assert.equal(s.attemptTransport({ http_status: 503, rpc_code: 0 }), 'HTTP 503 · RPC 0')
    assert.equal(s.submissionCount({ submission_count: 0 }), 0)
    for (const invalid of [true, '12', -1, NaN, Infinity, 604800001])
        assert.deepEqual(plain(s.attemptTimings({ captcha_ms: invalid, captcha_to_submit_ms: invalid, submit_ms: invalid })), [])
    for (const invalid of [true, '200', -1, 200.5, 600])
        assert.equal(s.attemptTransport({ http_status: invalid, rpc_code: invalid }), '')
    for (const invalid of [true, '1', -1, 1.5, 101])
        assert.equal(s.submissionCount({ submission_count: invalid }), null)
})

test('Vids logs label generation kinds and distinguish unresolved receipts from processing and failure', () => {
    const s = logsView()
    for (const [kind, text] of [['image', '图片'], ['video', '视频'], ['speech', '语音']])
        assert.equal(s.operationLabel({ operation: `vids_generate_${kind}` }), `Vids ${text}生成`)
    for (const status_text of ['unknown', 'download_failed']) {
        assert.equal(s.classify({ status_text, status_code: 202 }), 'attention')
        assert.equal(s.statusType({ status_text }), 'warning')
    }
    assert.equal(s.statusLabel({ status_text: 'unknown' }), '受理结果待确认')
    assert.equal(s.statusLabel({ status_text: 'download_failed' }), '作品下载待恢复')
    assert.equal(s.classify({ status_text: 'pending', status_code: 202 }), 'processing')
    assert.equal(s.statusLabel({ status_text: 'received', status_code: 102 }), '已受理，等待作品')
    assert.equal(s.statusLabel({ status_text: 'generating', status_code: 102 }), '正在生成')
    assert.equal(s.classify({ status_text: 'completed', status_code: 200 }), 'success')
    assert.equal(s.classify({ status_text: 'no_result' }), 'failed')
    assert.equal(s.classify({ status_text: 'specification_mismatch' }), 'failed')
    assert.match(s.outcome({ status_text: 'unknown' }), /保留原任务/)
    s.logs.value = [
        { status_text: 'pending' }, { status_text: 'unknown' }, { status_text: 'download_failed' },
        { status_text: 'completed', attempt_count: 1, retry_count: 0, account_count: 1 },
    ]
    assert.equal(s.stats.value.processing, 1)
    assert.equal(s.stats.value.attention, 2)
    assert.deepEqual(plain(s.attemptSummary(s.logs.value[3])), { attempts: 1, retries: 0, accounts: 1 })
})
