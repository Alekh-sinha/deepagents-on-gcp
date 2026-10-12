import type { Item } from "./transcript";
import { toBlocks } from "./blocks";

const tool = (id: string, agent: string, ns: string, args: Record<string, any> = {}, name = "execute"): Item => ({ kind: "tool", id, turnId: "t", agent, ns, name, args, status: "success", result: "", resultTruncated: false, edited: false });
const say = (id: string, agent: string, ns: string): Item => ({ kind: "assistant", id, turnId: "t", agent, ns, text: "x", streaming: false, truncated: false });
const delegate = (id: string, type: string, model?: string): Item => tool(id, "assistant", "", { subagent_type: type, description: "d", ...(model ? { model } : {}) }, "delegate");

describe("helper blocks", () => {
  it("gathers one helper's steps into one block, in place", () => {
    const b = toBlocks([delegate("d1", "researcher", "m-lite"), tool("a", "researcher", "tools:1"), say("b", "researcher", "tools:1"), say("c", "assistant", "")]);
    expect(b.map((x) => x.type)).toEqual(["item", "helper", "item"]);
    expect((b[1] as any).items).toHaveLength(2);
  });

  it("keeps parallel helpers of the same kind apart even when their steps interleave, each with its own model", () => {
    const b = toBlocks([delegate("d1", "coder", "m-lite"), delegate("d2", "coder", "m-flash"), tool("a", "coder", "tools:A"), tool("b", "coder", "tools:B"), tool("c", "coder", "tools:A")],
      { "tools:A": "m-flash", "tools:B": "m-lite" });
    const helpers = b.filter((x) => x.type === "helper") as any[];
    expect(helpers.map((h) => [h.key, h.model, h.items.length])).toEqual([["tools:A", "m-flash", 2], ["tools:B", "m-lite", 1]]);
  });

  it("uses the model the assistant asked for until the real one is known, handing them out in the order the helpers started", () => {
    const b = toBlocks([delegate("d1", "coder", "m-lite"), delegate("d2", "coder", "m-flash"), tool("a", "coder", "tools:A"), tool("b", "coder", "tools:B")]);
    expect((b.filter((x) => x.type === "helper") as any[]).map((h) => h.model)).toEqual(["m-lite", "m-flash"]);
  });

  it("has no model to show when none was chosen or reported", () => {
    expect((toBlocks([delegate("d1", "researcher"), tool("a", "researcher", "tools:1")])[1] as any).model).toBe("");
  });

  it("falls back to the helper's name when events carry no namespace", () => {
    const b = toBlocks([tool("a", "researcher", ""), tool("b", "researcher", "")]);
    expect(b).toHaveLength(1);
    expect((b[0] as any).items).toHaveLength(2);
  });

  it("leaves the assistant's own steps alone", () => {
    expect(toBlocks([tool("a", "assistant", ""), say("b", "assistant", "")]).every((x) => x.type === "item")).toBe(true);
  });
});
