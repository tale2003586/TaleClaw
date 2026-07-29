import { defineConfig } from "vitest/config";
import react from "@vitejs/plugin-react";

export default defineConfig(({ command }) => ({
  base: command === "build" ? "/static/app/" : "/",
  plugins: [react()],
  build: {
    outDir: "../static/app",
    emptyOutDir: true,
    sourcemap: false,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/login": "http://127.0.0.1:8000",
    },
  },
  test: {
    environment: "jsdom",
    setupFiles: "./src/test/setup.ts",
    css: true,
  },
}));
