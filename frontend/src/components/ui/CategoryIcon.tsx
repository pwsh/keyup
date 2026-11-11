import {
  Ambulance,
  Briefcase,
  Bus,
  Database,
  Flame,
  GraduationCap,
  HardHat,
  Landmark,
  Lock,
  Newspaper,
  Plane,
  RadioTower,
  Share2,
  Shield,
  ShieldCheck,
  Siren,
  Star,
  Tag,
  TrainFront,
  Zap,
} from "lucide-react";
import type { ComponentType } from "react";
import type { TalkgroupCategory } from "../../api/types";
import { cn } from "../../lib/cn";
import { channelEdgeColor } from "../../lib/color";
import { categoryLabel } from "../../lib/format";
import { useTheme } from "../../state/theme";
import { Tip } from "./Tooltip";

/**
 * One glyph per talkgroup category. Chosen so the picture is the thing itself
 * rather than a symbol to be learned — an ambulance for EMS, a hard hat for public
 * works, a train for rail — because the icon sits on every row of a list a reader
 * scans thousands of rows of.
 */
const ICONS: Record<TalkgroupCategory, ComponentType<{ size?: number }>> = {
  law: Shield,
  fire: Flame,
  ems: Ambulance,
  multi: Siren,
  interop: Share2,
  pw: HardHat,
  utility: Zap,
  transit: Bus,
  rail: TrainFront,
  aviation: Plane,
  schools: GraduationCap,
  corrections: Lock,
  security: ShieldCheck,
  federal: Landmark,
  military: Star,
  media: Newspaper,
  business: Briefcase,
  ham: RadioTower,
  data: Database,
  other: Tag,
};

/**
 * What kind of channel this is, as one small colored glyph.
 *
 * The category is free text in the database, so an unknown value is expected
 * rather than exceptional: it falls back to the neutral tag glyph and the muted
 * color, and the tooltip still says whatever the value was — never silently
 * dropping a classification somebody entered.
 *
 * Color is never the only cue. Categories share hues (see `categoryColor`), the
 * glyph differs within a hue, and the tooltip and the screen-reader text both
 * carry the words — including RadioReference's finer `service_tag` when the row
 * has one, which is what distinguishes a dispatch channel from a tac channel that
 * necessarily share both hue and glyph.
 */
export function CategoryIcon({
  category,
  serviceTag = null,
  size = 13,
  interactive = true,
  className,
}: {
  category: string | null;
  /** RadioReference's service tag, when the row carries one; shown in the tooltip. */
  serviceTag?: string | null;
  size?: number;
  /**
   * Own its tooltip and its own focus stop. False inside something already
   * focusable — a calls-table row is one big button — where a nested tab stop
   * would be a focus trap for a decoration; there the browser's own `title`
   * carries the words instead, as the emergency and encrypted marks do.
   */
  interactive?: boolean;
  className?: string;
}) {
  const mode = useTheme();
  const known = category !== null && category in ICONS ? (category as TalkgroupCategory) : null;
  const Icon = known ? ICONS[known] : Tag;
  // The words live in `lib/format.ts`, so a table cell and this tooltip say the same thing.
  const label = categoryLabel(category);
  const description = serviceTag ? `${label} — ${serviceTag}` : label;
  const glyph = (
    <>
      <Icon size={size} />
      <span className="sr-only">{`Category: ${description}`}</span>
    </>
  );
  const classes = cn("inline-flex shrink-0 items-center", !known && "text-muted", className);
  // The same clamped hue as the row's key-up edge: the raw public-works amber is about
  // 2:1 on the light bezel, under the 3:1 a mark needs.
  const tint = known ? { color: channelEdgeColor(known, mode) } : undefined;

  if (!interactive) {
    return (
      <span className={classes} style={tint} title={description}>
        {glyph}
      </span>
    );
  }
  return (
    <Tip label={description}>
      <span tabIndex={0} className={classes} style={tint}>
        {glyph}
      </span>
    </Tip>
  );
}
