import { test } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import { runInNewContext } from 'node:vm'
import { parse, compileScript } from '@vue/compiler-sfc'
import * as Vue from 'vue'
import { CAPTCHA_SERVICE_URL } from '../src/lib/captcha-service.js'

function app() {
    const filename = new URL('../src/App.vue', import.meta.url)
    const { descriptor } = parse(readFileSync(filename, 'utf8'))
    const compiled = compileScript(descriptor, { id: 'captcha-app' }).content
    const imported = [
        ...descriptor.scriptSetup.content.matchAll(/import\s+([\s\S]*?)\s+from\s+['"][^'"]+['"]/g),
    ].flatMap((match) =>
        match[1]
            .replace(/[{}]/g, '')
            .split(',')
            .map((name) => name.trim())
            .filter(Boolean),
    )
    const route = Vue.reactive({ path: '/accounts', meta: {}, query: {} })
    const mounted = [],
        unmounted = [],
        routes = [],
        listeners = new Map(),
        removed = []
    let result = { backend_running: true, remote_browser_configured: false }
    let reads = 0
    const media = { matches: false, addEventListener() {}, removeEventListener() {} }
    const context = {
        ...Object.fromEntries(imported.map((name) => [name, {}])),
        ...Vue,
        useRoute: () => route,
        useRouter: () => ({ push: (path) => routes.push(path) }),
        onMounted: (callback) => mounted.push(callback),
        onUnmounted: (callback) => unmounted.push(callback),
        localStorage: { getItem: () => null, setItem() {} },
        matchMedia: () => media,
        document: { documentElement: { classList: { toggle() {} } } },
        window: {
            innerWidth: 1440,
            addEventListener: (name, callback) => listeners.set(name, callback),
            removeEventListener: (name, callback) => removed.push([name, callback]),
        },
        setInterval: () => 1,
        clearInterval() {},
        api: {
            get: async () => {
                reads++
                return result
            },
        },
        CAPTCHA_SERVICE_URL,
    }
    const executable =
        compiled
            .replace(/^import\s+[\s\S]*?\s+from\s+['"][^'"]+['"];?\s*$/gm, '')
            .replace('export default', 'const component =') + '\ncomponent'
    const component = runInNewContext(executable, context, { filename: filename.pathname })
    return {
        state: component.setup({}, { expose() {} }),
        route,
        mounted,
        unmounted,
        routes,
        listeners,
        removed,
        setHealth: (health) => {
            result = health
        },
        reads: () => reads,
        template: descriptor.template.content,
    }
}

test('global captcha reminder follows configured state and stays out of public pages', async () => {
    const fixture = app()
    const s = fixture.state
    assert.equal(s.captchaMissing.value, false)
    await s.checkHealth()
    assert.equal(s.captchaMissing.value, true)
    fixture.route.path = '/studio'
    fixture.route.query.platform = 'vids'
    assert.equal(s.captchaMissing.value, true)
    assert.equal(fixture.routes.length, 0)
    fixture.route.meta.public = true
    assert.equal(s.captchaMissing.value, false)
    fixture.route.meta.public = false
    fixture.setHealth({ backend_running: true, remote_browser_configured: true })
    await s.checkHealth()
    assert.equal(s.captchaMissing.value, false)
    assert.equal(fixture.routes.length, 0)
})

test('global reminder links directly to captcha setup and saving refreshes health immediately', async () => {
    const fixture = app()
    fixture.mounted.forEach((callback) => callback())
    await Vue.nextTick()
    const listener = fixture.listeners.get('captcha-config-saved')
    assert.equal(typeof listener, 'function')
    const before = fixture.reads()
    fixture.setHealth({ remote_browser_configured: true })
    await listener()
    assert.equal(fixture.reads(), before + 1)
    assert.equal(fixture.state.captchaMissing.value, false)
    fixture.state.navigate('/settings?section=captcha')
    assert.deepEqual(fixture.routes, ['/settings?section=captcha'])
    fixture.unmounted.forEach((callback) => callback())
    assert.ok(fixture.removed.some(([name, callback]) => name === 'captcha-config-saved' && callback === listener))
    assert.match(fixture.template, /Flow 图片\/视频创作需要打码服务/)
    assert.match(fixture.template, /:href="CAPTCHA_SERVICE_URL" target="_blank" rel="noopener noreferrer"/)
})

test('captcha settings show a fixed clickable address, copy action and visible key', () => {
    const source = readFileSync(new URL('../src/views/SettingsView.vue', import.meta.url), 'utf8')
    const captcha = source.split('data-setting-card="captcha"')[1].split('</section>')[0]
    assert.equal(CAPTCHA_SERVICE_URL, 'https://cap.tk218.top/')
    assert.match(captcha, /:href="CAPTCHA_SERVICE_URL" target="_blank" rel="noopener noreferrer"/)
    assert.match(captcha, /copy\(CAPTCHA_SERVICE_URL\)/)
    assert.doesNotMatch(captcha, /v-model="captcha\.remote_browser_base_url"|show-password|type="password"/)
    assert.match(captcha, /v-model="captcha\.remote_browser_api_key"\s+type="text"/)
    assert.match(captcha, /复制 API 密钥/)
    assert.match(captcha, /@click="saveCaptcha"/)
})
