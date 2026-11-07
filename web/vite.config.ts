import { fileURLToPath } from "node:url";
import { defineConfig } from "vite";
export default defineConfig({
  plugins: [
    {
      name: "canonical-loopback-navigation",
      configureServer(server) {
        server.middlewares.use((req, res, next) => {
          if (req.method === "GET" && req.headers.host === "localhost:5101") {
            res.writeHead(307, {
              Location: `http://127.0.0.1:5101${req.url || "/"}`,
              "Cache-Control": "no-store",
            });
            res.end();
            return;
          }
          next();
        });
      },
    },
  ],
  server: {
    host: "127.0.0.1",
    port: Number(process.env.FOLIO_UI_PORT || 5101),
    strictPort: true,
    fs: {
      strict: true,
      allow: [fileURLToPath(new URL(".", import.meta.url))],
      deny: [
        "**/.local/**",
        "**/.portfolio-local-access/**",
        "**/.env",
        "**/.env.*",
        "**/*.pem",
        "**/*.crt",
        "**/.git/**",
      ],
    },
    proxy: {
      "/api": process.env.FOLIO_API_URL || "http://127.0.0.1:8101",
      "/health": process.env.FOLIO_API_URL || "http://127.0.0.1:8101",
    },
  },
});
