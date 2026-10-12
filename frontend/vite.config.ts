import react from "@vitejs/plugin-react";
import { cpSync, existsSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { defineConfig, type Plugin } from "vite";
import { VitePWA } from "vite-plugin-pwa";

const backend = process.env.BCP_BACKEND ?? "http://127.0.0.1:8000";
// Everything except the app's own files is served by FastAPI (cookies, CSRF, PDFs, page images).
const proxied = ["/api", "/view", "/download", "/documents", "/review", "/meetings", "/board", "/login", "/logout", "/static", "/auth"];

/** pdf.js loads CMaps (non-embedded CJK/Indic fonts), standard fonts, and image decoders by URL; CSP needs them same-origin. */
function pdfjsAssets(): Plugin {
  const copy = () => {
    for (const dir of ["cmaps", "standard_fonts", "wasm"]) {
      const from = fileURLToPath(new URL(`./node_modules/pdfjs-dist/${dir}`, import.meta.url));
      const to = fileURLToPath(new URL(`./public/pdfjs/${dir}`, import.meta.url));
      if (existsSync(from)) cpSync(from, to, { recursive: true });
    }
  };
  return { name: "pdfjs-assets", buildStart: copy, configureServer: copy };
}

export default defineConfig({
  base: "/app/",
  plugins: [
    pdfjsAssets(),
    react(),
    VitePWA({
      registerType: "prompt",
      injectRegister: false,
      filename: "sw.js",
      scope: "/app/",
      manifest: {
        id: "/app/",
        name: "Sonali Bank Archive",
        short_name: "Sonali Archive",
        description: "Board archive, meetings, and briefings for Sonali Bank PLC",
        start_url: "/app/?source=pwa",
        scope: "/app/",
        display: "standalone",
        background_color: "#f4f5fa",
        theme_color: "#1a1a54",
        lang: "en",
        icons: [
          { src: "icons/icon-192.png", sizes: "192x192", type: "image/png", purpose: "any" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "any" },
          { src: "icons/icon-512.png", sizes: "512x512", type: "image/png", purpose: "maskable" },
        ],
        shortcuts: [
          { name: "Archive search", url: "/app/search?source=pwa" },
          { name: "Meetings", url: "/app/meetings?source=pwa" },
        ],
      },
      workbox: {
        // Only the static shell is precached; API responses, PDFs, and page images are never cached.
        globPatterns: ["**/*.{js,css,html,png,svg,woff2,mjs}"],
        globIgnores: ["pdfjs/**", "assets/pdf.worker*"],
        maximumFileSizeToCacheInBytes: 4 * 1024 * 1024,
        navigateFallback: "/app/index.html",
        navigateFallbackAllowlist: [/^\/app\//],
        navigateFallbackDenylist: [/^\/api\//],
        runtimeCaching: [],
        cleanupOutdatedCaches: true,
      },
    }),
  ],
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 1500,
  },
  server: {
    port: 5173,
    proxy: Object.fromEntries(proxied.map((p) => [p, { target: backend, changeOrigin: false }])),
  },
});
