import {
  Bell,
  ChartColumn,
  CircleQuestionMark,
  Headphones,
  type LucideIcon,
  Radio,
  RadioTower,
  Rss,
  ScrollText,
  Search,
  Settings,
  Signal,
  SquareArrowOutUpRight,
  TowerControl,
  Truck,
  Users,
  Waypoints,
} from "lucide-react";
import type { HelpId } from "../../help/help";

export interface NavItem {
  to: string;
  label: string;
  /** The two-word form the phone's bottom bar has room for. */
  short: string;
  icon: LucideIcon;
  help: HelpId;
  /**
   * Only listed while signed in — and, for the pages App.tsx wraps in `RequireAuth`,
   * only reachable then: the roster, the site list and the alert rules are the
   * operator's working state, not part of what a guest is shown.
   */
  requiresAuth?: boolean;
  /** Only listed for an admin; the page itself stays reachable by URL. */
  requiresAdmin?: boolean;
  /**
   * A page outside the app (served by nginx, not a route of this SPA): rendered as a
   * plain link opening in a new tab, since the page has no navigation back.
   */
  external?: boolean;
}

export const NAV_ITEMS: NavItem[] = [
  { to: "/calls", label: "Calls", short: "Calls", icon: Radio, help: "app.nav.calls" },
  { to: "/live", label: "Live", short: "Live", icon: Signal, help: "app.nav.live" },
  { to: "/search", label: "Search", short: "Search", icon: Search, help: "app.nav.search" },
  {
    to: "/talkgroups",
    label: "Talkgroups",
    short: "Talkgroups",
    icon: Waypoints,
    help: "app.nav.talkgroups",
  },
  { to: "/radios", label: "Radios", short: "Radios", icon: RadioTower, help: "app.nav.radios" },
  { to: "/units", label: "Units", short: "Units", icon: Truck, help: "app.nav.units", requiresAuth: true },
  {
    to: "/personnel",
    label: "Personnel",
    short: "Personnel",
    icon: Users,
    help: "app.nav.personnel",
    requiresAuth: true,
  },
  {
    to: "/sites",
    label: "Sites",
    short: "Sites",
    icon: TowerControl,
    help: "app.nav.sites",
    requiresAuth: true,
  },
  { to: "/stats", label: "Statistics", short: "Stats", icon: ChartColumn, help: "app.nav.stats" },
  { to: "/feeds", label: "Feeds", short: "Feeds", icon: Rss, help: "app.nav.feeds" },
  {
    to: "/embed/transcripts",
    label: "Transcripts",
    short: "Transcripts",
    icon: SquareArrowOutUpRight,
    help: "app.nav.transcripts",
    external: true,
  },
  { to: "/alerts", label: "Alerts", short: "Alerts", icon: Bell, help: "app.nav.alerts", requiresAuth: true },
  { to: "/admin", label: "Admin", short: "Admin", icon: Settings, help: "app.nav.admin", requiresAuth: true },
  {
    to: "/feed-stats",
    label: "Feed stats",
    short: "Feed",
    icon: Headphones,
    help: "app.nav.feedstats",
    requiresAdmin: true,
  },
  {
    to: "/activity",
    label: "Activity",
    short: "Activity",
    icon: ScrollText,
    help: "app.nav.activity",
    requiresAdmin: true,
  },
  { to: "/help", label: "Help", short: "Help", icon: CircleQuestionMark, help: "app.nav.help" },
];
