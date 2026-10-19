import { createRouter, createWebHashHistory } from 'vue-router'
import { api } from './lib/api'

const initialPath = () => {
  const profile = window.location.pathname.match(/^\/api\/profile-browser\/([1-9]\d*)$/)
  if (profile) return `/profile/${profile[1]}`
  const paths = {
    '/manage': '/dashboard',
    '/test': '/studio',
    '/vids': '/studio',
    '/vids-accounts': '/accounts',
    '/vids/accounts': '/accounts',
    '/api-guide': '/guide',
    '/vids-api-guide': '/guide',
    '/vids/api-guide': '/guide',
  }
  const path = paths[window.location.pathname] || '/login'
  return path + (window.location.pathname.includes('vids') ? '?platform=vids' : '')
}
const router = createRouter({
  history: createWebHashHistory(),
  routes: [
    { path: '/', redirect: initialPath },
    { path: '/login', component: () => import('./views/LoginView.vue'), meta: { title: '欢迎回来', public: true } },
    {
      path: '/dashboard',
      component: () => import('./views/DashboardView.vue'),
      meta: { title: '工坊总览', description: '灵感，从这里开始' },
    },
    {
      path: '/studio',
      component: () => import('./views/StudioView.vue'),
      meta: { title: '创作工作台', description: '把想象变成作品' },
    },
    {
      path: '/accounts',
      component: () => import('./views/AccountsView.vue'),
      meta: { title: '账号管理', description: '连接你的创作能量' },
    },
    {
      path: '/proxies',
      component: () => import('./views/ProxyPoolView.vue'),
      meta: { title: '代理池', description: '统一管理账号与媒体下载出口' },
    },
    {
      path: '/logs',
      component: () => import('./views/LogsView.vue'),
      meta: { title: '请求日志', description: '每一次创作，都有迹可循' },
    },
    {
      path: '/settings',
      component: () => import('./views/SettingsView.vue'),
      meta: { title: '工坊设置', description: '让工坊按你的方式运转' },
    },
    {
      path: '/guide',
      component: () => import('./views/GuideView.vue'),
      meta: { title: 'API 文档', description: '把灵感接入你的应用', public: true },
    },
    { path: '/profile/:id', component: () => import('./views/ProfileView.vue'), meta: { title: '账号浏览器' } },
    {
      path: '/:pathMatch(.*)*',
      component: () => import('./views/NotFoundView.vue'),
      meta: { title: '迷路的灵感', public: true },
    },
  ],
  scrollBehavior: () => ({ top: 0 }),
})
let sessionValid = false
export function resetSession() {
  sessionValid = false
}
export function acceptSession() {
  sessionValid = true
}
router.beforeEach(async (to) => {
  document.title = `${to.meta.title || '灵感工坊'} · lay4flow-remote`
  if (to.meta.public || sessionValid) return true
  try {
    await api.get('/api/stats')
    sessionValid = true
    return true
  } catch (error) {
    if (error.status === 401) return { path: '/login', query: { next: to.fullPath } }
    return true
  }
})
export default router
