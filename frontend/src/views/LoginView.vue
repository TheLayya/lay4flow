<script setup>
import { onMounted, reactive, ref } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
    User,
    Lock,
    ArrowRight,
    Picture,
    VideoCamera,
    Headset,
    VideoPause,
    VideoPlay,
    RefreshRight,
} from '@element-plus/icons-vue'
import { api } from '../lib/api'
import { acceptSession } from '../router'
import BrandMark from '../components/BrandMark.vue'
import KoharuScene from '../components/KoharuScene.vue'

const router = useRouter(),
    route = useRoute()
const scene = ref(null),
    mobileScene = ref(null),
    motionPaused = ref(false)
function toggleMotion() {
    motionPaused.value = !motionPaused.value
    scene.value?.toggle()
    mobileScene.value?.toggle()
}
function replayMotion() {
    motionPaused.value = false
    scene.value?.replay()
    mobileScene.value?.replay()
}
const form = reactive({ username: '', password: '' }),
    formRef = ref(),
    busy = ref(false),
    error = ref('')
const rules = {
    username: [{ required: true, message: '请输入管理员账号', trigger: ['blur', 'change'] }],
    password: [{ required: true, message: '请输入登录密码', trigger: ['blur', 'change'] }],
}
const nextPath = () => {
    const next = route.query.next
    if (typeof next !== 'string' || !next.startsWith('/') || next.startsWith('//')) return '/dashboard'
    const path = next.split(/[?#]/, 1)[0]
    return path === '/' || path.startsWith('/login') ? '/dashboard' : next
}
async function login() {
    if (busy.value) return
    busy.value = true
    error.value = ''
    if (!(await formRef.value.validate().catch(() => false))) {
        busy.value = false
        return
    }
    try {
        const data = await api.post('/api/login', form)
        localStorage.removeItem('token')
        localStorage.setItem('admin_username', data.username || form.username)
        form.password = ''
        acceptSession()
        router.replace(nextPath())
    } catch (e) {
        error.value = e.status === 401 ? '账号或密码不匹配，请检查后重试。' : e.message
    } finally {
        busy.value = false
    }
}
onMounted(async () => {
    try {
        await api.get('/api/stats')
        acceptSession()
        router.replace(nextPath())
    } catch {}
})
</script>
<template>
    <main id="main-content" class="login-page">
        <section class="login-world" aria-label="灵感工坊">
            <KoharuScene ref="scene" class="login-scene" />
            <BrandMark />
            <el-button class="motion-replay" :icon="RefreshRight" aria-label="重播人物登场动画" @click="replayMotion">
                再来一次
            </el-button>
            <el-button
                class="motion-toggle"
                :icon="motionPaused ? VideoPlay : VideoPause"
                :aria-label="motionPaused ? '播放人物动画' : '暂停人物动画'"
                :aria-pressed="motionPaused"
                @click="toggleMotion"
            >
                {{ motionPaused ? '播放动画' : '暂停动画' }}
            </el-button>
            <div class="login-world-caption">
                <p>让想象，在这里发生。</p>
                <span>你的创作搭档，lay</span>
            </div>
        </section>
        <section class="login-entry">
            <div class="login-mobile-brand"><BrandMark /></div>
            <div class="login-form-wrap">
                <div class="login-mobile-portrait">
                    <KoharuScene ref="mobileScene" portrait />
                </div>
                <el-button
                    class="motion-toggle-mobile"
                    text
                    :icon="motionPaused ? VideoPlay : VideoPause"
                    :aria-label="motionPaused ? '播放人物动画' : '暂停人物动画'"
                    :aria-pressed="motionPaused"
                    @click="toggleMotion"
                >
                    {{ motionPaused ? '播放动画' : '暂停动画' }}
                </el-button>
                <h1>欢迎回到灵感工坊</h1>
                <p class="login-subtitle">登录账号，继续你的创作。</p>
                <el-alert v-if="error" :title="error" type="error" show-icon :closable="false" class="login-error" />
                <el-form
                    ref="formRef"
                    :model="form"
                    :rules="rules"
                    label-position="top"
                    size="large"
                    @submit.prevent="login"
                >
                    <el-form-item label="管理员账号" prop="username">
                        <el-input
                            v-model="form.username"
                            placeholder="输入你的账号"
                            :prefix-icon="User"
                            autocomplete="username"
                        />
                    </el-form-item>
                    <el-form-item label="登录密码" prop="password">
                        <el-input
                            v-model="form.password"
                            type="password"
                            placeholder="输入登录密码"
                            :prefix-icon="Lock"
                            autocomplete="current-password"
                            show-password
                        />
                    </el-form-item>
                    <el-button type="primary" native-type="submit" :loading="busy" class="login-submit">
                        {{ busy ? '正在打开工坊' : '进入灵感工坊' }}
                        <el-icon v-if="!busy"><ArrowRight /></el-icon>
                    </el-button>
                </el-form>
                <div class="login-note">
                    <el-icon><Lock /></el-icon>
                    <span>使用工坊管理员账号登录</span>
                </div>
                <router-link class="login-doc-link" to="/guide">
                    开发者？查看 API 文档
                    <el-icon><ArrowRight /></el-icon>
                </router-link>
            </div>
            <footer class="login-footer">
                <div class="login-mediums">
                    <span>
                        <el-icon><Picture /></el-icon>
                        图片
                    </span>
                    <span>
                        <el-icon><VideoCamera /></el-icon>
                        视频
                    </span>
                    <span>
                        <el-icon><Headset /></el-icon>
                        声音
                    </span>
                </div>
                <span>lay4flow-remote · 为每一份灵感留个位置</span>
            </footer>
        </section>
    </main>
</template>
<style scoped>
.login-page {
    display: grid;
    grid-template-columns: 58% 42%;
    min-height: 100svh;
    background: var(--surface);
}
.login-world {
    position: relative;
    min-height: 760px;
    overflow: hidden;
    background: #e3c2ba;
}
.login-scene {
    width: 100%;
    height: 100%;
    position: absolute;
    inset: 0;
    object-fit: cover;
    object-position: center top;
    animation: scene-arrive 0.85s ease-out both;
}
.motion-toggle.el-button {
    position: absolute;
    z-index: 2;
    right: 30px;
    top: 36px;
    border: 0;
    background: rgb(255 255 255 / 91%);
    color: #725265;
    font-size: 12px;
    min-height: 38px;
    border-radius: 12px;
}
.motion-replay.el-button {
    position: absolute;
    z-index: 2;
    right: 148px;
    top: 36px;
    min-height: 38px;
    margin: 0;
    border: 0;
    border-radius: 12px;
    background: #cf4b7f;
    color: #fff;
    font-size: 12px;
}
.motion-toggle-mobile.el-button {
    display: none;
}
.login-world > :deep(.brand) {
    position: absolute;
    z-index: 1;
    left: 42px;
    top: 36px;
    padding: 12px 16px;
    background: rgb(255 255 255 / 91%);
    border-radius: 16px;
    color: #c84e7b;
}
.login-world > :deep(.brand .brand-text) {
    color: #283348;
    font-size: 27px;
}
.login-world-caption {
    position: absolute;
    bottom: 35px;
    left: 42px;
    right: 42px;
    padding: 16px 22px;
    background: rgb(255 255 255 / 92%);
    border-radius: 16px;
    color: #283348;
    animation: enter-workspace 0.65s 0.15s both;
}
.login-world-caption p {
    font-size: 20px;
    font-weight: 650;
    letter-spacing: -0.025em;
}
.login-world-caption span {
    display: block;
    margin-top: 5px;
    font-size: 13px;
    color: #766173;
}
.login-entry {
    padding: 64px 46px 28px;
    position: relative;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    background: var(--surface);
    min-width: 0;
}
.login-form-wrap {
    max-width: 390px;
    width: 100%;
    margin: auto 0;
}
.login-form-wrap h1 {
    font-size: clamp(25px, 2.1vw, 32px);
    color: var(--ink);
    line-height: 1.35;
    font-weight: 750;
    letter-spacing: -0.05em;
    animation: enter-workspace 0.55s 0.1s both;
}
.login-subtitle {
    font-size: 15px;
    color: var(--muted);
    margin: 13px 0 40px;
    animation: enter-workspace 0.55s 0.17s both;
}
.login-form-wrap :deep(.el-form-item) {
    margin-bottom: 26px;
    animation: enter-workspace 0.55s 0.22s both;
}
.login-form-wrap :deep(.el-form-item:nth-child(2)) {
    animation-delay: 0.29s;
}
.login-form-wrap :deep(.el-form-item__label) {
    font-size: 14px;
    color: var(--ink);
    font-weight: 550;
    height: auto;
    padding-bottom: 10px;
}
.login-form-wrap :deep(.el-input__wrapper) {
    min-height: 49px;
    padding: 0 14px;
    border-radius: 10px;
    background: var(--surface);
}
.login-form-wrap :deep(.el-input__inner) {
    font-size: 14px;
}
.login-form-wrap :deep(.el-input__prefix) {
    margin-right: 6px;
}
.login-form-wrap :deep(.el-input__wrapper.is-focus) {
    outline: 2px solid var(--accent);
    outline-offset: -2px;
}
.login-submit.el-button {
    width: 100%;
    height: 49px;
    border-radius: 10px;
    font-size: 15px;
    font-weight: 600;
    margin-top: 8px;
    animation: enter-workspace 0.55s 0.35s both;
}
.login-submit .el-icon {
    margin-left: 20px;
    font-size: 17px;
}
.login-note {
    display: flex;
    gap: 6px;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    color: var(--muted);
    margin: 19px 0 35px;
    animation: enter-workspace 0.55s 0.42s both;
}
.login-doc-link {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 7px;
    font-size: 14px;
    color: var(--accent);
    animation: enter-workspace 0.55s 0.48s both;
}
.login-doc-link:hover {
    color: var(--accent-strong);
}
.login-footer {
    text-align: center;
    padding-top: 64px;
    width: 100%;
    color: var(--muted);
    font-size: 12px;
}
.login-mediums {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 28px;
    margin-bottom: 12px;
}
.login-mediums > span {
    display: flex;
    align-items: center;
    gap: 7px;
    font-size: 13px;
}
.login-mediums .el-icon {
    font-size: 16px;
    color: var(--accent);
}
.login-footer > span {
    font-size: 12px;
}
.login-mobile-brand,
.login-mobile-portrait {
    display: none;
}
.login-error {
    margin-bottom: 22px;
}
html.dark .login-scene {
    filter: brightness(0.8) saturate(0.9);
}
@keyframes scene-arrive {
    from {
        opacity: 0.5;
        transform: scale(1.025);
    }
    to {
        opacity: 1;
        transform: scale(1);
    }
}
@media (min-width: 1800px) {
    .login-entry {
        padding-left: 90px;
        padding-right: 90px;
    }
    .login-form-wrap {
        max-width: 430px;
    }
    .login-form-wrap h1 {
        font-size: 36px;
    }
    .login-scene {
        object-position: center 28%;
    }
}
@media (max-width: 1100px) {
    .login-page {
        grid-template-columns: 52% 48%;
    }
    .login-entry {
        padding: 55px 34px 27px;
    }
    .login-world > :deep(.brand) {
        top: 28px;
        left: 28px;
    }
    .login-world-caption {
        left: 28px;
        right: 28px;
        bottom: 28px;
    }
    .login-form-wrap h1 {
        font-size: 28px;
    }
}
@media (max-width: 767px) {
    .login-page {
        display: block;
    }
    .login-world {
        display: none;
    }
    .login-entry {
        min-height: 100svh;
        padding: 30px 26px 25px;
    }
    .login-mobile-brand {
        display: block;
        width: 100%;
    }
    .login-mobile-brand :deep(.brand-text) {
        font-size: 25px;
    }
    .login-form-wrap {
        margin: 32px 0 auto;
        max-width: 390px;
    }
    .login-mobile-portrait {
        display: block;
        width: 86px;
        height: 86px;
        overflow: hidden;
        border-radius: 50%;
        background: var(--pink-soft);
        margin-bottom: 24px;
    }
    .motion-toggle-mobile.el-button {
        display: inline-flex;
        position: absolute;
        right: 0;
        top: 23px;
        font-size: 12px;
        color: var(--muted);
    }
    .login-form-wrap {
        position: relative;
    }
    .login-mobile-portrait img {
        width: 100%;
        height: 100%;
        object-fit: cover;
        object-position: center 15%;
    }
    .login-form-wrap h1 {
        font-size: 30px;
    }
    .login-subtitle {
        margin-bottom: 30px;
        font-size: 14px;
    }
    .login-note {
        margin-bottom: 25px;
    }
    .login-footer {
        padding-top: 44px;
    }
    .login-mediums {
        gap: 30px;
    }
    .login-footer > span {
        font-size: 12px;
    }
}
@media (max-width: 400px) {
    .login-entry {
        padding: 26px 23px 22px;
    }
    .login-form-wrap h1 {
        font-size: 28px;
    }
    .login-mobile-portrait {
        width: 74px;
        height: 74px;
        margin-bottom: 20px;
    }
    .login-form-wrap {
        margin-top: 24px;
    }
}
@media (prefers-reduced-motion: reduce) {
    .login-scene,
    .login-world-caption,
    .login-form-wrap h1,
    .login-subtitle,
    .login-form-wrap :deep(.el-form-item),
    .login-submit,
    .login-note,
    .login-doc-link {
        animation: none;
    }
}
</style>
