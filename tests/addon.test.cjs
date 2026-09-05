const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const { test } = require("node:test");

function setup(headless = true, token = "a".repeat(32)) {
  const files = new Map();
  const timers = [];
  let quits = 0;
  const context = vm.createContext({
    ExtensionAPI: class {},
    PathUtils: {
      profileDir: "/profile",
      join: (...parts) => parts.join("/"),
      filename: path => path.split("/").pop()
    },
    Services: {
      env: {
        get: key =>
          ({
            MOZ_HEADLESS: headless ? "1" : "",
            TBMAIL_STARTUP_TOKEN: token,
            TBMAIL_SAFETY_DEADLINE: String(Date.now() + 1800000)
          })[key] || ""
      },
      startup: { quit: () => quits++ },
      uuid: { generateUUID: () => "test-id" }
    },
    Ci: { nsIAppStartup: { eAttemptQuit: 1 } },
    Cu: {
      reportError: error => {
        throw error;
      }
    },
    ChromeUtils: {
      importESModule: () => ({
        MailServices: {},
        setTimeout: (callback, delay) => {
          timers.push({ callback, delay });
          if (delay <= 1000) Promise.resolve().then(callback);
          return timers.length;
        },
        clearTimeout: () => {},
        setInterval: () => 1,
        clearInterval: () => {}
      })
    },
    IOUtils: {
      readJSON: async path => {
        if (!files.has(path))
          throw Object.assign(new Error("missing"), { name: "NotFoundError" });
        return files.get(path);
      },
      getChildren: async () => [],
      remove: async () => {}
    }
  });
  vm.runInContext(
    fs.readFileSync(
      "thunderbird-addon/api/TbmailBridge/implementation.js",
      "utf8"
    ) + "\nglobalThis.TestBridge = Bridge;",
    context
  );
  const bridge = new context.TestBridge({ manifest: { version: "0.3.0" } });
  bridge.log = async () => {};
  bridge.writeHeartbeat = async () => {};
  return { bridge, files, timers, context, quits: () => quits };
}

test("GUI and unowned headless never acquire timers or accept shutdown", async () => {
  for (const [headless, token] of [
    [false, "a".repeat(32)],
    [true, ""]
  ]) {
    const { bridge, timers, quits } = setup(headless, token);
    bridge.resetIdleTimer();
    assert.equal(timers.length, 0);
    assert.equal(
      (await bridge.shutdown({ startupToken: token })).status,
      "invalid"
    );
    await bridge.poll();
    assert.equal(quits(), 0);
  }
});

test("managed deadline is fixed and shutdown requires matching token", async () => {
  const { bridge, timers } = setup();
  const deadline = bridge.safetyDeadline;
  bridge.resetIdleTimer();
  bridge.resetIdleTimer();
  assert.equal(timers.length, 1);
  assert.equal(bridge.safetyDeadline, deadline);
  assert.ok(timers[0].delay <= 1800000);
  assert.equal(
    (await bridge.shutdown({ startupToken: "wrong" })).status,
    "invalid"
  );
  assert.equal(
    (await bridge.shutdown({ startupToken: "a".repeat(32) })).status,
    "success"
  );
});

test("drain signal stops scheduling but waits for active completion", async () => {
  const { bridge, files, quits } = setup();
  bridge.active = { operation: "sync" };
  files.set("/profile/tbmail-ipc/drain.json", { startupToken: "wrong" });
  await bridge.poll();
  assert.equal(bridge.draining, false);
  files.set("/profile/tbmail-ipc/drain.json", {
    startupToken: bridge.startupToken
  });
  await bridge.poll();
  assert.equal(bridge.draining, true);
  assert.equal(quits(), 0);
  assert.equal(
    (await bridge.runFolder({}, "download", Date.now() + 1000)).timedOut,
    true
  );
  bridge.active = null;
  await bridge.poll();
  assert.equal(quits(), 1);
});

test("safety expiry drains active work rather than extending deadline", async () => {
  const { bridge, quits } = setup();
  bridge.active = { operation: "sync" };
  await bridge.onIdle();
  assert.equal(bridge.draining, true);
  assert.equal(quits(), 0);
  bridge.active = null;
  await bridge.poll();
  assert.equal(quits(), 1);
});

test("timed-out work settles before managed shutdown", async () => {
  const { bridge, quits } = setup();
  let complete;
  const settle = new Promise(resolve => {
    complete = resolve;
  });
  bridge.sync = async () => ({ status: "timeout", body: {}, settle });
  bridge.writeResponse = async () => {};
  bridge.active = { operation: "sync" };
  const pending = bridge.handleClaimed("request", {
    protocolVersion: 1,
    requestId: "test",
    operation: "sync",
    deadline: Date.now() + 1000
  });
  await Promise.resolve();
  await bridge.onIdle();
  assert.equal(quits(), 0);
  complete();
  await pending;
  assert.equal(quits(), 1);
  assert.equal(bridge.active, null);
});

test("partial sync advances freshness only for successful accounts", async () => {
  const { bridge } = setup();
  const updated = [];
  bridge.getAccount = key => ({ key });
  bridge.selectableFolders = account => [{ URI: account.key }];
  bridge.runFolder = async folder => ({ ok: folder.URI == "good" });
  bridge.updateLastSync = async key => updated.push(key);
  const result = await bridge.sync({
    serverKeys: ["bad", "good"],
    deadline: Date.now() + 10000
  });
  assert.equal(result.status, "partial");
  assert.deepEqual(updated, ["good"]);
});

test("draining sync does not schedule remaining folders or advance freshness", async () => {
  const { bridge } = setup();
  let started = 0;
  bridge.getAccount = () => ({});
  bridge.selectableFolders = () => [{ URI: "first" }, { URI: "second" }];
  bridge.waitForUrl = async () => {
    started++;
    bridge.draining = true;
    return { ok: true };
  };
  bridge.updateLastSync = async () =>
    assert.fail("incomplete sync advanced freshness");
  const result = await bridge.sync({
    serverKeys: ["one", "two"],
    deadline: Date.now() + 10000
  });
  await result.settle;
  assert.equal(started, 1);
  assert.equal(result.status, "timeout");
  assert.equal(result.body.accounts[1].status, "not_started");
});

test("identity probe can confirm a live token while sync remains active", async () => {
  const { bridge, context } = setup();
  const request = {
    protocolVersion: 1,
    requestId: "identity",
    operation: "identify",
    deadline: Date.now() + 10000
  };
  context.IOUtils.getChildren = async () => ["/request.json"];
  context.IOUtils.move = async () => {};
  context.IOUtils.readJSON = async path => {
    if (path.endsWith("drain.json"))
      throw Object.assign(new Error("missing"), { name: "NotFoundError" });
    return request;
  };
  let response;
  bridge.writeResponse = async (_request, status, body) => {
    response = { status, body };
  };
  bridge.active = { operation: "sync", requestId: "original" };
  await bridge.poll();
  assert.equal(response.status, "success");
  assert.equal(response.body.startupToken, bridge.startupToken);
  assert.equal(bridge.active.requestId, "original");
});
