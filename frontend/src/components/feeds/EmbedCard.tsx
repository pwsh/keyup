/**
 * "Embed live transcripts" — builds the `<iframe>` snippet for `/embed/transcripts`
 * (docs/EMBED.md) from a built-in feed and a few display options.
 *
 * The URL is built by the embed's own `params.ts`, so the snippet and the page that reads
 * it can never disagree about a parameter name or a default.
 */
import { Copy, ExternalLink } from "lucide-react";
import { useState } from "react";
import type { FeedPreset } from "../../api/types";
import {
  defaultEmbedOptions,
  type EmbedOptions,
  type EmbedTheme,
  embedUrl,
  MAX_MINUTES,
} from "../../embed/params";
import { HELP } from "../../help/help";
import { copyAndToast } from "../../lib/clipboard";
import { Button } from "../ui/Button";
import { Card, CardHeader } from "../ui/Card";
import { Field, TextArea, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";
import { Switch } from "../ui/Switch";
import { Tip } from "../ui/Tooltip";

const THEMES: { value: EmbedTheme; label: string }[] = [
  { value: "auto", label: "Follow the visitor's system" },
  { value: "light", label: "Light" },
  { value: "dark", label: "Dark" },
];

/**
 * The archive's public origin. The built-in feed URLs are built from `PUBLIC_BASE_URL` on the
 * server, so they name the address the rest of the world uses even when this page was
 * opened on a LAN address; the browser's own origin is the fallback.
 */
function publicOrigin(presets: FeedPreset[]): string {
  const sample = presets[0]?.rss_url;
  if (sample) {
    try {
      return new URL(sample).origin;
    } catch {
      /* fall through */
    }
  }
  return window.location.origin;
}

/** `<iframe …>` for an embed URL. Attribute values are escaped for HTML. */
export function iframeSnippet(url: string): string {
  const src = url.replace(/&/g, "&amp;").replace(/"/g, "&quot;");
  return `<iframe src="${src}" title="Live radio transcripts" width="100%" height="480" style="border:0" loading="lazy"></iframe>`;
}

export function EmbedCard({ presets }: { presets: FeedPreset[] }) {
  const [options, setOptions] = useState<EmbedOptions>(defaultEmbedOptions);
  const [talkgroups, setTalkgroups] = useState("");
  const [minutes, setMinutes] = useState(String(options.minutes));

  const tgids = talkgroups
    .split(/[\s,]+/)
    .map((value) => value.trim())
    .filter((value) => /^\d+$/.test(value));
  const built: EmbedOptions = {
    ...options,
    filters: tgids.length > 0 ? { talkgroup: tgids } : {},
    // A talkgroup list names channels; it must not be read as "replace the default feed"
    // by accident, so the chosen feed is always spelled out alongside it.
    preset: options.preset ?? "all",
  };
  const url = embedUrl(publicOrigin(presets), built);
  const snippet = iframeSnippet(url);

  const presetOptions = presets.map((preset) => ({
    value: preset.slug,
    label: preset.name.replace(/^Keyup · /, ""),
  }));
  if (!presetOptions.some((option) => option.value === options.preset)) {
    const slug = options.preset ?? "all";
    presetOptions.unshift({ value: slug, label: slug === "emergency" ? "Emergency dispatch" : slug });
  }

  return (
    <Card>
      <CardHeader title="Embed transcripts" hint={HELP["feeds.embed"]} />
      <div className="grid gap-4 p-4 lg:grid-cols-[minmax(0,22rem)_minmax(0,1fr)]">
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-1">
          <Field label="Feed" help="feeds.embed.preset">
            {() => (
              <Select
                value={options.preset ?? "all"}
                onValueChange={(value) => setOptions({ ...options, preset: value })}
                options={presetOptions}
                tip={HELP["feeds.embed.preset"]}
                ariaLabel="Feed to embed"
                className="w-full"
              />
            )}
          </Field>
          <Field label="Talkgroups (optional)" help="feeds.embed.talkgroups">
            {(id) => (
              <TextInput
                id={id}
                inputMode="numeric"
                placeholder="1101, 1102"
                value={talkgroups}
                onChange={(event) => setTalkgroups(event.target.value)}
              />
            )}
          </Field>
          <Field label="Minutes of history on load" help="feeds.embed.minutes">
            {(id) => (
              <TextInput
                id={id}
                type="number"
                min={1}
                max={MAX_MINUTES}
                value={minutes}
                onChange={(event) => {
                  setMinutes(event.target.value);
                  const value = Number.parseInt(event.target.value, 10);
                  if (Number.isFinite(value) && value >= 1) {
                    setOptions({ ...options, minutes: Math.min(value, MAX_MINUTES) });
                  }
                }}
              />
            )}
          </Field>
          <Field label="Theme" help="feeds.embed.theme">
            {() => (
              <Select
                value={options.theme}
                onValueChange={(value) => setOptions({ ...options, theme: value as EmbedTheme })}
                options={THEMES}
                tip={HELP["feeds.embed.theme"]}
                ariaLabel="Embed theme"
                className="w-full"
              />
            )}
          </Field>
          <div className="flex flex-wrap gap-4 sm:col-span-2 lg:col-span-1">
            <Switch
              checked={options.controls}
              onCheckedChange={(controls) => setOptions({ ...options, controls })}
              label="Filter bar"
              tip={HELP["feeds.embed.controls"]}
            />
            <Switch
              checked={options.compact}
              onCheckedChange={(compact) => setOptions({ ...options, compact })}
              label="Compact rows"
              tip={HELP["feeds.embed.compact"]}
            />
          </div>
        </div>

        <div className="flex min-w-0 flex-col gap-2">
          <label htmlFor="embed-snippet" className="text-xs font-medium text-ink-2">
            Snippet
          </label>
          <TextArea
            id="embed-snippet"
            readOnly
            rows={4}
            value={snippet}
            className="font-mono text-xs"
            onFocus={(event) => event.currentTarget.select()}
          />
          <div className="flex flex-wrap items-center gap-2">
            <Button
              tip={HELP["feeds.embed.copy"]}
              size="sm"
              aria-label="Copy the embed snippet"
              onClick={() =>
                copyAndToast(snippet, {
                  label: "Embed snippet",
                  body: "Paste it into any web page. It holds no password.",
                })
              }
            >
              <Copy size={13} aria-hidden="true" /> Copy snippet
            </Button>
            <Tip label={HELP["feeds.embed.preview"]}>
              <a
                href={url}
                target="_blank"
                rel="noreferrer"
                aria-label="Preview the embed in a new tab"
                className="inline-flex h-8 items-center gap-1.5 rounded-md border border-transparent px-2.5 text-sm font-medium text-ink-2 hover:bg-surface-2 hover:text-ink"
              >
                <ExternalLink size={13} aria-hidden="true" /> Preview
              </a>
            </Tip>
          </div>
          <p className="text-[11px] text-muted">
            Shows the time, talkgroup and transcript of each call as it is transcribed, with a link to the
            full call here. It reads what the archive already lets a guest read — close the archive to guests
            and the embed asks visitors to sign in.
          </p>
        </div>
      </div>
    </Card>
  );
}
