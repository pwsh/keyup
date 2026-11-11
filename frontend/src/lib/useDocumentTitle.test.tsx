import { render } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { PageHeader } from "../components/layout/PageHeader";
import { documentTitle, SITE_TITLE, useDocumentTitle } from "./useDocumentTitle";

function Titled({ parts }: { parts: (string | null)[] }) {
  useDocumentTitle(...parts);
  return null;
}

describe("the document title (WCAG 2.4.2)", () => {
  it("joins the page's parts with the site name", () => {
    expect(documentTitle("Calls")).toBe("Calls · Keyup");
    expect(documentTitle("DPS LAW 2D", "Oct 29, 20:32:19")).toBe("DPS LAW 2D · Oct 29, 20:32:19 · Keyup");
    expect(documentTitle(null, "", "  ")).toBe(SITE_TITLE);
  });

  it("is set by PageHeader from its title", () => {
    render(<PageHeader title="Talkgroups" description="Every channel." />);
    expect(document.title).toBe("Talkgroups · Keyup");
  });

  it("goes back to the bare site name when the page leaves", () => {
    const { unmount } = render(<Titled parts={["Live"]} />);
    expect(document.title).toBe("Live · Keyup");
    unmount();
    expect(document.title).toBe("Keyup");
  });

  it("follows a title that changes while the page stays", () => {
    const { rerender } = render(<Titled parts={[null]} />);
    expect(document.title).toBe("Keyup");
    rerender(<Titled parts={["EC FIRE OPS", "Oct 29, 21:03:32"]} />);
    expect(document.title).toBe("EC FIRE OPS · Oct 29, 21:03:32 · Keyup");
  });
});
