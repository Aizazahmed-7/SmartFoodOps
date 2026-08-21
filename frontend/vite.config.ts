import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Everything under /v1 (and the merged spec) proxies to the local gateway —
// same-origin in the browser, so no CORS configuration anywhere.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    // The preview harness assigns a free port via PORT (autoPort);
    // 5173 stays the default for a bare `npm run dev`.
    port: Number(process.env.PORT) || 5173,
    proxy: {
      "/v1": "http://localhost:8080",
      "/openapi.json": "http://localhost:8080",
    },
  },
});
