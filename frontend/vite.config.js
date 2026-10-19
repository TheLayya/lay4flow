import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import Components from 'unplugin-vue-components/vite'
import { ElementPlusResolver } from 'unplugin-vue-components/resolvers'

export default defineConfig({
  plugins: [vue(), Components({ dts: false, resolvers: [ElementPlusResolver()] })],
  base: '/static/app/',
  build: {
    outDir: '../static/app',
    emptyOutDir: true,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(
      ['/api', '/health', '/v1', '/providers', '/compat', '/legacy', '/static/js', '/tmp'].map((path) => [
        path,
        { target: 'http://127.0.0.1:38123' },
      ]),
    ),
  },
})
