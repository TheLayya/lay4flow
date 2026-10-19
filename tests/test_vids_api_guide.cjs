const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {spawnSync} = require('node:child_process');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'static/vids-api-guide.html'), 'utf8');
const check = (pattern, message) => assert(pattern.test(html), message);
const decode = value => value.replaceAll('&quot;', '"').replaceAll('&#39;', "'")
  .replaceAll('&lt;', '<').replaceAll('&gt;', '>').replaceAll('&amp;', '&');
const sections = Object.fromEntries([...html.matchAll(/<section\b[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/section>/g)]
  .map(match => [match[1], match[2]]));
const ids = [...html.matchAll(/\bid="([^"]+)"/g)].map(match => match[1]);
assert.equal(ids.length, new Set(ids).size, 'duplicate document anchors');
for (const match of html.matchAll(/href="#([^"]+)"/g)) assert(ids.includes(match[1]), `missing anchor ${match[1]}`);
for (const id of ['overview', 'console-accounts', 'controls', 'console-document', 'auth', 'models', 'generation', 'image', 'image-edit', 'speech', 'video', 'receipts', 'errors', 'advanced']) {
  assert(sections[id], `missing section ${id}`);
}
for (const link of ['/manage', '/vids', '/vids-accounts', '/vids-api-guide', '/providers/vids/api-docs', '/providers/vids/openapi.json']) {
  assert(html.includes(`href="${link}"`), `missing navigation link ${link}`);
}
check(/<title>Vids API 文档/, 'Vids title');
check(/@media\(max-width:720px\)/, 'responsive layout');
check(/Bearer YOUR_API_KEY/, 'placeholder shared key');
check(/Idempotency-Key/, 'idempotency contract');
check(/1–200/, 'idempotency length');
check(/unknown.*(?:不重发|禁止换 key)|禁止换 key.*unknown/s, 'unknown must not resubmit');
check(/原账号绑定、原 runtime 目录和会话加密密钥/, 'preserve original receipt ownership');
check(/disabled/, 'disabled account receipt limits');
check(/account_not_found/, 'unbound accounts');
check(/音乐：排除/, 'music excluded');
check(/真实多账号生成、跨天会话寿命和生产部署仍待验/, 'remaining real acceptance work');
check(/docs\/VIDS_API\.md/, 'source reference');
assert(/新导入.*Profile 成功验证后.*自动尝试 Vids 接入/.test(sections['console-accounts']), 'future accounts auto-onboard after import or Profile verification');
assert(/需要登录、验证码/.test(sections['console-accounts']), 'login and CAPTCHA require admin handling');
assert(/可能打开 Profile/.test(sections['console-accounts']), 'session repair may open the Profile');
assert(/不提交生成/.test(sections['console-accounts']) && /不会重试已提交的付费生成/.test(sections['console-accounts']), 'onboarding and repair must not generate or replay paid work');
assert(/不返回 Cookie、会话密钥、编辑文档地址或私有文件路径/.test(sections['console-accounts']), 'account management must keep session secrets private');
assert(/保留上次余额并标记过期/.test(sections['console-accounts']), 'failed quota reads preserve stale cache honestly');
assert(/付费提交前.*只读会话\/额度预检/.test(sections['console-accounts']) && /最多修复一次同账号会话/.test(sections['console-accounts']), 'new generation preflight repairs only the selected account once before paid submission');
assert(/历史收据重放直接读取原收据，不做该预检/.test(sections['console-accounts']), 'durable receipt replays must skip upstream preflight');
assert(!/<a\b[^>]*href="[^"]*VIDS_API\.md"/.test(html), 'unpublished source guide must not use a broken URL');
assert(!/fetch\s*\(|XMLHttpRequest|document\.cookie|localStorage|sessionStorage|<script\b[^>]*src=/.test(html),
  'guide must not make requests, load remote scripts or read browser credentials');

// Parse Python syntax only; no imports, settings, services, accounts or network calls.
const sourceCheck = spawnSync('python', ['-c', `
import ast, json
from pathlib import Path
tree = ast.parse(Path('src/api/vids_media.py').read_text(encoding='utf-8'))
factory = next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == 'create_vids_media_router')
# Public routes are declared directly in the factory; document routes exist only in auto_pool.
functions = {node.name: node for node in factory.body if isinstance(node, ast.AsyncFunctionDef)}
functions.update({node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))})
console_functions = [child for node in factory.body if isinstance(node, ast.If) and any(isinstance(item, ast.Name) and item.id == 'auto_pool' for item in ast.walk(node.test)) for child in node.body if isinstance(child, ast.AsyncFunctionDef)]
prefix = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PUBLIC_PREFIX' for t in node.targets))
console_prefix = next(ast.literal_eval(node.value) for node in tree.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'CONSOLE_PREFIX' for t in node.targets))
def routes(nodes):
    result = []
    for node in nodes:
        for decorator in node.decorator_list:
            if isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Attribute) and isinstance(decorator.func.value, ast.Name) and decorator.func.value.id == 'router':
                result.append([decorator.func.attr.upper(), ast.literal_eval(decorator.args[0])])
    return result
def body_field(node, name):
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name) and node.func.value.id == 'body' and node.func.attr == 'get' and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == name
def compared(function, name):
    for node in ast.walk(functions[function]):
        if isinstance(node, ast.Compare) and (body_field(node.left, name) or isinstance(node.left, ast.Name) and node.left.id == name):
            try:
                return ast.literal_eval(node.comparators[0])
            except ValueError:
                pass
    for node in ast.walk(functions[function]):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in functions:
            result = compared(node.func.id, name)
            if result is not None:
                return result
def allowed(function):
    for node in ast.walk(functions[function]):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Sub) and isinstance(node.right, ast.Set):
            return sorted(ast.literal_eval(node.right))
gateway = ast.parse(Path('src/services/vids/gateway.py').read_text(encoding='utf-8'))
specs = next(ast.literal_eval(node.value) for node in gateway.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'VERIFIED_VIDEO_SPECIFICATIONS' for t in node.targets))
gateway_constants = {}
for node in gateway.body:
    if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name):
        try:
            gateway_constants[node.targets[0].id] = ast.literal_eval(node.value)
        except ValueError:
            pass
videos = ast.parse(Path('src/api/vids_videos.py').read_text(encoding='utf-8'))
functions.update({node.name: node for node in videos.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))})
constants = {node.targets[0].id: ast.literal_eval(node.value) for node in videos.body if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name)}
account_routes = []
for filename in ('vids.py', 'vids_onboarding.py'):
    admin_tree = ast.parse(Path('src/api/admin_routes', filename).read_text(encoding='utf-8'))
    account_routes.extend(routes(node for node in ast.walk(admin_tree) if isinstance(node, ast.AsyncFunctionDef)))
schemas = ast.parse(Path('src/api/vids_schemas.py').read_text(encoding='utf-8'))
image_edit = next(node.value for node in schemas.body if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'IMAGE_EDIT_REQUEST' for t in node.targets))
edit_properties = next(value for key, value in zip(image_edit.keys, image_edit.values) if isinstance(key, ast.Constant) and key.value == 'properties')
excluded_edit_fields = set()
for node in ast.walk(edit_properties):
    if isinstance(node, ast.Compare) and isinstance(node.ops[0], ast.NotIn) and isinstance(node.comparators[0], ast.Set):
        excluded_edit_fields.update(ast.literal_eval(node.comparators[0]))
edit_fields = sorted((set(allowed('validate_image_options')) - excluded_edit_fields) | {key.value for key in edit_properties.keys if isinstance(key, ast.Constant)})
print(json.dumps({'prefix':prefix, 'routes':routes(functions.values()), 'consolePrefix':console_prefix, 'consoleRoutes':routes(console_functions), 'accountRoutes':account_routes, 'specifications':specs['text'], 'allSpecifications':specs, 'statuses':constants['STATUS'], 'videoFields':sorted(constants['FIELDS']), 'imageFields':allowed('validate_image_options'), 'imageEditFields':edit_fields, 'speechFields':allowed('speech'), 'imageModel':compared('validate_image_options','model'), 'speechModel':compared('speech','model'), 'videoModel':compared('read_vids_video_request','model'), 'imageSizes':compared('validate_image_options','size'), 'imageFormats':compared('validate_image_options','response_format'), 'imageAspects':list(gateway_constants['IMAGE_ASPECTS']), 'imageStyles':gateway_constants['IMAGE_STYLES'], 'videoSizes':list(gateway_constants['VIDEO_SIZES']), 'videoShots':gateway_constants['VIDEO_SHOT_MODES'], 'speechVoices':ast.literal_eval(next(n.value for n in ast.parse(Path('src/services/vids/client.py').read_text(encoding='utf-8')).body if isinstance(n, ast.Assign) and any(isinstance(t, ast.Name) and t.id == 'PRESET_VOICES' for t in n.targets))), 'speechFormat':compared('speech','response_format')}))
`], {cwd: root, encoding: 'utf8'});
assert.equal(sourceCheck.status, 0, sourceCheck.stderr);
const source = JSON.parse(sourceCheck.stdout);
const documented = [...html.matchAll(/class="endpoint" data-method="([^"]+)" data-path="([^"]+)"/g)]
  .map(match => [match[1], match[2]]);
const sort = rows => rows.map(row => row.join(' ')).sort();
assert.equal(source.routes.length, 12, 'public provider has exactly 12 media operations');
assert.deepEqual(sort(documented), sort(source.routes), 'documented endpoint methods and paths must match Python routes');
for (const [, route] of source.routes) assert(html.includes(source.prefix + route), `missing full source route ${route}`);
const consoleDocument = [...html.matchAll(/data-console-method="([^"]+)" data-console-path="([^"]+)"/g)]
  .map(match => [match[1], match[2]]);
assert.equal(consoleDocument.length, 4, 'console documents exactly four document operations');
assert.deepEqual(sort(consoleDocument), sort(source.consoleRoutes.map(([method, route]) => [method, source.consolePrefix + route])),
  'console document methods and paths must match auto_pool routes');
assert(!documented.some(([, route]) => route.includes('/document/')), 'console document operations must not enter public provider documentation');
const accountRoutes = [...html.matchAll(/data-account-method="([^"]+)" data-account-path="([^"]+)"/g)]
  .map(match => [match[1], match[2]]);
assert.deepEqual(sort(accountRoutes), sort(source.accountRoutes), 'account onboarding and quota documentation must match admin routes');
assert(!documented.some(([, route]) => route.includes('/onboard/') || route.includes('/quota-refresh')), 'account management must not enter public provider documentation');
for (const [section, fields] of [['image', source.imageFields], ['speech', source.speechFields], ['video', source.videoFields]]) {
  const documentedFields = [...sections[section].matchAll(/<tr><td><code>([^<]+)<\/code><\/td>/g)]
    .map(match => match[1]).sort();
  assert.deepEqual(documentedFields, fields, `${section} parameters must match the source allowlist`);
}
const editFields = [...sections['image-edit'].matchAll(/<tr><td>((?:<code>[^<]+<\/code>| \/ )+)<\/td>/g)]
  .flatMap(match => [...match[1].matchAll(/<code>([^<]+)<\/code>/g)].map(field => field[1])).sort();
assert.deepEqual(editFields, source.imageEditFields, 'image edit multipart parameters must match the source schema');
for (const field of source.imageFields.filter(field => !['aspect_ratio', 'style'].includes(field))) assert(editFields.includes(field), `missing shared edit option ${field}`);
assert(/multipart\/form-data/.test(sections['image-edit']) && /11 MiB/.test(sections['image-edit']) && /10 MiB/.test(sections['image-edit']), 'image edit request and file limits');
assert(/files=/.test(sections['image-edit']) && /image=@input\.png/.test(sections['image-edit']), 'image edit examples upload a real local file');
assert(/无需读取可编辑图片、场景或文档内部 ID/.test(sections['image-edit']), 'API callers must not discover document materials');
assert(/image_upload_unknown/.test(sections['image-edit']) && /同键仅重放/.test(sections['image-edit']) && /不会重复上传或继续付费/.test(sections['image-edit']), 'unknown raw image edit preserves its original receipt without automatic replay');
assert(/"kind":"image","mode":"edit".*"image_base64"/.test(sections['image-edit']), 'generic image edits accept raw Base64');
for (const [section, model] of [['image', source.imageModel], ['speech', source.speechModel], ['video', source.videoModel]]) {
  assert(sections[section].includes(`<code>${model}</code>`), `missing ${section} model`);
}
for (const value of [...source.imageSizes, ...source.imageFormats]) assert(sections.image.includes(`<code>${value}</code>`), `missing image enum ${value}`);
for (const value of [...source.speechVoices, source.speechFormat]) assert(sections.speech.includes(`<code>${value}</code>`), `missing speech enum ${value}`);
for (const value of [...source.imageAspects, ...source.imageStyles]) assert(sections.image.includes(`<code>${value}</code>`), `missing image native option ${value}`);
for (const value of [...source.videoSizes, ...source.videoShots, 3, 4, 5, 6, 7, 8, 9, 10]) assert(sections.video.includes(`<code>${value}</code>`), `missing supported video option ${value}`);
assert(sections.controls.includes('supported_specifications') && sections.controls.includes('verified_specifications') && /尚未逐项真实付费实测/.test(sections.controls), 'supported parameters must remain distinct from paid verification');
const combinations = [...html.matchAll(/data-seconds="(\d+)" data-size="([^"]+)"/g)].map(match => [Number(match[1]), match[2]]);
assert.deepEqual(combinations, source.specifications, 'video specification combinations must match source');
const controlSpecifications = [...html.matchAll(/data-control-mode="([^"]+)" data-control-seconds="(\d+)" data-control-size="([^"]+)"/g)]
  .map(match => [match[1], Number(match[2]), match[3]]);
assert.deepEqual(sort(controlSpecifications), sort(Object.entries(source.allSpecifications).flatMap(([mode, rows]) => rows.map(row => [mode, ...row]))),
  'all console video modes and specification combinations must match source');
for (const field of ['source_id', 'vids_image_', 'vids_scene_', 'image_base64', 'images_base64']) {
  assert((sections.controls + sections['console-document']).includes(field), `missing console material contract ${field}`);
}
assert(sections.controls.includes('10 MiB') && sections.controls.includes('1–7') && sections.controls.includes('1–3'), 'reference image count and total decoded limit');
assert(sections.controls.includes('上传 MP4') && sections.controls.includes('3–60') && sections.controls.includes('末 10 秒'), 'all source video operations upload client MP4');
assert(sections.video.includes('input_video') && sections.video.includes('video_base64') && sections.video.includes('50 MiB') && sections.video.includes('61 MiB') && sections.video.includes('85 MiB'), 'MP4 upload and bounded transport documentation');
assert(/仅 text\/ingredients\/edit 接受非 auto/.test(sections.video), 'shot control applies only to supported video modes');
assert(sections.video.includes('源方向 720p') && sections.video.includes('源时长四舍五入后最多 10'), 'video defaults depend on uploaded input');
assert(sections['image-edit'].includes('aspect_ratio') && sections['image-edit'].includes('style') && sections['image-edit'].includes('被拒绝'), 'editing must not promise native output ratio or style');
for (const name of ['Avatar', '首尾帧', '模型切换']) assert(sections.advanced.includes(name), `missing incomplete capability ${name}`);
assert(sections.image.includes('n=1–4') && sections.image.includes('outputs') && sections.image.includes('?index=1/2/3'), 'multiple image outputs and indexed downloads must be documented');
assert(sections.image.includes('不再次生成') && sections.image.includes('配额扣减方式尚未'), 'multiple output recovery and quota verification must be explicit');
for (const name of ['16143507', '16443280', 'gemini-omni-11-flash']) assert(html.includes(name), `missing official evidence link ${name}`);
assert(sections['console-document'].includes('unknown') && sections['console-document'].includes('verify'), 'unknown insertion must recover by verify');
assert(/选择本地原图、填写提示词、点击生成/.test(sections.controls) && /无需用户先读取或选择场景、单独保存图片/.test(sections.controls), 'local image editing must use one generation action without scene setup');
assert(/一次请求发送.*image_base64/.test(sections.controls), 'console sends one raw image API request');
assert(/后台自动读取可保存场景、选择首个场景、上传并确认原图，再提交编辑生成/.test(sections['console-document']), 'local image preparation happens in the backend');
assert(/不再次上传，也不提交付费生成/.test(sections['console-document']) && /保留原生成收据/.test(sections['console-document']), 'unknown raw image preparation must stop and preserve the generation receipt');
assert(!/class="doc-nav"[\s\S]*?<a href="#console-document"/.test(html.split('</aside>')[0]), 'historical document operations must not be a primary navigation step');
const statuses = Object.fromEntries([...html.matchAll(/data-status="([^"]+)" data-video-status="([^"]+)"/g)]
  .map(match => [match[1], match[2]]));
assert.deepEqual(statuses, source.statuses, 'video receipt status mapping must match source');

const pythonExamples = [];
for (const match of html.matchAll(/<pre><code([^>]*)>([\s\S]*?)<\/code><\/pre>/g)) {
  const code = decode(match[2]);
  if (match[1].includes('data-json')) JSON.parse(code);
  if (match[1].includes('data-python')) pythonExamples.push(code);
}
assert(pythonExamples.length >= 10, 'request examples cover standard creation and receipt lifecycle');
const compile = spawnSync('python', ['-c', 'import json,sys; [compile(x, "doc-example", "exec") for x in json.loads(sys.stdin.buffer.read().decode("utf-8"))]'],
  {input: JSON.stringify(pythonExamples), encoding: 'utf8'});
assert.equal(compile.status, 0, compile.stderr);

// The only interaction is navigation/copying; exercise hash fallback and denied clipboard.
const links = Object.keys(sections).map(id => ({hash: `#${id}`, setAttribute(name, value) { this[name] = value; }}));
const nodes = Object.fromEntries(ids.map(id => [id, {id, closest() { return nodes[id] || nodes.overview; }}]));
nodes['video-content'].closest = () => nodes.receipts;
let clipboard = '';
const timers = [];
const button = {textContent: '复制', closest() { return {querySelector() { return {textContent: 'placeholder example'}; }}; },
  addEventListener(type, callback) { this[type] = callback; }};
const context = vm.createContext({
  document: {querySelectorAll(selector) { return selector === '.doc-nav a' ? links : selector === '.copy' ? [button] : []; }, getElementById: id => nodes[id]},
  location: {hash: '#video-content'},
  window: {addEventListener(type, callback) { this[type] = callback; }},
  navigator: {clipboard: {async writeText(value) { clipboard = value; }}},
  setTimeout: callback => timers.push(callback)
});
vm.runInContext(html.match(/<script>([\s\S]*?)<\/script>/)[1], context);
assert.equal(links.find(link => link.hash === '#receipts')['aria-current'], 'location');
context.location.hash = '#missing';
context.window.hashchange();
assert.equal(links[0]['aria-current'], 'location');
(async () => {
  await button.click();
  assert.equal(clipboard, 'placeholder example');
  assert.equal(button.textContent, '已复制');
  timers.shift()();
  context.navigator.clipboard.writeText = async () => { throw new Error('denied'); };
  await button.click();
  assert.equal(button.textContent, '请选中文本复制');
  console.log('Vids API guide: routes, enums, examples and safe interactions passed');
})().catch(error => { console.error(error); process.exitCode = 1; });
