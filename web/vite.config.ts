import { defineConfig, loadEnv } from "vite";
import vue from "@vitejs/plugin-vue";

// Dev server supports Docker Compose via VITE_API_PROXY / VITE_HMR_CLIENT_PORT / polling.
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const apiProxy = env.VITE_API_PROXY || "http://127.0.0.1:8010";
  const usePolling = env.VITE_USE_POLLING === "true" || env.CHOKIDAR_USEPOLLING === "true";
  const hmrClientPort = Number(env.VITE_HMR_CLIENT_PORT || 0);

  return {
    plugins: [vue()],
    build: {
      rollupOptions: {
        output: {
          manualChunks: {
            three: ["three", "three/examples/jsm/controls/OrbitControls.js"],
          },
        },
      },
    },
    server: {
      host: true,
      port: 5173,
      strictPort: true,
      watch: usePolling
        ? {
            usePolling: true,
            interval: 300,
          }
        : undefined,
      hmr: hmrClientPort > 0
        ? {
            clientPort: hmrClientPort,
          }
        : undefined,
      proxy: {
        "/api": {
          target: apiProxy,
          changeOrigin: true,
          proxyTimeout: 0,
          timeout: 0,
        },
        "/healthz": {
          target: apiProxy,
          changeOrigin: true,
        },
        "/artifacts": {
          target: apiProxy,
          changeOrigin: true,
          proxyTimeout: 0,
          timeout: 0,
        },
      },
    },
  };
});
