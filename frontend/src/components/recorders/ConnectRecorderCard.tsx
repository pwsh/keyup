import { Copy, Plug } from "lucide-react";
import { useState } from "react";
import { errorDetail } from "../../api/client";
import { useCreateToken } from "../../api/hooks";
import { HELP } from "../../help/help";
import { copyAndToast } from "../../lib/clipboard";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Card, CardHeader } from "../ui/Card";
import { Field, TextInput } from "../ui/Field";
import { Select } from "../ui/Select";

/** The three recorders keyup speaks to, in the order docs/RECORDERS.md introduces them. */
const RECORDERS = [
  { value: "trunkd", label: "trunkd" },
  { value: "sdrtrunk", label: "sdrtrunk (Rdio Scanner)" },
  { value: "trunk-recorder", label: "trunk-recorder (rdioscanner_uploader)" },
] as const;

type RecorderKind = (typeof RECORDERS)[number]["value"];

/** Shown in place of the key until one is minted, so a snippet is never half a lie. */
const PLACEHOLDER = "<paste an upload-scoped token>";

/**
 * The base URL a recorder should post to.
 *
 * `window.location.origin` and nothing cleverer: the operator is looking at this page
 * *from* the address the recorder will have to reach, which is the one piece of the
 * configuration the server itself cannot know — behind a reverse proxy it has no idea
 * what name or port the outside world uses.
 */
function baseUrl(): string {
  if (typeof window === "undefined") return "http://keyup.local";
  return window.location.origin;
}

/** The config to paste, per recorder — the same three from docs/RECORDERS.md. */
export function snippetFor(kind: RecorderKind, key: string, base: string): string {
  const upload = `${base}/api/call-upload`;
  if (kind === "trunkd") {
    return [
      "# trunkd.toml",
      "[uplink]",
      `url = "${upload}"`,
      `api_key = "${key}"`,
      'system_label = "Example Network"   # becomes the Keyup system name',
    ].join("\n");
  }
  if (kind === "sdrtrunk") {
    return [
      "# sdrtrunk → Playlist → Streaming → New → Rdio Scanner",
      `Server URL:  ${upload}`,
      `API Key:     ${key}`,
      "System ID:   1        # any number; Keyup reads systemLabel, not this",
      "",
      "Then enable the stream on each channel's Record tab.",
    ].join("\n");
  }
  return [
    "// trunk-recorder config.json",
    '"plugins": [',
    "  {",
    '    "name": "rdioscanner_uploader",',
    '    "library": "librdioscanner_uploader.so",',
    `    "server": "${upload}",`,
    '    "systems": [',
    `      { "shortName": "CENTRAL", "apiKey": "${key}", "systemId": 1 }`,
    "    ]",
    "  }",
    "]",
  ].join("\n");
}

/**
 * How an operator actually gets a recorder onto this page.
 *
 * The empty state used to say a recorder "appears the first time it polls", which is true
 * and useless: it does not say that polling needs a token, that the token needs the
 * `upload` scope and nothing else, or what to paste where. Both halves are here — the key
 * is minted in place, and the snippet it goes into is shown with the key already in it —
 * because the two were previously in a token dialog and a Markdown file that never
 * appeared on the same screen.
 *
 * The key is shown exactly once, like everywhere else it is minted: only its sha256 is
 * stored, so a lost token is replaced rather than recovered.
 */
export function ConnectRecorderCard({ canAdmin }: { canAdmin: boolean }) {
  const create = useCreateToken();
  const [label, setLabel] = useState("");
  const [kind, setKind] = useState<RecorderKind>("trunkd");
  const [secret, setSecret] = useState<string | null>(null);

  const snippet = snippetFor(kind, secret ?? PLACEHOLDER, baseUrl());

  return (
    <Card>
      <CardHeader
        title={
          <span className="flex items-center gap-2">
            <Plug size={15} aria-hidden="true" /> Connect a recorder
          </span>
        }
        hint={HELP["admin.recorder.connect"]}
      />
      <div className="space-y-3 p-4">
        <div className="flex flex-wrap items-end gap-3">
          <Field label="Token label" help="admin.recorder.connect.label" className="w-56">
            {(id) => (
              <TextInput
                id={id}
                value={label}
                placeholder="attic trunkd"
                disabled={!canAdmin}
                onChange={(event) => setLabel(event.target.value)}
              />
            )}
          </Field>
          <Button
            tip={canAdmin ? HELP["admin.recorder.connect.mint"] : HELP["app.adminOnly"]}
            size="sm"
            variant="primary"
            disabled={!canAdmin || !label.trim() || create.isPending}
            onClick={() =>
              create.mutate(
                { label: label.trim(), scopes: ["upload"], expires_at: null },
                {
                  onSuccess: (created) => setSecret(created.token),
                  onError: (error) =>
                    toast("Could not mint the token", {
                      tone: "error",
                      body: errorDetail(error),
                    }),
                },
              )
            }
          >
            Mint an upload token
          </Button>
          <span className="ml-auto">
            <Select
              value={kind}
              onValueChange={(value) => setKind(value as RecorderKind)}
              options={[...RECORDERS]}
              ariaLabel="Recorder software"
              tip={HELP["admin.recorder.connect.kind"]}
              size="sm"
              className="w-64"
            />
          </span>
        </div>

        {secret ? (
          <div className="flex items-center gap-2">
            <code
              data-testid="minted-token"
              className="min-w-0 flex-1 truncate rounded-md border border-line bg-surface-2 px-2 py-1.5 font-mono text-xs text-ink"
            >
              {secret}
            </code>
            <Button
              tip={HELP["admin.recorder.connect.copyToken"]}
              size="iconSm"
              aria-label="Copy the token"
              onClick={() => {
                void copyAndToast(secret, { label: "Token" });
              }}
            >
              <Copy size={14} aria-hidden="true" />
            </Button>
          </div>
        ) : null}
        <p className="text-xs text-ink-2">
          {secret
            ? "Copy the key now — it is stored only as a hash and will not be shown again."
            : "Mint a key to have it filled into the configuration below. It needs the upload scope and nothing more: the key travels in the body of every call."}
        </p>

        <div className="relative">
          {/* A config file keeps its lines whole, so it scrolls; the box is a tab stop so
              a keyboard can scroll it too. It stays a <pre>, hence the role. */}
          {/* biome-ignore lint/a11y/useSemanticElements: a <section> cannot be preformatted text */}
          <pre
            className="overflow-x-auto rounded-md border border-line bg-surface-2 p-3 pr-12 font-mono text-[11px] leading-relaxed text-ink"
            role="region"
            aria-label="Recorder configuration"
            tabIndex={0}
          >
            {snippet}
          </pre>
          <span className="absolute right-2 top-2">
            <Button
              tip={HELP["admin.recorder.connect.copySnippet"]}
              size="iconSm"
              aria-label="Copy the configuration"
              onClick={() => {
                void copyAndToast(snippet, { label: "Configuration" });
              }}
            >
              <Copy size={14} aria-hidden="true" />
            </Button>
          </span>
        </div>
        <p className="text-xs text-ink-2">
          The recorder appears above the first time it polls with this key. docs/RECORDERS.md has the rest —
          what each recorder sends, and how a call with no timestamp is handled.
        </p>
      </div>
    </Card>
  );
}
