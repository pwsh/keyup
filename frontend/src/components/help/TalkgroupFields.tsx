/**
 * The "Talkgroup fields" reference on the Help page.
 *
 * The rest of `/help` is the tooltip registry, which answers "what does this
 * control do". Three talkgroup fields need the other kind of answer — "what
 * should I put in it" — and a one-sentence tooltip cannot carry twenty
 * categories and thirty-one service tags.
 *
 * Every word here comes from `GET /directory/vocabulary`, which the backend
 * builds from `app/vocabulary.py`; the same module renders `docs/TALKGROUPS.md`,
 * and a backend test fails if the two drift. So this component holds the page's
 * *shape* and none of its content — adding a category to the backend needs no
 * frontend release for the app to say what it is for.
 */

import { useVocabulary } from "../../api/hooks";
import type { CategoryTerm, ServiceTagTerm, VocabularyTerm } from "../../api/types";
import { Card, CardHeader } from "../ui/Card";
import { CategoryIcon } from "../ui/CategoryIcon";
import { SkeletonText } from "../ui/Skeleton";
import { EmptyState } from "../ui/States";

/** The anchors an InfoDot in the talkgroup dialog links to. Exported so it cannot typo. */
export const TALKGROUP_FIELD_ANCHORS = {
  agency: "help-agencies",
  category: "help-categories",
  serviceTag: "help-service-tags",
} as const;

export type TalkgroupField = keyof typeof TALKGROUP_FIELD_ANCHORS;

/** `/help#…` for one of the three fields; what `InfoDot`'s `to` prop is given. */
export const helpHref = (field: TalkgroupField): string => `/help#${TALKGROUP_FIELD_ANCHORS[field]}`;

function Term({ label, code, description }: { label: string; code?: string; description: string }) {
  return (
    <div className="grid gap-1 px-4 py-2.5 sm:grid-cols-[14rem_1fr] sm:gap-4">
      <dt className="min-w-0">
        <span className="text-sm font-medium text-ink">{label}</span>
        {code ? <span className="ml-2 font-mono text-[10px] text-muted">{code}</span> : null}
      </dt>
      <dd className="max-w-[70ch] text-sm text-ink-2">{description}</dd>
    </div>
  );
}

function Categories({ terms, effect }: { terms: CategoryTerm[]; effect: string }) {
  return (
    <Card id={TALKGROUP_FIELD_ANCHORS.category} className="scroll-mt-4">
      <CardHeader
        title="Categories"
        hint="This archive's own coarse bucket for what kind of channel something is — the values the app knows how to draw, filter and count."
      />
      <p className="border-b border-line px-4 py-2.5 text-sm text-ink-2">
        <span className="block max-w-[70ch]">{effect}</span>
      </p>
      <dl className="divide-y divide-line">
        {terms.map((term) => (
          <div key={term.key} className="grid gap-1 px-4 py-2.5 sm:grid-cols-[14rem_1fr] sm:gap-4">
            <dt className="flex min-w-0 items-center gap-1.5">
              <CategoryIcon category={term.key} interactive={false} />
              <span className="text-sm font-medium text-ink">{term.label}</span>
              <span className="font-mono text-[10px] text-muted">{term.key}</span>
            </dt>
            <dd className="max-w-[70ch] text-sm text-ink-2">
              {term.description}
              {term.service_tags.length > 0 ? (
                <span className="mt-1 block text-xs text-muted">
                  RadioReference tags that land here: {term.service_tags.join(", ")}
                </span>
              ) : null}
            </dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

function ServiceTags({ terms }: { terms: ServiceTagTerm[] }) {
  return (
    <Card id={TALKGROUP_FIELD_ANCHORS.serviceTag} className="scroll-mt-4">
      <CardHeader
        title="Service tags"
        hint="RadioReference's own vocabulary, stored in RR's spelling. Finer than the category — RR separates dispatch from tac from talk — and each one maps to exactly one category."
      />
      {/* The table is three columns wide on a phone; it scrolls in its own box
          rather than making the page scroll sideways — a tab stop, so a keyboard can too. */}
      <section className="overflow-x-auto scrollbar-thin" aria-label="Service tags" tabIndex={0}>
        <table className="w-full min-w-[34rem] text-sm">
          <thead>
            <tr className="border-b border-line text-left text-xs text-muted">
              <th scope="col" className="px-4 py-2 font-medium">
                RadioReference tag
              </th>
              <th scope="col" className="px-4 py-2 font-medium">
                What RR means by it
              </th>
              <th scope="col" className="px-4 py-2 font-medium">
                Category
              </th>
            </tr>
          </thead>
          <tbody>
            {terms.map((term) => (
              <tr key={term.tag} className="border-b border-line last:border-0">
                <th scope="row" className="whitespace-nowrap px-4 py-2 text-left font-medium text-ink">
                  {term.tag}
                </th>
                <td className="px-4 py-2 text-ink-2">{term.description}</td>
                <td className="whitespace-nowrap px-4 py-2">
                  <span className="inline-flex items-center gap-1.5 text-ink-2">
                    <CategoryIcon category={term.category} interactive={false} />
                    {term.category_label}
                  </span>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </section>
    </Card>
  );
}

function Agencies({ kinds, sources }: { kinds: VocabularyTerm[]; sources: VocabularyTerm[] }) {
  return (
    <Card id={TALKGROUP_FIELD_ANCHORS.agency} className="scroll-mt-4">
      <CardHeader
        title="Agencies"
        hint="Who owns the channel. The list is seeded from the deployment's agency list, plus any agency added here that it has no code for."
      />
      <div className="space-y-3 border-b border-line px-4 py-3 text-sm text-ink-2 [&>p]:max-w-[70ch]">
        <p>
          Most agencies are seeded from an agency list (keyup seed-agencies) — usually the dispatch system's
          own codes, so a call lined up against a CAD incident matches on the same string. Re-seeding
          refreshes them and never deletes anything.
        </p>
        <p>
          Not everything this receiver hears belongs to one of those. A neighbouring county's sheriff, a
          railroad, a school district, a federal unit: those are added on{" "}
          <span className="font-medium text-ink">Admin → Agencies</span>, are marked{" "}
          <span className="font-mono text-xs">local</span>, and a re-seed leaves them alone. An agency that
          has merged or disbanded is deactivated rather than deleted, so the calls still mapped to it keep
          reading correctly.
        </p>
        <p>
          Mapping a talkgroup to an agency is what makes the agency filter on Calls work, what lets a CAD
          incident be correlated against the audio, and what the roster and the statistics group by. It
          changes nothing about recording, transcription or retention.
        </p>
      </div>
      <dl className="divide-y divide-line">
        {sources.map((term) => (
          <Term key={term.key} label={term.label} code={term.key} description={term.description} />
        ))}
      </dl>
      <h3 className="border-y border-line bg-surface-2 px-4 py-2 text-xs font-semibold text-ink-2">
        Agency kinds
      </h3>
      <dl className="divide-y divide-line">
        {kinds.map((term) => (
          <Term key={term.key} label={term.label} code={term.key} description={term.description} />
        ))}
      </dl>
    </Card>
  );
}

/** The three cards, in the order the talkgroup dialog asks for the fields. */
export function TalkgroupFields() {
  const vocabulary = useVocabulary();

  if (vocabulary.isPending) {
    return (
      <Card className="p-4">
        <SkeletonText lines={6} />
      </Card>
    );
  }
  if (!vocabulary.data) {
    return (
      <Card className="p-4">
        <EmptyState
          title="The field reference is not available"
          hint="It comes from the API; the rest of this page works without it."
        />
      </Card>
    );
  }

  const { categories, category_effect, service_tags, agency_kinds, agency_sources } = vocabulary.data;
  return (
    <div className="space-y-4">
      <Agencies kinds={agency_kinds} sources={agency_sources} />
      <Categories terms={categories} effect={category_effect} />
      <ServiceTags terms={service_tags} />
    </div>
  );
}
