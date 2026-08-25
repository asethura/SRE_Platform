import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// During `npm run dev`, the API runs separately (uvicorn api.app:app on
// :8000) -- proxy /api so the SPA can call relative paths in both dev and
// the built/served-by-FastAPI production mode.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://localhost:8000",
    },
  },
});
