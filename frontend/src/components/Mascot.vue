<script setup>
import { computed } from 'vue'
import character from '../assets/koharu-character.webp'
import portraitImage from '../assets/koharu-portrait.webp'
import welcome from '../assets/koharu-welcome.webp'
const props = defineProps({
    mood: { type: String, default: 'happy' },
    pose: { type: String, default: 'character' },
    compact: Boolean,
    portrait: Boolean,
})
const image = computed(() =>
    props.portrait || props.compact || props.pose === 'portrait'
        ? portraitImage
        : props.pose === 'welcome'
          ? welcome
          : character,
)
const dimensions = computed(() =>
    image.value === portraitImage ? [449, 640] : image.value === welcome ? [766, 1000] : [772, 1680],
)
</script>
<template>
    <figure
        class="mascot"
        :class="[
            { 'mascot-compact': compact, 'mascot-portrait': portrait || pose === 'portrait' },
            `mascot-${mood}`,
            `mascot-pose-${pose}`,
        ]"
    >
        <img
            :src="image"
            alt="lay，樱发蓝眸的日系动漫创作助手"
            :width="dimensions[0]"
            :height="dimensions[1]"
            draggable="false"
            decoding="async"
        />
    </figure>
</template>
<style scoped>
.mascot {
    margin: 0;
    position: relative;
    line-height: 0;
    isolation: isolate;
}
.mascot img {
    width: 100%;
    height: 100%;
    object-fit: contain;
    object-position: center top;
}
.mascot-compact img {
    object-position: center 10%;
    object-fit: cover;
}
.mascot-compact {
    overflow: hidden;
    border-radius: 50%;
    aspect-ratio: 1;
    background: var(--pink-soft);
    border: 3px solid var(--surface);
}
.mascot-portrait:not(.mascot-compact) img {
    object-fit: contain;
}
</style>
