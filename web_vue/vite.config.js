import { defineConfig } from "vite";
import vue from "@vitejs/plugin-vue";

// 后端 Flask API 跑在 155:8080。开发时用 dev server(5173) 代理 /api，
// 生产时用 Flask 直接托管 dist（同源 /api），两种环境前端都只调相对路径 /api/*。
export default defineConfig({
  plugins: [vue()],
  server: {
    host: "0.0.0.0",
    port: 5173,
    proxy: {
      "/api": {
        target: "http://192.168.30.155:8080",
        changeOrigin: true,
      },
      "/graph": {
        target: "http://192.168.30.155:8080",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
  },
});
