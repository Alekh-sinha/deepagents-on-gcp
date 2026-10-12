import { ApiError, api, describeError, setUnauthorizedHandler } from "./client";

const reply = (status: number, body: unknown) => Promise.resolve(new Response(typeof body === "string" ? body : JSON.stringify(body), { status }));
let fetchMock: ReturnType<typeof vi.fn>;
beforeEach(() => { fetchMock = vi.fn(); vi.stubGlobal("fetch", fetchMock); setUnauthorizedHandler(null); });
afterEach(() => vi.unstubAllGlobals());

describe("what is sent", () => {
  it("adds the CSRF header to everything that changes something, and only to those", async () => {
    fetchMock.mockImplementation(() => reply(200, {}));
    await api.me();
    await api.createSession({ title: "x" });
    await api.mcpDelete("calc");
    const sent = fetchMock.mock.calls.map(([, init]) => ({ method: init.method, csrf: init.headers["X-Requested-With"] }));
    expect(sent).toEqual([{ method: "GET", csrf: undefined }, { method: "POST", csrf: "pinboard" }, { method: "DELETE", csrf: "pinboard" }]);
  });

  it("sends JSON bodies with the cookie, and builds query strings without empty values", async () => {
    fetchMock.mockImplementation(() => reply(200, {}));
    await api.send("s1", "hi", { auto_approve: false });
    expect(fetchMock.mock.calls[0][0]).toBe("/api/sessions/s1/messages");
    expect(fetchMock.mock.calls[0][1]).toMatchObject({ credentials: "same-origin", body: JSON.stringify({ text: "hi", auto_approve: false }), headers: { "Content-Type": "application/json" } });
    await api.transcript("s1", 20, undefined);
    await api.approvals("pending", undefined);
    expect(fetchMock.mock.calls[1][0]).toBe("/api/sessions/s1/transcript?limit=20");
    expect(fetchMock.mock.calls[2][0]).toBe("/api/approvals?status=pending");
  });

  it("keeps an explicit null (it means 'off')", async () => {
    fetchMock.mockImplementation(() => reply(200, {}));
    await api.updateSession("s1", { auto_approve: null });
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ auto_approve: null });
  });
});

describe("what comes back", () => {
  it("returns the parsed body", async () => {
    fetchMock.mockImplementation(() => reply(200, { user: "a@b.c" }));
    expect(await api.me()).toEqual({ user: "a@b.c" });
  });

  it("turns the server's reason into the error message", async () => {
    fetchMock.mockImplementation(() => reply(409, { detail: "this session already has a turn in progress" }));
    await expect(api.send("s1", "hi")).rejects.toMatchObject({ status: 409, message: "this session already has a turn in progress" });
  });

  it("explains validation problems in words", () => {
    expect(describeError(422, { detail: [{ loc: ["body", "cron"], msg: "String should have at least 9 characters" }] })).toBe("cron: String should have at least 9 characters");
    expect(describeError(400, { detail: "unknown class_method 'list_mcp_servers'; expected one of [...]" })).toMatch(/not been deployed to your cloud engine yet/);
    expect(describeError(401, null)).toBe("You are signed out.");
    expect(describeError(429, "not json")).toMatch(/Too many requests/);
    expect(describeError(500, null)).toBe("The server answered 500.");
  });

  it("says plainly when the server cannot be reached", async () => {
    fetchMock.mockImplementation(() => Promise.reject(new TypeError("Failed to fetch")));
    await expect(api.me()).rejects.toMatchObject({ status: 0, message: expect.stringContaining("Cannot reach the server") });
  });

  it("tells the app when the session ended, and still fails the call", async () => {
    const gone = vi.fn();
    setUnauthorizedHandler(gone);
    fetchMock.mockImplementation(() => reply(401, { detail: "not signed in" }));
    await expect(api.sessions()).rejects.toBeInstanceOf(ApiError);
    expect(gone).toHaveBeenCalledTimes(1);
  });

  it("tolerates an empty body and a body that is not JSON", async () => {
    fetchMock.mockImplementationOnce(() => reply(200, ""));
    expect(await api.me()).toBeNull();
    fetchMock.mockImplementationOnce(() => reply(502, "<html>bad gateway</html>"));
    await expect(api.me()).rejects.toMatchObject({ status: 502, message: "The server answered 502." });
  });
});
