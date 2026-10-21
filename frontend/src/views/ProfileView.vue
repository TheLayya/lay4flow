<script setup>
import { onMounted } from 'vue'
import { ArrowLeft, CopyDocument, Refresh, CircleClose, Monitor, Lock } from '@element-plus/icons-vue'
import BrandMark from '../components/BrandMark.vue'

function returnToManagement() { location.assign('/manage') }
onMounted(async () => {
  document.querySelector('meta[name="referrer"]')?.setAttribute('content', 'no-referrer')
  if (!document.querySelector('meta[name="referrer"]')) {
    const referrer = document.createElement('meta')
    referrer.name = 'referrer'; referrer.content = 'no-referrer'; document.head.appendChild(referrer)
  }
  try {
    const viewerModule = '/static/js/profile_browser.js?v=20261022-native-profile'
    await import(/* @vite-ignore */ viewerModule)
  } catch (error) {
    const status = document.getElementById('status')
    status.textContent = `浏览器组件加载中断：${error.message}`
    status.dataset.error = 'true'
    for (const id of ['refreshOpenCredentials', 'refreshCredentials', 'closeBrowser']) document.getElementById(id).disabled = true
  }
})
</script>

<template>
  <div class="profile-view">
    <header class="profile-header">
      <button class="profile-brand" aria-label="返回管理页" @click="returnToManagement"><BrandMark small /></button>
      <div class="profile-heading"><h1 id="accountTitle">账号浏览器</h1></div>
      <div class="profile-actions" role="navigation" aria-label="浏览器操作">
        <button id="focusNativeBrowser" class="el-button" type="button" hidden><el-icon><Monitor /></el-icon><span>显示本地窗口</span></button>
        <button id="pasteText" class="el-button" type="button" disabled aria-controls="pastePanel" aria-expanded="false"><el-icon><CopyDocument /></el-icon><span>粘贴文本</span></button>
        <button id="refreshOpenCredentials" class="el-button el-button--primary" type="button"><el-icon><Refresh /></el-icon><span>刷新凭据，保持窗口</span></button>
        <button id="refreshCredentials" class="el-button is-plain" type="button"><el-icon><Refresh /></el-icon><span>关闭并刷新凭据</span></button>
        <button id="closeBrowser" class="el-button el-button--danger is-plain" type="button"><el-icon><CircleClose /></el-icon><span>关闭浏览器和窗口</span></button>
        <el-button :icon="ArrowLeft" text @click="returnToManagement">返回管理页</el-button>
      </div>
    </header>
    <div class="connection-strip"><span class="connection-indicator"></span><div id="status" role="status" aria-live="polite">正在连接浏览器…</div><span class="session-note"><el-icon><Lock /></el-icon>账号独立会话</span></div>
    <section id="pastePanel" class="paste-panel" role="dialog" aria-modal="false" aria-labelledby="pasteLabel" hidden>
      <div class="paste-heading"><span class="paste-icon"><el-icon><CopyDocument /></el-icon></span><div><label id="pasteLabel" for="pasteInput">粘贴文本或密码</label><small>将文字输入到账号浏览器中</small></div></div>
      <p>先点选画面中的输入框，再在这里粘贴。内容以圆点显示，输入后自动清空；换行和制表符会被忽略。</p>
      <input id="pasteInput" type="password" autocomplete="off" spellcheck="false" autocapitalize="off" aria-label="要粘贴的文本或密码" placeholder="在这里粘贴文本" />
      <div class="paste-actions"><button id="pasteCancel" class="el-button" type="button"><span>取消</span></button><button id="pasteSend" class="el-button el-button--primary" type="button" disabled><span>输入到浏览器</span></button></div>
    </section>
    <main id="screen" class="browser-screen" aria-label="可交互的账号浏览器"><div class="screen-loading"><span class="screen-icon"><el-icon><Monitor /></el-icon></span><h2>正在打开你的账号空间</h2><p>浏览器画面将在这里显示。连接完成后，可直接操作画面。</p><span class="loading-orbit"><i></i><i></i><i></i></span></div></main>
    <footer id="profileFooter" class="profile-footer"><el-icon><Monitor /></el-icon><p>点选画面中的输入框后，可用 Ctrl / ⌘ + V 或顶部「粘贴文本」输入。</p><small>「刷新凭据，保持窗口」可刷新登录态并继续调试。</small></footer>
  </div>
</template>

<style scoped>
.profile-view{display:flex;flex-direction:column;height:100dvh;min-height:360px;color:var(--ink);background:#302a37}
.profile-header{display:flex;align-items:center;gap:25px;flex-wrap:wrap;padding:19px 26px;background:var(--surface)}
.profile-brand{padding:0;background:none;border:0;cursor:pointer;color:var(--accent)}
.profile-brand :deep(.brand){display:flex;align-items:center;gap:9px}
.profile-brand :deep(.brand-avatar){width:34px;height:34px}
.profile-brand :deep(.brand-text){font-size:20px;font-weight:750;letter-spacing:-.8px}
.profile-brand :deep(.brand-text>span){color:var(--blue)}
.profile-heading{padding-left:23px}
.profile-heading h1{font-size:15px;line-height:1.4;margin:5px 0 0;font-weight:650}
.profile-actions{margin-left:auto;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.profile-actions :deep(.el-button){margin:0;font-size:14px;border-radius:10px}
.connection-strip{display:flex;align-items:center;gap:10px;padding:10px 27px;background:var(--pink-soft);font-size:13px;min-height:39px;}
.connection-indicator{width:6px;height:6px;flex:none;border-radius:50%;background:var(--success)}
#status{flex:1;min-width:0;overflow-wrap:anywhere;color:var(--muted)}
#status[data-error="true"]{color:var(--el-color-danger)}
.session-note{display:flex;align-items:center;gap:5px;color:var(--muted);font-size:13px;white-space:nowrap}
.browser-screen{position:relative;flex:1;min-height:0;overflow:hidden;background:#302a37}
.browser-screen[data-presentation="native"]{display:grid;place-items:center;padding:32px;background:var(--surface-soft);color:var(--ink)}
.browser-screen[data-presentation="native"] :deep(p){max-width:540px;line-height:2;font-size:16px;text-align:center}
.screen-loading{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;flex-direction:column;text-align:center;padding:25px;color:#eee2ed}
.screen-icon{display:grid;place-items:center;width:73px;height:73px;border-radius:24px;background:#45394a;color:#cb9bb6;font-size:30px;margin-bottom:22px;}
.screen-loading h2{font-size:20px;font-weight:550;margin:0 0 12px}
.screen-loading p{font-size:14px;line-height:1.9;color:#b29bb3;max-width:400px}
.loading-orbit{display:flex;gap:6px;margin-top:15px}
.loading-orbit i{width:5px;height:5px;border-radius:50%;background:#c694b0;animation:profile-dot 1.3s ease-in-out infinite}
.loading-orbit i:nth-child(2){animation-delay:.15s}
.loading-orbit i:nth-child(3){animation-delay:.3s}
.profile-footer{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:11px 26px;background:var(--surface);color:var(--muted);}
.profile-footer>.el-icon{color:var(--accent);font-size:18px}
.profile-footer p{font-size:13px;line-height:1.7;margin:0}
.profile-footer small{font-size:13px;margin-left:auto;color:var(--muted)}
.paste-panel{position:fixed;top:133px;right:27px;width:370px;max-width:calc(100vw - 32px);padding:23px;border-radius:19px;background:var(--surface);box-shadow:0 16px 60px #25172c40;z-index:30} /* taste-skill-ignore: Floating paste dialog needs an overlay shadow to separate it from the live browser screen. */
.paste-panel[hidden]{display:none}
.paste-heading{display:flex;align-items:center;gap:12px}
.paste-icon{display:grid;place-items:center;width:39px;height:39px;border-radius:12px;background:var(--pink-soft);color:var(--accent);font-size:18px}
.paste-heading label{font-size:14px;font-weight:650}
.paste-heading small{display:block;font-size:13px;color:var(--muted);margin-top:4px}
.paste-panel p{font-size:13px;line-height:1.9;color:var(--muted);margin:18px 0 12px}
#pasteInput{width:100%;font:inherit;font-size:14px;border:1px solid var(--line);border-radius:11px;color:var(--ink);background:var(--surface);padding:12px 13px;outline:none} /* taste-skill-ignore: Native password input keeps its field boundary for focus and sensitive text entry. */
#pasteInput:focus-visible{border-color:var(--accent);outline:2px solid var(--accent);outline-offset:2px}
.paste-actions{display:flex;justify-content:flex-end;gap:8px;margin-top:18px}
.paste-actions :deep(.el-button){margin:0;font-size:14px}
.profile-brand:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
@keyframes profile-dot{0%,80%,100%{opacity:.3;transform:translateY(0)}
40%{opacity:1;transform:translateY(-4px)}
}
@media(max-width:1000px){.profile-header{gap:17px;padding:15px 20px}
.profile-actions{width:100%;margin:0}
.profile-heading{margin-left:auto;border:0;padding:0;text-align:right}
.connection-strip{padding:10px 20px}
.paste-panel{top:175px;right:18px}
.profile-footer{padding:10px 20px}
.profile-footer small{display:none}
}
@media(max-width:560px){.profile-header{padding:13px 15px;gap:12px}
.profile-actions{gap:6px}
.profile-actions :deep(.el-button){font-size:14px;padding:8px 10px}
.profile-heading h1{font-size:16px}
.profile-brand :deep(.brand-text){font-size:18px}
.profile-brand :deep(.brand-avatar){width:29px;height:29px}
.session-note{display:none}
.connection-strip{padding:10px 16px}
.profile-footer{padding:9px 16px;gap:8px}
.profile-footer p{font-size:13px}
.paste-panel{top:190px;right:16px;padding:19px}
.screen-loading h2{font-size:17px}
}
@media(prefers-reduced-motion:reduce){.loading-orbit i{animation:none}
}

</style>
