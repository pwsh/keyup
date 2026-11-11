import { Search } from "lucide-react";
import { useMemo, useState } from "react";
import { Link } from "react-router";
import { TalkgroupFields } from "../components/help/TalkgroupFields";
import { PageHeader } from "../components/layout/PageHeader";
import { Card, CardHeader } from "../components/ui/Card";
import { inputClass } from "../components/ui/Field";
import { EmptyState } from "../components/ui/States";
import { Tip } from "../components/ui/Tooltip";
// The help page is the one place that needs every sentence at once, so it reads the
// text module directly (it is its own lazily loaded chunk; see `help/help.ts`).
import { HELP_TEXT as HELP, HELP_AREAS, type HelpId, idsForArea, labelForId } from "../help/text";

/**
 * The whole tooltip registry, on one page.
 *
 * Every control in the app takes its explanation from `help/help.ts`, so this
 * page is not a hand-written manual that drifts from the software — it is the
 * software's own words, grouped by where they appear.
 */
export function Help() {
  const [query, setQuery] = useState("");

  const areas = useMemo(() => {
    const needle = query.trim().toLowerCase();
    return HELP_AREAS.map((area) => ({
      area,
      ids: idsForArea(area.prefix).filter(
        (id) =>
          !needle ||
          id.toLowerCase().includes(needle) ||
          HELP[id].toLowerCase().includes(needle) ||
          labelForId(id).toLowerCase().includes(needle),
      ),
    })).filter((entry) => entry.ids.length > 0);
  }, [query]);

  const total = Object.keys(HELP).length;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <Link to="/privacy" className="sr-only focus:not-sr-only">
        Privacy and removal requests
      </Link>
      <PageHeader
        title="Help"
        description={`What every control on this site does, in the same words its own tooltip uses. ${total} of them.`}
        actions={
          <Tip label={HELP["help.search"]}>
            <div className="relative flex items-center">
              <Search
                size={14}
                aria-hidden="true"
                className="pointer-events-none absolute left-2.5 text-muted"
              />
              <input
                type="search"
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                placeholder="Find a control…"
                aria-label="Search the help"
                className={`${inputClass} h-8 w-56 pl-8`}
              />
            </div>
          </Tip>
        }
      />

      <div className="min-h-0 flex-1 overflow-y-auto scrollbar-thin p-4 pb-40 md:p-6">
        <nav aria-label="Help sections" className="mb-4 flex flex-wrap gap-1.5">
          {/* The reference section is not part of the registry, so its chip is written
              here rather than derived. It leads because "what do I put in this field"
              is the question people arrive on this page with. */}
          <Tip label={HELP["help.talkgroupFields"]}>
            <a
              href="#help-talkgroup-fields"
              className="rounded-full border border-line px-2.5 py-1 text-xs text-ink-2 hover:bg-surface-2 hover:text-ink"
            >
              Talkgroup fields
            </a>
          </Tip>
          {areas.map(({ area }) => (
            <Tip key={area.prefix} label={HELP["help.area"]}>
              <a
                href={`#help-${area.prefix}`}
                className="rounded-full border border-line px-2.5 py-1 text-xs text-ink-2 hover:bg-surface-2 hover:text-ink"
              >
                {area.label}
              </a>
            </Tip>
          ))}
        </nav>

        {/* Not filtered by the search box: the box searches the tooltip registry, and
            silently hiding a manual because a control's name did not match would read as
            the section having gone. */}
        <section id="help-talkgroup-fields" className="mb-4 scroll-mt-4 space-y-2">
          <h2 className="text-sm font-semibold text-ink">Talkgroup fields</h2>
          <p className="max-w-[70ch] text-sm text-ink-2">
            A talkgroup's agency, category and service tag are the three things a person decides rather than
            the recorder discovering. Here is where each comes from, what to put in it, and what it changes.
          </p>
          <TalkgroupFields />
        </section>

        {areas.length === 0 ? (
          <EmptyState
            title="Nothing matches"
            hint="Try a shorter word — the search covers both the control's name and its description."
          />
        ) : (
          <div className="space-y-4">
            {areas.map(({ area, ids }) => (
              <Card key={area.prefix} id={`help-${area.prefix}`} className="scroll-mt-4">
                <CardHeader title={area.label} hint={area.description} />
                <dl className="divide-y divide-line">
                  {ids.map((id: HelpId) => (
                    <div key={id} className="grid gap-1 px-4 py-2.5 sm:grid-cols-[14rem_1fr] sm:gap-4">
                      <dt className="min-w-0">
                        <span className="text-sm font-medium text-ink">{labelForId(id)}</span>
                        <span className="ml-2 hidden font-mono text-[10px] text-muted sm:inline">{id}</span>
                      </dt>
                      <dd className="max-w-[70ch] text-sm text-ink-2">{HELP[id]}</dd>
                    </div>
                  ))}
                </dl>
              </Card>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
