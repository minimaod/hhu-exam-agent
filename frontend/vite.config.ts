import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// 开发时浏览器只访问 5173，/api 请求由 Vite 转发到本地 FastAPI。
export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    // DSH/编辑器保存文件时会在源码目录生成 .<名字>.tmpdir/*.tmp；
    // Windows 上这些临时文件可能短暂被锁，Vite 监听会因 EBUSY 退出。
    // 忽略原子写入临时目录，避免开发服务器被一次保存打断。
    watch: {
      ignored: ["**/.*tmpdir/**", "**/*.tmp"],
    },
    proxy: {
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
});
