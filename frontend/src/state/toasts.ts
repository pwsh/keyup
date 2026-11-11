import { create } from "zustand";
import { ApiError, describeError } from "../api/client";

export type ToastTone = "success" | "error" | "info";

export interface ToastMessage {
  id: number;
  title: string;
  body?: string;
  tone: ToastTone;
}

interface ToastStore {
  toasts: ToastMessage[];
  push: (toast: ToastMessage) => void;
  dismiss: (id: number) => void;
}

let nextId = 1;

export const useToastStore = create<ToastStore>((set) => ({
  toasts: [],
  push: (toast) => set((state) => ({ toasts: [...state.toasts, toast] })),
  dismiss: (id) => set((state) => ({ toasts: state.toasts.filter((t) => t.id !== id) })),
}));

/** Show a background result (job queued, save succeeded, export failed…). */
export function toast(title: string, opts: { body?: string; tone?: ToastTone; timeout?: number } = {}): void {
  const item: ToastMessage = { id: nextId++, title, tone: opts.tone ?? "info" };
  if (opts.body) item.body = opts.body;
  useToastStore.getState().push(item);
  const timeout = opts.timeout ?? 6000;
  if (timeout > 0) setTimeout(() => useToastStore.getState().dismiss(item.id), timeout);
}

export const dismissToast = (id: number): void => useToastStore.getState().dismiss(id);

/**
 * What the API actually said, in one line, or null when it said nothing useful.
 *
 * `ApiError.detail` is the server's own sentence — "callsign 'E1' already exists",
 * "unknown agency code 'NOPE'" — and it is the only part of a 409 or a 422 that
 * tells the reader what to do differently. `ErrorState` has always printed it for
 * failed *reads*; this is the same courtesy for failed *writes*.
 */
export function errorDetail(error: unknown): string | null {
  // Through `describeError`, so a 500's raw exception text never becomes the body: the
  // API's reason when it wrote one for people, otherwise a plain sentence for the status.
  if (!(error instanceof ApiError) && !(error instanceof Error)) return null;
  const text = describeError(error).message;
  return text.trim() ? text : null;
}

/**
 * Report a failed mutation with the API's reason attached.
 *
 * The fallback stays the headline, because it names the thing that did not save in
 * the app's own words; the server's detail goes in the body, where it explains why.
 * Every directory dialog used to pass a fixed string and drop the detail entirely,
 * which turned "that agency code does not exist" into "Could not save".
 */
export function mutationErrorToast(error: unknown, fallback: string): void {
  toast(fallback, { tone: "error", body: errorDetail(error) ?? undefined });
}
