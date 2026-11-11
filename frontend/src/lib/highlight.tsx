import { Fragment, type ReactNode } from "react";

const ESCAPE = /[.*+?^${}()|[\]\\]/g;

/** The bare terms of a websearch query, quotes and negations resolved. */
export function queryTerms(query: string | undefined): string[] {
  if (!query) return [];
  return (query.match(/"[^"]*"|\S+/g) ?? [])
    .filter((term) => !term.startsWith("-") && term.toLowerCase() !== "or" && term.toLowerCase() !== "and")
    .map((term) => term.replace(/^"|"$/g, "").trim())
    .filter((term) => term.length > 1);
}

/**
 * Wrap every occurrence of the query's terms in `<mark>`.
 *
 * Built as React nodes rather than an HTML string, so a transcript can never
 * inject markup — which matters because transcripts are machine output over
 * audio anybody with a radio can produce.
 */
export function highlightTerms(text: string, query: string | undefined): ReactNode {
  const terms = queryTerms(query);
  if (!terms.length) return text;
  const pattern = new RegExp(`(${terms.map((term) => term.replace(ESCAPE, "\\$&")).join("|")})`, "gi");
  const parts = text.split(pattern);
  return parts.map((part, index) =>
    index % 2 === 1 ? (
      // biome-ignore lint/suspicious/noArrayIndexKey: split() parts are positional, and the same word may appear twice.
      <mark key={index}>{part}</mark>
    ) : (
      // biome-ignore lint/suspicious/noArrayIndexKey: as above.
      <Fragment key={index}>{part}</Fragment>
    ),
  );
}

/**
 * The API's `ts_headline` output, which is plain text with `<mark>` in it.
 * Anything else is escaped rather than rendered, so only the highlight survives.
 */
export function renderHeadline(headline: string): ReactNode {
  const parts = headline.split(/(<mark>.*?<\/mark>)/g);
  return parts.map((part, index) => {
    const match = /^<mark>(.*?)<\/mark>$/s.exec(part);
    return match ? (
      // biome-ignore lint/suspicious/noArrayIndexKey: positional split, duplicates expected.
      <mark key={index}>{match[1]}</mark>
    ) : (
      // biome-ignore lint/suspicious/noArrayIndexKey: as above.
      <Fragment key={index}>{part}</Fragment>
    );
  });
}
