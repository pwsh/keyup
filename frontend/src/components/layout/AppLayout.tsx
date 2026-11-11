import {
  Braces,
  ChevronsLeft,
  ChevronsRight,
  Ellipsis,
  LogIn,
  LogOut,
  Moon,
  Search,
  Sun,
} from "lucide-react";
import { type FormEvent, type ReactNode, useEffect, useId, useState } from "react";
import { NavLink, useLocation, useMatch, useNavigate } from "react-router";
import { useCurrentUser, useLogout } from "../../api/hooks";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";
import { useSignInStore } from "../../state/auth";
import { SIDEBAR_PANEL, usePanelOpen } from "../../state/panels";
import { toggleTheme, useTheme } from "../../state/theme";
import { Button } from "../ui/Button";
import { Popover } from "../ui/Popover";
import { Tip } from "../ui/Tooltip";
import { NAV_ITEMS, type NavItem } from "./nav";
import {
  ConnectionBadge,
  IngestLagBadge,
  MockBadge,
  NowPlaying,
  useHeaderConnection,
  useMockMode,
} from "./StatusBadges";

const SITE_NAME = "Keyup";
const TAGLINE = "Trunked radio archive";

/** Sidebar from `md` up, bottom tab bar below it. */
export function AppLayout({ children }: { children: ReactNode }) {
  const { user } = useCurrentUser();
  const navItems = NAV_ITEMS.filter(
    (item) => (!item.requiresAuth || user !== null) && (!item.requiresAdmin || user?.is_admin === true),
  );
  const mode = useTheme();
  const [expanded, setExpanded] = usePanelOpen(SIDEBAR_PANEL);
  const navId = useId();
  const mock = useMockMode();
  useCollapseShortcut(expanded, setExpanded);

  return (
    <div className="flex h-full min-h-0 flex-col md:flex-row">
      <a
        href="#main"
        className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2 focus:text-sm focus:text-ink"
      >
        Skip to content
      </a>

      <aside
        data-testid="sidebar"
        data-collapsed={expanded ? undefined : "true"}
        className={cn(
          "hidden shrink-0 flex-col border-r border-line bg-surface md:flex",
          // Reduced motion is honored globally in index.css.
          "transition-[width] duration-150",
          expanded ? "w-52" : "w-14",
        )}
      >
        <div
          className={cn(
            "flex py-4",
            expanded ? "items-start justify-between gap-1 pl-4 pr-2" : "flex-col items-center gap-2 px-1.5",
          )}
        >
          {expanded ? (
            // One paragraph, the name then its tagline: two <p>s read as a heading over
            // body text (axe p-as-heading), and the brand is not a heading of anything.
            <p className="min-w-0">
              <span className="block text-sm font-semibold leading-tight tracking-tight text-ink">
                {SITE_NAME}
              </span>
              <span className="mt-0.5 block text-[11px] text-ink-2">{TAGLINE}</span>
            </p>
          ) : (
            <Tip label={`${SITE_NAME} — ${TAGLINE}`} side="right">
              <span tabIndex={0} className="rounded-md text-xs font-semibold tracking-tight text-ink">
                {SITE_NAME}
              </span>
            </Tip>
          )}
          <Tip label={HELP["app.nav.toggle"]} side={expanded ? "bottom" : "right"}>
            <button
              type="button"
              aria-expanded={expanded}
              aria-controls={navId}
              aria-label={expanded ? "Collapse the menu" : "Expand the menu"}
              onClick={() => setExpanded(!expanded)}
              className="shrink-0 rounded-md p-1 text-ink-2 hover:bg-surface-2 hover:text-ink"
            >
              {expanded ? (
                <ChevronsLeft size={16} aria-hidden="true" />
              ) : (
                <ChevronsRight size={16} aria-hidden="true" />
              )}
            </button>
          </Tip>
        </div>

        <nav id={navId} aria-label="Main" className={cn("flex-1", expanded ? "px-2" : "px-1.5")}>
          <ul className="space-y-0.5">
            {navItems.map((item) => (
              <li key={item.to}>
                <SidebarLink item={item} collapsed={!expanded} />
              </li>
            ))}
          </ul>
        </nav>

        <div
          className={cn(
            "border-t border-line py-3",
            expanded ? "space-y-2 px-3" : "flex flex-col items-center gap-2 px-1.5",
          )}
        >
          <div className={cn("flex items-center gap-2", expanded ? "justify-between" : "flex-col")}>
            <AccountControl compact={!expanded} />
            <ThemeToggle mode={mode} />
          </div>
          {mock ? null : (
            <Tip label={HELP["app.apiDocs"]}>
              {expanded ? (
                <a
                  href="/api/docs"
                  target="_blank"
                  rel="noreferrer"
                  className="block text-[11px] text-ink-2 underline decoration-dotted underline-offset-2 hover:text-ink"
                >
                  API docs
                </a>
              ) : (
                <a
                  href="/api/docs"
                  target="_blank"
                  rel="noreferrer"
                  aria-label="API docs"
                  className="inline-flex rounded-md border border-line p-1.5 text-ink-2 hover:bg-surface-2 hover:text-ink"
                >
                  <Braces size={15} aria-hidden="true" />
                </a>
              )}
            </Tip>
          )}
        </div>
      </aside>

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <header className="flex shrink-0 items-center gap-2 border-b border-line bg-surface px-3 py-2 md:px-4">
          <p className="shrink-0 truncate text-sm font-semibold text-ink md:hidden">{SITE_NAME}</p>
          <GlobalSearch />
          <div className="flex shrink-0 items-center gap-1.5">
            <NowPlaying />
            {mock ? <MockBadge /> : null}
            <IngestLagBadge />
            <HeaderConnection />
            {/* The sidebar — and with it the account button — is hidden below `md`, so
                without these the only way to sign in on a phone was to type `/login`. */}
            <span className="flex items-center gap-1.5 md:hidden">
              <AccountControl compact />
              <ThemeToggle mode={mode} />
            </span>
          </div>
        </header>

        {/* `relative` is load-bearing. An absolutely positioned descendant with no
            positioned ancestor — every `sr-only` caption and label is one — resolves
            against the page instead, and an ancestor's `overflow` does not clip it.
            One `<caption className="sr-only">` at the foot of the statistics page was
            enough to give the *window* a 900px scrollbar over an app that scrolls
            inside its own panels. This makes this box their containing block, so the
            scroll region clips them like everything else. */}
        <main
          id="main"
          // Reserve the fixed player's height at the foot so it never overlaps content:
          // mobile also clears the bottom tab bar (3.5rem) the player sits above; desktop
          // has no tab bar. `--player-height` is 0 when nothing is playing (set by Player).
          className="relative min-h-0 flex-1 overflow-y-auto pb-[calc(3.5rem+var(--player-height,0px))] md:pb-[var(--player-height,0px)]"
        >
          {children}
        </main>
      </div>

      <MobileNav items={navItems} />
    </div>
  );
}

/** How many pages the phone's tab bar shows before the rest go behind "More". */
const MOBILE_TABS = 4;

/**
 * The phone's bottom bar: four pages, then everything else.
 *
 * It used to be `navItems.slice(0, 5)` — Calls, Live, Search, Talkgroups, Radios — and the
 * other eight pages simply did not exist on a phone. Sites, Statistics, Alerts, Admin,
 * Activity and Help were reachable only by typing a URL, which on a phone means they were
 * not reachable. The overflow is a popover rather than a route of its own so the page
 * underneath stays where it was, and the trigger lights up while one of the pages inside it
 * is the current one — otherwise the bar would claim nothing is selected on six of the
 * thirteen pages.
 */
/** How a nav item outside the SPA opens: its own tab, with no handle back on this one. */
const NEW_TAB = { target: "_blank", rel: "noopener noreferrer" } as const;

function MobileNav({ items }: { items: NavItem[] }) {
  const [open, setOpen] = useState(false);
  const { pathname } = useLocation();
  const primary = items.slice(0, MOBILE_TABS);
  const overflow = items.slice(MOBILE_TABS);
  const inOverflow = overflow.some((item) => pathname.startsWith(item.to));

  const linkClass = ({ isActive }: { isActive: boolean }) =>
    cn("flex flex-col items-center gap-0.5 py-2 text-[10px]", isActive ? "text-accent-ink" : "text-ink-2");

  return (
    <nav
      aria-label="Main"
      data-testid="mobile-nav"
      className={cn(
        "fixed inset-x-0 bottom-0 z-40 grid border-t border-line bg-surface md:hidden",
        overflow.length > 0 ? "grid-cols-5" : "grid-cols-4",
      )}
    >
      {primary.map((item) =>
        item.external ? (
          <a key={item.to} href={item.to} {...NEW_TAB} className={linkClass({ isActive: false })}>
            <item.icon size={18} aria-hidden="true" />
            <span className="max-w-full truncate px-0.5">{item.short}</span>
          </a>
        ) : (
          <NavLink key={item.to} to={item.to} className={linkClass}>
            <item.icon size={18} aria-hidden="true" />
            <span className="max-w-full truncate px-0.5">{item.short}</span>
          </NavLink>
        ),
      )}
      {overflow.length > 0 ? (
        <Popover
          open={open}
          onOpenChange={setOpen}
          align="end"
          className="w-56 p-1"
          trigger={
            <button
              type="button"
              aria-label="More pages"
              aria-expanded={open}
              className={cn(
                "flex flex-col items-center gap-0.5 py-2 text-[10px]",
                inOverflow ? "text-accent-ink" : "text-ink-2",
              )}
            >
              <Ellipsis size={18} aria-hidden="true" />
              <span className="max-w-full truncate px-0.5">More</span>
            </button>
          }
        >
          <ul>
            {overflow.map((item) => (
              <li key={item.to}>
                {item.external ? (
                  <a
                    href={item.to}
                    {...NEW_TAB}
                    onClick={() => setOpen(false)}
                    className="flex items-center gap-2 rounded px-2 py-2 text-sm text-ink hover:bg-surface-2"
                  >
                    <item.icon size={16} aria-hidden="true" />
                    {item.label}
                  </a>
                ) : (
                  <NavLink
                    to={item.to}
                    onClick={() => setOpen(false)}
                    className={({ isActive }) =>
                      cn(
                        "flex items-center gap-2 rounded px-2 py-2 text-sm",
                        isActive ? "bg-accent-soft text-accent-ink" : "text-ink hover:bg-surface-2",
                      )
                    }
                  >
                    <item.icon size={16} aria-hidden="true" />
                    {item.label}
                  </NavLink>
                )}
              </li>
            ))}
            <li className="mt-1 border-t border-line pt-1">
              <AccountRow onDone={() => setOpen(false)} />
            </li>
          </ul>
        </Popover>
      ) : null}
    </nav>
  );
}

function HeaderConnection() {
  const { state, following } = useHeaderConnection();
  // Nothing is streaming (a directory page, the help): no pill, rather than "Offline".
  if (state === "closed") return null;
  return <ConnectionBadge state={state} following={following} />;
}

/** The header's search box: Enter opens `/search` with the query already applied. */
function GlobalSearch() {
  const navigate = useNavigate();
  const [value, setValue] = useState("");
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) return;
    void navigate(`/search?q=${encodeURIComponent(trimmed)}`);
  };
  return (
    <form onSubmit={onSubmit} className="min-w-0 flex-1" aria-label="Search transcripts">
      <Tip label={HELP["app.search.global"]}>
        <div className="relative flex min-w-0 items-center">
          <Search size={14} aria-hidden="true" className="pointer-events-none absolute left-2.5 text-muted" />
          <input
            type="search"
            value={value}
            onChange={(event) => setValue(event.target.value)}
            placeholder="Search transcripts…"
            aria-label="Search transcripts, notes and aliases"
            className="h-8 w-full max-w-xl rounded-md border border-control-border bg-page pl-8 pr-2.5 text-sm text-ink placeholder:text-muted"
          />
        </div>
      </Tip>
    </form>
  );
}

/**
 * `[` toggles the rail — the one-key shortcut editors use for the same thing.
 * It stays out of the way of anyone typing.
 */
function useCollapseShortcut(expanded: boolean, setExpanded: (next: boolean) => void) {
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "[" || event.metaKey || event.ctrlKey || event.altKey) return;
      const target = event.target;
      if (target instanceof HTMLElement) {
        if (target.isContentEditable) return;
        if (["INPUT", "TEXTAREA", "SELECT"].includes(target.tagName)) return;
      }
      setExpanded(!expanded);
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [expanded, setExpanded]);
}

/**
 * The active state is computed here and passed as a plain string on purpose:
 * `Tip` renders a Radix `Slot`, which merges `className` by string-joining, so a
 * NavLink render-prop className would end up as the class attribute's literal
 * source text.
 */
function SidebarLink({ item, collapsed }: { item: NavItem; collapsed: boolean }) {
  const active = useMatch({ path: item.to, end: false }) !== null;
  return (
    <Tip
      label={
        collapsed ? (
          <>
            <span className="font-medium">{item.label}</span> — {HELP[item.help]}
          </>
        ) : (
          HELP[item.help]
        )
      }
      side="right"
    >
      {item.external ? (
        <a
          href={item.to}
          {...NEW_TAB}
          className={cn(
            "flex items-center rounded-md py-2 text-sm text-ink-2 hover:bg-surface-2 hover:text-ink",
            collapsed ? "justify-center px-0" : "gap-2.5 px-2.5",
          )}
        >
          <item.icon size={16} aria-hidden="true" className="shrink-0" />
          <span className={collapsed ? "sr-only" : undefined}>{item.label}</span>
        </a>
      ) : (
        <NavLink
          to={item.to}
          className={cn(
            "flex items-center rounded-md py-2 text-sm",
            collapsed ? "justify-center px-0" : "gap-2.5 px-2.5",
            active
              ? "bg-accent-soft font-medium text-accent-ink"
              : "text-ink-2 hover:bg-surface-2 hover:text-ink",
          )}
        >
          <item.icon size={16} aria-hidden="true" className="shrink-0" />
          <span className={collapsed ? "sr-only" : undefined}>{item.label}</span>
        </NavLink>
      )}
    </Tip>
  );
}

function AccountControl({ compact }: { compact: boolean }) {
  const { user } = useCurrentUser();
  const logout = useLogout();
  const navigate = useNavigate();
  const location = useLocation();
  const setRedirect = useSignInStore((state) => state.setRedirect);

  if (user) {
    return (
      <Button
        tip={HELP["app.account.signOut"]}
        variant="ghost"
        size={compact ? "iconSm" : "sm"}
        onClick={() => logout.mutate()}
        aria-label={`Sign out of ${user.username}`}
      >
        <LogOut size={14} aria-hidden="true" />
        {compact ? null : <span className="max-w-24 truncate">{user.username}</span>}
      </Button>
    );
  }
  return (
    <Button
      tip={HELP["app.account.signIn"]}
      variant="ghost"
      size={compact ? "iconSm" : "sm"}
      aria-label="Sign in"
      onClick={() => {
        setRedirect(`${location.pathname}${location.search}`);
        void navigate("/login");
      }}
    >
      <LogIn size={14} aria-hidden="true" />
      {compact ? null : "Sign in"}
    </Button>
  );
}

/**
 * The same account action as the sidebar's button, as a full-width row for the phone's
 * "More" sheet. The header's copy is icon-only for want of width; this one says which
 * account it would sign out of, which is the half a phone had no way to see at all.
 */
function AccountRow({ onDone }: { onDone: () => void }) {
  const { user } = useCurrentUser();
  const logout = useLogout();
  const navigate = useNavigate();
  const location = useLocation();
  const setRedirect = useSignInStore((state) => state.setRedirect);

  const className =
    "flex w-full items-center gap-2 rounded px-2 py-2 text-left text-sm text-ink hover:bg-surface-2";

  if (user) {
    return (
      <button
        type="button"
        className={className}
        onClick={() => {
          onDone();
          logout.mutate();
        }}
      >
        <LogOut size={16} aria-hidden="true" />
        <span className="min-w-0 truncate">Sign out {user.username}</span>
      </button>
    );
  }
  return (
    <button
      type="button"
      className={className}
      onClick={() => {
        onDone();
        setRedirect(`${location.pathname}${location.search}`);
        void navigate("/login");
      }}
    >
      <LogIn size={16} aria-hidden="true" />
      Sign in
    </button>
  );
}

function ThemeToggle({ mode }: { mode: "light" | "dark" }) {
  return (
    <Tip label={HELP["app.theme.toggle"]}>
      <button
        type="button"
        onClick={toggleTheme}
        aria-label={`Switch to ${mode === "dark" ? "light" : "dark"} theme`}
        className="rounded-md border border-line p-1.5 text-ink-2 hover:bg-surface-2 hover:text-ink"
      >
        {mode === "dark" ? <Sun size={15} aria-hidden="true" /> : <Moon size={15} aria-hidden="true" />}
      </button>
    </Tip>
  );
}
