import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { fileURLToPath, URL } from "node:url";

export default defineConfig({
  plugins: [react()],
  base: "/crew/",
  build: {
    outDir: "../backend/static/crew",
    emptyOutDir: true,
    sourcemap: true
  },
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url))
    }
  },
  server: {
    port: 4174,
    host: "0.0.0.0",
    proxy: {
      "/dashboard": "http://localhost:8000",
      "/crm": "http://localhost:8000",
      "/simulator": "http://localhost:8000"
    }
  }
});
