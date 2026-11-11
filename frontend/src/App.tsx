import { lazy, type ReactNode, Suspense, useEffect } from "react";
import { Navigate, Route, Routes, useLocation } from "react-router";
import { useCurrentUser } from "./api/hooks";
import { ErrorBoundary } from "./components/ErrorBoundary";
import { AppLayout } from "./components/layout/AppLayout";
import { Player } from "./components/player/Player";
import { SkeletonText } from "./components/ui/Skeleton";
import { EmptyState } from "./components/ui/States";
import { Toaster } from "./components/ui/Toast";
import { TooltipProvider } from "./components/ui/Tooltip";
import { usePublicSite } from "./lib/publicSite";
import { useDocumentTitle } from "./lib/useDocumentTitle";
import { useSignInStore } from "./state/auth";
import { watchSystemTheme } from "./state/theme";

// Every route is code-split; the charts (ECharts) are the heavy ones and only
// download when Statistics or a detail page is opened.
const Calls = lazy(() => import("./pages/Calls").then((m) => ({ default: m.Calls })));
const CallDetail = lazy(() => import("./pages/CallDetail").then((m) => ({ default: m.CallDetail })));
const SearchPage = lazy(() => import("./pages/SearchPage").then((m) => ({ default: m.SearchPage })));
const Live = lazy(() => import("./pages/Live").then((m) => ({ default: m.Live })));
const Talkgroups = lazy(() => import("./pages/Talkgroups").then((m) => ({ default: m.Talkgroups })));
const TalkgroupDetail = lazy(() =>
  import("./pages/TalkgroupDetail").then((m) => ({ default: m.TalkgroupDetail })),
);
const Radios = lazy(() => import("./pages/Radios").then((m) => ({ default: m.Radios })));
const Sites = lazy(() => import("./pages/Sites").then((m) => ({ default: m.Sites })));
const RadioDetail = lazy(() => import("./pages/RadioDetail").then((m) => ({ default: m.RadioDetail })));
const Units = lazy(() => import("./pages/Units").then((m) => ({ default: m.Units })));
const UnitDetail = lazy(() => import("./pages/UnitDetail").then((m) => ({ default: m.UnitDetail })));
const Personnel = lazy(() => import("./pages/Personnel").then((m) => ({ default: m.Personnel })));
const PersonDetail = lazy(() => import("./pages/PersonDetail").then((m) => ({ default: m.PersonDetail })));
const Stats = lazy(() => import("./pages/Stats").then((m) => ({ default: m.Stats })));
const Feeds = lazy(() => import("./pages/Feeds").then((m) => ({ default: m.Feeds })));
const Alerts = lazy(() => import("./pages/Alerts").then((m) => ({ default: m.Alerts })));
const Admin = lazy(() => import("./pages/Admin").then((m) => ({ default: m.Admin })));
const FeedStats = lazy(() => import("./pages/FeedStats").then((m) => ({ default: m.FeedStats })));
const Activity = lazy(() => import("./pages/Activity").then((m) => ({ default: m.Activity })));
const Help = lazy(() => import("./pages/Help").then((m) => ({ default: m.Help })));
const Login = lazy(() => import("./pages/Login").then((m) => ({ default: m.Login })));
const Home = lazy(() => import("./pages/Landing").then((m) => ({ default: m.Home })));
const Privacy = lazy(() => import("./pages/Privacy").then((m) => ({ default: m.Privacy })));

/**
 * A page a guest is not shown at all.
 *
 * Hiding the nav entry is not the guard — a typed URL, a bookmark or a shared link all
 * arrive at the route directly. So the roster, the site list and the alert rules send an
 * unauthenticated reader to the sign-in form, remembering where they were headed so
 * signing in lands them there rather than back at the calls. The answer to "who is this"
 * is a request in flight on the first paint, so nothing is decided while it is loading:
 * redirecting on a not-yet-known user would bounce the operator out of their own page.
 */
export function RequireAuth({ children, admin = false }: { children: ReactNode; admin?: boolean }) {
  const { user, isAdmin, isLoading } = useCurrentUser();
  const location = useLocation();
  const setRedirect = useSignInStore((state) => state.setRedirect);
  const destination = `${location.pathname}${location.search}`;

  useEffect(() => {
    if (!isLoading && user === null) setRedirect(destination);
  }, [isLoading, user, destination, setRedirect]);

  if (isLoading) return <SkeletonText lines={6} className="p-6" />;
  if (user === null) return <Navigate to="/login" replace />;
  if (admin && !isAdmin)
    return (
      <EmptyState
        title="Administrators only"
        hint="This page is for the archive's administrators. Ask one to give your account the admin scope."
      />
    );
  return <>{children}</>;
}

/**
 * The operator's pages — the activity log and the export feeds' statistics. A guest is
 * sent to sign in, like `RequireAuth`; a signed-in account without the admin scope is
 * told so rather than shown a page of 403s.
 */
export function RequireAdmin({ children }: { children: ReactNode }) {
  return <RequireAuth admin>{children}</RequireAuth>;
}

/** The not-found route: a heading and a tab title of its own. */
function NotFound() {
  useDocumentTitle("Page not found");
  return (
    <div className="p-6">
      <h1 className="sr-only">Page not found</h1>
      <EmptyState
        title="Page not found"
        hint="Use the navigation to reach the calls, the live feed, search, the talkgroup, radio, unit, personnel and site directories, statistics, the RSS feeds, alerts or the help."
      />
    </div>
  );
}

export function App() {
  // Keep following the OS theme while the reader has not picked one of their own.
  useEffect(watchSystemTheme, []);
  usePublicSite();

  return (
    <TooltipProvider>
      <AppLayout>
        <ErrorBoundary>
          <Suspense fallback={<SkeletonText lines={6} className="p-6" />}>
            <Routes>
              <Route path="/" element={<Home />} />
              <Route path="/privacy" element={<Privacy />} />
              <Route path="/calls" element={<Calls />} />
              <Route path="/calls/:id" element={<CallDetail />} />
              <Route path="/search" element={<SearchPage />} />
              <Route path="/live" element={<Live />} />
              <Route path="/talkgroups" element={<Talkgroups />} />
              <Route path="/talkgroups/:id" element={<TalkgroupDetail />} />
              <Route path="/radios" element={<Radios />} />
              <Route path="/radios/:id" element={<RadioDetail />} />
              <Route
                path="/units"
                element={
                  <RequireAuth>
                    <Units />
                  </RequireAuth>
                }
              />
              <Route
                path="/units/:id"
                element={
                  <RequireAuth>
                    <UnitDetail />
                  </RequireAuth>
                }
              />
              <Route
                path="/personnel"
                element={
                  <RequireAuth>
                    <Personnel />
                  </RequireAuth>
                }
              />
              <Route
                path="/personnel/:id"
                element={
                  <RequireAuth>
                    <PersonDetail />
                  </RequireAuth>
                }
              />
              <Route
                path="/sites"
                element={
                  <RequireAuth>
                    <Sites />
                  </RequireAuth>
                }
              />
              <Route path="/stats" element={<Stats />} />
              <Route path="/feeds" element={<Feeds />} />
              <Route
                path="/alerts"
                element={
                  <RequireAuth>
                    <Alerts />
                  </RequireAuth>
                }
              />
              <Route
                path="/admin"
                element={
                  <RequireAuth>
                    <Admin />
                  </RequireAuth>
                }
              />
              <Route
                path="/feed-stats"
                element={
                  <RequireAdmin>
                    <FeedStats />
                  </RequireAdmin>
                }
              />
              <Route
                path="/activity"
                element={
                  <RequireAdmin>
                    <Activity />
                  </RequireAdmin>
                }
              />
              <Route path="/help" element={<Help />} />
              <Route path="/login" element={<Login />} />
              <Route path="*" element={<NotFound />} />
            </Routes>
          </Suspense>
        </ErrorBoundary>
      </AppLayout>
      {/* One player for the whole app, so it keeps playing across navigation. */}
      <Player />
      <Toaster />
    </TooltipProvider>
  );
}
