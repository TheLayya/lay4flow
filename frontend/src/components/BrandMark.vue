<script setup>
import avatar from '../assets/koharu-avatar.webp'
defineProps({ small: Boolean })
</script>
<template>
    <span class="brand" :class="{ 'brand-small': small }">
        <img class="brand-avatar" :src="avatar" alt="萌娘 lay" width="40" height="40" draggable="false" />
        <span class="brand-text">
            lay
            <span>4</span>
            flow
            <small>remote</small>
        </span>
    </span>
</template>
