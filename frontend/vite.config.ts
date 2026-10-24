import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时浏览器只访问 5173，/api 请求由 Vite 转发到本地 FastAPI。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
