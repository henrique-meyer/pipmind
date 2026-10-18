import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../web/static/analytics.js", import.meta.url), "utf8");
function browser(id, referrer = "https://referrer.example/path?private=value#fragment") {
  const scripts = [];
  const context = vm.createContext({
    URL,
    window: {location: {href: "https://site.example/?private=value#fragment"}},
    document: {
      currentScript: {dataset: {measurementId: id}},
      referrer,
      createElement: () => ({}),
      head: {appendChild: element => scripts.push(element)},
    },
  });
  return {context, scripts};
}

test("initialization omits URL parameters and advertising signals, and runs once", () => {
  const {context, scripts} = browser("G-TEST123456");
  vm.runInContext(source, context);
  vm.runInContext(source, context);
  assert.equal(scripts.length, 1);
  assert.equal(scripts[0].src, "https://www.googletagmanager.com/gtag/js?id=G-TEST123456");
  assert.equal(scripts[0].async, true);
  const commands = context.window.dataLayer.map(args => Array.from(args));
  assert.equal(commands.length, 2);
  assert.equal(commands[0][0], "js");
  assert.deepEqual(JSON.parse(JSON.stringify(commands[1])), ["config", "G-TEST123456", {
    page_location: "https://site.example/",
    page_referrer: "https://referrer.example/path",
    allow_google_signals: false,
    allow_ad_personalization_signals: false,
  }]);
});

test("missing or invalid IDs never load Google or initialize tracking", () => {
  for (const id of [undefined, "", "GTM-ABC123", 'G-ABC123\"><script>']) {
    const {context, scripts} = browser(id);
    vm.runInContext(source, context);
    assert.equal(scripts.length, 0);
    assert.equal(context.window.dataLayer, undefined);
  }
});

test("empty and non-web referrers are not forwarded", () => {
  for (const referrer of ["", "not a URL", "file:///private/path", "javascript:private"]) {
    const {context} = browser("G-TEST123456", referrer);
    vm.runInContext(source, context);
    assert.equal(context.window.dataLayer[1][2].page_referrer, "");
  }
});
