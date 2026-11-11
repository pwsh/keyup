import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import type { Plugin } from "vite";
import { defineConfig } from "vitest/config";
import { renderIndexHtml, robotsTxt, type SiteMeta, siteMeta, sitemapXml, webManifest } from "./src/lib/siteMeta.ts";

// The Keyup API (compose maps it to host port 8096). `VITE_API_TARGET=… npm run dev`
// points the proxy at an API started by hand instead. Reached through globalThis because
// this repo does not depend on @types/node, and `tsc --noEmit` covers this file.
const API_TARGET =
  (globalThis as { process?: { env?: Record<string, string | undefined> } }).process?.env
    ?.VITE_API_TARGET || "http://localhost:8096";

/**
 * The bit of the http-proxy instance this config touches. Vite types it as an
 * `EventEmitter`, whose methods are only visible with `@types/node` installed —
 * which this repo deliberately does not depend on — so the one listener below is
 * attached through this shape instead.
 */
interface ProxyLike {
  on: (
    event: "error",
    handler: (
      error: Error,
      request: unknown,
      response: {
        writableEnded?: boolean;
        writeHead?: (status: number, headers: Record<string, string>) => void;
        end?: (body: string) => void;
      },
    ) => void,
  ) => void;
}

/**
 * `/embed/transcripts` is its own page (`embed.html`), not a route of the SPA. In
 * production nginx maps `/embed/*` to it; this does the same for `npm run dev`, which
 * would otherwise answer with the app's `index.html`.
 */
const embedDevRewrite: Plugin = {
  name: "keyup-embed-rewrite",
  configureServer(server) {
    server.middlewares.use((request: { url?: string }, _response: unknown, next: () => void) => {
      if (request.url?.startsWith("/embed/")) {
        const query = request.url.indexOf("?");
        request.url = `/embed.html${query >= 0 ? request.url.slice(query) : ""}`;
      }
      next();
    });
  },
};

/**
 * The public site's identity (src/lib/siteMeta.ts, .env.example "public site"): fills
 * index.html's `%KEYUP_…%` placeholders and writes robots.txt, sitemap.xml and
 * site.webmanifest from VITE_PUBLIC_HOST, VITE_SITE_NAME, VITE_SITE_DESCRIPTION and
 * VITE_SITE_ABOUT. With no public host, robots.txt disallows everything and there is no
 * sitemap. The same files are served by `npm run dev`.
 */
function siteFiles(meta: SiteMeta): Record<string, string> {
  const files: Record<string, string> = {
    "robots.txt": robotsTxt(meta),
    "site.webmanifest": webManifest(meta),
  };
  const sitemap = sitemapXml(meta);
  if (sitemap !== null) files["sitemap.xml"] = sitemap;
  return files;
}

function siteMetaPlugin(): Plugin {
  let meta = siteMeta({});
  return {
    name: "keyup-site-meta",
    configResolved(config) {
      meta = siteMeta(config.env);
    },
    transformIndexHtml: {
      order: "pre",
      handler: (html) => (html.includes("%KEYUP_") ? renderIndexHtml(html, meta) : html),
    },
    configureServer(server) {
      server.middlewares.use((request: { url?: string }, raw: unknown, next: () => void) => {
          const response = raw as { setHeader: (name: string, value: string) => void; end: (body: string) => void };
          const body = siteFiles(meta)[(request.url ?? "").slice(1)];
          if (body === undefined) return next();
          const type = request.url?.endsWith(".xml")
            ? "application/xml"
            : request.url?.endsWith(".webmanifest")
              ? "application/manifest+json"
              : "text/plain";
          response.setHeader("Content-Type", type);
          response.end(body);
      });
    },
    generateBundle() {
      for (const [fileName, source] of Object.entries(siteFiles(meta))) {
        this.emitFile({ type: "asset", fileName, source });
      }
    },
  };
}

/** The part of `node:zlib` the plugin below uses (no `@types/node` here, see above). */
interface ZlibLike {
  gzipSync: (data: Uint8Array, options?: { level?: number }) => Uint8Array;
}

/** The part of `node:fs/promises` the plugin below uses. */
interface FsLike {
  readFile: (path: string) => Promise<Uint8Array>;
  writeFile: (path: string, data: Uint8Array) => Promise<void>;
}

/**
 * Placeholders Vite's own plugins rewrite in their `generateBundle`/`renderChunk`. One left
 * in a written file means the build shipped an intermediate chunk — the `.gz` twins once
 * did exactly that ("Can't find variable: __VITE_PRELOAD__" on every page), so the build
 * now refuses it rather than trusting hook order.
 */
const UNRESOLVED = /__VITE_PRELOAD__|__VITE_IS_MODERN__|__VITE_ASSET__/;

/** What is worth a `.gz` twin: text, and big enough that the gzip header does not eat the win. */
const COMPRESSIBLE = /\.(?:js|mjs|css|html|svg|json|webmanifest|txt)$/;
const MIN_COMPRESS_BYTES = 1024;

/**
 * Writes a `.gz` beside every compressible file in the build, at level 9, for nginx's
 * `gzip_static` (nginx.conf) to send as it is. nginx otherwise compresses each response
 * on the fly at level 5, on every request, for files whose content never changes between
 * deploys. Gzip only: the stock nginx image has no brotli module, and one from Alpine's
 * repositories is built for a different nginx version, so a `.br` would never be served.
 *
 * It compresses in `writeBundle`, from the files Vite has already written, never in
 * `generateBundle`: Vite's import-analysis plugin fills `__VITE_PRELOAD__` in its own
 * `generateBundle`, which can run after this one, and a twin taken before that point is a
 * different (broken) file from the one it sits beside.
 *
 * `node:zlib` and `node:fs/promises` are loaded through variables so TypeScript does not
 * look for their types.
 */
const precompress: Plugin = {
  name: "keyup-precompress",
  apply: "build",
  enforce: "post",
  async writeBundle(options, bundle) {
    const zlibModule = "node:zlib";
    const fsModule = "node:fs/promises";
    const zlib = (await import(/* @vite-ignore */ zlibModule)) as ZlibLike;
    const fs = (await import(/* @vite-ignore */ fsModule)) as FsLike;
    const dir = options.dir ?? "dist";
    const decoder = new TextDecoder();
    for (const fileName of Object.keys(bundle)) {
      if (!COMPRESSIBLE.test(fileName)) continue;
      const path = `${dir}/${fileName}`;
      const bytes = await fs.readFile(path);
      if (/\.m?js$/.test(fileName) && UNRESOLVED.test(decoder.decode(bytes))) {
        throw new Error(`${fileName} still holds an unresolved Vite placeholder`);
      }
      if (bytes.byteLength < MIN_COMPRESS_BYTES) continue;
      const packed = zlib.gzipSync(bytes, { level: 9 });
      if (packed.byteLength >= bytes.byteLength) continue;
      await fs.writeFile(`${path}.gz`, packed);
    }
  },
};

export default defineConfig({
  plugins: [react(), tailwindcss(), siteMetaPlugin(), embedDevRewrite, precompress],
  server: {
    port: 5174,
    host: true,
    watch: {
      // Playwright writes traces and screenshots inside the project while the e2e
      // suite is driving this very server. Without this, every artifact it saves
      // looks like a source change and HMR full-reloads the page under test — the
      // app never finishes loading a route and every spec times out.
      ignored: ["**/e2e/.results/**", "**/e2e/screenshots/**", "**/playwright-report/**"],
    },
    proxy: {
      // `/api/live` is a WebSocket; the same entry upgrades it because `ws: true`.
      "/api": {
        target: API_TARGET,
        /**
         * Deliberately NOT `changeOrigin: true`.
         *
         * The API refuses a cookie-authenticated mutation whose `Origin` is not one
         * of its own hosts (`app/auth.py`, `check_csrf`). Rewriting `Host` to the
         * API's address while the browser still sends `Origin: localhost:5174`
         * makes every sign-in and every save fail with 403 in dev and only in dev.
         * Passing the browser's `Host` through means the API sees one consistent
         * origin, exactly as it does behind nginx in production.
         */
        changeOrigin: false,
        ws: true,
        /**
         * With no API running, the proxy's default answer is a bare 500 — which the
         * client cannot tell from a real server error, so its "fall back to the
         * fixtures" rule would never fire and `npm run dev` would show a wall of
         * failures instead of a working app. A 503 carrying this header says exactly
         * what happened: there is nothing upstream to talk to.
         */
        configure: (proxy) => {
          (proxy as unknown as ProxyLike).on("error", (error, _request, response) => {
            if (!response?.writeHead || response.writableEnded) return;
            response.writeHead(503, {
              "Content-Type": "application/json",
              "X-Api-Unreachable": "1",
            });
            response.end?.(JSON.stringify({ detail: `API unreachable: ${error.message}` }));
          });
        },
      },
    },
  },
  optimizeDeps: {
    // echarts-for-react ships CommonJS: pre-bundling keeps dev and build identical.
    include: ["echarts-for-react/lib/core"],
  },
  build: {
    outDir: "dist",
    sourcemap: false,
    chunkSizeWarningLimit: 900,
    rolldownOptions: {
      // Two pages. `embed.html` is the iframe-able live transcript view (docs/EMBED.md):
      // it shares the React chunk with the app and nothing else.
      input: { index: "index.html", embed: "embed.html" },
    },
  },
  test: {
    // Every unit test runs against the in-memory fixtures. It has to be set here
    // rather than in `setup.ts`: `client.ts` reads it once, when it is first
    // imported, so that a production build can fold the fixtures out entirely — and
    // that import happens before any setup file's statements run.
    env: { VITE_MOCK: "1" },
    // The calls table renders a full page of rows at once (the virtualizer measures
    // nothing in jsdom), so a page-level test can take several seconds.
    testTimeout: 20_000,
    environment: "jsdom",
    globals: true,
    setupFiles: ["./src/test/setup.ts"],
    include: ["src/**/*.test.{ts,tsx}"],
  },
});
