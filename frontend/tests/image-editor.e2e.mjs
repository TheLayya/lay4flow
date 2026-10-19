import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { chromium } from 'playwright'
import { createServer } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

// A self-contained browser harness: no production server, credentials, or upstream calls.
const root = fileURLToPath(new URL('..', import.meta.url))
const output = resolve(root, '../../../output/studio-image-editor')
const component = resolve(root, 'src/components/StudioImageEditor.vue').replaceAll('\\', '/')
const entry = `
import {createApp,h,reactive} from 'vue';
import Editor from ${JSON.stringify(component)};
import 'element-plus/theme-chalk/index.css';
import '/src/styles/theme.css';
window.state=reactive({open:true,loading:false,source:{title:'像素编辑验证',prompt:'保留人物，改变背景'}});
window.applied=[];
window.reloads=0;
createApp({setup(){return()=>h(Editor,{modelValue:state.open,source:state.source,loading:state.loading,'onUpdate:modelValue':value=>state.open=value,onReload:()=>window.reloads++,onApply:async payload=>{const bitmap=await createImageBitmap(payload.file);const result=document.createElement('canvas');result.width=bitmap.width;result.height=bitmap.height;result.getContext('2d').drawImage(bitmap,0,0);window.applied.push({mode:payload.mode,prompt:payload.prompt,name:payload.file.name,type:payload.file.type,width:bitmap.width,height:bitmap.height,firstPixel:[...result.getContext('2d').getImageData(0,0,1,1).data]});bitmap.close();}})}}).mount('#app');
const source=document.createElement('canvas');source.width=100;source.height=60;const c=source.getContext('2d');c.fillStyle='#e91a22';c.fillRect(0,0,50,60);c.fillStyle='#1564ee';c.fillRect(50,0,50,60);source.toBlob(blob=>state.source={...state.source,file:new File([blob],'source.png',{type:'image/png'})});
`
const server = await createServer({
    configFile: false,
    root,
    plugins: [
        vue(),
        Components({ dts: false, resolvers: [ElementPlusResolver()] }),
        {
            name: 'editor-test-harness',
            resolveId(id) {
                if (id === '/__editor_entry__.js') return '\0editor-test-entry'
            },
            load(id) {
                if (id === '\0editor-test-entry') return entry
            },
            configureServer(instance) {
                instance.middlewares.use('/__editor_test__', async (request, response) => {
                    response.setHeader('Content-Type', 'text/html')
                    response.end(
                        await instance.transformIndexHtml(
                            '/__editor_test__',
                            '<html><body><div id="app"></div><script type="module" src="/__editor_entry__.js"></script></body></html>',
                        ),
                    )
                })
            },
        },
    ],
    server: { host: '127.0.0.1', port: 38127, strictPort: true },
    logLevel: 'error',
})
await server.listen()
const browserPath = [
    process.env.PLAYWRIGHT_BROWSER_PATH,
    'C:/Program Files/Google/Chrome/Application/chrome.exe',
    'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe',
    '/usr/bin/google-chrome',
    '/usr/bin/chromium',
].find((value) => value && existsSync(value))
assert.ok(browserPath, 'Install Chromium or set PLAYWRIGHT_BROWSER_PATH')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ executablePath: browserPath, headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const errors = []
page.on('pageerror', (error) => errors.push(error.message))
const pixel = (x, y) =>
    page
        .locator('.editor-canvas')
        .evaluate(
            (canvas, position) => [...canvas.getContext('2d').getImageData(position[0], position[1], 1, 1).data],
            [x, y],
        )
async function waitDimensions(width, height) {
    await page.waitForFunction(
        ([w, h]) => {
            const element = document.querySelector('.editor-canvas')
            return element?.width === w && element?.height === h
        },
        [width, height],
    )
}
const button = (name) => page.getByRole('button', { name, exact: true })
try {
    await page.goto('http://127.0.0.1:38127/__editor_test__')
    await waitDimensions(100, 60)
    assert.deepEqual(await pixel(25, 30), [233, 26, 34, 255])
    assert.deepEqual(await pixel(75, 30), [21, 100, 238, 255])

    // Ink uses a separate transparent layer; the eraser reveals the source pixels.
    const canvas = page.locator('.editor-canvas')
    await page.locator('.brush-range [role="slider"]').focus()
    await page.keyboard.press('End')
    const box = await canvas.boundingBox()
    await page.mouse.click(box.x + box.width / 4, box.y + box.height / 2)
    const ink = await pixel(25, 30)
    assert.notDeepEqual(ink, [233, 26, 34, 255])
    await button('橡皮擦').click()
    await page.mouse.click(box.x + box.width / 4, box.y + box.height / 2)
    assert.deepEqual(await pixel(25, 30), [233, 26, 34, 255])
    await button('撤销').click()
    assert.deepEqual(await pixel(25, 30), ink)
    await button('重做').click()
    assert.deepEqual(await pixel(25, 30), [233, 26, 34, 255])
    await button('还原').click()
    await button('向右旋转').click()
    await waitDimensions(60, 100)
    assert.deepEqual(await pixel(30, 25), [233, 26, 34, 255])
    assert.deepEqual(await pixel(30, 75), [21, 100, 238, 255])
    await button('垂直翻转').click()
    assert.deepEqual(await pixel(30, 25), [21, 100, 238, 255])
    await button('还原').click()
    await page.getByText('1 : 1', { exact: true }).click()
    await waitDimensions(60, 60)
    assert.deepEqual(await pixel(0, 30), [233, 26, 34, 255])
    assert.deepEqual(await pixel(59, 30), [21, 100, 238, 255])
    await button('加入参考图').click()
    await page.waitForFunction(() => window.applied.length === 1)
    assert.deepEqual(await page.evaluate(() => window.applied[0]), {
        mode: 'reference',
        prompt: '保留人物，改变背景',
        name: '像素编辑验证.png',
        type: 'image/png',
        width: 60,
        height: 60,
        firstPixel: [233, 26, 34, 255],
    })
    await button('使用此图进行 AI 编辑').click()
    await page.waitForFunction(() => window.applied.length === 2)
    assert.equal(await page.evaluate(() => window.applied[1].mode), 'edit')
    await page.screenshot({ path: resolve(output, 'editor-desktop.png') })
    await page.getByText('自由', { exact: true }).click()
    const freeSlider = page.locator('.free-crop-controls [role="slider"]').first()
    await freeSlider.focus()
    await page.keyboard.press('Home')
    await waitDimensions(10, 60)
    await page.mouse.move(1, 1)
    await page.waitForTimeout(250)
    const contrast = await button('使用此图进行 AI 编辑').evaluate((element) => {
        const style = getComputedStyle(element)
        const luminance = (value) => {
            const channels = value
                .match(/[\d.]+/g)
                .slice(0, 3)
                .map(Number)
                .map((channel) => channel / 255)
                .map((channel) => (channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4))
            return channels[0] * 0.2126 + channels[1] * 0.7152 + channels[2] * 0.0722
        }
        const foreground = luminance(style.color)
        const background = luminance(style.backgroundColor)
        return (Math.max(foreground, background) + 0.05) / (Math.min(foreground, background) + 0.05)
    })
    assert.ok(
        contrast >= 4.5,
        `AI edit button contrast ${contrast}; ${await button('使用此图进行 AI 编辑').evaluate((element) => JSON.stringify({ class: element.className, color: getComputedStyle(element).color, bg: getComputedStyle(element).backgroundColor }))}`,
    )
    await page.setViewportSize({ width: 390, height: 844 })
    const shell = page.locator('.editor-shell')
    assert.ok(await shell.evaluate((element) => element.scrollWidth <= element.clientWidth + 1))
    await button('使用此图进行 AI 编辑').scrollIntoViewIfNeeded()
    assert.ok(await button('使用此图进行 AI 编辑').isVisible())
    assert.ok(
        await button('关闭').evaluate((element) => {
            const box = element.getBoundingClientRect()
            return box.bottom < innerHeight && box.top > 0
        }),
    )
    await page.screenshot({ path: resolve(output, 'editor-mobile.png') })

    await page.evaluate(() => {
        window.state.source = { title: '读取失败', error: '测试图片已失效' }
    })
    await page.getByText('测试图片已失效', { exact: true }).first().waitFor()
    assert.equal(await page.locator('.editor-canvas').count(), 0)
    await button('重新读取').click()
    assert.equal(await page.evaluate(() => window.reloads), 1)
    await page.evaluate(async () => {
        window.state.open = false
        await new Promise((resolve) => setTimeout(resolve, 30))
        const canvas = document.createElement('canvas')
        canvas.width = 5000
        canvas.height = 100
        const context = canvas.getContext('2d')
        context.fillStyle = '#18bb88'
        context.fillRect(0, 0, canvas.width, canvas.height)
        const blob = await new Promise((resolve) => canvas.toBlob(resolve))
        window.state.source = { title: '大图导出', file: blob }
        window.state.open = true
    })
    await waitDimensions(1600, 32)
    await button('加入参考图').scrollIntoViewIfNeeded()
    await button('加入参考图').click()
    await page.waitForFunction(() => window.applied.length === 3)
    assert.equal(await page.evaluate(() => window.applied[2].width), 4096)
    assert.equal(await page.evaluate(() => window.applied[2].height), 82)
    assert.deepEqual(errors, [])
    await writeFile(
        resolve(output, 'verification.json'),
        JSON.stringify(
            {
                status: 'passed',
                checks: [
                    'source-pixels',
                    'isolated-ink-eraser',
                    'undo-redo',
                    'rotation-flip',
                    'center-crop',
                    'free-crop',
                    'png-file-export',
                    'ai-edit-payload',
                    'desktop-mobile-layout',
                    'reload-after-failure',
                    'long-edge-4096',
                    'button-contrast',
                ],
                aiEditButtonContrast: contrast,
                errors,
            },
            null,
            2,
        ),
    )
    console.log(
        'Image editor browser checks passed: source pixels, isolated ink eraser, undo/redo, rotation, flip, crop, PNG apply, AI-edit payload, mobile overflow, reload.',
    )
} finally {
    await page.close()
    await browser.close()
    await server.environments.client.close()
    await server.close()
}
