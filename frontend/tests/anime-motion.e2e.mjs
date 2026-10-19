import assert from 'node:assert/strict'
import { existsSync } from 'node:fs'
import { mkdir, writeFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { resolve } from 'node:path'
import { createHash } from 'node:crypto'
import { execFileSync } from 'node:child_process'
import { chromium } from 'playwright'

// This is a read-only visual regression. It never sends login or generation requests.
const url = process.env.FRONTEND_MOTION_URL || 'http://127.0.0.1:38123/manage#/login'
assert.match(url, /^http:\/\/(127\.0\.0\.1|localhost):\d+\//)
const output = resolve(fileURLToPath(new URL('../../../..', import.meta.url)), 'output/anime-motion')
const executablePath = [process.env.PLAYWRIGHT_BROWSER_PATH, 'C:/Program Files/Google/Chrome/Application/chrome.exe', 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', '/usr/bin/google-chrome', '/usr/bin/chromium'].filter(Boolean).find(existsSync)
assert.ok(executablePath, 'Install Chrome/Edge or set PLAYWRIGHT_BROWSER_PATH')
await mkdir(output, { recursive: true })
const browser = await chromium.launch({ executablePath, headless: true })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 }, reducedMotion: 'no-preference' })
const page = await context.newPage()
const errors = [], checks = [], frames = []
let motionMetrics
page.on('pageerror', error => errors.push(error.message))
context.setDefaultTimeout(15000)
await context.route('**/api/**', route => route.fulfill({ status: 401, contentType: 'application/json', body: '{"detail":"Motion test has no session"}' }))
const record = value => { checks.push(value); console.log(`PASS ${value}`) }
const scene = () => page.locator('.login-world svg.login-scene')

async function frame(milliseconds) {
  const measurements = await scene().evaluate(async (svg, time) => {
    svg.pauseAnimations()
    svg.setCurrentTime(time / 1000)
    for (const animation of svg.getAnimations({ subtree: true })) {
      if (animation.effect?.target === svg) continue
      animation.pause()
      animation.currentTime = animation.effect?.target?.dataset.motionPart === 'entrance' ? animation.effect.getComputedTiming().endTime : time
    }
    await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
    const base = svg.querySelector('[data-motion-part="body"]') || svg
    const inverse = base.getCTM().inverse()
    const parts = Object.fromEntries([...svg.querySelectorAll('[data-motion-part]')].map(part => {
      const matrix = inverse.multiply(part.getCTM())
      return [part.dataset.motionPart, [matrix.a, matrix.b, matrix.c, matrix.d, matrix.e, matrix.f, Number(getComputedStyle(part).opacity)]]
    }))
    const eyeOpen = svg.querySelector('.eye-open'), eyeClosed = svg.querySelector('.eye-closed')
    const anchor = new DOMPoint(386, 850).matrixTransform(svg.getCTM().inverse().multiply(base.getCTM()))
    return { ...parts, bodyAnchor: [anchor.x, anchor.y], waveScale: svg.querySelector('feDisplacementMap')?.scale.animVal, eyeOpen: eyeOpen ? getComputedStyle(eyeOpen).transform : null, eyeClosed: eyeClosed ? Number(getComputedStyle(eyeClosed).opacity) : null }
  }, milliseconds)
  const buffer = await scene().screenshot({ path: resolve(output, `character-${milliseconds}ms.png`), animations: 'allow' })
  frames.push({ milliseconds, sha256: createHash('sha256').update(buffer).digest('hex'), measurements })
  return measurements
}

async function previewGif() {
  const directory = resolve(output, 'energetic-frames')
  await mkdir(directory, { recursive: true })
  for (let index = 0; index < 44; index++) {
    await scene().evaluate(async (svg, time) => {
      svg.pauseAnimations()
      svg.setCurrentTime(time / 1000)
      for (const animation of svg.getAnimations({ subtree: true })) {
        if (animation.effect?.target === svg) continue
        animation.pause()
        animation.currentTime = animation.effect?.target?.dataset.motionPart === 'entrance' ? animation.effect.getComputedTiming().endTime : time
      }
      await new Promise(resolve => requestAnimationFrame(resolve))
    }, index * 1000 / 12)
    await scene().screenshot({ path: resolve(directory, `frame-${String(index).padStart(3, '0')}.png`), animations: 'allow' })
  }
  execFileSync('python', ['-c', 'from pathlib import Path; from PIL import Image; import sys; p=Path(sys.argv[1]); frames=[Image.open(f).convert("RGB") for f in sorted(p.glob("frame-*.png"))]; frames=[im.resize((600, round(im.height*600/im.width)), Image.Resampling.LANCZOS) for im in frames]; frames[0].save(sys.argv[2], save_all=True, append_images=frames[1:], duration=83, loop=0, optimize=False)', directory, resolve(output, 'energetic-preview.gif')])
  record('A 3.6-second animated preview records the actual SVG character frames at 12 fps')
}

async function renderedHandMotion() {
  const samples = []
  for (const [index, time] of [0, 0.4, 0.8, 1.2, 1.8].entries()) {
    const sample = await scene().evaluate(async (svg, seconds) => {
      for (const animation of svg.getAnimations({ subtree: true })) {
        if (animation.effect?.target === svg) continue
        animation.pause()
        animation.currentTime = animation.effect?.target?.dataset.motionPart === 'entrance' ? animation.effect.getComputedTiming().endTime : 0
      }
      svg.pauseAnimations()
      svg.setCurrentTime(seconds)
      await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))
      const body = svg.querySelector('[data-motion-part="body"]')
      const matrix = body.getScreenCTM()
      const points = [[560,300],[860,300],[560,650],[860,650]].map(([x,y]) => new DOMPoint(x,y).matrixTransform(matrix))
      const x = Math.max(0, Math.min(...points.map(point => point.x))), y = Math.max(0, Math.min(...points.map(point => point.y)))
      return { clip: { x, y, width: Math.min(innerWidth - x, Math.max(...points.map(point => point.x)) - x), height: Math.min(innerHeight - y, Math.max(...points.map(point => point.y)) - y) }, scale: svg.querySelector('feDisplacementMap')?.scale.animVal }
    }, time)
    const path = resolve(output, `hand-${index}.png`)
    await page.screenshot({ path, clip: sample.clip, animations: 'allow' })
    samples.push({ path, seconds: time, scale: sample.scale })
  }
  const comparison = JSON.parse(execFileSync('python', ['-c', 'from PIL import Image; import numpy as np, json, sys; ims=[np.array(Image.open(p).convert("RGB"),dtype=np.int16) for p in sys.argv[1:]]; scores=[]; [scores.append(float(np.mean(np.max(np.abs(a-b),axis=2)>20))) for i,a in enumerate(ims) for b in ims[i+1:]]; print(json.dumps({"changedFraction":max(scores)}))', ...samples.map(item => item.path)], { encoding: 'utf8' }))
  assert.ok(comparison.changedFraction >= 0.06, `Wave must change actual hand-region pixels with other character layers frozen; measured ${(comparison.changedFraction * 100).toFixed(1)}%`)
  motionMetrics.handRegionChangedFraction = comparison.changedFraction
  motionMetrics.waveScaleSamples = samples.map(({seconds,scale}) => ({seconds,scale}))
  record('With body, head and effects held still, the waving hand visibly changes the rendered arm region')
}

function rotationChange(part, sampledFrames = frames) {
  const values = sampledFrames.map(item => item.measurements[part]).filter(Boolean)
  assert.equal(values.length, sampledFrames.length, `Missing animated ${part} group`)
  const angles = values.map(matrix => Math.atan2(matrix[1], matrix[0]) * 180 / Math.PI)
  return Math.max(...angles) - Math.min(...angles)
}

try {
  await page.goto(url, { waitUntil: 'domcontentloaded' })
  await scene().waitFor({ state: 'visible' })
  await page.waitForFunction(() => {
    const svg = document.querySelector('.login-world svg.login-scene')
    return svg?.dataset.motionState === 'running' && svg.getAnimations({ subtree: true }).some(animation => animation.playState === 'running')
  })
  await page.evaluate(() => document.fonts.ready)
  await page.waitForTimeout(1000)
  const images = await scene().locator('image').evaluateAll(nodes => [...new Set(nodes.map(image => image.href.baseVal))])
  assert.ok(images.length >= 3, 'Character rig must use separate approved artwork layers')
  await page.evaluate(async sources => Promise.all(sources.map(async src => { const image = new Image(); image.src = src; await image.decode() })), images)
  record('Inline SVG loads separate full-quality anime artwork layers')
  for (const time of [0, 400, 800, 1200, 1800, 2400]) await frame(time)
  const earlyFrames = frames.filter(item => item.milliseconds <= 1800)
  const anchors = earlyFrames.map(item => item.measurements.bodyAnchor)
  const bodyTravel = Math.max(...anchors.flatMap(from => anchors.map(to => Math.hypot(to[0] - from[0], to[1] - from[1]))))
  const waveScales = earlyFrames.map(item => item.measurements.waveScale)
  motionMetrics = { withinMilliseconds: 1800, bodyTravelStagePixels: bodyTravel, headRotationDegrees: rotationChange('head', earlyFrames), waveDeformationScaleSpan: Math.max(...waveScales) - Math.min(...waveScales) }
  assert.ok(bodyTravel >= 18, `Character body must travel at least 18 stage pixels; measured ${bodyTravel.toFixed(2)}`)
  assert.ok(motionMetrics.headRotationDegrees >= 3, `Head must visibly rotate at least 3 degrees; measured ${motionMetrics.headRotationDegrees.toFixed(2)}`)
  assert.ok(motionMetrics.waveDeformationScaleSpan >= 180, `Greeting arm must strongly deform in both directions; scale span ${motionMetrics.waveDeformationScaleSpan.toFixed(2)}`)
  assert.ok(new Set(frames.map(item => item.sha256)).size > 1, 'Rendered character pixels must change between animation frames')
  record('Within 1.8 seconds the character travels at least 18 px, turns her head at least 3 degrees and strongly waves the connected arm')
  const blink = await frame(4410)
  assert.notEqual(blink.eyeOpen, frames[0].measurements.eyeOpen, 'Original eye artwork must animate into a blink')
  assert.ok(blink.eyeClosed > 0.8, 'Closed-eye artwork must appear during the blink')
  record('Blink changes the actual anime eye artwork instead of adding unrelated effects')
  await renderedHandMotion()
  await previewGif()

  const desktopToggle = page.locator('.motion-toggle')
  await desktopToggle.click()
  await page.locator('.login-world svg.login-scene').waitFor({ state: 'visible' })
  await page.waitForFunction(() => document.querySelector('.login-world svg.login-scene')?.dataset.motionState === 'paused')
  assert.equal(await desktopToggle.getAttribute('aria-pressed'), 'true')
  await desktopToggle.click()
  await page.waitForFunction(() => document.querySelector('.login-world svg.login-scene')?.dataset.motionState === 'running')
  assert.equal(await desktopToggle.getAttribute('aria-pressed'), 'false')
  record('Visible pause and resume control changes the character animation state')
  await desktopToggle.click()
  await page.waitForFunction(() => document.querySelector('.login-world svg.login-scene')?.dataset.motionState === 'paused')
  await page.getByRole('button', { name: '重播人物登场动画', exact: true }).click()
  await page.waitForFunction(() => document.querySelector('.login-world svg.login-scene')?.dataset.motionState === 'running')
  assert.equal(await desktopToggle.getAttribute('aria-pressed'), 'false')
  const replayedEntrance = await scene().locator('[data-motion-part="entrance"]').evaluate(element => {
    const animation = element.getAnimations().find(item => item.effect?.target === element)
    return { transform: getComputedStyle(element).transform, running: animation?.playState, elapsed: animation?.currentTime }
  })
  assert.equal(replayedEntrance.running, 'running', 'Replay must start a fresh entrance animation')
  assert.ok(replayedEntrance.elapsed < 500, 'Replay must restart the entrance within the first half second')
  assert.notEqual(replayedEntrance.transform, 'none')
  assert.notEqual(replayedEntrance.transform, 'matrix(1, 0, 0, 1, 0, 0)')
  record('Replay resumes a paused character and restarts the visible entrance animation')

  await page.setViewportSize({ width: 390, height: 844 })
  const portrait = page.locator('.login-mobile-portrait svg.koharu-portrait')
  await portrait.waitFor({ state: 'visible' })
  await page.waitForFunction(() => document.querySelector('.login-mobile-portrait svg.koharu-portrait')?.dataset.motionState === 'running')
  assert.equal(await portrait.getAttribute('data-motion-state'), 'running')
  const portraitBlink = await portrait.evaluate(async svg => {
    svg.pauseAnimations()
    svg.setCurrentTime(4.41)
    for (const animation of svg.getAnimations({ subtree: true })) {
      animation.pause()
      animation.currentTime = 4410
    }
    await new Promise(resolve => requestAnimationFrame(resolve))
    return { closed: Number(getComputedStyle(svg.querySelector('.eye-closed')).opacity), open: getComputedStyle(svg.querySelector('.eye-open')).transform }
  })
  assert.ok(portraitBlink.closed > 0.8, 'Portrait SVG must preserve the blink animation')
  assert.notEqual(portraitBlink.open, 'matrix(1, 0, 0, 1, 0, 0)')
  record('Mobile portrait keeps the live SVG character and blink')

  // Reload after deterministic sampling so browser visibility checks use the normal timeline.
  await page.setViewportSize({ width: 1440, height: 1000 })
  await page.reload({ waitUntil: 'domcontentloaded' })
  await scene().waitFor({ state: 'visible' })
  const hiddenScene = await page.addStyleTag({ content: '.login-world { display: none !important; }' })
  await page.waitForFunction(() => {
    const svg = document.querySelector('.login-world svg.login-scene')
    return svg && [...svg.getAnimations({ subtree: true })].every(animation => animation.playState !== 'running') && (!svg.querySelector('animate, animateTransform') || svg.animationsPaused())
  })
  await hiddenScene.evaluate(element => element.remove())
  await scene().waitFor({ state: 'visible' })
  record('Animation pauses when the login illustration is outside the visible layout')

  await page.emulateMedia({ reducedMotion: 'reduce' })
  await page.waitForFunction(() => {
    const svg = document.querySelector('.login-world svg.login-scene')
    return svg && [...svg.getAnimations({ subtree: true })].every(animation => animation.playState !== 'running') && (!svg.querySelector('animate, animateTransform') || svg.animationsPaused())
  })
  const staticOne = await scene().screenshot({ path: resolve(output, 'reduced-motion.png'), animations: 'allow' })
  await page.waitForTimeout(300)
  const staticTwo = await scene().screenshot({ animations: 'allow' })
  assert.deepEqual(staticOne, staticTwo, 'Reduced-motion rendering must remain static')
  record('Reduced-motion preference keeps the anime character visible and still')
  for (const [width, height] of [[1440, 1000], [768, 1024], [390, 844]]) {
    await page.setViewportSize({ width, height })
    await page.screenshot({ path: resolve(output, `login-${width}.png`), fullPage: true, animations: 'disabled' })
    assert.ok(await page.getByPlaceholder('输入你的账号').isVisible())
    assert.ok(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), `Login overflows at ${width}px`)
  }
  record('Animated login retains usable desktop, tablet and phone layouts')
  assert.deepEqual(errors, [])
} finally {
  await writeFile(resolve(output, 'verification.json'), JSON.stringify({ url, checks, motionMetrics, frames, errors }, null, 2))
  await browser.close()
}
