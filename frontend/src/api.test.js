import assert from "node:assert/strict";
import { beforeEach, describe, it, mock } from "node:test";
import { apiRequest, storeToken, storedToken } from "./api.js";

beforeEach(() => {
  const localStorage = new Map();
  const sessionStorage = new Map();
  const storageApi = (storage) => ({
    getItem: (key) => storage.get(key) || null,
    setItem: (key, value) => storage.set(key, value),
    removeItem: (key) => storage.delete(key),
    clear: () => storage.clear(),
  });
  globalThis.window = {
    localStorage: storageApi(localStorage),
    sessionStorage: storageApi(sessionStorage),
  };
  storeToken("");
  mock.restoreAll();
});

describe("api token storage", () => {
  it("stores and clears bearer tokens", () => {
    storeToken(" deployer-token ");

    assert.equal(storedToken(), "deployer-token");

    storeToken("");

    assert.equal(storedToken(), "");
  });

  it("does not read tokens from browser storage", () => {
    window.localStorage.setItem("autodeploy-control-plane-token", "legacy-token");
    window.sessionStorage.setItem("autodeploy-control-plane-token", "session-token");

    assert.equal(storedToken(), "");
  });
});

describe("apiRequest", () => {
  it("adds authorization when a token is configured", async () => {
    storeToken("deployer-token");
    globalThis.fetch = mock.fn(() =>
      Promise.resolve(
      new Response(JSON.stringify({ status: "ok" }), {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
      ),
    );

    await apiRequest("/api/projects");

    const requestOptions = globalThis.fetch.mock.calls[0].arguments[1];
    assert.equal(requestOptions.headers.get("Authorization"), "Bearer deployer-token");
  });

  it("throws API errors with response metadata", async () => {
    globalThis.fetch = mock.fn(() =>
      Promise.resolve(
      new Response(JSON.stringify({ error: "Forbidden" }), {
        status: 403,
        headers: { "Content-Type": "application/json" },
      }),
      ),
    );

    await assert.rejects(apiRequest("/api/projects"), (error) => {
      assert.equal(error.message, "Forbidden");
      assert.equal(error.status, 403);
      return true;
    });
  });
});
