import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// SPA は管理サービスの /app で配信される
export default defineConfig({
  plugins: [react()],
  base: "/app/",
  build: { outDir: "dist" },
  server: {
    proxy: {
      // ローカル開発: /api を FastAPI(8080) にプロキシ
      "/api": "http://localhost:8080",
    },
  },
});
