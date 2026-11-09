import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { resolve, extname, sep } from 'node:path'
import { createServer } from 'node:http'
import { spawn, spawnSync } from 'node:child_process'
import { chromium } from '../frontend/node_modules/playwright/index.mjs'

// Product UI rendered exclusively with synthetic APIs. External traffic and all
// mutations are blocked. Only allowlisted public screenshots are emitted.
const root = resolve(import.meta.dirname, '..')
const output = resolve(root, 'docs/images')
const build = resolve(root, 'static/app')
await mkdir(output, { recursive: true })
await mkdir(resolve(root, 'tmp/docs-visuals'), { recursive: true })
const version = (await readFile(resolve(root, 'VERSION'), 'utf8')).trim()
const python = process.env.PYTHON || 'python'
const painted = spawnSync(python, ['-c', `
from pathlib import Path
from PIL import Image, ImageDraw
import sys
dest=Path(sys.argv[1]); image=Image.new('RGB',(1280,720)); draw=ImageDraw.Draw(image)
for y in range(720):
    t=y/720; draw.line((0,y,1280,y),fill=(int(50+100*t),int(63+40*t),int(111+30*t)))
draw.ellipse((880,110,1040,270),fill=(255,215,145))
draw.polygon([(0,650),(250,300),(600,720)],fill=(48,55,88))
draw.polygon([(280,720),(780,320),(1250,720)],fill=(39,47,78))
draw.polygon([(700,720),(1280,400),(1280,720)],fill=(31,38,65))
image.save(dest)
`, resolve(root, 'tmp/docs-visuals/demo-landscape.png')], { encoding: 'utf8', windowsHide: true })
assert.equal(painted.status, 0, painted.stderr)
const encoded = spawnSync(process.env.FFMPEG || 'ffmpeg', ['-y', '-loglevel', 'error', '-loop', '1',
    '-i', resolve(root, 'tmp/docs-visuals/demo-landscape.png'), '-t', '8', '-r', '12', '-c:v', 'libx264',
    '-pix_fmt', 'yuv420p', '-movflags', '+faststart', resolve(root, 'tmp/docs-visuals/demo-original.mp4'),
], { encoding: 'utf8', windowsHide: true })
assert.equal(encoded.status, 0, encoded.stderr || 'Install FFmpeg or set FFMPEG')
const catalogText = await readFile(resolve(root, 'static/js/model_catalog.js'), 'utf8')
const catalog = JSON.parse(catalogText.slice(catalogText.indexOf('{'), catalogText.lastIndexOf('}') + 1))
const video = await readFile(resolve(root, 'tmp/docs-visuals/demo-original.mp4'))
const server = createServer(async (request, response) => {
    const pathname = new URL(request.url, 'http://127.0.0.1').pathname
    const target = pathname.startsWith('/static/app/assets/')
        ? resolve(build, 'assets', decodeURIComponent(pathname.slice('/static/app/assets/'.length)))
        : resolve(build, 'index.html')
    if (!target.startsWith(build + sep)) return response.writeHead(404).end()
    try {
        response.writeHead(200, { 'Content-Type': { '.html':'text/html; charset=utf-8', '.js':'text/javascript', '.css':'text/css', '.svg':'image/svg+xml' }[extname(target)] || 'application/octet-stream' })
        response.end(await readFile(target))
    } catch { response.writeHead(404).end() }
})
await new Promise(done => server.listen(0, '127.0.0.1', done))
const base = `http://127.0.0.1:${server.address().port}`
const executablePath = [process.env.PLAYWRIGHT_BROWSER_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/chromium', '/usr/bin/google-chrome'].filter(Boolean).find(existsSync)
assert.ok(executablePath)
const browserServer = await chromium.launchServer({ executablePath, headless: true })
const browser = await chromium.connect(browserServer.wsEndpoint())
const context = await browser.newContext({ viewport: {width:1920,height:1080}, locale:'zh-CN', timezoneId:'Asia/Shanghai', reducedMotion:'reduce' })
const page = await context.newPage()
page.setDefaultTimeout(15000)
const errors=[], blocked=[], writes=[], screenshots=[]
page.on('pageerror', error => errors.push(error.message))
const date='2026-11-09T02:00:00Z'
const tiers=['PAYGATE_TIER_NOT_PAID','PAYGATE_TIER_ZERO','PAYGATE_TIER_ONE','PAYGATE_TIER_TWO']
const accounts=Array.from({length:28},(_,i)=>({
    id:i+1,email:`creator-${String(i+1).padStart(2,'0')}@example.test`, name:`演示账号 ${i+1}`,
    remark:['内容创作','产品素材','分镜制作','高分辨率输出'][i%4], st:`profile-session:public-demo-${i+1}`,
    is_active:i!==5,credits:[50,180,1000,2500][i%4],user_paygate_tier:tiers[i%4],
    image_enabled:true,video_enabled:true,image_count:24+i,video_count:8+i,today_error_count:i===7?2:0,
    at_expired:false,at_expires:'2026-12-09T03:14:00Z',browser_profile_source:'cloak',browser_profile_status:'ready',
    refresh_status:'idle',protocol_mode:'session',auto_refresh_enabled:true,refresh_interval_minutes:30,
    last_refresh_at:date,next_refresh_at:'2026-11-09T02:30:00Z',created_at:'2026-11-01T02:00:00Z',last_used_at:date,
    current_project_id:`demo-project-${i+1}`, current_project_name:'演示项目',proxy_pool_id:i+1,
    proxy_url:`socks5h://exit-${String(i+1).padStart(2,'0')}.example.test:1080`,submission_proxy_mode:'fixed',
}))
const proxies=accounts.map((a,i)=>({id:a.id,proxy_url:a.proxy_url,note:`创作出口 ${String(i+1).padStart(2,'0')}`,enabled:i!==5,
    bound_account_count:1,last_check_ok:i!==7,last_check_at:date,circuit_state:i===7?'cooling':'ready',inflight:i%4,
    consecutive_failures:i===7?3:0,success_count:48+i,failure_count:i===7?3:0,success_rate:i===7?0.8:1,
    cooldown_until:i===7?'2099-11-09T02:05:00Z':null}))
const logs=accounts.slice(0,12).map((a,i)=>({id:301-i,token_id:a.id,token_email:a.email,
    operation:i%3===0?'generate_video':'generate_image',status_code:i===3?429:200,status_text:i===3?'failed':'completed',
    error_summary:i===3?'PUBLIC_ERROR_USER_QUOTA_REACHED':'',duration:i===0?31.8:12+i*2,progress:i===3?0:100,created_at:date,
    attempt_count:i===0?3:1,retry_count:i===0?2:0,account_count:i===0?2:1}))
const history={version:2,total_attempts:3,retry_count:2,account_count:2,limit:20,account_allocations:2,attempts:[
    {attempt:1,account_id:3,account_email:accounts[2].email,status:'failed',stage:'captcha',started_at:date,duration_ms:2200,error_code:'REMOTE_CAPTCHA_TIMEOUT',error:'远程验证码服务响应超时',next_action:'retry_same_account'},
    {attempt:2,account_id:3,account_email:accounts[2].email,status:'failed',stage:'submit',started_at:'2026-11-09T02:00:03Z',duration_ms:3600,error_code:'PUBLIC_ERROR_USER_QUOTA_REACHED',error:'当前账号的可用额度已用完',rule_keyword:'PUBLIC_ERROR_USER_QUOTA_REACHED',rule_action:'LIMIT',rule_seconds:86400,next_action:'switch_account'},
    {attempt:3,account_id:4,account_email:accounts[3].email,status:'success',stage:'submit',started_at:'2026-11-09T02:00:07Z',duration_ms:26000,next_action:'completed'},
]}
const rules=['PUBLIC_ERROR_MODEL_ACCESS_DENIED|LIMIT|0s','PUBLIC_ERROR_USER_QUOTA_REACHED|LIMIT|24h','Resource has been exhausted (e.g. check quota).|LIMIT|1s','PUBLIC_ERROR_USER_REQUESTS_THROTTLED|LIMIT|3s','RESOURCE_EXHAUSTED|LIMIT|5s'].join('\n')
const json=(route,body,status=200)=>route.fulfill({status,contentType:'application/json',body:JSON.stringify(body)})
const receipt='vids_'+'f'.repeat(64)
await context.route('**/*',async route=>{
    const req=route.request(), url=new URL(req.url())
    if(url.origin!==base){blocked.push(url.origin);return route.abort()}
    if(!url.pathname.startsWith('/api/')&&url.pathname!=='/health')return route.continue()
    if(req.method()!=='GET'){writes.push({method:req.method(),path:url.pathname});return json(route,{detail:'Documentation fixture is read-only'},503)}
    if(url.pathname===`/api/admin/vids/v1/vids/generations/${receipt}/content`)return route.fulfill({contentType:'video/mp4',body:video})
    if(url.pathname===`/api/admin/vids/v1/vids/generations/${receipt}`)return json(route,{id:receipt,kind:'video',status:'completed',content_url:`/api/admin/vids/v1/vids/generations/${receipt}/content`})
    const fixtures={
        '/health':{version,backend_running:true,has_active_tokens:true,remote_browser_configured:true},
        '/api/stats':{total_tokens:accounts.length,active_tokens:accounts.length-1,today_images:128,today_videos:36,today_errors:2},
        '/api/admin/network':{mode:'direct',reachable:true},
        '/api/admin/release':{version,release_date:'2026-11-09',entries:[],update_docs:{}},
        '/api/tokens':accounts,'/api/proxy/pool':{success:true,entries:proxies},'/api/logs':logs,
        '/api/profile-transfer/jobs':{success:true,jobs:[]},'/api/plugin/import/jobs':{success:true,jobs:[]},
        '/api/admin/vids/onboard/candidates':{accounts:[]},
        '/api/admin/vids/accounts/detail':{accounts:accounts.map(a=>({token_id:a.id,email:a.email,enabled:true,ready:true,identity_verified:true,session_state:'ready',image_enabled:true,video_enabled:true,last_quota:{image:{available:19,total:30,queried_at:date,stale:false},video:{available:427,total:500,queried_at:date,stale:false}}}))},
        '/api/admin/studio/models':{data:Object.entries(catalog).map(([id,spec])=>({id,...spec}))},
        '/api/admin/studio/jobs':{jobs:[]},
        '/api/admin/config':{admin_username:'演示管理员',api_key:'',error_ban_threshold:3,debug_enabled:false},
        '/api/config/generation':{config:{image_timeout:300,video_timeout:1500,max_retries:3}},
        '/api/account-error-policy':{success:true,config:{rules,consecutive_failure_threshold:3,failure_cooldown_seconds:300,max_account_attempts:3}},
        '/api/admin/vids/config':{config:{keepalive_enabled:true,keepalive_interval_minutes:10,repair_cooldown_seconds:300,consecutive_failure_threshold:3,failure_cooldown_seconds:300,rules:''}},
        '/api/call-logic/config':{config:{call_mode:'default'}},
        '/api/proxy/config':{proxy_enabled:true,proxy_url:'',media_proxy_enabled:false,media_proxy_url:''},
        '/api/cache/config':{config:{enabled:true,timeout:7200,base_url:''}},
        '/api/captcha/config':{captcha_method:'remote_browser',remote_browser_base_url:'https://captcha.example.test',remote_browser_api_key:'',remote_browser_timeout:60},
    }
    if(Object.hasOwn(fixtures,url.pathname))return json(route,fixtures[url.pathname])
    const log=logs.find(l=>url.pathname===`/api/logs/${l.id}`)
    if(log)return json(route,{...log,request_body:'{}',response_body:'{}',...(log.id===301?{attempt_history:history}:{})})
    return json(route,{})
})
async function shot(name){
    await page.evaluate(()=>document.fonts.ready)
    await page.waitForTimeout(250)
    assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth+1),'No horizontal page overflow')
    await page.screenshot({path:resolve(output,name),fullPage:false,animations:'disabled'})
    screenshots.push(name)
}
try{
    await page.addInitScript(()=>localStorage.setItem('admin_username','演示管理员'))
    await page.goto(`${base}/#/accounts`,{waitUntil:'domcontentloaded'})
    await page.locator('.accounts-table .el-table__row').first().waitFor()
    for(const label of ['Free','Plus','Pro','Ultra'])assert.ok((await page.locator('.account-plan-tag').allInnerTexts()).includes(label))
    await shot('accounts-overview.png')
    await page.goto(`${base}/#/proxies`,{waitUntil:'domcontentloaded'})
    await page.locator('.pool-table-dense .el-table__row').first().waitFor()
    await shot('proxy-pool-overview.png')
    await page.goto(`${base}/#/logs`,{waitUntil:'domcontentloaded'})
    await page.locator('.logs-table .el-table__row').first().waitFor()
    await shot('request-logs-overview.png')
    await page.getByRole('button',{name:'REQUEST #301',exact:true}).click()
    await page.locator('.attempt-item').nth(2).waitFor()
    await shot('request-attempts.png')
    await page.keyboard.press('Escape')
    await page.goto(`${base}/#/settings`,{waitUntil:'domcontentloaded'})
    await page.locator('.settings-skeleton').waitFor({state:'hidden'})
    await page.getByRole('tab',{name:'自定义规则',exact:true}).click()
    await page.getByRole('heading',{name:'Flow 自定义规则',exact:true}).waitFor()
    await shot('error-rules.png')
    await page.evaluate(receipt=>{
        localStorage.setItem('lay4flow_studio_receipts:v2:creator',JSON.stringify([{provider:'vids',kind:'video',status:'completed',started:Date.now()-120000,title:'暮色山谷 · 镜头缓慢前进',description:'保持暮色与山谷构图，让镜头继续缓慢前进，远处的夕阳逐渐落下。',receipt,outputs:[{kind:'video',path:`/api/admin/vids/v1/vids/generations/${receipt}/content`}]}]))
    },receipt)
    // The administrator namespace is controlled by the actual UI identity.
    await page.evaluate(receipt=>{
        const value=localStorage.getItem('lay4flow_studio_receipts:v2:creator');
        localStorage.setItem('lay4flow_studio_receipts:v2:'+encodeURIComponent('演示管理员'),value)
    },receipt)
    await page.setViewportSize({width:1600,height:1550})
    await page.goto(`${base}/#/studio`,{waitUntil:'domcontentloaded'})
    const existing=page.locator('.gallery-card').filter({hasText:'暮色山谷 · 镜头缓慢前进'}).first()
    await existing.getByRole('button',{name:'续写视频',exact:true}).click()
    await page.locator('.source-video video').waitFor()
    await page.waitForFunction(()=>{const v=document.querySelector('.source-video video');return v&&v.readyState>=2&&v.duration>3})
    await page.locator('.source-video video').evaluate(video=>{video.pause();video.currentTime=0.3})
    await page.waitForFunction(()=>!document.querySelector('.el-message'))
    await shot('omni12-video-extend.png')
    await page.locator('.source-video').getByRole('button',{name:'移除',exact:true}).click()
    await shot('omni12-video-upload.png')
    const converted = spawnSync(python, ['-c', `
from pathlib import Path
from PIL import Image
import sys
root=Path(sys.argv[1])
for source,target in [('accounts-overview','release-accounts'),('omni12-video-extend','release-studio'),('error-rules','release-rules'),('request-attempts','release-logs')]:
    Image.open(root/(source+'.png')).convert('RGB').save(root/(target+'.webp'),'WEBP',lossless=True,method=6)
`, output], { encoding: 'utf8', windowsHide: true })
    assert.equal(converted.status, 0, converted.stderr)
    assert.deepEqual(writes,[],'Documentation fixture must never send a generation or settings write')
    assert.deepEqual(errors,[])
    await writeFile(resolve(root,'tmp/docs-visuals/evidence.json'),JSON.stringify({screenshots,writes,errors,blocked,source:'built application; synthetic accounts, request history and locally generated MP4 only'},null,2))
    console.log(JSON.stringify({screenshots,writes,errors}))
}catch(error){
    await page.screenshot({path:resolve(root,'tmp/docs-visuals/failure.png'),fullPage:true}).catch(()=>{})
    throw error
}finally{
    await context.close()
    let timer
    await Promise.race([(async()=>{await browser.close();await browserServer.close()})().catch(()=>{}),new Promise(done=>{timer=setTimeout(()=>{if(process.platform!=='win32'){browserServer.process().kill('SIGKILL');done();return}const child=spawn('taskkill',['/PID',String(browserServer.process().pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});child.once('exit',done);child.once('error',done)},5000)})])
    clearTimeout(timer);server.closeAllConnections();await new Promise(done=>server.close(done))
}
