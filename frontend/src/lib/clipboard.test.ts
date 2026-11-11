import { afterEach, describe, expect, it, vi } from "vitest";
import { copyText } from "./clipboard";

/** jsdom has neither a clipboard nor `execCommand`, so both are installed per test. */
function withClipboard(writeText: ((text: string) => Promise<void>) | null) {
  Object.defineProperty(navigator, "clipboard", {
    value: writeText ? { writeText } : undefined,
    configurable: true,
  });
}

function withExecCommand(result: boolean | Error) {
  const copied: string[] = [];
  const exec = vi.fn(() => {
    if (result instanceof Error) throw result;
    // The real command copies the document's selection; jsdom has none, so read the
    // off-screen textarea the fallback is holding open at this moment instead.
    for (const area of document.querySelectorAll("textarea")) copied.push(area.value);
    return result;
  });
  Object.defineProperty(document, "execCommand", { value: exec, configurable: true });
  return { exec, copied };
}

afterEach(() => {
  withClipboard(null);
  vi.restoreAllMocks();
});

describe("copyText", () => {
  it("uses the async clipboard API when the page has one", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    withClipboard(writeText);
    const { exec } = withExecCommand(true);

    await expect(copyText("kup_secret")).resolves.toBe(true);
    expect(writeText).toHaveBeenCalledWith("kup_secret");
    expect(exec).not.toHaveBeenCalled();
  });

  it("falls back to a textarea when there is no clipboard at all", async () => {
    // Firefox on plain HTTP: `navigator.clipboard` is not merely blocked, it is absent,
    // so the old optional-chained call evaluated to undefined and did nothing, silently.
    withClipboard(null);
    const { exec, copied } = withExecCommand(true);

    await expect(copyText("http://keyup.lan/calls/41")).resolves.toBe(true);
    expect(exec).toHaveBeenCalledWith("copy");
    expect(copied).toEqual(["http://keyup.lan/calls/41"]);
  });

  it("falls back when the clipboard API is present but refuses", async () => {
    withClipboard(() => Promise.reject(new Error("NotAllowedError")));
    const { exec } = withExecCommand(true);

    await expect(copyText("denied")).resolves.toBe(true);
    expect(exec).toHaveBeenCalledWith("copy");
  });

  it("reports failure rather than throwing when nothing works", async () => {
    withClipboard(null);
    withExecCommand(false);
    await expect(copyText("nowhere")).resolves.toBe(false);

    withExecCommand(new Error("not supported"));
    await expect(copyText("nowhere")).resolves.toBe(false);
  });

  it("leaves no textarea behind, whichever way it went", async () => {
    withClipboard(null);
    withExecCommand(false);
    await copyText("tidy");
    expect(document.querySelectorAll("textarea")).toHaveLength(0);
  });

  it("does not bother the clipboard with an empty string", async () => {
    const writeText = vi.fn(() => Promise.resolve());
    withClipboard(writeText);
    await expect(copyText("")).resolves.toBe(false);
    expect(writeText).not.toHaveBeenCalled();
  });
});
