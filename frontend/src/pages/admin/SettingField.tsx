import { RotateCcw } from "lucide-react";
import type { ReactNode } from "react";
import type { SettingDefinition } from "../../api/types";
import { Button } from "../../components/ui/Button";
import { inputClass, TextArea } from "../../components/ui/Field";
import { Select } from "../../components/ui/Select";
import { Switch } from "../../components/ui/Switch";
import { Tip } from "../../components/ui/Tooltip";
import { HELP } from "../../help/help";
import { cn } from "../../lib/cn";

/**
 * The kinds whose control is a third of a line wide and can share the label's row.
 *
 * A path, a URL, a comma-separated event list and a prompt are all as long as the line
 * they are on, so they get the full width under the label instead — squeezing them into
 * a right-hand column produced a box you could see six characters of.
 */
const INLINE_KINDS = new Set(["bool", "enum", "int", "float"]);

/**
 * One runtime setting, rendered by its `kind`.
 *
 * Lives on its own because two tabs render these rows: the Settings tab shows every group
 * that has no home of its own, and the Audio tab shows the `Audio` group beside the
 * controls that act on it. Duplicating the `kind` switch would have meant a new setting
 * type rendering in one place and not the other.
 *
 * The two tabs want different shapes, which is what `variant` is. Audio lays its
 * seventeen short numeric knobs out two-up in a grid, where a stacked label-over-control
 * is right. The Settings tab is one column of forty settings from eight unrelated groups,
 * where stacking cost three lines a setting and made the page a scroll with no landmarks;
 * there the label, the key and the control share a row and the description sits under it.
 */
export function SettingField({
  setting,
  value,
  dirty,
  disabled,
  onChange,
  onReset,
  variant = "stacked",
}: {
  setting: SettingDefinition;
  /** The value in the form right now — the draft when there is one, else the stored value. */
  value: string;
  /** True while the operator has typed something here that is not saved yet. */
  dirty: boolean;
  disabled: boolean;
  onChange: (next: string) => void;
  /**
   * Put this setting back to `default_value`. Given only where the affordance makes
   * sense: a secret's stored value never comes back from the API, so "reset" there could
   * not tell "clear it" from "leave it alone" and is not offered.
   */
  onReset?: () => void;
  variant?: "stacked" | "row";
}) {
  const id = `setting-${setting.key}`;
  const inline = variant === "row" && INLINE_KINDS.has(setting.kind);
  const control = renderControl(setting, id, value, disabled, onChange);

  if (variant === "stacked") {
    return (
      <div className="min-w-0">
        <label htmlFor={id} className="mb-1 flex items-center gap-1 text-xs font-medium text-ink-2">
          {setting.label}
          <span className="font-mono text-[10px] text-muted">{setting.key}</span>
          {setting.is_default && !dirty ? <DefaultTag /> : null}
        </label>
        {control}
        <p className="mt-1 text-[11px] leading-snug text-muted">{setting.description}</p>
      </div>
    );
  }

  return (
    <div
      data-setting={setting.key}
      className={cn("border-t border-line px-3 py-2.5 first:border-t-0", dirty && "bg-accent-soft/40")}
    >
      <div className={cn("flex min-w-0 gap-3", inline ? "flex-wrap items-center" : "flex-col")}>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <label htmlFor={id} className="text-xs font-medium text-ink">
              {setting.label}
            </label>
            <span className="font-mono text-[10px] text-muted">{setting.key}</span>
            {dirty ? (
              <Tip label={HELP["admin.settings.unsaved"]}>
                <span tabIndex={0} className="text-[10px] font-medium text-accent">
                  unsaved
                </span>
              </Tip>
            ) : setting.is_default ? (
              <DefaultTag />
            ) : (
              <Tip label={HELP["admin.settings.changed"]}>
                <span tabIndex={0} className="text-[10px] text-ink-2">
                  • changed
                </span>
              </Tip>
            )}
          </div>
          {setting.description ? (
            <p className="mt-0.5 max-w-prose text-[11px] leading-snug text-muted">{setting.description}</p>
          ) : null}
        </div>
        <div
          className={cn(
            "flex items-center gap-1.5",
            inline ? "w-full shrink-0 sm:w-56 sm:justify-end" : "w-full",
          )}
        >
          <div className="min-w-0 flex-1">{control}</div>
          {onReset ? (
            <Button
              tip={HELP["admin.settings.reset"]}
              size="iconSm"
              variant="ghost"
              disabled={disabled}
              aria-label={`Reset ${setting.label} to default`}
              onClick={onReset}
            >
              <RotateCcw size={13} aria-hidden="true" />
            </Button>
          ) : null}
        </div>
      </div>
    </div>
  );
}

function DefaultTag(): ReactNode {
  return (
    <Tip label={HELP["admin.settings.default"]}>
      <span tabIndex={0} className="text-[10px] text-muted">
        default
      </span>
    </Tip>
  );
}

function renderControl(
  setting: SettingDefinition,
  id: string,
  value: string,
  disabled: boolean,
  onChange: (next: string) => void,
): ReactNode {
  if (setting.kind === "bool") {
    return (
      <Switch
        checked={String(value).toLowerCase() === "true"}
        disabled={disabled}
        onCheckedChange={(next) => onChange(next ? "true" : "false")}
        label={String(value).toLowerCase() === "true" ? "on" : "off"}
        tip={setting.description}
      />
    );
  }
  if (setting.kind === "enum") {
    return (
      <Select
        value={value}
        onValueChange={onChange}
        options={(setting.options ?? []).map((option) => ({ value: option, label: option }))}
        tip={setting.description}
        ariaLabel={setting.label}
        className="w-full"
      />
    );
  }
  if (setting.kind === "secret") {
    // Write-only: the API never hands the stored value back, so the box starts empty
    // whether or not one is set. `is_default` above is what says which — and leaving
    // it alone sends nothing, because the form only submits keys somebody touched.
    return (
      <input
        id={id}
        type="password"
        autoComplete="new-password"
        disabled={disabled}
        placeholder={setting.is_default ? "not set" : "set — type to replace"}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className={inputClass}
      />
    );
  }
  if (setting.kind === "text") {
    return (
      <TextArea
        id={id}
        rows={2}
        disabled={disabled}
        value={value}
        onChange={(event) => onChange(event.target.value)}
      />
    );
  }
  return (
    <input
      id={id}
      type={setting.kind === "string" ? "text" : "number"}
      step={setting.kind === "float" ? "0.01" : "1"}
      disabled={disabled}
      value={value}
      onChange={(event) => onChange(event.target.value)}
      className={inputClass}
    />
  );
}
