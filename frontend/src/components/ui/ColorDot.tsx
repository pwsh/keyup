import { useMemo } from "react";
import { cn } from "../../lib/cn";
import { legibleOnSurface } from "../../lib/color";
import { useTheme } from "../../state/theme";

/**
 * A talkgroup's own colour as a small dot, lightened or darkened just enough to be
 * seen on the current theme's surface (3:1, see `legibleOnSurface`). Decorative: the
 * name beside it is what identifies the talkgroup.
 */
export function ColorDot({ color, className }: { color: string; className?: string }) {
  const theme = useTheme();
  const shown = useMemo(() => legibleOnSurface(color, theme), [color, theme]);
  return (
    <span
      className={cn("h-2 w-2 shrink-0 rounded-full", className)}
      style={{ backgroundColor: shown }}
      data-color={shown}
      aria-hidden="true"
    />
  );
}
