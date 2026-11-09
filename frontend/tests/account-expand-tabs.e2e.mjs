import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { spawn } from 'node:child_process'
import { chromium } from 'playwright'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// Only local static assets and synthetic APIs. No credentials/providers used.
const frontend = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(frontend, '../tmp/account-expand-tabs')
await mkdir(output, { recursive: true })
const entry = `import {createApp,h} from 'vue';import {createRouter,createWebHashHistory,RouterView} from 'vue-router';
import Accounts from '/src/views/AccountsView.vue';import 'element-plus/theme-chalk/index.css';import '/src/styles/theme.css';
const router=createRouter({history:createWebHashHistory(),routes:[{path:'/:pathMatch(.*)*',component:Accounts}]});
createApp({render:()=>h(RouterView)}).use(router).mount('#app');`
const server = await createServer({ configFile: false, root: frontend, logLevel: 'error',
    cacheDir: resolve(output, 'vite-cache'), optimizeDeps: { include: ['vue', 'vue-router', 'element-plus', 'dayjs'] },
    plugins: [vue(), Components({ dts: false, resolvers: [ElementPlusResolver()] }), {
        name: 'account-expanded-tabs-fixture',
        resolveId(id) { if (id === '/__expanded_entry__.js') return '\0expanded-entry' },
        load(id) { if (id === '\0expanded-entry') return entry },
        configureServer(instance) { instance.middlewares.use('/__expanded_test__', async (_, response) => {
            response.setHeader('Content-Type', 'text/html; charset=utf-8')
            response.end(await instance.transformIndexHtml('/__expanded_test__', '<html><meta charset="UTF-8"><body><div id="app"></div><script type="module" src="/__expanded_entry__.js"></script></body></html>'))
        }) },
    }], server: { host: '127.0.0.1', port: 0 },
})
await server.listen()
const base = server.resolvedUrls.local[0].replace(/\/$/, '')
const executablePath = [process.env.PLAYWRIGHT_BROWSER_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/chromium'].find(path => path && existsSync(path))
assert.ok(executablePath)
const browserServer = await chromium.launchServer({ executablePath, headless: true })
const browser = await chromium.connect(browserServer.wsEndpoint())
const context = await browser.newContext({ viewport: { width: 1920, height: 1080 }, locale: 'zh-CN', reducedMotion: 'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(20000)
const errors = [], writes = [], blocked = [], checks = []
page.on('pageerror', error => errors.push(error.message))
const account = { id: 11, email: 'creator@example.test', name: '演示账号', remark: '主表备注', st: 'synthetic-session',
    at: 'synthetic-access', google_cookies: JSON.stringify([
        {name:'SID',value:'synthetic-cookie-'+'x'.repeat(240),domain:'.google.com',path:'/',secure:true,httpOnly:true},
        {name:'OSID',value:'</code><img src=x onerror="window.cookieExecuted=true">',domain:'flow.google.com',path:'/',secure:true},
    ]), credits: 1000, user_paygate_tier: 'PAYGATE_TIER_ONE',
    is_active: true, browser_profile_source: 'cookies', browser_profile_status: 'ready', refresh_status: 'ready',
    auto_refresh_enabled: true, refresh_interval_minutes: 30, image_enabled: true, video_enabled: true,
    proxy_pool_id: 1, proxy_url: 'socks5://synthetic:password@192.0.2.1:1080', submission_proxy_mode: 'fixed',
    at_expires: '2099-01-01T00:00:00Z', current_project_name: '演示项目' }
const proxies = [1,2].map(id => ({ id, proxy_url: `socks5://synthetic:password@192.0.2.${id}:1080`, enabled: true,
    note: `演示出口 ${id}`, last_check_ok: true, bound_account_count: id === 1 ? 1 : 0 }))
const json = (route, body, status=200) => route.fulfill({ status, contentType: 'application/json', body: JSON.stringify(body) })
await context.addInitScript(() => { window.copies=[]; Object.defineProperty(navigator, 'clipboard', { value: { writeText: async value => window.copies.push(value) } }) })
await context.route('**/*', async route => {
    const request=route.request(), url=new URL(request.url())
    if (url.origin !== base) { blocked.push(url.origin); return route.abort() }
    if (request.method() === 'PUT' && url.pathname === '/api/tokens/11') {
        const body=request.postDataJSON(); writes.push(body); Object.assign(account, body)
        if (body.proxy_pool_id) account.proxy_url=proxies.find(p => p.id===body.proxy_pool_id).proxy_url
        return json(route, { success: true })
    }
    if (!url.pathname.startsWith('/api/') && url.pathname!=='/health') return route.continue()
    if (request.method()!=='GET') throw new Error('Unexpected mutation: '+request.method()+' '+url.pathname)
    if (url.pathname==='/health') return json(route,{backend_running:true,viewer_presentation:'relay'})
    if (url.pathname==='/api/tokens') return json(route,[account])
    if (url.pathname==='/api/proxy/pool') return json(route,{entries:proxies})
    if (url.pathname==='/api/profile-transfer/jobs'||url.pathname==='/api/plugin/import/jobs') return json(route,{jobs:[]})
    if (url.pathname.startsWith('/api/admin/vids/')) return json(route,{accounts:[]})
    if (url.pathname==='/api/token-refresh/config') return json(route,{config:{}})
    return json(route,{})
})
const record = value => { checks.push(value); console.log('PASS '+value) }
const row = page.locator('.accounts-table .el-table__row').filter({hasText:'creator@example.test'})
const panel = page.locator('.account-quick-panel[data-account-id="11"]')
try {
    await page.goto(`${base}/__expanded_test__#/accounts`, {waitUntil:'domcontentloaded',timeout:60000})
    await row.locator('.el-table__expand-icon').click()
    await panel.waitFor()
    assert.equal(await panel.getByRole('tab',{name:'账号信息',exact:true}).getAttribute('aria-selected'),'true')
    assert.equal(await panel.getByRole('tab',{name:'代理',exact:true}).getAttribute('aria-selected'),'false')
    const accountPanel=panel.locator('.quick-account-section')
    await accountPanel.getByText('synthetic-cookie-',{exact:false}).waitFor()
    assert.equal(await panel.locator('input:visible, .el-switch:visible, .el-input-number:visible').count(),0)
    assert.equal(await accountPanel.locator('details').count(),0)
    assert.doesNotMatch(await accountPanel.innerText(),/常用配置|Flow 套餐|Flow 积分|最近维护|最近使用|账号 ID|会话维护周期/)
    assert.equal(await accountPanel.locator('img,script').count(),0)
    assert.equal(await page.evaluate(()=>window.cookieExecuted),undefined)
    await accountPanel.getByRole('button',{name:/复制/}).click()
    const copied=JSON.parse((await page.evaluate(()=>window.copies)).at(-1))
    assert.deepEqual(copied.cookies,JSON.parse(account.google_cookies))
    assert.equal(copied.email,account.email)
    assert.equal(copied.userPaygateTier,account.user_paygate_tier)
    assert.equal(Object.hasOwn(copied,'sid'),false)
    assert.equal(Object.hasOwn(copied,'st'),false)
    assert.ok(!JSON.stringify(copied).includes('synthetic-session'))
    assert.deepEqual(writes,[])
    const bounds=await panel.evaluate(element=>({panel:element.getBoundingClientRect().width, wrap:element.closest('.account-table-wrap').getBoundingClientRect().width}))
    assert.ok(bounds.panel>=bounds.wrap-2, JSON.stringify(bounds))
    await page.screenshot({path:resolve(output,'account-info-desktop.png'),fullPage:true})
    record('Default account tab directly displays complete Cookie JSON, copies import-compatible attributes and renders untrusted values as plain text')
    await panel.getByRole('tab',{name:'账号信息',exact:true}).focus()
    await page.keyboard.press('ArrowRight')
    assert.equal(await panel.getByRole('tab',{name:'代理',exact:true}).getAttribute('aria-selected'),'true')
    await panel.locator('.quick-proxy-picker:not(.quick-submission-picker) .el-select').click()
    await page.getByRole('option').filter({hasText:'演示出口 2'}).click()
    await panel.getByRole('button',{name:'保存绑定',exact:true}).click()
    await panel.getByRole('status').filter({hasText:'已绑定代理 #2'}).waitFor()
    assert.deepEqual(writes,[{proxy_pool_id:2}])
    await panel.getByRole('button',{name:'复制代理密码',exact:true}).click()
    assert.equal((await page.evaluate(()=>window.copies)).at(-1),'password')
    await page.screenshot({path:resolve(output,'proxy-desktop.png'),fullPage:true})
    record('Keyboard tabs switch predictably; proxy binding saves only selected ID and copy keeps decoded values')
    await panel.getByRole('tab',{name:'账号信息',exact:true}).click()
    await row.locator('.account-proxy-trigger').click()
    assert.equal(await panel.getByRole('tab',{name:'代理',exact:true}).getAttribute('aria-selected'),'true')
    assert.equal(writes.length,1)
    await page.setViewportSize({width:390,height:844})
    await panel.scrollIntoViewIfNeeded()
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1))
    const mobile=await panel.evaluate(element=>({panel:element.getBoundingClientRect().width,wrap:element.closest('.account-table-wrap').getBoundingClientRect().width}))
    assert.ok(mobile.panel>=mobile.wrap-2 && mobile.panel<=mobile.wrap+2,JSON.stringify(mobile))
    await page.screenshot({path:resolve(output,'proxy-mobile.png'),fullPage:true})
    await panel.getByRole('tab',{name:'账号信息',exact:true}).click()
    await accountPanel.getByText('synthetic-cookie-',{exact:false}).waitFor()
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1))
    await accountPanel.getByRole('button',{name:/复制/}).click()
    assert.deepEqual(JSON.parse((await page.evaluate(()=>window.copies)).at(-1)).cookies,JSON.parse(account.google_cookies))
    await page.screenshot({path:resolve(output,'account-info-mobile.png'),fullPage:true})
    assert.deepEqual(errors,[]); assert.deepEqual(blocked,[])
    record('Proxy trigger opens its own tab without folding or saving; mobile uses available width without page overflow')
    await writeFile(resolve(output,'verification.json'),JSON.stringify({checks,writes,errors,blocked},null,2))
} catch(error) {
    await page.screenshot({path:resolve(output,'failure.png'),fullPage:true}).catch(()=>{})
    throw error
} finally {
    await context.close()
    let timer
    await Promise.race([(async()=>{await browser.close();await browserServer.close()})().catch(()=>{}),new Promise(done=>{timer=setTimeout(()=>{
        if(process.platform==='win32'){const child=spawn('taskkill',['/PID',String(browserServer.process().pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});child.once('exit',done);child.once('error',done)}
        else {browserServer.process().kill('SIGKILL');done()}
    },5000)})])
    clearTimeout(timer);server.ws.close();await server.watcher.close();await server.close()
}
