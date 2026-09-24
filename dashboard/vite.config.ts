import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// `base: "./"` keeps every asset reference relative, so the built bundle works
// when served from the FastAPI process, from a static host, or from a published
// artifact, without a rebuild.
export default defineConfig({
  plugins: [react()],
  base: "./",
  build: {
    outDir: "dist",
    assetsInlineLimit: 4096,
    chunkSizeWarningLimit: 900,
    rollupOptions: { output: { manualChunks: undefined } },
  },
  server: { port: 5173, strictPort: true },
});
