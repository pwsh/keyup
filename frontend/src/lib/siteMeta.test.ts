import { describe, expect, it } from "vitest";
import indexHtml from "../../index.html?raw";
import {
  DEFAULT_SITE_DESCRIPTION,
  DEFAULT_SITE_NAME,
  normalizeHost,
  renderIndexHtml,
  robotsTxt,
  siteMeta,
  sitemapXml,
  webManifest,
} from "./siteMeta";

const PUBLIC = siteMeta({
  VITE_PUBLIC_HOST: "archive.example.org",
  VITE_SITE_NAME: "Example County radio scanner archive",
  VITE_SITE_DESCRIPTION: "Live radio from Example County, with a searchable archive of past calls.",
  VITE_SITE_ABOUT: "An independent archive of Example County radio traffic.",
});
const PRIVATE = siteMeta({});

describe("siteMeta", () => {
  it("falls back to generic wording and no public host", () => {
    expect(PRIVATE).toEqual(
      expect.objectContaining({
        host: "",
        origin: "",
        name: DEFAULT_SITE_NAME,
        description: DEFAULT_SITE_DESCRIPTION,
      }),
    );
  });

  it("takes a bare hostname only", () => {
    expect(normalizeHost(" Archive.Example.org ")).toBe("archive.example.org");
    expect(normalizeHost("https://archive.example.org/")).toBe("");
    expect(normalizeHost("archive.example.org:8443")).toBe("");
    expect(normalizeHost("a b")).toBe("");
    expect(PUBLIC.origin).toBe("https://archive.example.org");
  });
});

describe("index.html", () => {
  it("fills every placeholder for a public host", () => {
    const html = renderIndexHtml(indexHtml, PUBLIC);
    expect(html).not.toContain("%KEYUP_");
    expect(html).toContain("<title>Example County radio scanner archive · Keyup</title>");
    expect(html).toContain('<link rel="canonical" href="https://archive.example.org/" />');
    expect(html).toContain('<meta property="og:url" content="https://archive.example.org/" />');
    expect(html).toContain('<meta property="og:image" content="https://archive.example.org/og-image.png" />');
    expect(html).toContain(
      '{"@context":"https://schema.org","@type":"WebSite","name":"Example County radio scanner archive","url":"https://archive.example.org/","description":"An independent archive of Example County radio traffic."}',
    );
    expect(html).toContain('location.hostname !== "archive.example.org"');
  });

  it("points nowhere without a public host", () => {
    const html = renderIndexHtml(indexHtml, PRIVATE);
    expect(html).not.toContain("%KEYUP_");
    expect(html).not.toContain('rel="canonical"');
    expect(html).not.toContain("og:url");
    expect(html).toContain('<meta property="og:image" content="/og-image.png" />');
    expect(html).not.toContain('"url":');
    // The hostname is never "", so the scripts-off landing text is always cleared.
    expect(html).toContain('location.hostname !== ""');
  });

  it("escapes what it puts into the page", () => {
    const html = renderIndexHtml(
      "<title>%KEYUP_SITE_NAME%</title><script>%KEYUP_SITE_JSONLD%</script>",
      siteMeta({ VITE_SITE_NAME: 'A "<b>" & </script>' }),
    );
    expect(html).toContain("<title>A &quot;&lt;b&gt;&quot; &amp; &lt;/script&gt;</title>");
    expect(html).not.toContain("</script></script>");
    expect(html).toContain("\\u003c/script>");
  });

  it("refuses an unknown placeholder", () => {
    expect(() => renderIndexHtml("%KEYUP_NOPE%", PUBLIC)).toThrow(/KEYUP_NOPE/);
  });
});

describe("crawler files", () => {
  it("lets search engines in on a public host", () => {
    expect(robotsTxt(PUBLIC)).toContain("Disallow: /api/\n");
    expect(robotsTxt(PUBLIC)).toContain("Sitemap: https://archive.example.org/sitemap.xml\n");
    expect(sitemapXml(PUBLIC)).toContain("<loc>https://archive.example.org/talkgroups</loc>");
  });

  it("closes everything without one", () => {
    expect(robotsTxt(PRIVATE)).toContain("User-agent: *\nDisallow: /\n");
    expect(robotsTxt(PRIVATE)).not.toContain("Sitemap");
    expect(sitemapXml(PRIVATE)).toBeNull();
  });

  it("names the site in the web manifest", () => {
    expect(JSON.parse(webManifest(PUBLIC)).name).toBe("Example County radio scanner archive");
  });
});
