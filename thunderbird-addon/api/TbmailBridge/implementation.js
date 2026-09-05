/* global ExtensionAPI, IOUtils, PathUtils, Services, Ci, Cu, ChromeUtils, Components */

"use strict";

const PROTOCOL_VERSION = 1;
const POLL_INTERVAL_MS = 300;
const HEARTBEAT_INTERVAL_MS = 2000;
const SAFETY_SHUTDOWN_MS = 30 * 60 * 1000;
const STALE_AGE_MS = 24 * 60 * 60 * 1000;
const LOG_LIMIT_BYTES = 5 * 1024 * 1024;
const LOG_BACKUPS = 3;

const { MailServices } = ChromeUtils.importESModule(
  "resource:///modules/MailServices.sys.mjs"
);
const { setTimeout, clearTimeout, setInterval, clearInterval } =
  ChromeUtils.importESModule("resource://gre/modules/Timer.sys.mjs");

function errorDetails(error) {
  return {
    message: String(error?.message || error),
    result:
      typeof error?.result == "number"
        ? `0x${(error.result >>> 0).toString(16)}`
        : undefined,
    stack: error?.stack
  };
}

function normalizeMessageId(value) {
  return String(value || "")
    .trim()
    .replace(/^<|>$/g, "");
}

class Bridge {
  constructor(extension) {
    this.extension = extension;
    this.root = PathUtils.join(PathUtils.profileDir, "tbmail-ipc");
    this.requests = PathUtils.join(this.root, "requests");
    this.responses = PathUtils.join(this.root, "responses");
    this.logPath = PathUtils.join(this.root, "bridge.log");
    this.heartbeatPath = PathUtils.join(this.root, "heartbeat.json");
    this.lastSyncPath = PathUtils.join(this.root, "last-sync.json");
    this.profileIdPath = PathUtils.join(this.root, "profile-id");
    this.active = null;
    this.stopped = false;
    this.pollTimer = null;
    this.heartbeatTimer = null;
    this.idleTimer = null;
    this.logging = Promise.resolve();
    this.startupToken = Services.env.get("TBMAIL_STARTUP_TOKEN");
    this.managed =
      this.isHeadless() && /^[a-f0-9]{32}$/.test(this.startupToken);
    const requestedDeadline = Number(
      Services.env.get("TBMAIL_SAFETY_DEADLINE")
    );
    this.safetyDeadline = this.managed
      ? Math.min(
          Date.now() + SAFETY_SHUTDOWN_MS,
          requestedDeadline || Date.now()
        )
      : null;
    this.draining = false;
    this.polling = false;
    this.lastQuitAttempt = 0;
  }

  async start() {
    await IOUtils.makeDirectory(this.requests, { permissions: 0o700 });
    await IOUtils.makeDirectory(this.responses, { permissions: 0o700 });
    await IOUtils.setPermissions(this.root, 0o700, false);
    await IOUtils.setPermissions(this.requests, 0o700, false);
    await IOUtils.setPermissions(this.responses, 0o700, false);
    this.profileId = await this.getProfileId();
    await this.cleanupStaleFiles();
    await this.log("startup", {
      thunderbirdVersion: Services.appinfo.version,
      headless: this.isHeadless()
    });
    await this.writeHeartbeat();
    this.pollTimer = setInterval(
      () =>
        this.poll().catch(error => {
          this.log("poll-error", { error: errorDetails(error) });
        }),
      POLL_INTERVAL_MS
    );
    this.heartbeatTimer = setInterval(
      () =>
        this.writeHeartbeat().catch(error => {
          this.log("heartbeat-error", { error: errorDetails(error) });
        }),
      HEARTBEAT_INTERVAL_MS
    );
    this.resetIdleTimer();
  }

  async stop(reason) {
    if (this.stopped) {
      return;
    }
    this.stopped = true;
    clearInterval(this.pollTimer);
    clearInterval(this.heartbeatTimer);
    clearTimeout(this.idleTimer);
    await this.log("shutdown", { reason });
  }

  isHeadless() {
    return Services.env.get("MOZ_HEADLESS") == "1";
  }

  resetIdleTimer() {
    if (!this.managed || this.idleTimer) {
      return;
    }
    this.idleTimer = setTimeout(
      () => this.onIdle(),
      Math.max(0, this.safetyDeadline - Date.now())
    );
  }

  async onIdle() {
    this.draining = true;
    if (this.active) {
      return;
    }
    if (Date.now() - this.lastQuitAttempt < 1000) return;
    this.lastQuitAttempt = Date.now();
    await this.log("managed-quit-requested");
    // Keep polling until application shutdown, in case a quit observer cancels.
    Services.startup.quit(Ci.nsIAppStartup.eAttemptQuit);
  }

  async getProfileId() {
    try {
      return (await IOUtils.readUTF8(this.profileIdPath)).trim();
    } catch (error) {
      if (error.name != "NotFoundError") {
        throw error;
      }
    }
    const id = Services.uuid.generateUUID().toString().replace(/[{}]/g, "");
    await this.atomicWriteText(this.profileIdPath, `${id}\n`);
    return id;
  }

  async atomicWriteJSON(path, value) {
    const tmpPath = `${path}.tmp-${Services.uuid.generateUUID()}`;
    await IOUtils.writeJSON(path, value, { tmpPath, flush: true });
    await IOUtils.setPermissions(path, 0o600, false);
  }

  async atomicWriteText(path, value) {
    const tmpPath = `${path}.tmp-${Services.uuid.generateUUID()}`;
    await IOUtils.writeUTF8(path, value, { tmpPath, flush: true });
    await IOUtils.setPermissions(path, 0o600, false);
  }

  async writeHeartbeat() {
    await this.atomicWriteJSON(this.heartbeatPath, {
      protocolVersion: PROTOCOL_VERSION,
      addonVersion: this.extension.manifest.version,
      thunderbirdVersion: Services.appinfo.version,
      profileId: this.profileId,
      timestamp: new Date().toISOString(),
      active: Boolean(this.active),
      operation: this.active?.operation || null,
      requestId: this.active?.requestId || null,
      headless: this.isHeadless(),
      startupToken: this.managed ? this.startupToken : null,
      safetyDeadline: this.safetyDeadline,
      draining: this.draining
    });
  }

  log(event, fields = {}) {
    const entry = `${JSON.stringify({
      timestamp: new Date().toISOString(),
      event,
      ...fields
    })}\n`;
    this.logging = this.logging
      .then(async () => {
        let size = 0;
        try {
          size = (await IOUtils.stat(this.logPath)).size;
        } catch (error) {
          if (error.name != "NotFoundError") {
            throw error;
          }
        }
        // JSON log entries are overwhelmingly ASCII; four bytes per JS code
        // unit is a safe UTF-8 upper bound in this privileged scope.
        if (size + entry.length * 4 > LOG_LIMIT_BYTES) {
          await IOUtils.remove(`${this.logPath}.${LOG_BACKUPS}`, {
            ignoreAbsent: true
          });
          for (let index = LOG_BACKUPS - 1; index >= 1; index--) {
            const source = `${this.logPath}.${index}`;
            if (await IOUtils.exists(source)) {
              await IOUtils.move(source, `${this.logPath}.${index + 1}`);
            }
          }
          if (await IOUtils.exists(this.logPath)) {
            await IOUtils.move(this.logPath, `${this.logPath}.1`);
          }
        }
        await IOUtils.writeUTF8(this.logPath, entry, {
          mode: "appendOrCreate"
        });
        await IOUtils.setPermissions(this.logPath, 0o600, false);
      })
      .catch(error => Cu.reportError(error));
    return this.logging;
  }

  async cleanupStaleFiles() {
    const cutoff = Date.now() - STALE_AGE_MS;
    for (const directory of [this.requests, this.responses]) {
      for (const path of await IOUtils.getChildren(directory)) {
        try {
          const stat = await IOUtils.stat(path);
          if (stat.type == "regular" && stat.lastModified < cutoff) {
            await IOUtils.remove(path);
            await this.log("stale-file-removed", {
              queue: PathUtils.filename(directory),
              file: PathUtils.filename(path)
            });
          }
        } catch (error) {
          if (error.name != "NotFoundError") {
            throw error;
          }
        }
      }
    }
  }

  async poll() {
    if (this.stopped || this.polling) {
      return;
    }
    this.polling = true;
    try {
      if (this.managed) {
        try {
          const drain = await IOUtils.readJSON(
            PathUtils.join(this.root, "drain.json")
          );
          if (drain.startupToken == this.startupToken) {
            this.draining = true;
          }
        } catch (error) {
          if (error.name != "NotFoundError") {
            await this.log("drain-read-error", { error: errorDetails(error) });
          }
        }
        if (Date.now() >= this.safetyDeadline) this.draining = true;
      }
      if (this.draining) {
        if (!this.active) await this.onIdle();
        return;
      }
      const paths = (await IOUtils.getChildren(this.requests))
        .filter(path => PathUtils.filename(path).endsWith(".json"))
        .sort();
      for (const path of paths) {
        if (this.draining || this.stopped) break;
        const claimed = `${path}.processing-${Services.uuid.generateUUID()}`;
        try {
          await IOUtils.move(path, claimed, { noOverwrite: true });
        } catch (error) {
          if (
            ["NotFoundError", "NoModificationAllowedError"].includes(error.name)
          ) {
            continue;
          }
          throw error;
        }
        let request;
        try {
          request = await IOUtils.readJSON(claimed);
        } catch (error) {
          await this.finishInvalid(claimed, null, "Request is not valid JSON");
          continue;
        }
        if (request.operation == "identify" && !this.validateRequest(request)) {
          await this.writeResponse(request, "success", {
            startupToken: this.managed ? this.startupToken : null,
            headless: this.isHeadless()
          });
          await IOUtils.remove(claimed);
          continue;
        }
        if (this.active) {
          await this.writeResponse(request, "busy", {
            error: "Another operation is active",
            activeRequestId: this.active.requestId
          });
          await IOUtils.remove(claimed);
          continue;
        }
        this.active = {
          requestId: request?.requestId,
          operation: request?.operation
        };
        await this.writeHeartbeat();
        this.handleClaimed(claimed, request).catch(error => {
          Cu.reportError(error);
        });
      }
    } finally {
      this.polling = false;
    }
  }

  validateRequest(request) {
    if (!request || typeof request != "object") {
      return "Request must be a JSON object";
    }
    if (request.protocolVersion != PROTOCOL_VERSION) {
      return "Unsupported protocol version";
    }
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(request.requestId || "")) {
      return "Invalid request ID";
    }
    if (
      !["sync", "mark-read", "mark-unread", "shutdown", "identify"].includes(
        request.operation
      )
    ) {
      return "Invalid operation";
    }
    if (!Number.isFinite(request.deadline) || request.deadline <= 0) {
      return "Invalid absolute deadline";
    }
    return null;
  }

  async handleClaimed(path, request) {
    const requestId = request?.requestId;
    const operation = request?.operation;
    let settle = Promise.resolve();
    try {
      const invalid = this.validateRequest(request);
      if (invalid) {
        await this.writeResponse(request, "invalid", { error: invalid });
        return;
      }
      await this.log("request-start", { requestId, operation });
      if (Date.now() >= request.deadline) {
        await this.writeResponse(request, "timeout", {
          error: "Request deadline expired before processing"
        });
        return;
      }
      let result;
      if (operation == "sync") {
        result = await this.sync(request);
      } else if (operation == "shutdown") {
        result = await this.shutdown(request);
      } else {
        result = await this.markRead(request, operation == "mark-read");
      }
      settle = result.settle || settle;
      await this.writeResponse(request, result.status, result.body);
      await this.log("request-result", {
        requestId,
        operation,
        status: result.status
      });
      await settle;
      if (result.quit) {
        this.draining = true;
      }
    } catch (error) {
      await this.writeResponse(request, "error", {
        error: errorDetails(error)
      });
      await this.log("request-error", {
        requestId,
        operation,
        error: errorDetails(error)
      });
    } finally {
      await IOUtils.remove(path, { ignoreAbsent: true });
      await settle.catch(() => {});
      this.active = null;
      await this.writeHeartbeat().catch(() => {});
      if (this.draining && !this.stopped) await this.onIdle();
    }
  }

  async finishInvalid(path, request, message) {
    await this.writeResponse(request, "invalid", { error: message });
    await IOUtils.remove(path, { ignoreAbsent: true });
  }

  async writeResponse(request, status, body) {
    const requestId = request?.requestId;
    if (!/^[A-Za-z0-9._-]{1,128}$/.test(requestId || "")) {
      await this.log("invalid-request-without-response", { status, body });
      return;
    }
    await this.atomicWriteJSON(
      PathUtils.join(this.responses, `${requestId}.json`),
      {
        protocolVersion: PROTOCOL_VERSION,
        requestId,
        status,
        timestamp: new Date().toISOString(),
        ...body
      }
    );
  }

  async shutdown(request) {
    if (!this.managed || request.startupToken != this.startupToken) {
      return {
        status: "invalid",
        body: {
          error:
            "Refusing to close Thunderbird without managed headless ownership"
        }
      };
    }
    return { status: "success", body: { shutdownRequested: true }, quit: true };
  }

  getAccount(serverKey) {
    const server = MailServices.accounts.getIncomingServer(serverKey);
    const account = MailServices.accounts.findAccountForServer(server);
    if (!account || server.type != "imap") {
      throw new Error(`IMAP account not found for server key ${serverKey}`);
    }
    return account;
  }

  selectableFolders(account, offlineOnly = false) {
    return account.incomingServer.rootFolder.descendants.filter(folder => {
      return (
        !folder.isServer &&
        !folder.noSelect &&
        !(folder.flags & Ci.nsMsgFolderFlags.Virtual) &&
        (!offlineOnly || folder.supportsOffline)
      );
    });
  }

  waitForUrl(folder, phase, start, deadline) {
    let resolveCompletion;
    const completion = new Promise(resolve => {
      resolveCompletion = resolve;
    });
    let timeout;
    const operation = new Promise(resolve => {
      let done = false;
      let completed = false;
      let folderListener = null;
      const complete = value => {
        if (completed) {
          return;
        }
        completed = true;
        if (folderListener) {
          folder.RemoveFolderListener(folderListener);
        }
        resolveCompletion(value);
        finish({ ...value, completion });
      };
      const finish = value => {
        if (!done) {
          done = true;
          clearTimeout(timeout);
          resolve(value);
        }
      };
      const listener = {
        QueryInterface: ChromeUtils.generateQI(["nsIUrlListener"]),
        OnStartRunningUrl() {},
        OnStopRunningUrl(_url, status) {
          complete({ ok: Components.isSuccessCode(status), status });
        }
      };
      if (phase != "download") {
        folderListener = {
          QueryInterface: ChromeUtils.generateQI(["nsIFolderListener"]),
          onFolderAdded() {},
          onMessageAdded() {},
          onFolderRemoved() {},
          onMessageRemoved() {},
          onFolderPropertyChanged() {},
          onFolderIntPropertyChanged() {},
          onFolderBoolPropertyChanged() {},
          onFolderPropertyFlagChanged() {},
          onFolderEvent(changedFolder, event) {
            if (changedFolder == folder && event == "FolderLoaded") {
              complete({ ok: true, completedBy: "folder-event" });
            }
          }
        };
        folder.AddFolderListener(folderListener);
      }
      try {
        start(listener);
      } catch (error) {
        complete({ ok: false, error });
        return;
      }
      timeout = setTimeout(
        () => {
          finish({ ok: false, timedOut: true, completion });
        },
        Math.max(0, deadline - Date.now())
      );
    });
    return operation;
  }

  async runFolder(folder, phase, deadline) {
    if (
      this.draining ||
      Date.now() >= deadline ||
      (this.managed && Date.now() >= this.safetyDeadline)
    ) {
      return { ok: false, timedOut: true, completion: Promise.resolve() };
    }
    await this.log("folder-start", { phase, folder: folder.URI });
    const result = await this.waitForUrl(
      folder,
      phase,
      listener => {
        if (phase == "download") {
          folder.downloadAllForOffline(listener, null);
        } else {
          folder
            .QueryInterface(Ci.nsIMsgImapMailFolder)
            .updateFolderWithListener(null, listener);
        }
      },
      deadline
    );
    await this.log("folder-result", {
      phase,
      folder: folder.URI,
      status: result.timedOut ? "timeout" : result.ok ? "success" : "error",
      result: result.status,
      error: result.error ? errorDetails(result.error) : undefined
    });
    return result;
  }

  async sync(request) {
    if (!Array.isArray(request.serverKeys) || !request.serverKeys.length) {
      return {
        status: "invalid",
        body: { error: "serverKeys must be non-empty" }
      };
    }
    const accounts = [];
    const pending = [];
    let timedOut = false;
    for (const serverKey of request.serverKeys) {
      if (this.draining || timedOut || Date.now() >= request.deadline) {
        timedOut = true;
        const accountResult = {
          serverKey,
          status: "not_started",
          folders: [],
          incompletePhases: ["refresh-1", "refresh-2", "download"]
        };
        try {
          const account = this.getAccount(serverKey);
          for (const phase of accountResult.incompletePhases) {
            accountResult.folders.push(
              ...this.selectableFolders(account, phase == "download").map(
                folder => ({
                  uri: folder.URI,
                  phase,
                  status: "not_started"
                })
              )
            );
          }
        } catch (error) {
          accountResult.error = errorDetails(error);
        }
        accounts.push(accountResult);
        continue;
      }
      const accountResult = { serverKey, status: "success", folders: [] };
      accounts.push(accountResult);
      try {
        const account = this.getAccount(serverKey);
        for (const phase of ["refresh-1", "refresh-2", "download"]) {
          if (phase == "refresh-2") {
            const remaining = request.deadline - Date.now();
            if (remaining <= 0) {
              timedOut = true;
              break;
            }
            await new Promise(resolve =>
              setTimeout(resolve, Math.min(1000, remaining))
            );
          }
          const folders = this.selectableFolders(account, phase == "download");
          for (const [folderIndex, folder] of folders.entries()) {
            const result = await this.runFolder(
              folder,
              phase,
              request.deadline
            );
            accountResult.folders.push({
              uri: folder.URI,
              phase,
              status: result.timedOut
                ? "timeout"
                : result.ok
                  ? "success"
                  : "error",
              result:
                typeof result.status == "number"
                  ? `0x${(result.status >>> 0).toString(16)}`
                  : undefined,
              error: result.error ? errorDetails(result.error) : undefined
            });
            if (result.timedOut) {
              pending.push(result.completion);
              accountResult.folders.push(
                ...folders.slice(folderIndex + 1).map(incomplete => ({
                  uri: incomplete.URI,
                  phase,
                  status: "incomplete"
                }))
              );
              accountResult.incompletePhases = [
                ...["refresh-1", "refresh-2", "download"].slice(
                  ["refresh-1", "refresh-2", "download"].indexOf(phase) + 1
                )
              ];
              for (const incompletePhase of accountResult.incompletePhases) {
                accountResult.folders.push(
                  ...this.selectableFolders(
                    account,
                    incompletePhase == "download"
                  ).map(incomplete => ({
                    uri: incomplete.URI,
                    phase: incompletePhase,
                    status: "incomplete"
                  }))
                );
              }
              timedOut = true;
              break;
            }
            if (!result.ok) {
              accountResult.status = "error";
            }
          }
          if (timedOut) {
            break;
          }
        }
        if (timedOut) {
          accountResult.status = "timeout";
        } else if (accountResult.status == "success") {
          await this.updateLastSync(serverKey);
        }
      } catch (error) {
        accountResult.status = "error";
        accountResult.error = errorDetails(error);
      }
    }
    const failed = accounts.some(account => account.status != "success");
    return {
      status: timedOut ? "timeout" : failed ? "partial" : "success",
      body: { accounts },
      settle: Promise.allSettled(pending)
    };
  }

  async updateLastSync(serverKey) {
    let state = { protocolVersion: PROTOCOL_VERSION, accounts: {} };
    try {
      state = await IOUtils.readJSON(this.lastSyncPath);
      if (!state.accounts || typeof state.accounts != "object") {
        state.accounts = {};
      }
    } catch (error) {
      if (error.name != "NotFoundError") {
        await this.log("last-sync-reset", { error: errorDetails(error) });
      }
    }
    state.protocolVersion = PROTOCOL_VERSION;
    state.accounts[serverKey] = new Date().toISOString();
    await this.atomicWriteJSON(this.lastSyncPath, state);
  }

  async markRead(request, desiredRead) {
    for (const field of ["serverKey", "folderPath", "messageId"]) {
      if (typeof request[field] != "string" || !request[field]) {
        return { status: "invalid", body: { error: `${field} is required` } };
      }
    }
    const account = this.getAccount(request.serverKey);
    const parts = request.folderPath.split("/");
    if (parts.some(part => !part || part == "." || part == "..")) {
      return { status: "invalid", body: { error: "Invalid folderPath" } };
    }
    const expectedPath = PathUtils.join(PathUtils.profileDir, ...parts);
    const matches = this.selectableFolders(account).filter(
      candidate => candidate.filePath.path == expectedPath
    );
    if (matches.length != 1) {
      return {
        status: "error",
        body: { error: "Folder not found in account" }
      };
    }
    const [folder] = matches;
    folder.msgDatabase;
    const expectedId = normalizeMessageId(request.messageId);
    const token = String(request.storeToken ?? request.mboxOffset ?? "");
    let tokenMatch = null;
    const idMatches = [];
    for (const header of folder.messages) {
      const messageId = normalizeMessageId(header.messageId);
      if (messageId == expectedId) {
        idMatches.push(header);
      }
      if (token && header.storeToken == token) {
        tokenMatch = header;
      }
    }
    let header;
    let matchedBy;
    if (tokenMatch && normalizeMessageId(tokenMatch.messageId) == expectedId) {
      header = tokenMatch;
      matchedBy = "storeToken";
    } else if (idMatches.length == 1) {
      [header] = idMatches;
      matchedBy = "messageId";
    } else {
      return {
        status: "error",
        body: {
          error:
            idMatches.length == 0
              ? "Message not found"
              : "Message-ID is ambiguous in the folder",
          messageIdMatches: idMatches.length
        }
      };
    }
    if (Date.now() >= request.deadline) {
      return {
        status: "timeout",
        body: { error: "Request deadline expired before applying the change" }
      };
    }
    folder.markMessagesRead([header], desiredRead);
    return {
      status: "success",
      body: {
        read: desiredRead,
        matchedBy,
        folderUri: folder.URI,
        messageKey: header.messageKey
      }
    };
  }
}

let bridge;

this.TbmailBridge = class extends ExtensionAPI {
  onStartup() {
    bridge = new Bridge(this.extension);
    bridge.start().catch(error => Cu.reportError(error));
  }

  onShutdown(isAppShutdown) {
    const current = bridge;
    bridge = null;
    if (current) {
      if (!isAppShutdown) {
        Services.obs.notifyObservers(null, "startupcache-invalidate");
      }
      return current
        .stop(isAppShutdown ? "application-shutdown" : "add-on-shutdown")
        .catch(error => Cu.reportError(error));
    }
    return undefined;
  }

  getAPI() {
    return { TbmailBridge: {} };
  }
};
