import assert from 'node:assert/strict'
import { test } from 'node:test'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import * as Vue from 'vue'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'

const source = readFileSync(new URL('../src/views/GuideView.vue', import.meta.url), 'utf8')
const modelSource = readFileSync(new URL('../../static/js/model_catalog.js', import.meta.url), 'utf8')
const skuSource = readFileSync(new URL('../../static/js/media_skus.js', import.meta.url), 'utf8')

function makeGuide(platform = 'flow') {
  const context = vm.createContext({ ...Vue, window: { location: { origin: 'http://fixture.local' } }, useRoute: () => ({ query: { platform } }), ElMessage: { success() {} }, notifyError() {}, modelSource, skuSource })
  for (const icon of ['Search', 'CopyDocument', 'Link', 'Document', 'Connection', 'ArrowRight', 'Picture', 'VideoCamera', 'Microphone', 'Collection', 'Check', 'InfoFilled']) context[icon] = {}
  const { descriptor, errors } = parse(source)
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'guide-test' })
  const template = compileTemplate({ source: descriptor.template.content, filename: 'GuideView.vue', id: 'guide-test', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
  vm.runInContext(script.content.replace(/^import .*$/gm, '').replace('export default {', 'globalThis.component = {'), context)
  return context.component.setup({}, { expose() {} })
}

async function executeFetchSample(code, files = []) {
  let captured
  const execute = new Function('fetch', 'document', 'FormData', 'console', `return (async () => { ${code} })()`)
  await execute(async (path, options) => { captured = { path, options }; return new Response('{}', { headers: { 'Content-Type': 'application/json' } }) }, { querySelector: () => ({ files }) }, FormData, { log() {} })
  return captured
}

test('Vids entry opens the public Bearer directory and schema URLs', () => {
  const guide = makeGuide('vids')
  assert.equal(guide.channel.value, 'vids')
  assert.equal(guide.baseURL.value, 'http://fixture.local/providers/vids/v1')
  assert.equal(guide.schemaURL.value, '/providers/vids/openapi.json')
  assert.equal(guide.swaggerURL.value, '/providers/vids/api-docs')
  assert.equal(guide.current.value.path, '/providers/vids/v1/models')
})

test('all public Fetch samples are executable and use their advertised method and path', async () => {
  const guide = makeGuide()
  for (const entries of Object.values(guide.endpoints)) for (const entry of entries.filter(item => !item.binary && !item.multipart)) {
    const captured = await executeFetchSample(guide.codeFor(entry, 'fetch'))
    assert.equal(captured.path, 'http://fixture.local' + entry.path)
    assert.equal(captured.options.method, entry.method)
    assert.equal(captured.options.headers.Authorization, 'Bearer YOUR_API_KEY')
    if (entry.idempotent) assert.equal(captured.options.headers['Idempotency-Key'], 'my-creation-001')
    if (entry.body) assert.equal(JSON.parse(captured.options.body).model, entry.body.model)
  }
})

test('multipart examples attach image bytes and leave the boundary to the browser', async () => {
  const guide = makeGuide()
  const original = new File(['synthetic PNG bytes'], 'input.png', { type: 'image/png' })
  for (const entries of Object.values(guide.endpoints)) for (const entry of entries.filter(item => item.multipart)) {
    const captured = await executeFetchSample(guide.codeFor(entry, 'fetch'), [original])
    assert.equal(captured.options.headers['Content-Type'], undefined)
    assert.equal(await captured.options.body.get('image').text(), 'synthetic PNG bytes')
    assert.equal(captured.options.body.get('prompt'), entry.body.prompt)
    if (entry.body.generationConfig) assert.equal(JSON.parse(captured.options.body.get('generationConfig')).imageConfig.aspectRatio, entry.body.generationConfig.imageConfig.aspectRatio)
    if (entry.idempotent) assert.equal(captured.options.headers['Idempotency-Key'], 'my-creation-001')
  }
})

test('Python samples preserve Boolean words inside text while translating literal JSON values', () => {
  const guide = makeGuide()
  const python = guide.pythonJSON({ description: 'true false null, quoted "true"', enabled: true, disabled: false, empty: null })
  assert.ok(python.includes('"true false null, quoted \\"true\\""'))
  assert.ok(python.includes('"enabled": True'))
  assert.ok(python.includes('"disabled": False'))
  assert.ok(python.includes('"empty": None'))
})

test('fixed SKU samples name locked models and do not override their billing specification', async () => {
  const guide = makeGuide()
  guide.channel.value = 'sku'; await Vue.nextTick()
  assert.ok(guide.current.value.response.data[0].id.startsWith('flow-'))
  for (const entry of guide.endpoints.sku.filter(item => item.body)) {
    assert.ok(entry.body.model.startsWith('flow-'))
    assert.ok(!entry.body.model.endsWith('-standard'))
    if (entry.response?.model) assert.ok(!entry.response.model.endsWith('-standard'))
    const config = entry.body.generationConfig || {}
    assert.equal(config.imageConfig?.imageSize, undefined)
    assert.equal(config.videoConfig?.quality, undefined)
    assert.equal(config.videoConfig?.resolution, undefined)
    assert.ok(entry.fields.some(item => item.name.includes('model') && item.required))
  }
  assert.ok(guide.modelCatalog.value.every(item => !item.id.endsWith('-standard')))
  assert.ok(guide.modelCatalog.value.some(item => item.id.endsWith('-relaxed')))
})

test('cURL multipart samples escape apostrophes without changing uploaded filenames', () => {
  const guide = makeGuide()
  const entry = { ...guide.endpoints.vids.find(item => item.id === 'image-edit'), body: { model: 'vids-image', image: '@input.png', prompt: "Keep the character's blue coat" } }
  const curl = guide.codeFor(entry, 'curl')
  assert.ok(curl.includes("character'\\''s"))
  assert.ok(curl.includes("-F 'image=@input.png'"))
})

test('the public guide keeps valid Chinese labels instead of encoding replacement markers', () => {
  const { descriptor } = parse(source)
  assert.doesNotMatch(descriptor.template.content, /\?{3,}/)
  for (const label of ['好创作，也要好接入。', '请求参数', '请求示例', '返回结构', '完整 Flow 文档', '完整 Vids 文档']) assert.ok(descriptor.template.content.includes(label), `missing Chinese label: ${label}`)
})
