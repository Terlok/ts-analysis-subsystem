import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Everything stays local: the dev server listens on 127.0.0.1 and forwards /api and /ws
// to the backend on 127.0.0.1:8000 (the browser talks to a single origin).
const backend = process.env.TSA_BACKEND ?? "127.0.0.1:8000";

export default defineConfig({
  plugins: [react()],
  server: {
    host: "127.0.0.1",
    port: 5173,
    strictPort: true,
    proxy: {
      "/api": { target: `http://${backend}`, changeOrigin: true },
      "/ws": { target: `ws://${backend}`, ws: true, changeOrigin: true },
    },
  },
  preview: { host: "127.0.0.1", port: 4173 },
  worker: { format: "es" },
});
