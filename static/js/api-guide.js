const origin = window.location.origin;
const sections = [...document.querySelectorAll('.doc-section')];
const navigation = [...document.querySelectorAll('.doc-nav a')];
const legacyAnchors = {quickstart: 'overview', contracts: 'parameters', code: 'images'};
const modeButtons = [...document.querySelectorAll('.mode-switch [data-doc-mode]')];
const navGroups = [...document.querySelectorAll('[data-nav-mode]')];
const lastSections = {aggregate: 'overview', business: 'downstream'};
let docMode = 'aggregate';

function sectionMode(section) {
  return section.dataset?.docMode || 'aggregate';
}

function setDocMode(mode) {
  if (!(mode in lastSections)) return;
  window.location.hash = '#' + lastSections[mode];
  showSection();
}

document.querySelectorAll('[data-origin]').forEach(element => element.textContent = origin);
document.querySelectorAll('[data-origin-suffix]').forEach(element => {
  element.textContent = origin + element.dataset.originSuffix;
});
document.querySelectorAll('pre code').forEach(element => {
  element.textContent = element.textContent.replaceAll('https://your-lay4flow-host', origin);
});

function showSection() {
  const anchor = window.location.hash.slice(1);
  const target = document.getElementById(legacyAnchors[anchor] || anchor);
  const section = target?.closest('.doc-section') || sections[0];
  docMode = sectionMode(section);
  lastSections[docMode] = section.id;
  sections.forEach(element => element.hidden = element !== section);
  modeButtons.forEach(button => button.setAttribute('aria-pressed', String(button.dataset.docMode === docMode)));
  navGroups.forEach(group => group.hidden = group.dataset.navMode !== docMode);
  navigation.forEach(link => {
    const linkedSection = document.getElementById(link.getAttribute('href').slice(1))?.closest('.doc-section');
    link.hidden = linkedSection && sectionMode(linkedSection) !== docMode;
    if (link.getAttribute('href') === '#' + section.id) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  });
  const description = document.getElementById('docModeDescription');
  if (description) description.textContent = docMode === 'business'
    ? '固定 SKU + 独立价格，面向 New API / One API 渠道。'
    : '聚合模型 + 参数选择，适合直接调用与测试。';
  document.querySelectorAll('[data-doc-schema]').forEach(link => link.href = docMode === 'business' ? '/compat/v1/openapi.json' : '/v1/openapi.json');
  document.querySelectorAll('[data-doc-swagger]').forEach(link => link.href = docMode === 'business' ? '/compat/api-docs' : '/api-docs');
  const toc = document.getElementById('pageToc');
  toc.replaceChildren();
  section.querySelectorAll('h2[id], h3[id]').forEach(heading => {
    const link = document.createElement('a');
    link.href = '#' + heading.id;
    link.textContent = heading.textContent;
    toc.appendChild(link);
  });
  document.title = section.querySelector('h1').textContent + ' · lay4flow';
  (target?.closest('.doc-section') ? target : section).scrollIntoView({block: 'start'});
}

function renderExample(example, language) {
  const code = example.querySelector('pre code');
  const url = origin + example.dataset.path;
  const method = example.dataset.method;
  const body = example.requestBody;
  const form = example.dataset.form === 'true';
  const stream = example.dataset.stream === 'true';
  if (language === 'json') {
    code.textContent = body;
  } else if (language === 'curl') {
    const lines = ["curl '" + url + "'", "  -H 'Authorization: Bearer YOUR_API_KEY'"];
    if (stream) lines.push('  -N');
    if (example.dataset.idempotency) lines.push("  -H 'Idempotency-Key: demo-video-001'");
    if (method && method !== 'GET' && method !== 'POST') lines.push('  -X ' + method);
    if (form) {
      Object.entries(JSON.parse(body)).forEach(([key,value]) => {
        const values = Array.isArray(value) ? value : [value];
        values.forEach(item => {
          const file = typeof item === 'string' && item.startsWith('@');
          const field = key + '=' + (typeof item === 'object' ? JSON.stringify(item) : String(item));
          lines.push('  ' + (file ? '-F' : '--form-string') + " '" + field.replaceAll("'", "'\\''") + "'");
        });
      });
    } else if (method === 'POST') {
      lines.push("  -H 'Content-Type: application/json'");
      lines.push("  --data-raw '" + body.replaceAll("'", "'\\''") + "'");
    }
    if (example.dataset.download) lines.push('  -o ' + example.dataset.download);
    code.textContent = lines.join(' \\\n');
  } else if (language === 'python') {
    if (form) {
      const fields = JSON.parse(body);
      const data = {};
      const files = [];
      Object.entries(fields).forEach(([name,value]) => {
        const values = Array.isArray(value) ? value : [value];
        values.forEach(item => {
          if (typeof item === 'string' && item.startsWith('@')) files.push([name,item.slice(1)]);
          else data[name] = typeof item === 'object' ? JSON.stringify(item) : String(item);
        });
      });
      const lines = ['import json', 'from contextlib import ExitStack', 'from pathlib import Path', 'import httpx', '',
        'url = ' + JSON.stringify(url), 'api_key = "YOUR_API_KEY"', 'headers = {"Authorization": "Bearer " + api_key}',
        'data = json.loads(' + JSON.stringify(JSON.stringify(data)) + ')', 'with ExitStack() as stack:', '    files = ['];
      if (example.dataset.idempotency) {
        lines.splice(1, 0, 'import uuid');
        lines.splice(lines.indexOf('with ExitStack() as stack:'), 0, 'headers["Idempotency-Key"] = str(uuid.uuid4())');
      }
      files.forEach(([name,path]) => lines.push('        (' + JSON.stringify(name) + ', (Path(' + JSON.stringify(path) + ').name, stack.enter_context(open(' + JSON.stringify(path) + ', "rb")))),'));
      lines.push('    ]', '    response = httpx.request(' + JSON.stringify(method) + ', url, headers=headers, data=data, files=files, timeout=2400)',
        '    response.raise_for_status()', '    print(json.dumps(response.json(), ensure_ascii=False, indent=2))');
      code.textContent = lines.join('\n');
      example.querySelectorAll('[data-language]').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.language === language)));
      return;
    }
    const lines = ['import json', 'import urllib.request'];
    if (example.dataset.idempotency) lines.push('import uuid');
    if (example.dataset.download) lines.push('from pathlib import Path');
    lines.push('', 'url = ' + JSON.stringify(url), 'api_key = "YOUR_API_KEY"',
      'headers = {"Authorization": "Bearer " + api_key}');
    if (example.dataset.idempotency) lines.push('headers["Idempotency-Key"] = str(uuid.uuid4())');
    if (method === 'POST') {
      lines.push('headers["Content-Type"] = "application/json"',
        'body = json.loads(' + JSON.stringify(body) + ')',
        'request = urllib.request.Request(url, data=json.dumps(body).encode("utf-8"), headers=headers)');
    } else lines.push('request = urllib.request.Request(url, headers=headers, method=' + JSON.stringify(method || 'GET') + ')');
    lines.push('with urllib.request.urlopen(request, timeout=2400) as response:');
    if (example.dataset.download) {
      lines.push('    Path(' + JSON.stringify(example.dataset.download) + ').write_bytes(response.read())');
    } else if (stream) {
      lines.push('    for line in response:', '        event = line.decode("utf-8").strip()',
        '        if event == "data: [DONE]":', '            break', '        if event.startswith("data:"):', '            print(event[5:].strip())');
    } else {
      lines.push('    result = json.load(response)', 'print(json.dumps(result, ensure_ascii=False, indent=2))');
    }
    code.textContent = lines.join('\n');
  }
  example.querySelectorAll('[data-language]').forEach(button => {
    button.setAttribute('aria-pressed', String(button.dataset.language === language));
  });
}

document.querySelectorAll('.example[data-path]').forEach(example => {
  example.requestBody = example.querySelector('[data-json]')?.textContent || example.querySelector('pre code').textContent;
  example.querySelectorAll('[data-language]').forEach(button => {
    button.addEventListener('click', () => renderExample(example, button.dataset.language));
  });
  renderExample(example, example.querySelector('[aria-pressed="true"]').dataset.language);
});

document.querySelectorAll('.copy').forEach(button => {
  button.addEventListener('click', async () => {
    const text = button.dataset?.copyText || button.closest('.example').querySelector('pre code').textContent;
    try {
      if (navigator.clipboard && window.isSecureContext) await navigator.clipboard.writeText(text);
      else {
        const input = document.createElement('textarea');
        input.value = text;
        input.style.cssText = 'position:fixed;left:-9999px';
        document.body.appendChild(input);
        try {
          input.select();
          if (!document.execCommand('copy')) throw new Error('Copy failed');
        } finally {
          input.remove();
        }
      }
      button.textContent = '已复制';
    } catch {
      button.textContent = '请手动复制';
    }
    setTimeout(() => button.textContent = '复制', 1500);
  });
});

const skuRowsElement = document.getElementById('skuRows');
const skuFilterElement = document.getElementById('skuFilter');
const modeNames = {generate:'生成',edit:'编辑',text_to_video:'文生',image_to_video:'首帧',first_last_frame:'首尾帧',reference_to_video:'参考图'};

function renderSKUs() {
  if (!skuRowsElement || typeof FALLBACK_MEDIA_SKUS === 'undefined') return;
  const items = FALLBACK_MEDIA_SKUS.filter(sku => !skuFilterElement || skuFilterElement.value === 'all' || sku.type === skuFilterElement.value);
  skuRowsElement.replaceChildren();
  items.forEach(sku => {
    const row = document.createElement('tr');
    const nameCell = document.createElement('td');
    const name = document.createElement('code');
    name.textContent = sku.id;
    nameCell.appendChild(name);
    row.appendChild(nameCell);
    const values = [sku.type === 'image' ? '图片' : '视频',
      Object.entries(sku.locked_spec).filter(([key,value]) => key !== 'priority' || value !== 'standard').map(([key,value]) => key === 'durationSeconds' ? value+' 秒' : key === 'priority' ? 'Relaxed' : String(value).toUpperCase()).join(' · '),
      sku.allowed_modes.map(mode => modeNames[mode] || mode).join(' / ')];
    values.forEach(value => {
      const cell = document.createElement('td');
      cell.textContent = value;
      row.appendChild(cell);
    });
    skuRowsElement.appendChild(row);
  });
  const count = document.getElementById('skuCount');
  if (count) count.textContent = items.length + ' 个 SKU · 价格由下游设置';
}

skuFilterElement?.addEventListener('change', renderSKUs);
renderSKUs();
modeButtons.forEach(button => button.addEventListener('click', () => setDocMode(button.dataset.docMode)));

window.addEventListener('hashchange', showSection);
showSection();
