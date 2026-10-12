// Lays the transcript out in blocks: the assistant's own lines, and one block per helper run.
import type { Item } from "./transcript";

export type Block =
  | { type: "item"; item: Item }
  | { type: "helper"; key: string; agent: string; model: string; items: Item[] };

const isHelper = (i: Item): i is Extract<Item, { kind: "tool" | "assistant" }> => (i.kind === "tool" || i.kind === "assistant") && i.agent !== "assistant" && i.agent !== "";

/**
 * Every step a helper took is gathered into ONE block per helper run (its stream namespace), placed where the helper first appeared. Helpers that run
 * in parallel therefore stay separate even when their steps interleave, and each block names the model that helper really used (from the usage events;
 * until the first one arrives, the model the assistant chose in its `delegate` call, matched by helper type in the order they were started).
 */
export function toBlocks(items: Item[], nsModel: Record<string, string> = {}): Block[] {
  const blocks: Block[] = [];
  const byKey = new Map<string, Extract<Block, { type: "helper" }>>();
  const planned: { agent: string; model: string; used: boolean }[] = [];
  for (const item of items) {
    if (item.kind === "tool" && (item.name === "delegate" || item.name === "task") && (item.agent === "assistant" || item.agent === "")) {
      planned.push({ agent: String(item.args.subagent_type ?? ""), model: typeof item.args.model === "string" ? item.args.model : "", used: false });
    }
    if (isHelper(item)) {
      const key = item.ns || `agent:${item.agent}`;
      let block = byKey.get(key);
      if (!block) {
        const plan = planned.find((p) => p.agent === item.agent && !p.used);
        if (plan) plan.used = true;
        block = { type: "helper", key, agent: item.agent, model: nsModel[item.ns] ?? plan?.model ?? "", items: [] };
        byKey.set(key, block);
        blocks.push(block);
      }
      block.items.push(item);
      if (item.ns && nsModel[item.ns]) block.model = nsModel[item.ns];
      continue;
    }
    blocks.push({ type: "item", item });
  }
  return blocks;
}
