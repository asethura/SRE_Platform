import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// During `npm run dev`, the API runs separately (uvicorn api.app:app on
// :8000) -- proxy /api so the SPA can call relative paths in both dev and
// the built/served-by-FastAPI production mode. Target 127.0.0.1 explicitly,
// not "localhost" -- on a machine also running Docker/WSL2 containers that
// publish the same port, "localhost" can resolve to ::1 and get relayed
// into an unrelated container instead of reaching uvicorn.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8000",
    },
  },
});
