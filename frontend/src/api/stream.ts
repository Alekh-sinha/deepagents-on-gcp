// Follow one turn's live events. EventSource reconnects by itself and sends Last-Event-ID, so a dropped connection loses nothing.
import type { EngineEvent } from "./types";

export interface StreamHandlers {
  onEvent: (e: EngineEvent) => void;
  /** The server closed the stream: the turn finished, or paused for an approval, or (reason "time") the connection was recycled. */
  onEnd: (info: { after_seq: number; reason: string }) => void;
  onError: (info: { status: number; message: string; after_seq: number }) => void;
}

export interface StreamSource {
  addEventListener(type: string, listener: (e: MessageEvent) => void): void;
  close(): void;
  onmessage: ((e: MessageEvent) => void) | null;
}
export type StreamFactory = (url: string) => StreamSource;

const defaultFactory: StreamFactory = (url) => new EventSource(url) as unknown as StreamSource;

/** Returns a function that stops following. */
export function followTurn(turnId: string, afterSeq: number, h: StreamHandlers, factory: StreamFactory = defaultFactory): () => void {
  const es = factory(`/api/turns/${turnId}/stream?after_seq=${afterSeq}`);
  let closed = false;
  const close = () => { if (!closed) { closed = true; es.close(); } };
  es.onmessage = (m) => {
    try { h.onEvent(JSON.parse(m.data) as EngineEvent); } catch { /* a malformed message is skipped, never fatal */ }
  };
  es.addEventListener("end", (m) => {
    let info = { after_seq: afterSeq, reason: "finished" };
    try { info = JSON.parse(m.data); } catch { /* keep defaults */ }
    close();
    h.onEnd(info);
  });
  es.addEventListener("error", (m) => {
    // the server's own `event: error` carries JSON; the browser's connection-level errors have no data (and EventSource retries those itself)
    const data = (m as MessageEvent).data;
    if (typeof data !== "string") return;
    let info = { status: 502, message: "The live connection failed.", after_seq: afterSeq };
    try { info = JSON.parse(data); } catch { /* keep defaults */ }
    close();
    h.onError(info);
  });
  return close;
}
