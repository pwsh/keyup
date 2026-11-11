/**
 * The help registry as the app sees it: ids, placeholders and a lazy loader.
 *
 * The text lives in `text.ts` (~94 KB of source). It used to be imported by the app
 * shell, so it sat in the shared chunk every page waited for, although a sentence of it
 * is only read when someone hovers or focuses a control. Now `HELP["x.y"]` is a short
 * placeholder until the text chunk arrives — fetched on idle after the first render, or
 * at once on the first tooltip — and the real sentence afterwards. `Tip` resolves
 * placeholders at the moment it shows, so every tooltip, however early its label was
 * computed, reads the real text.
 *
 * Anything that renders a help sentence *outside* a tooltip (visible text, an
 * `aria-label`) should use `useHelpText` / `<HelpText>` below,
 * which re-render when the text lands.
 */
import { cloneElement, isValidElement, type ReactNode, useSyncExternalStore } from "react";
import type { HELP_TEXT } from "./text";

export type HelpId = keyof typeof HELP_TEXT;

type HelpTable = Readonly<Record<HelpId, string>>;

/** Private-use delimiters: never typed by anyone, so a placeholder cannot collide with text. */
const OPEN = "";
const CLOSE = "";
const TOKEN = /([^]*)/g;

let table: HelpTable | null = null;
let pending: Promise<void> | null = null;
const listeners = new Set<() => void>();

/** Fetch the text chunk once; every later call returns the same promise. */
export function loadHelp(): Promise<void> {
  if (table) return Promise.resolve();
  pending ??= import("./text").then(
    (module) => {
      table = module.HELP_TEXT;
      for (const listener of listeners) listener();
    },
    (error: unknown) => {
      // A failed fetch (offline, a deploy replaced the chunk) must not wedge every
      // tooltip: forget it so the next hover tries again.
      pending = null;
      throw error;
    },
  );
  return pending;
}

export function isHelpLoaded(): boolean {
  return table !== null;
}

export function subscribeHelp(listener: () => void): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** True once the help text is here; re-renders the caller when it lands. */
export function useHelpLoaded(): boolean {
  return useSyncExternalStore(subscribeHelp, isHelpLoaded, isHelpLoaded);
}

/**
 * `HELP["x.y"]` — the sentence once loaded, a placeholder before. Typed as the string it
 * becomes, so template literals and `tip=` props keep working unchanged.
 */
export const HELP: HelpTable = new Proxy({} as HelpTable, {
  get(_target, id) {
    if (typeof id !== "string") return undefined;
    return table ? table[id as HelpId] : `${OPEN}${id}${CLOSE}`;
  },
});

/** The tooltip text for an id. Typed, so a stale id is a compile error. */
export const helpText = (id: HelpId): string => HELP[id];

export function hasHelpPlaceholder(text: string): boolean {
  return text.includes(OPEN);
}

/**
 * Replace every placeholder in `text` with its sentence. Before the text has loaded a
 * placeholder becomes "" — never the raw marker.
 */
export function resolveHelp(text: string): string {
  if (!text.includes(OPEN)) return text;
  return text.replace(TOKEN, (_match, id: string) => (table ? (table[id as HelpId] ?? "") : ""));
}

/** {@link resolveHelp} through a React tree: strings, arrays and element children. */
export function resolveHelpNode(node: ReactNode): ReactNode {
  if (typeof node === "string") return resolveHelp(node);
  if (Array.isArray(node)) return node.map(resolveHelpNode);
  if (isValidElement<{ children?: ReactNode }>(node) && node.props.children !== undefined) {
    const children = node.props.children;
    // Spread, not one array argument: as varargs the children keep needing no keys.
    return Array.isArray(children)
      ? cloneElement(node, undefined, ...children.map(resolveHelpNode))
      : cloneElement(node, undefined, resolveHelpNode(children));
  }
  return node;
}

/** One sentence for visible text or an `aria-label`: "" until loaded, then the text. */
export function useHelpText(id: HelpId): string {
  useHelpLoaded();
  return resolveHelp(HELP[id]);
}

/** `<HelpText id="x.y" />` — {@link useHelpText} as an element, for JSX children. */
export function HelpText({ id }: { id: HelpId }): string {
  return useHelpText(id);
}

/**
 * Render `children` with any `HELP[...]` placeholders in them resolved, re-rendering when
 * the text lands — for components that show a hint passed in as a prop.
 */
export function ResolvedHelp({ children }: { children: ReactNode }): ReactNode {
  useHelpLoaded();
  return resolveHelpNode(children);
}

/** Test hook: back to "nothing loaded yet". */
export function resetHelpForTest(): void {
  table = null;
  pending = null;
}
