import { useEffect } from "react";
import { useLocation } from "react-router";
import { useCurrentUser } from "../api/hooks";
import { usePlayerStore } from "../state/player";
import { type SiteMeta, siteMeta } from "./siteMeta";

const GTM_SCRIPT = "https://www.googletagmanager.com/gtm.js";

declare global {
  interface Window {
    dataLayer?: unknown[];
  }
}

/** This build's public identity (siteMeta.ts), from the VITE_* variables it was built with. */
export function publicSite(): SiteMeta {
  return siteMeta({
    VITE_PUBLIC_HOST: import.meta.env.VITE_PUBLIC_HOST,
    VITE_SITE_NAME: import.meta.env.VITE_SITE_NAME,
    VITE_SITE_DESCRIPTION: import.meta.env.VITE_SITE_DESCRIPTION,
    VITE_SITE_ABOUT: import.meta.env.VITE_SITE_ABOUT,
  });
}

/**
 * Whether this page is on the archive's public name (VITE_PUBLIC_HOST). The same build also
 * answers on the LAN and on other hostnames; only that one gets the landing page, a canonical
 * link and analytics. With no public host configured, no hostname is public.
 */
export function isPublicHost(): boolean {
  const { host } = publicSite();
  return host !== "" && window.location.hostname === host;
}

/** The configured Tag Manager container, or "" (analytics off): the id goes into a script URL. */
export function gtmContainerId(): string {
  const raw = String(import.meta.env.VITE_GTM_CONTAINER_ID ?? "")
    .trim()
    .toUpperCase();
  return /^GTM-[A-Z0-9]{4,12}$/.test(raw) ? raw : "";
}

function push(entry: Record<string, unknown>): void {
  window.dataLayer = window.dataLayer ?? [];
  window.dataLayer.push(entry);
}

/** A command for the Google tag. Its queue honours only real `arguments` objects. */
function gtag(..._command: unknown[]): void {
  window.dataLayer = window.dataLayer ?? [];
  // biome-ignore lint/complexity/noArguments: Google's queue silently ignores an array.
  window.dataLayer.push(arguments);
}

function startContainer(id: string): void {
  if (document.head.querySelector(`script[src^="${GTM_SCRIPT}"]`)) return;
  push({ "gtm.start": Date.now(), event: "gtm.js" });
  const script = document.createElement("script");
  script.async = true;
  script.src = `${GTM_SCRIPT}?id=${id}`;
  document.head.appendChild(script);
}

/** A path with its ids taken out: which call or radio someone opened is not analytics data. */
function pageTemplate(pathname: string): string {
  return pathname.replace(/\/\d+(?=\/|$)/g, "/:id");
}

/** Another site's address is ordinary attribution; this site's own can hold a search. */
function outsideReferrer(origin: string): string {
  return document.referrer.startsWith(origin) ? "" : document.referrer;
}

/**
 * Google attaches the page to every event it sends, and otherwise reads the real address and
 * tab title. Set before the container starts, so nothing goes out with the real ones.
 */
function setCleanPage(origin: string, pathname: string): string {
  const path = pageTemplate(pathname);
  gtag("set", {
    page_location: `${origin}${path}`,
    page_title: path,
    page_referrer: outsideReferrer(origin),
    send_page_view: false,
  });
  return path;
}

function reportPageView(origin: string, path: string): void {
  gtag("event", "page_view", {
    page_path: path,
    page_location: `${origin}${path}`,
    page_title: path,
  });
}

function setCanonical(origin: string, pathname: string): void {
  let link = document.head.querySelector<HTMLLinkElement>('link[rel="canonical"]');
  if (!link) {
    link = document.createElement("link");
    link.rel = "canonical";
    document.head.appendChild(link);
  }
  link.href = `${origin}${pathname}`;
}

/**
 * What the public host does on every route: keep the canonical link on the current page and,
 * for a guest, report page views and playback to Tag Manager (docs/ANALYTICS.md).
 */
export function usePublicSite(): void {
  const { pathname } = useLocation();
  const { user, isLoading } = useCurrentUser();
  const callId = usePlayerStore((state) => state.current?.id ?? null);
  const live = usePlayerStore((state) => state.live);
  const publicHost = isPublicHost();
  const origin = publicHost ? publicSite().origin : "";
  const container = publicHost ? gtmContainerId() : "";
  const measuring = container !== "" && !isLoading && user === null;

  useEffect(() => {
    if (publicHost) setCanonical(origin, pathname);
  }, [publicHost, origin, pathname]);

  useEffect(() => {
    if (!measuring) return;
    const path = setCleanPage(origin, pathname);
    startContainer(container);
    reportPageView(origin, path);
  }, [measuring, origin, container, pathname]);

  useEffect(() => {
    if (measuring && callId !== null) gtag("event", "call_play");
  }, [measuring, callId]);

  useEffect(() => {
    if (measuring && live) gtag("event", "live_start");
  }, [measuring, live]);
}
