<script setup>
import { computed, onMounted, onUnmounted, ref, useId, watch } from 'vue'
import background from '../assets/koharu-login-background.webp'
import bodyArtwork from '../assets/koharu-motion-base-energetic.webp'
import headArtwork from '../assets/koharu-motion-head-energetic.webp'
import armArtwork from '../assets/koharu-motion-wave.webp'
import leftHairArtwork from '../assets/koharu-motion-hair-left.webp'
import rightHairArtwork from '../assets/koharu-motion-hair-right.webp'
import originalCharacter from '../assets/koharu-character.webp'
import waveDisplacement from '../assets/koharu-wave-displacement.png'

const props = defineProps({ portrait: Boolean })
const root = ref(null)
const visible = ref(false)
const reduced = ref(false)
const pageVisible = ref(true)
const userPaused = ref(false)
const performanceKey = ref(0)
const ids = useId().replaceAll(':', '')
const id = (name) => `${ids}-${name}`
const state = computed(() =>
    reduced.value ? 'reduced' : visible.value && pageVisible.value && !userPaused.value ? 'running' : 'paused',
)
let observer, media
function syncVisibility() {
    pageVisible.value = !document.hidden
}
function syncPreference() {
    reduced.value = media.matches
}
function toggle() {
    userPaused.value = !userPaused.value
}
function replay() {
    userPaused.value = false
    performanceKey.value++
    root.value?.setCurrentTime(0)
}
watch(
    [root, state],
    ([svg, motion]) => {
        if (!svg) return
        if (motion === 'running') svg.unpauseAnimations()
        else svg.pauseAnimations()
    },
    { flush: 'post' },
)
defineExpose({ toggle, replay, state })
onMounted(() => {
    syncVisibility()
    media = matchMedia('(prefers-reduced-motion: reduce)')
    syncPreference()
    media.addEventListener('change', syncPreference)
    document.addEventListener('visibilitychange', syncVisibility)
    observer = new IntersectionObserver(
        (entries) => {
            visible.value = entries.some((entry) => entry.isIntersecting)
        },
        { threshold: 0.01 },
    )
    observer.observe(root.value)
})
onUnmounted(() => {
    observer?.disconnect()
    media?.removeEventListener('change', syncPreference)
    document.removeEventListener('visibilitychange', syncVisibility)
})
</script>

<template>
    <svg
        ref="root"
        class="koharu-scene"
        :class="{ 'koharu-portrait': portrait }"
        :viewBox="portrait ? '230 4 325 350' : '0 0 1000 1400'"
        preserveAspectRatio="xMidYMin slice"
        :data-motion-state="state"
        role="img"
        :aria-labelledby="id('title')"
        xmlns="http://www.w3.org/2000/svg"
    >
        <title :id="id('title')">lay 元气登场，挥手、歪头，长发随动作摆动，欢迎你开始创作</title>
        <defs>
            <filter
                v-if="!portrait"
                :id="id('wave')"
                x="-100"
                y="0"
                width="972"
                height="1680"
                filterUnits="userSpaceOnUse"
                color-interpolation-filters="sRGB"
            >
                <feImage
                    :href="waveDisplacement"
                    x="-100"
                    y="0"
                    width="972"
                    height="1680"
                    preserveAspectRatio="none"
                    result="wave-field"
                />
                <feComponentTransfer in="wave-field" result="wave-neutral">
                    <feFuncR type="linear" slope="1" intercept="-0.00196078431372549" />
                    <feFuncG type="linear" slope="1" intercept="-0.00196078431372549" />
                </feComponentTransfer>
                <feGaussianBlur in="wave-neutral" stdDeviation=".6" result="wave-smooth" />
                <feDisplacementMap
                    class="wave-displacement"
                    in="SourceGraphic"
                    in2="wave-smooth"
                    xChannelSelector="R"
                    yChannelSelector="G"
                    scale="0"
                >
                    <animate
                        attributeName="scale"
                        dur="3.6s"
                        values="-108;120;-120;120;-120;96;-108"
                        keyTimes="0;.18;.36;.54;.72;.88;1"
                        calcMode="spline"
                        keySplines=".42 0 .58 1;.42 0 .58 1;.42 0 .58 1;.42 0 .58 1;.42 0 .58 1;.42 0 .58 1"
                        repeatCount="indefinite"
                    />
                </feDisplacementMap>
            </filter>
            <clipPath :id="id('left-eye')">
                <path d="M282 218 C302 191 335 193 357 217 L358 239 C338 258 299 253 282 231Z" />
            </clipPath>
            <clipPath :id="id('right-eye')">
                <path d="M397 202 C415 176 453 181 476 202 L475 229 C456 247 420 243 399 223Z" />
            </clipPath>
            <mask :id="id('head-without-eyes')" maskUnits="userSpaceOnUse" x="0" y="0" width="772" height="1680">
                <rect width="772" height="1680" fill="white" />
                <path
                    d="M282 218 C302 191 335 193 357 217 L358 239 C338 258 299 253 282 231Z M397 202 C415 176 453 181 476 202 L475 229 C456 247 420 243 399 223Z"
                    fill="black"
                />
            </mask>
            <path :id="id('petal')" d="M0 0C-9-13-13-24-1-29C9-25 13-12 0 0Z" />
        </defs>
        <g v-if="!portrait" class="scene-setting" aria-hidden="true">
            <image :href="background" width="1000" height="1400" preserveAspectRatio="xMidYMin slice" />
            <g class="petals" fill="#f9cadb" opacity=".8">
                <g
                    v-for="(position, index) in [
                        [80, 80],
                        [260, -100],
                        [730, 200],
                        [920, -230],
                        [420, 150],
                        [120, 480],
                        [850, 650],
                    ]"
                    :key="index"
                    :transform="`translate(${position[0]} ${position[1]})`"
                >
                    <use :href="`#${id('petal')}`" class="petal" :class="`petal-${index}`" />
                </g>
            </g>
        </g>
        <g class="koharu-character" :transform="portrait ? undefined : 'translate(175 132) scale(.86)'">
            <g :key="performanceKey" class="character-performance">
                <g v-if="!portrait" class="greeting-accents" aria-hidden="true" fill="none" stroke-linecap="round">
                    <g class="greeting-swoosh">
                        <path d="M595 360Q754 235 822 381" stroke="#fff5fa" stroke-width="21" />
                        <path d="M595 360Q754 235 822 381" stroke="#e885ae" stroke-width="7" />
                        <path d="M765 300l42-42m3 93 61-11m-170-66 7-52" stroke="#85c4df" stroke-width="8" />
                    </g>
                    <g class="greeting-spark" fill="#fff4b5" stroke="#dc90ad" stroke-width="2">
                        <path d="m142 210 9 23 25 8-25 9-9 24-9-24-25-9 25-8Z" />
                        <path d="m734 92 6 17 18 6-18 7-6 17-7-17-17-7 17-6Z" />
                    </g>
                </g>
                <g class="character-entrance" data-motion-part="entrance">
                    <g class="character-breath" data-motion-part="body">
                        <image class="static-character" :href="originalCharacter" width="772" height="1680" />
                        <g class="animated-character" :filter="portrait ? undefined : `url(#${id('wave')})`">
                            <g data-motion-part="hair" class="hair-left">
                                <image :href="leftHairArtwork" width="772" height="1680" />
                            </g>
                            <g class="hair-right"><image :href="rightHairArtwork" width="772" height="1680" /></g>
                            <image :href="bodyArtwork" width="772" height="1680" />
                            <g class="character-head" data-motion-part="head">
                                <image
                                    :href="headArtwork"
                                    width="772"
                                    height="1680"
                                    :mask="`url(#${id('head-without-eyes')})`"
                                />
                                <g class="eye-skin">
                                    <path
                                        d="M282 218 C302 191 335 193 357 217 L358 239 C338 258 299 253 282 231Z"
                                        fill="#fbe8e2"
                                    />
                                    <path
                                        d="M397 202 C415 176 453 181 476 202 L475 229 C456 247 420 243 399 223Z"
                                        fill="#fbe7df"
                                    />
                                </g>
                                <g :clip-path="`url(#${id('left-eye')})`">
                                    <g class="eye-open eye-open-left" data-motion-part="eyes">
                                        <image :href="headArtwork" width="772" height="1680" />
                                    </g>
                                </g>
                                <g :clip-path="`url(#${id('right-eye')})`">
                                    <g class="eye-open eye-open-right">
                                        <image :href="headArtwork" width="772" height="1680" />
                                    </g>
                                </g>
                                <g
                                    class="eye-closed"
                                    fill="none"
                                    stroke="#503541"
                                    stroke-width="4.5"
                                    stroke-linecap="round"
                                >
                                    <path d="M289 223Q322 243 353 221M400 212Q433 232 472 207" />
                                    <path d="m292 225-7-4m14 10-6-2m172-18 8-6m-16 12 7-2" stroke-width="2.8" />
                                </g>
                            </g>
                            <g class="character-arm" data-motion-part="arm">
                                <image :href="armArtwork" width="772" height="1680" />
                            </g>
                        </g>
                    </g>
                </g>
            </g>
        </g>
    </svg>
</template>

<style scoped>
.koharu-scene {
    display: block;
    width: 100%;
    height: 100%;
    overflow: hidden;
    isolation: isolate;
    animation-play-state: running;
}
.koharu-scene :deep(*) {
    animation-play-state: inherit;
}
.koharu-scene[data-motion-state='paused'],
.koharu-scene[data-motion-state='reduced'] {
    animation-play-state: paused;
}
.static-character {
    display: none;
}
.character-breath {
    transform-origin: 386px 1080px;
    animation: character-breathe 3.6s cubic-bezier(0.45, 0, 0.3, 1) infinite;
}
.character-entrance {
    transform-origin: 386px 1000px;
    animation: character-arrive 750ms cubic-bezier(0.2, 0.8, 0.25, 1) both;
}
.greeting-swoosh {
    transform-origin: 710px 380px;
    animation: greeting-swoosh 3.6s ease-out infinite;
}
.greeting-spark {
    transform-origin: 400px 200px;
    animation: greeting-spark 3.6s ease-out infinite;
}
.character-head {
    transform-origin: 388px 350px;
    animation: head-tilt 3.6s cubic-bezier(0.45, 0, 0.3, 1) infinite;
}
.character-arm {
    transform: none;
}
.hair-left {
    transform-origin: 135px 516px;
    animation: hair-sway-left 3.6s ease-in-out infinite;
}
.hair-right {
    transform-origin: 630px 650px;
    animation: hair-sway-right 3.6s ease-in-out infinite;
}
.eye-open {
    transform-box: view-box;
    animation: eye-blink 5.8s linear infinite;
}
.eye-open-left {
    transform-origin: 324px 226px;
}
.eye-open-right {
    transform-origin: 434px 214px;
}
.eye-closed {
    opacity: 0;
    animation: closed-lashes 5.8s linear infinite;
}
.petal {
    animation: petal-fall 12s linear infinite;
    opacity: 0;
}
.petal-1 {
    animation-delay: -3s;
    animation-duration: 15s;
}
.petal-2 {
    animation-delay: -9s;
    animation-duration: 13s;
}
.petal-3 {
    animation-delay: -5s;
    animation-duration: 17s;
}
.petal-4 {
    animation-delay: -12s;
    animation-duration: 16s;
}
.petal-5 {
    animation-delay: -2s;
    animation-duration: 14s;
}
.petal-6 {
    animation-delay: -7s;
    animation-duration: 18s;
}
.koharu-scene[data-motion-state='reduced'] .animated-character {
    display: none;
}
.koharu-scene[data-motion-state='reduced'] .static-character {
    display: block;
}
.koharu-scene[data-motion-state='reduced'] .character-breath {
    transform: none;
    animation: none;
}
.koharu-scene[data-motion-state='reduced'] .petals {
    display: none;
}
.koharu-scene[data-motion-state='reduced'] .character-entrance {
    animation: none;
    transform: none;
}
.koharu-scene[data-motion-state='reduced'] .greeting-accents {
    display: none;
}
.koharu-portrait .character-arm,
.koharu-portrait .hair-left,
.koharu-portrait .hair-right {
    animation: none;
}
.koharu-portrait .character-breath {
    animation: portrait-bounce 3.6s ease-in-out infinite;
    transform-origin: 386px 350px;
}
.koharu-portrait .character-head {
    animation: portrait-tilt 3.6s ease-in-out infinite;
}
@keyframes character-arrive {
    from {
        transform: translate(105px, 75px) rotate(7deg) scale(0.91);
        opacity: 0;
    }
    65% {
        transform: translate(-12px, -9px) rotate(-1deg) scale(1.015);
        opacity: 1;
    }
    to {
        transform: none;
        opacity: 1;
    }
}
@keyframes character-breathe {
    0%,
    100% {
        transform: translate(-12px, 5px) rotate(-1.8deg);
    }
    22% {
        transform: translate(15px, -17px) rotate(1.7deg);
    }
    48% {
        transform: translate(-16px, -5px) rotate(-1.7deg);
    }
    72% {
        transform: translate(11px, -16px) rotate(1.3deg);
    }
}
@keyframes head-tilt {
    0%,
    100% {
        transform: rotate(-2.3deg);
    }
    24% {
        transform: rotate(2.2deg);
    }
    48% {
        transform: rotate(-1.8deg);
    }
    72% {
        transform: rotate(1.9deg);
    }
}
@keyframes hair-sway-left {
    0%,
    100% {
        transform: rotate(1.8deg);
    }
    28%,
    78% {
        transform: rotate(-2.3deg);
    }
    54% {
        transform: rotate(2deg);
    }
}
@keyframes hair-sway-right {
    0%,
    100% {
        transform: rotate(-1.9deg);
    }
    28%,
    78% {
        transform: rotate(2.1deg);
    }
    54% {
        transform: rotate(-2deg);
    }
}
@keyframes portrait-bounce {
    0%,
    100% {
        transform: translate(-2px, 3px);
    }
    25%,
    75% {
        transform: translate(3px, -5px);
    }
    50% {
        transform: translate(-3px, -1px);
    }
}
@keyframes portrait-tilt {
    0%,
    100% {
        transform: rotate(-1.6deg);
    }
    25%,
    75% {
        transform: rotate(1.6deg);
    }
    50% {
        transform: rotate(-1.3deg);
    }
}
@keyframes greeting-swoosh {
    0%,
    8%,
    82%,
    100% {
        opacity: 0;
        transform: rotate(-12deg) scale(0.88);
    }
    22%,
    60% {
        opacity: 0.8;
        transform: rotate(8deg) scale(1.04);
    }
}
@keyframes greeting-spark {
    0%,
    100% {
        opacity: 0;
        transform: scale(0.75) rotate(-5deg);
    }
    18%,
    60% {
        opacity: 0.9;
        transform: scale(1.1) rotate(4deg);
    }
    42%,
    82% {
        opacity: 0.35;
        transform: scale(0.9) rotate(-3deg);
    }
}
@keyframes eye-blink {
    0%,
    72%,
    79%,
    100% {
        transform: scaleY(1);
    }
    74.7% {
        transform: scaleY(0.55);
    }
    75.5%,
    76.6% {
        transform: scaleY(0.025);
    }
    77.6% {
        transform: scaleY(0.45);
    }
}
@keyframes closed-lashes {
    0%,
    74.7%,
    77.6%,
    100% {
        opacity: 0;
    }
    75.5%,
    76.6% {
        opacity: 1;
    }
}
@keyframes petal-fall {
    0% {
        transform: translate(0, -80px) rotate(-20deg);
        opacity: 0;
    }
    8% {
        opacity: 0.75;
    }
    85% {
        opacity: 0.7;
    }
    100% {
        transform: translate(190px, 930px) rotate(240deg);
        opacity: 0;
    }
}
@media (prefers-reduced-motion: reduce) {
    .koharu-scene * {
        animation: none !important;
    }
    .animated-character,
    .petals,
    .greeting-accents {
        display: none;
    }
    .static-character {
        display: block;
    }
}
</style>
