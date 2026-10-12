import { defineConfig, loadEnv } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// 后端端口 / 地址的真源是 job/api/net.py（可用 JOB_COURIER_PORT / JOB_COURIER_HOST
// 覆盖）。开发代理跟着同一组环境变量走，免得前后端各写一个端口对不上。
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const host = env.JOB_COURIER_HOST || "127.0.0.1";
  const port = env.JOB_COURIER_PORT || "8000";
  const target = `http://${host}:${port}`;
  return {
    plugins: [react(), tailwindcss()],
    server: {
      port: 5173,
      // 端口被占用时直接报错，而不是悄悄换一个——否则代理指向就全乱了。
      strictPort: true,
      proxy: {
        "/api": target,
        "/ws": { target: target.replace(/^http/, "ws"), ws: true },
      },
    },
  };
});
