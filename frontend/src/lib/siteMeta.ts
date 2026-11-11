/**
 * Who the public site says it is, from build-time variables (.env.example, "public site").
 * Pure and dependency-free: `vite.config.ts` uses it to write index.html's head, robots.txt,
 * sitemap.xml and site.webmanifest, and the app uses it for the landing page and analytics,
 * so both read the same values the same way.
 */

export const DEFAULT_SITE_NAME = "Radio scanner archive";
export const DEFAULT_SITE_DESCRIPTION =
  "Live public-safety and public-service radio, with a searchable archive of past calls.";
export const DEFAULT_SITE_ABOUT =
  "An independent archive of local radio traffic, not affiliated with any government agency.";

export interface SiteMeta {
  /** The one hostname that gets the landing page, a canonical link, indexing and analytics; "" = none. */
  host: string;
  /** `https://<host>`, or "" when there is no public host. */
  origin: string;
  name: string;
  description: string;
  /** The longer line in the page's structured data (JSON-LD). */
  about: string;
}

export interface SiteEnv {
  VITE_PUBLIC_HOST?: unknown;
  VITE_SITE_NAME?: unknown;
  VITE_SITE_DESCRIPTION?: unknown;
  VITE_SITE_ABOUT?: unknown;
}

const HOSTNAME = /^[a-z0-9](?:[a-z0-9-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]*[a-z0-9])?)*$/;

function text(value: unknown, fallback: string): string {
  const trimmed = String(value ?? "").trim();
  return trimmed === "" ? fallback : trimmed;
}

/** A bare hostname, lower-cased; anything else (a URL, a port, a typo with a space) is "": no public host. */
export function normalizeHost(value: unknown): string {
  const host = String(value ?? "")
    .trim()
    .toLowerCase();
  return HOSTNAME.test(host) ? host : "";
}

export function siteMeta(env: SiteEnv): SiteMeta {
  const host = normalizeHost(env.VITE_PUBLIC_HOST);
  return {
    host,
    origin: host === "" ? "" : `https://${host}`,
    name: text(env.VITE_SITE_NAME, DEFAULT_SITE_NAME),
    description: text(env.VITE_SITE_DESCRIPTION, DEFAULT_SITE_DESCRIPTION),
    about: text(env.VITE_SITE_ABOUT, DEFAULT_SITE_ABOUT),
  };
}

/** The pages a search engine may index (nginx.conf's X-Robots-Tag map keeps the same list). */
export const INDEXABLE_PATHS = ["/", "/talkgroups", "/stats", "/help"] as const;

export function robotsTxt(meta: SiteMeta): string {
  if (meta.host === "") {
    return [
      "# No public hostname is configured (VITE_PUBLIC_HOST), so nothing here is for search engines.",
      "User-agent: *",
      "Disallow: /",
      "",
    ].join("\n");
  }
  return [
    "# The indexable pages are listed in the sitemap. Every other page answers",
    "# X-Robots-Tag: noindex, so it stays crawlable here for that header to be seen.",
    "# The API is closed except for what /talkgroups and /stats need to render.",
    "User-agent: *",
    "Allow: /api/talkgroups$",
    "Allow: /api/talkgroups?",
    "Allow: /api/stats",
    "Allow: /api/status",
    "Allow: /api/systems",
    "Allow: /api/sites",
    "Allow: /api/agencies",
    "Allow: /api/directory/",
    "Allow: /api/auth/me",
    "Disallow: /api/",
    "",
    `Sitemap: ${meta.origin}/sitemap.xml`,
    "",
  ].join("\n");
}

/** The sitemap, or null when there is no public host to list pages on. */
export function sitemapXml(meta: SiteMeta): string | null {
  if (meta.host === "") return null;
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ...INDEXABLE_PATHS.map((path) => `  <url><loc>${meta.origin}${path}</loc></url>`),
    "</urlset>",
    "",
  ].join("\n");
}

export function webManifest(meta: SiteMeta): string {
  return [
    "{",
    `  "name": ${JSON.stringify(meta.name)},`,
    '  "short_name": "Keyup",',
    '  "start_url": "/",',
    '  "display": "standalone",',
    '  "background_color": "#0f172a",',
    '  "theme_color": "#2a78d6",',
    '  "icons": [',
    '    { "src": "/icon-192.png", "sizes": "192x192", "type": "image/png" },',
    '    { "src": "/icon-512.png", "sizes": "512x512", "type": "image/png" }',
    "  ]",
    "}",
    "",
  ].join("\n");
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

/** JSON that is safe inside a `<script>` element. */
function scriptJson(value: unknown): string {
  return JSON.stringify(value).replace(/</g, "\\u003c");
}

/**
 * Fills index.html's `%KEYUP_…%` placeholders. With no public host the canonical link and
 * og:url are dropped (a LAN or test install has no address to point at), the images stay
 * relative, and the inline script's host check can never match.
 */
export function renderIndexHtml(html: string, meta: SiteMeta): string {
  let out = html;
  if (meta.host === "") {
    out = out.replace(/^[ \t]*<(?:link rel="canonical"|meta property="og:url")[^\n]*\n/gm, "");
  }
  const jsonLd: Record<string, string> = {
    "@context": "https://schema.org",
    "@type": "WebSite",
    name: meta.name,
  };
  if (meta.origin !== "") jsonLd.url = `${meta.origin}/`;
  jsonLd.description = meta.about;
  const values: Record<string, string> = {
    KEYUP_SITE_NAME: escapeHtml(meta.name),
    KEYUP_SITE_DESCRIPTION: escapeHtml(meta.description),
    KEYUP_PUBLIC_ORIGIN: escapeHtml(meta.origin),
    KEYUP_PUBLIC_HOST_JSON: scriptJson(meta.host),
    KEYUP_SITE_JSONLD: scriptJson(jsonLd),
  };
  return out.replace(/%(KEYUP_[A-Z_]+)%/g, (whole, key: string) => {
    const value = values[key];
    if (value === undefined) throw new Error(`index.html: unknown placeholder ${whole}`);
    return value;
  });
}
