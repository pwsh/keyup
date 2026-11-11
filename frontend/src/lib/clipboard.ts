/**
 * Copying text to the clipboard, on a page that is not served over HTTPS.
 *
 * `navigator.clipboard` is a secure-context API. Keyup is normally reached over plain
 * HTTP on a LAN address, and Firefox does not expose the object there at all — so
 * `navigator.clipboard?.writeText(…)` evaluated to `undefined`, the `.then()` never ran,
 * and every copy button on the site did nothing at all, silently. Chrome is no better in
 * principle; it merely happens to treat some private addresses as trustworthy.
 *
 * So: try the real API, and fall back to the deprecated `document.execCommand("copy")`,
 * which works in every browser without a secure context because it copies from a real
 * selection inside a user gesture. Nothing here throws — the caller is told whether it
 * worked, and must say so either way.
 */

import { toast } from "../state/toasts";

/** Copy `text`, returning whether it actually reached the clipboard. */
export async function copyText(text: string): Promise<boolean> {
  if (!text) return false;
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permission denied, or a browser that exposes the object and refuses the write.
    // Fall through: execCommand often still works where the async API does not.
  }
  return legacyCopy(text);
}

/**
 * The pre-2018 way: put the text in an off-screen textarea, select it, and let the
 * document copy the selection.
 *
 * It must be *visible* to the layout engine to be selectable, so it is moved off-screen
 * rather than hidden, and `readOnly` keeps the mobile keyboard from appearing. The
 * caller's own selection is saved and restored, because losing it under a copy button is
 * the kind of small rudeness nobody can explain afterwards.
 */
function legacyCopy(text: string): boolean {
  if (typeof document === "undefined") return false;
  const area = document.createElement("textarea");
  area.value = text;
  area.setAttribute("readonly", "");
  area.setAttribute("aria-hidden", "true");
  area.style.position = "fixed";
  area.style.top = "-1000px";
  area.style.left = "-1000px";
  area.style.opacity = "0";
  const selection = document.getSelection();
  const previous = selection && selection.rangeCount > 0 ? selection.getRangeAt(0) : null;
  document.body.appendChild(area);
  try {
    area.select();
    area.setSelectionRange(0, text.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    area.remove();
    if (previous && selection) {
      selection.removeAllRanges();
      selection.addRange(previous);
    }
  }
}

/**
 * Copy and toast, which is what every call site actually wants.
 *
 * `label` is the noun phrase for the thing ("Link", "Token", "Control frequencies"): it
 * names both messages, so a failure is never silent — which is the whole point of the
 * exercise. Returns whether it worked, for callers that also want to do something.
 */
export async function copyAndToast(
  text: string,
  { label, body }: { label: string; body?: string },
): Promise<boolean> {
  const ok = await copyText(text);
  if (ok) {
    toast(`${label} copied`, body ? { tone: "success", body } : { tone: "success" });
  } else {
    toast(`Could not copy the ${label.toLowerCase()}`, {
      tone: "error",
      body: "This browser would not let the page write to the clipboard.",
    });
  }
  return ok;
}
