# Analytics on the public site

Google Tag Manager with this site's Google Analytics 4 stream, loaded by `frontend/src/lib/publicSite.ts`.
It runs only for a guest on the site's public hostname (`VITE_PUBLIC_HOST` in `.env`, `archive.example.org`
in these docs). Signed-in readers, every other hostname and the
`/embed/` pages never load it, and with `VITE_PUBLIC_HOST` blank (the default) no hostname is public, so
analytics stays off even with a container id set.

## Turning it on

Set in `.env`, then rebuild the web image (the values are built into the frontend):

```sh
docker compose up -d --build web
```


| Variable | Value |
| --- | --- |
| `VITE_PUBLIC_HOST` | The site's public hostname, without scheme or port (`archive.example.org`). Required: blank means no hostname is public and analytics never loads. |
| `VITE_GTM_CONTAINER_ID` | The id of this site's own Tag Manager container, as shown at the top of its workspace. Blank or malformed: analytics off. |
| `VITE_PRIVACY_CONTACT` | Email address or URL for removal requests, shown on `/privacy`. |

## Required setup

**GA4 attaches the page to every event it sends**, and by default reads the real address and tab
title. The address can hold a search term or a call id, and the privacy page promises Google is not
told what someone searched for or which call they opened. So the code sets a clean page with
`gtag('set', …)` before the container starts and again on every navigation, with the automatic page
view off, and reports its own `page_view`.

Leave the Tag Manager container as Google created it: **no custom tags**. Adding a Google tag for
this stream inside the container stopped all sending (2026-11-08), because it loaded this container
back instead of GA4's own code.

Two GA4 settings on this site's stream are still required, and the code cannot change them:

1. Enhanced measurement: off entirely. Its history-change page view reads the real address and
   ignores the clean page.
2. Automatic detection of user-provided data (email, phone, address): off. Pages here hold transcripts.

Check by loading the site signed out on a URL with `?q=` and a call id: every request to
`/g/collect` must carry the clean `dl`.

## What is sent

| Event | Data |
| --- | --- |
| `page_view` | Path with ids replaced (`/calls/:id`). No query string, no tab title. |
| `call_play` | The clean page only, nothing about the call. |
| `live_start` | The clean page only, nothing about the feed. |

A referrer from this site is dropped; one from another site is kept.

Keep `/privacy` (`frontend/src/pages/Privacy.tsx`) true to what is sent.
