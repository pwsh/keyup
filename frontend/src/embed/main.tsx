/**
 * Entry point of `embed.html` — the page nginx serves for `/embed/*` (docs/EMBED.md).
 *
 * A separate Vite entry from the app's `main.tsx`: it shares React and nothing else, so an
 * `<iframe>` of the live transcripts does not download the router, the query cache, the
 * charts, the player or the help registry.
 */
import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "./embed.css";
import { parseEmbedParams } from "./params";
import { TranscriptFeed } from "./TranscriptFeed";

const options = parseEmbedParams(window.location.search);
// `auto` leaves the attribute off and lets `prefers-color-scheme` decide in the CSS.
if (options.theme !== "auto") document.documentElement.dataset.theme = options.theme;

const container = document.getElementById("root");
if (!container) throw new Error("#root element is missing from embed.html");

createRoot(container).render(
  <StrictMode>
    <TranscriptFeed initial={options} />
  </StrictMode>,
);
