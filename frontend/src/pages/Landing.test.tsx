import { screen } from "@testing-library/react";
import { Route, Routes } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderWithProviders } from "../test/render";
import { Home } from "./Landing";
import { Privacy } from "./Privacy";

const currentUser = vi.hoisted(() => ({ user: null as { id: number } | null, isLoading: false }));

vi.mock("../api/hooks", async (original) => ({
  ...(await original<typeof import("../api/hooks")>()),
  useCurrentUser: () => currentUser,
}));

function setHost(hostname: string) {
  vi.stubGlobal("location", { ...window.location, hostname });
}

function renderHome() {
  return renderWithProviders(
    <Routes>
      <Route path="/" element={<Home />} />
      <Route path="/calls" element={<p>calls page</p>} />
    </Routes>,
  );
}

beforeEach(() => {
  currentUser.user = null;
  currentUser.isLoading = false;
  vi.stubEnv("VITE_PUBLIC_HOST", "archive.example.org");
  setHost("archive.example.org");
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe("Home", () => {
  it("shows a guest on the public host the landing page", () => {
    renderHome();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Radio scanner archive");
    expect(screen.getByRole("link", { name: /listen live/i })).toHaveAttribute("href", "/live");
    expect(screen.getByRole("link", { name: /privacy/i })).toHaveAttribute("href", "/privacy");
  });

  it("says the site is not an official source", () => {
    renderHome();
    expect(screen.getByText(/not affiliated with any government/i)).toBeInTheDocument();
  });

  it("takes a signed-in reader straight to the calls", () => {
    currentUser.user = { id: 1 };
    renderHome();
    expect(screen.getByText("calls page")).toBeInTheDocument();
  });

  it("names the site as configured", () => {
    vi.stubEnv("VITE_SITE_NAME", "Example County scanner archive");
    vi.stubEnv("VITE_SITE_DESCRIPTION", "Radio from Example County.");
    renderHome();
    expect(screen.getByRole("heading", { level: 1 })).toHaveTextContent("Example County scanner archive");
    expect(screen.getByText("Radio from Example County.")).toBeInTheDocument();
  });

  it("keeps the calls as the front page on any other host", () => {
    setHost("192.0.2.10");
    renderHome();
    expect(screen.getByText("calls page")).toBeInTheDocument();
  });

  it("keeps the calls as the front page when no public host is configured", () => {
    vi.stubEnv("VITE_PUBLIC_HOST", "");
    renderHome();
    expect(screen.getByText("calls page")).toBeInTheDocument();
  });
});

describe("Privacy", () => {
  it("says transcripts are public and machine-made", () => {
    renderWithProviders(<Privacy />);
    expect(screen.getByText(/searchable by anyone/i)).toBeInTheDocument();
    expect(screen.getByText(/made by software/i)).toBeInTheDocument();
  });

  it("describes Google Analytics only while it is configured", () => {
    renderWithProviders(<Privacy />);
    expect(screen.queryByText(/google analytics/i)).not.toBeInTheDocument();
  });

  it("describes Google Analytics when a container is set", () => {
    vi.stubEnv("VITE_GTM_CONTAINER_ID", "GTM-ABC1234");
    renderWithProviders(<Privacy />);
    expect(screen.getAllByText(/google analytics/i).length).toBeGreaterThan(0);
  });

  it("gives the removal contact when one is set", () => {
    vi.stubEnv("VITE_PRIVACY_CONTACT", "removals@example.com");
    renderWithProviders(<Privacy />);
    expect(screen.getByRole("link", { name: "removals@example.com" })).toHaveAttribute(
      "href",
      "mailto:removals@example.com",
    );
  });
});
