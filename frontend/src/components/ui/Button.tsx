import { cva, type VariantProps } from "class-variance-authority";
import type { ButtonHTMLAttributes, ReactNode } from "react";
import { cn } from "../../lib/cn";
import { Tip } from "./Tooltip";

const button = cva(
  "inline-flex items-center justify-center gap-1.5 rounded-md border text-sm font-medium transition-colors disabled:cursor-not-allowed disabled:opacity-50",
  {
    variants: {
      variant: {
        primary: "border-transparent bg-accent-fill text-white hover:bg-accent-hover",
        secondary: "border-line bg-surface text-ink hover:bg-surface-2",
        ghost: "border-transparent bg-transparent text-ink-2 hover:bg-surface-2 hover:text-ink",
        danger: "border-transparent bg-critical text-white hover:opacity-90",
      },
      size: {
        sm: "h-8 px-2.5",
        md: "h-9 px-3",
        icon: "h-9 w-9 p-0",
        iconSm: "h-8 w-8 p-0",
      },
      active: { true: "", false: "" },
    },
    compoundVariants: [
      { variant: "secondary", active: true, className: "border-accent bg-accent-soft text-accent-ink" },
      { variant: "ghost", active: true, className: "bg-accent-soft text-accent-ink" },
    ],
    defaultVariants: { variant: "secondary", size: "md", active: false },
  },
);

export interface ButtonProps
  extends Omit<ButtonHTMLAttributes<HTMLButtonElement>, "className">,
    VariantProps<typeof button> {
  /** Tooltip text — required so every control in the app explains itself. */
  tip: ReactNode;
  className?: string;
  children?: ReactNode;
  tipSide?: "top" | "right" | "bottom" | "left";
}

export function Button({ tip, tipSide, className, variant, size, active, ...props }: ButtonProps) {
  return (
    <Tip label={tip} side={tipSide}>
      <button type="button" {...props} className={cn(button({ variant, size, active }), className)} />
    </Tip>
  );
}

/**
 * A button whose action needs a session. Signed out it is disabled and its
 * tooltip says why rather than leaving the reader to guess at a dead control.
 */
export function GuardedButton({
  tip,
  canWrite,
  disabledTip,
  ...props
}: ButtonProps & { canWrite: boolean; disabledTip: ReactNode }) {
  return <Button {...props} tip={canWrite ? tip : disabledTip} disabled={!canWrite || props.disabled} />;
}
