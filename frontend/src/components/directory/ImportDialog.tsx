import { useState } from "react";
import { useImportPlaylist } from "../../api/hooks";
import { HELP } from "../../help/help";
import { toast } from "../../state/toasts";
import { Button } from "../ui/Button";
import { Dialog } from "../ui/Dialog";
import { Field, TextArea } from "../ui/Field";

export type ImportKind = "talkgroups" | "radios" | "units" | "personnel";

/**
 * Load a directory or a roster from a file. The file is read in the browser and
 * posted as text, so a paste works as well as a file and there is no multipart
 * upload path to maintain.
 *
 * Talkgroups and radios come from an sdrtrunk playlist or a CSV; units and
 * personnel are CSV only, because there is no playlist that knows about them.
 */
const COPY: Record<ImportKind, { description: string; label: string; placeholder: string }> = {
  talkgroups: {
    description:
      "An sdrtrunk playlist (default.xml) or a CSV. Existing entries are updated; new ones are created.",
    label: "Playlist file",
    placeholder: '<alias name="EC FIRE DISPATCH" group="County Fire">…',
  },
  radios: {
    description:
      "An sdrtrunk playlist (default.xml) or a CSV. Existing entries are updated; new ones are created.",
    label: "Playlist file",
    placeholder: '<alias name="RC FIRE ENGINE 1" group="County Fire">…',
  },
  units: {
    description:
      "A CSV of callsign, name, agency_code, kind and notes. Units are matched on agency and callsign, and overwritten.",
    label: "Unit roster CSV",
    placeholder: "callsign,name,agency_code,kind,notes\nE1,Engine 1,XX0010100,vehicle,",
  },
  personnel: {
    description:
      "A CSV of name, rank, badge, agency_code and notes. People are matched on agency and name, and overwritten.",
    label: "Personnel roster CSV",
    placeholder: "name,rank,badge,agency_code,notes\nJ. Smith,Captain,4471,XX0010100,",
  },
};
export function ImportDialog({
  kind,
  open,
  onOpenChange,
}: {
  kind: ImportKind;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const importPlaylist = useImportPlaylist(kind);
  const copy = COPY[kind];
  const [content, setContent] = useState("");
  const format: "xml" | "csv" = content.trimStart().startsWith("<") ? "xml" : "csv";

  return (
    <Dialog
      open={open}
      onOpenChange={onOpenChange}
      title={`Import ${kind}`}
      description={copy.description}
      footer={
        <>
          <Button tip="Close without importing" size="sm" onClick={() => onOpenChange(false)}>
            Cancel
          </Button>
          <Button
            tip={HELP["talkgroups.import.run"]}
            size="sm"
            variant="primary"
            disabled={!content.trim() || importPlaylist.isPending}
            onClick={() =>
              importPlaylist.mutate(
                { content, format },
                {
                  onSuccess: (result) => {
                    // A cell naming no RadioReference service tag is dropped rather than
                    // failing the file, so the count has to be said out loud — otherwise
                    // the import looks entirely clean and a column silently went missing.
                    const badTags = result.skipped_service_tags
                      ? ` ${result.skipped_service_tags} unrecognized service tag${result.skipped_service_tags === 1 ? "" : "s"} ignored.`
                      : "";
                    toast("Import finished", {
                      tone: "success",
                      body: `${result.created} created, ${result.updated} updated, ${result.skipped} skipped.${badTags}`,
                    });
                    onOpenChange(false);
                    setContent("");
                  },
                  onError: () => toast("Could not import the file", { tone: "error" }),
                },
              )
            }
          >
            Import
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <Field label={copy.label} help="talkgroups.import.file">
          {(id) => (
            <input
              id={id}
              type="file"
              accept=".xml,.csv,text/xml,text/csv"
              className="block w-full text-xs text-ink-2 file:mr-2 file:rounded-md file:border file:border-line file:bg-surface file:px-2 file:py-1 file:text-xs file:text-ink"
              onChange={(event) => {
                const file = event.target.files?.[0];
                if (!file) return;
                void file.text().then(setContent);
              }}
            />
          )}
        </Field>
        <Field label="…or paste it here" help="talkgroups.import.file">
          {(id) => (
            <TextArea
              id={id}
              rows={8}
              value={content}
              placeholder={copy.placeholder}
              className="font-mono text-[11px]"
              onChange={(event) => setContent(event.target.value)}
            />
          )}
        </Field>
        {content ? <p className="text-[11px] text-muted">Detected format: {format.toUpperCase()}</p> : null}
      </div>
    </Dialog>
  );
}
