import { followTurn, type StreamSource } from "./stream";

class Fake implements StreamSource {
  onmessage: ((e: MessageEvent) => void) | null = null;
  listeners: Record<string, ((e: MessageEvent) => void)[]> = {};
  closed = false;
  addEventListener(type: string, fn: (e: MessageEvent) => void) { (this.listeners[type] ||= []).push(fn); }
  close() { this.closed = true; }
  emit(type: string | null, data: string) { const e = { data } as MessageEvent; (type ? this.listeners[type] ?? [] : this.onmessage ? [this.onmessage] : []).forEach((f) => f(e)); }
}

function start(afterSeq = 0) {
  const es = new Fake();
  const urls: string[] = [];
  const h = { onEvent: vi.fn(), onEnd: vi.fn(), onError: vi.fn() };
  const stop = followTurn("t1", afterSeq, h, (u) => { urls.push(u); return es; });
  return { es, h, stop, urls };
}

describe("following a turn", () => {
  it("asks for events after the last one seen", () => {
    expect(start(7).urls).toEqual(["/api/turns/t1/stream?after_seq=7"]);
  });

  it("passes each event on, and skips a message it cannot read", () => {
    const { es, h } = start();
    es.emit(null, JSON.stringify({ seq: 1, type: "token", payload: { text: "a" } }));
    es.emit(null, "{broken");
    es.emit(null, JSON.stringify({ seq: 2, type: "turn_done", payload: {} }));
    expect(h.onEvent.mock.calls.map((c) => c[0].seq)).toEqual([1, 2]);
  });

  it("closes and reports when the server ends the stream", () => {
    const { es, h } = start();
    es.emit("end", JSON.stringify({ after_seq: 9, reason: "finished" }));
    expect(es.closed).toBe(true);
    expect(h.onEnd).toHaveBeenCalledWith({ after_seq: 9, reason: "finished" });
  });

  it("closes and reports the server's own error message", () => {
    const { es, h } = start();
    es.emit("error", JSON.stringify({ status: 404, message: "turn not found", after_seq: 0 }));
    expect(es.closed).toBe(true);
    expect(h.onError).toHaveBeenCalledWith({ status: 404, message: "turn not found", after_seq: 0 });
  });

  it("leaves connection-level errors to the browser, which reconnects by itself", () => {
    const { es, h } = start();
    es.listeners.error.forEach((f) => f(new Event("error") as unknown as MessageEvent));
    expect(es.closed).toBe(false);
    expect(h.onError).not.toHaveBeenCalled();
  });

  it("stops when asked, and a stopped stream stays quiet", () => {
    const { es, h, stop } = start();
    stop();
    stop();
    expect(es.closed).toBe(true);
    expect(h.onEnd).not.toHaveBeenCalled();
  });
});
