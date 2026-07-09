import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    open: true,
    // 允许加载 src/data 下的本地 JSON
    fs: { strict: false }
  },
  base: './'
})
