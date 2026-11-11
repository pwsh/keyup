import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { type RenderResult, render } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { MemoryRouter, Route, Routes } from "react-router";
import { TooltipProvider } from "../components/ui/Tooltip";

/** A fresh client per test: no cache leaks between cases, no retries to wait on. */
export function makeQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, staleTime: 0, gcTime: 0 },
      mutations: { retry: false },
    },
  });
}

export function Providers({ children, route = "/" }: { children: ReactNode; route?: string }) {
  return (
    <QueryClientProvider client={makeQueryClient()}>
      <MemoryRouter initialEntries={[route]}>
        <TooltipProvider>{children}</TooltipProvider>
      </MemoryRouter>
    </QueryClientProvider>
  );
}

/**
 * Render a component inside the providers every page assumes.
 *
 * `path` matters for any page that reads `useParams`: without a `<Route>` whose
 * pattern matches, the params are empty and the page renders its not-found state
 * rather than the thing under test.
 */
export function renderWithProviders(ui: ReactElement, route = "/", path?: string): RenderResult {
  if (path === undefined) return render(<Providers route={route}>{ui}</Providers>);
  return render(
    <Providers route={route}>
      <Routes>
        <Route path={path} element={ui} />
      </Routes>
    </Providers>,
  );
}
