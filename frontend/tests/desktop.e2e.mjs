import assert from 'node:assert/strict'
import { spawn } from 'node:child_process'
import { existsSync } from 'node:fs'
import { mkdir, mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import net from 'node:net'
import { tmpdir } from 'node:os'
import { resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { chromium } from 'playwright'

// Exercise the shipped desktop host, using a temporary application database and
// WebView profile. No Docker command, production account or generation is used.
assert.equal(process.platform, 'win32', 'The native desktop smoke runs on Windows')
const root = fileURLToPath(new URL('../..', import.meta.url))
const executable = process.env.LAY4FLOW_DESKTOP_EXE || resolve(root, 'desktop/runtime/windows-x64/Lay4Flow.Desktop.exe')
assert.ok(existsSync(executable), 'Publish the native host with scripts/build_desktop.ps1 first')
const fixture = await mkdtemp(resolve(tmpdir(), 'lay4flow-desktop-smoke-'))
const output = resolve(root, 'tmp/desktop-smoke')
await mkdir(output, { recursive: true })
const freePort = () => new Promise((accept, reject) => {
    const socket = net.createServer()
    socket.once('error', reject)
    socket.listen(0, '127.0.0.1', () => {
        const { port } = socket.address()
        socket.close(() => accept(port))
    })
})
const port = await freePort(), debuggingPort = await freePort()
const base = `http://127.0.0.1:${port}`
const version = (await readFile(resolve(root, 'VERSION'), 'utf8')).trim()
const logs = [], checks = [], errors = [], children = []
const record = message => { checks.push(message); console.log(`PASS ${message}`) }
const pause = milliseconds => new Promise(accept => setTimeout(accept, milliseconds))
async function waitFor(operation, message, timeout = 90000) {
    const deadline = Date.now() + timeout
    while (Date.now() < deadline) {
        try { const result = await operation(); if (result) return result } catch {}
        const failed = children.find(child => !child.stopping && child.exitCode !== null && child.exitCode !== 0)
        if (failed) throw new Error(`${message}; child exited with ${failed.exitCode}\n${logs.join('')}`)
        await pause(200)
    }
    throw new Error(message)
}
function start(command, args, options = {}) {
    const child = spawn(command, args, {
        cwd: root,
        env: { ...process.env, PYTHONUTF8: '1' },
        windowsHide: true,
        ...options,
    })
    children.push(child)
    child.stdout?.on('data', chunk => logs.push(chunk.toString('utf8')))
    child.stderr?.on('data', chunk => logs.push(chunk.toString('utf8')))
    child.once('error', error => errors.push(error.message))
    return child
}
async function nativeWindow(windowHandle, options) {
    const child = start('powershell.exe', [
        '-NoLogo', '-NoProfile', '-ExecutionPolicy', 'Bypass',
        '-File', resolve(root, 'frontend/tests/desktop-window.ps1'),
        '-WindowHandle', String(windowHandle), ...options,
    ])
    const code = await new Promise(accept => child.once('exit', accept))
    assert.equal(code, 0, logs.join(''))
}
async function stop(child) {
    if (!child?.pid || child.exitCode !== null) return
    child.stopping = true
    await new Promise(accept => {
        const killer = spawn('taskkill.exe', ['/PID', String(child.pid), '/T', '/F'], { windowsHide: true, stdio: 'ignore' })
        killer.once('exit', accept)
        killer.once('error', accept)
    })
    await pause(300)
}
let desktop, browser
try {
    start(process.env.PYTHON || 'python', ['-X', 'utf8', 'scripts/preview_frontend.py', '--port', String(port)])
    await waitFor(async () => (await fetch(`${base}/health`)).ok, 'Isolated preview did not start')
    record('Temporary preview backend is healthy; production Docker and data remain untouched')
    const readyFile = resolve(fixture, 'ready.json')
    desktop = start(executable, [
        '--url', `${base}/login`, '--no-start', '--user-data-dir', resolve(fixture, 'webview-profile'),
        '--remote-debugging-port', String(debuggingPort), '--ready-file', readyFile,
    ])
    const ready = await waitFor(async () => JSON.parse(await readFile(readyFile, 'utf8')), 'Desktop did not create its ready record')
    assert.equal(ready.pid, desktop.pid)
    assert.ok(Number(ready.window_handle) > 0, 'The desktop must own an actual Windows window')
    assert.equal(ready.version, version)
    browser = await chromium.connectOverCDP(`http://127.0.0.1:${debuggingPort}`)
    const context = browser.contexts()[0]
    const page = await waitFor(async () => context.pages().find(page => page.url().startsWith(base)), 'Desktop WebView page is missing')
    page.on('pageerror', error => errors.push(error.message))
    assert.equal(await page.evaluate(() => Boolean(window.chrome?.webview)), true)
    await page.locator('.login-form-wrap').waitFor()
    await page.screenshot({ path: resolve(output, 'desktop-login.png'), animations: 'disabled' })
    await nativeWindow(ready.window_handle, ['-Screenshot', resolve(output, 'native-window-login.png')])
    record('Published EXE opens a native window containing WebView2 and the application login')
    await page.getByLabel('管理员账号', { exact: true }).fill('preview-admin')
    await page.getByLabel('登录密码', { exact: true }).fill('preview-password')
    await page.getByRole('button', { name: '进入灵感工坊' }).click()
    await page.waitForURL(url => url.hash.startsWith('#/dashboard'))
    await page.locator('.sidebar').waitFor()
    record('Real login succeeds inside the desktop WebView with synthetic credentials')
    // WinForms' MenuStrip is intentionally verified through the host's
    // browser event bridge below; native capture verifies that this is a
    // desktop window rather than an external browser tab.
    await page.evaluate(() => window.dispatchEvent(new CustomEvent('open-release-history')))
    const drawer = page.locator('.el-drawer').filter({ hasText: '更新日志' })
    await drawer.waitFor({ state: 'visible' })
    await drawer.getByText(`v${version}`, { exact: true }).first().waitFor()
    assert.ok((await drawer.innerText()).includes('Windows'))
    await drawer.screenshot({ path: resolve(output, 'desktop-release-history.png'), animations: 'disabled' })
    await nativeWindow(ready.window_handle, ['-Screenshot', resolve(output, 'native-window-release-history.png')])
    record('Desktop shell update-log bridge opens the shared changelog drawer with the current release')
    await page.evaluate(() => { location.href = 'data:text/html,desktop-origin-boundary-test' })
    await pause(600)
    assert.ok(page.url().startsWith(base), 'The host must reject navigation away from its application origin')
    record('A data URL cannot replace the trusted desktop application page')
    // The host uses a per-install profile. The backend session persistence is
    // covered by the Python admin-session tests; this smoke intentionally
    // leaves the WebView process alive until cleanup rather than racing its
    // asynchronous profile flush.
    assert.deepEqual(errors, [])
    await writeFile(resolve(output, 'verification.json'), JSON.stringify({ executable, base, ready, checks, errors }, null, 2))
} finally {
    // taskkill only targets children created by this test, including the Python
    // preview child. The application's local Docker service is not involved.
    await stop(desktop)
    for (const child of children.reverse()) await stop(child)
    await writeFile(resolve(output, 'runtime.log'), logs.join(''), 'utf8')
}
