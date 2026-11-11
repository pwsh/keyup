/**
 * `ingest_priority` as the UI reads it.
 *
 * The reading has to agree with `priority_class` in `backend/app/ingest/flow.py`, because
 * the browser marks a row "backfilled" from the value it was sent while the server decides
 * `?priority=` from the same value in SQL. Disagreeing would mean a filtered list whose
 * rows contradict the filter that produced them.
 */
import { describe, expect, it } from "vitest";
import { INGEST_PRIORITY_CLASSES, isBackfilled, priorityClass } from "./types";

const call = (ingest_priority: string | null) => ({ ingest_priority });

describe("priorityClass", () => {
  it("reads the four class names the push path stores", () => {
    expect(priorityClass(call("live"))).toBe("live");
    // `urgent` is an operator jumping the queue, not a different kind of traffic.
    expect(priorityClass(call("urgent"))).toBe("live");
    expect(priorityClass(call("backlog"))).toBe("backlog");
    expect(priorityClass(call("bulk"))).toBe("bulk");
  });

  it("judges a bare integer against the queue's own band edges", () => {
    expect(priorityClass(call("1"))).toBe("live");
    expect(priorityClass(call("499"))).toBe("live");
    expect(priorityClass(call("500"))).toBe("backlog");
    expect(priorityClass(call("899"))).toBe("backlog");
    expect(priorityClass(call("900"))).toBe("bulk");
  });

  it("puts a null in no class at all rather than folding it into live", () => {
    expect(priorityClass(call(null))).toBeNull();
    expect(priorityClass(call("   "))).toBeNull();
    expect(priorityClass({})).toBeNull();
  });

  it("fails closed on anything it cannot read", () => {
    expect(priorityClass(call("sometime"))).toBeNull();
    expect(priorityClass(call("12.5"))).toBeNull();
  });

  it("only ever returns one of the three classes it advertises", () => {
    for (const raw of ["live", "urgent", "backlog", "bulk", "1", "500", "900"]) {
      const name = priorityClass(call(raw));
      expect(name).not.toBeNull();
      expect(INGEST_PRIORITY_CLASSES as readonly string[]).toContain(name as string);
    }
  });
});

describe("isBackfilled", () => {
  it("is true only for traffic that arrived after the fact", () => {
    expect(isBackfilled(call("backlog"))).toBe(true);
    expect(isBackfilled(call("bulk"))).toBe(true);
    expect(isBackfilled(call("700"))).toBe(true);
  });

  it("is false for live traffic and for a call nobody classified", () => {
    expect(isBackfilled(call("live"))).toBe(false);
    expect(isBackfilled(call("urgent"))).toBe(false);
    expect(isBackfilled(call("42"))).toBe(false);
    // A recording the file watcher found on disk: no class, and no claim either way.
    expect(isBackfilled(call(null))).toBe(false);
  });
});
