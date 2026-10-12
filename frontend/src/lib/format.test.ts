import type { Session } from "../api/types";
import { describeCron, formatBytes, groupSessions, relativeTime, toolLabel } from "./format";

const NOW = new Date(2026, 9, 11, 15, 0, 0);
const at = (days: number, hours = 0) => new Date(NOW.getTime() - days * 86400000 - hours * 3600000).toISOString();
const sess = (id: string, last: string): Session => ({ id, title: id, last_active_at: last } as Session);

describe("times", () => {
  it("says how long ago, and how long until", () => {
    expect(relativeTime(at(0, 0), NOW)).toBe("just now");
    expect(relativeTime(new Date(NOW.getTime() - 5 * 60000).toISOString(), NOW)).toBe("5 min ago");
    expect(relativeTime(at(0, 3), NOW)).toBe("3 h ago");
    expect(relativeTime(at(2), NOW)).toBe("2 d ago");
    expect(relativeTime(new Date(NOW.getTime() + 2 * 3600000).toISOString(), NOW)).toBe("in 2 h");
    expect(relativeTime(null, NOW)).toBe("");
    expect(relativeTime("not a date", NOW)).toBe("");
  });
  it("formats sizes", () => {
    expect([formatBytes(12), formatBytes(2048), formatBytes(5 * 1024 * 1024), formatBytes(-1)]).toEqual(["12 B", "2.0 KB", "5.0 MB", ""]);
  });
});

describe("the conversation list", () => {
  it("groups by day, newest first, and drops empty groups", () => {
    const g = groupSessions([sess("old", at(30)), sess("today-b", at(0, 1)), sess("yday", at(1)), sess("week", at(4)), sess("today-a", at(0, 0))], NOW);
    expect(g.map((x) => x.label)).toEqual(["Today", "Yesterday", "Previous 7 days", "Earlier"]);
    expect(g[0].sessions.map((s) => s.id)).toEqual(["today-a", "today-b"]);
    expect(groupSessions([sess("a", at(0, 1))], NOW).map((x) => x.label)).toEqual(["Today"]);
    expect(groupSessions([], NOW)).toEqual([]);
  });
});

describe("tools in plain words", () => {
  it("names what the agent did", () => {
    expect(toolLabel("execute", { command: "ls -la" })).toEqual({ title: "Ran a command", detail: "ls -la" });
    expect(toolLabel("mcp__calc__add", { a: 1 })).toEqual({ title: "calc · add", detail: '{"a":1}' });
    expect(toolLabel("delegate", { subagent_type: "researcher", description: "find it" })).toEqual({ title: "Asked the researcher", detail: "find it" });
    expect(toolLabel("some_new_tool", {}).title).toBe("some new tool");
  });
  it("shortens long details", () => {
    expect(toolLabel("execute", { command: "x".repeat(300) }).detail.length).toBeLessThanOrEqual(90);
  });
});

describe("schedules in plain words", () => {
  it("describes simple crons and leaves complex ones alone", () => {
    expect(describeCron("0 9 * * 1-5")).toBe("Weekdays at 9:00");
    expect(describeCron("30 18 * * *")).toBe("Every day at 18:30");
    expect(describeCron("0 7 * * 3")).toBe("Every Wednesday at 07:00");
    expect(describeCron("*/7 1-5 * * *")).toBe("*/7 1-5 * * *");
    expect(describeCron("nonsense")).toBe("nonsense");
  });
});
