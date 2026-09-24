// Regression test: a dismissed global log must stay dismissed for the
// current job, and a new job must clear the dismissal.
//
// Runs headlessly: kubby/ui/app.js is an IIFE that only touches the DOM
// from init(), so we load it in a vm sandbox with stubbed document/window
// (readyState "loading" keeps init() from running), then drive the real
// object directly. `node tests/js/log_dismissal.test.js` exits non-zero
// on any failed check; tests/test_ui_log_dismissal.py wraps it for pytest.
"use strict";

const fs = require("fs");
const path = require("path");
const vm = require("vm");

const APP = path.resolve(__dirname, "..", "..", "kubby", "ui", "app.js");

let failures = 0;
function check(label, actual, expected) {
  const ok = JSON.stringify(actual) === JSON.stringify(expected);
  if (ok) {
    console.log(`ok   ${label}`);
  } else {
    failures += 1;
    console.log(
      `FAIL ${label}\n  expected: ${JSON.stringify(expected)}\n  actual:   ${JSON.stringify(actual)}`,
    );
  }
}

function makeEl(tag = "div") {
  return {
    tag,
    hidden: false,
    disabled: false,
    textContent: "",
    innerHTML: "",
    value: "",
    className: "",
    dataset: {},
    style: {},
    children: [],
    listeners: {},
    scrollTop: 0,
    scrollHeight: 0,
    classList: {
      _set: new Set(),
      add(c) { this._set.add(c); },
      remove(c) { this._set.delete(c); },
      toggle(c, on) { on ? this._set.add(c) : this._set.delete(c); },
      contains(c) { return this._set.has(c); },
    },
    addEventListener(type, fn) { this.listeners[type] = fn; },
    appendChild(child) { this.children.push(child); return child; },
    append(...kids) { this.children.push(...kids); },
    remove() {},
    closest() { return null; },
    querySelector() { return makeEl(); },
    querySelectorAll() { return []; },
    click() { if (this.listeners.click) this.listeners.click({ target: this }); },
  };
}

const elements = new Map();
const bodyClasses = new Set();
const sandbox = {
  console,
  setTimeout,
  clearTimeout,
  window: { addEventListener() {}, pywebview: undefined },
  document: {
    readyState: "loading", // keep the IIFE from auto-running init()
    addEventListener() {},
    body: {
      classList: {
        add(c) { bodyClasses.add(c); },
        remove(c) { bodyClasses.delete(c); },
        contains(c) { return bodyClasses.has(c); },
      },
    },
    getElementById(id) {
      if (!elements.has(id)) elements.set(id, makeEl());
      return elements.get(id);
    },
    createElement(tag) { return makeEl(tag); },
  },
};
sandbox.global = sandbox;
vm.createContext(sandbox);
vm.runInContext(fs.readFileSync(APP, "utf8"), sandbox, { filename: APP });

const kubby = sandbox.window.kubby;
kubby.cache();
kubby.bindEvents();

const panel = () => kubby.els.globalLog.hidden === false;
const bodyOpen = () => bodyClasses.has("has-global-log");
// Pending API promise: runs only the synchronous prologue of an install
// (job key, dismissal reset, panel show), which is what we're asserting.
const never = () => new Promise(() => {});
const stubApi = (name) => {
  sandbox.window.pywebview = { api: { [name]: never } };
};

// --- minikube job: dismiss mid-job, later renders must not reopen ---
kubby.state.cluster = { running: true, nodes: [], namespaces: [] };
kubby._beginJobUi("start");
check("job start opens the panel", panel(), true);
check("job start leaves dismissal clear", kubby.state.globalLogDismissed, false);

kubby.els.globalLogClose.click(); // user clicks the close button
check("close button hides the panel", panel(), false);
check("close button records dismissal", kubby.state.globalLogDismissed, true);
check("close button drops body class", bodyOpen(), false);

kubby._syncGlobalLog(); // what Re-check / settings-save / renderCluster run
check("later render does NOT reopen (busy)", panel(), false);

kubby.state.minikubeJobRunning = false;
kubby.state.globalLogPinned = true; // job failed → pin diagnostics
kubby._syncGlobalLog();
check("later render does NOT reopen (post-failure pin)", panel(), false);

// --- a new job resets dismissal and shows its own log ---
kubby.state.globalLogPinned = false;
kubby._beginJobUi("stop");
check("new job reopens the panel", panel(), true);
check("new job resets dismissal", kubby.state.globalLogDismissed, false);

// --- undismissed behaviour is unchanged ---
kubby.state.minikubeJobRunning = false;
kubby.state.globalLogDismissed = false;
kubby.els.globalLogClose.click();
kubby.state.globalLogDismissed = false;
kubby._syncGlobalLog();
check("idle + not dismissed stays hidden", panel(), false);
kubby.state.globalLogPinned = true;
kubby._syncGlobalLog();
check("idle + pinned (no dismissal) still shows", panel(), true);
kubby.state.globalLogPinned = false;

// --- install (single tool): stale dismissal must not leak into a new job ---
kubby.state.tools = [
  { key: "kubectl", label: "kubectl", description: "d", installed: false },
];
kubby.state.globalLogDismissed = true;
kubby.state.installJobKey = null;
kubby._syncGlobalLog();
check("stale dismissal keeps panel closed", panel(), false);

stubApi("install_tool");
kubby._installTool("kubectl").catch(() => {});
check("install job clears stale dismissal", kubby.state.globalLogDismissed, false);
check("install job opens the panel", panel(), true);
kubby.els.globalLogClose.click();
check("dismissing during install hides it", panel(), false);
kubby._syncGlobalLog();
check("render during install does NOT reopen", panel(), false);

// --- install-all: same reset + same mid-job dismissal behaviour ---
kubby.state.installJobKey = null;
kubby.state.minikubeJobRunning = false;
kubby.state.globalLogDismissed = true;
stubApi("install_all");
kubby._installAll().catch(() => {});
check("install-all clears stale dismissal", kubby.state.globalLogDismissed, false);
check("install-all opens the panel", panel(), true);
kubby.els.globalLogClose.click();
kubby._syncGlobalLog();
check("render during install-all does NOT reopen", panel(), false);

console.log(failures === 0 ? "\nALL CHECKS PASSED" : `\n${failures} CHECK(S) FAILED`);
process.exit(failures === 0 ? 0 : 1);
