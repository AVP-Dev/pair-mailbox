/* A minimal DOM for running the dashboard client outside a browser.
 *
 * Why it stands. Static asset checks catch a function-name typo, but never a
 * `TypeError` on the third render line: such a bug reaches the human as a page with
 * a blank `#app` and not a word about the error, because the page only shows what
 * rendered. Launching a real browser in the gate is banned, while this file is
 * exactly the DOM pieces the client uses, nothing more: `createElement`,
 * `getElementById`, `textContent`, `classList`,
 * `appendChild`, `removeChild`, `querySelector` for the refresh tag, `localStorage`.
 *
 * Deliberately MISSING here: HTML parsing, CSSOM, the event model, timers. The stub
 * never tries being a browser — it surfaces an error in code that would otherwise stay
 * silent till the first human page open.
 *
 * Usage: node scripts/board-dom-stub.js <labels.js> <board.js> <snapshot.json> <watch-seconds>
 * Prints the `#app` tree line by line plus three `--- ...` lines on theme, localStorage and the refresh tag.
 */
"use strict";

const fs = require("fs");

function makeNode(tag) {
  const node = {
    tagName: tag,
    className: "",
    children: [],
    attrs: {},
    _text: null,
    set textContent(value) { node._text = String(value); node.children = []; },
    get textContent() { return node._text; },
    appendChild(child) { node.children.push(child); return child; },
    removeChild(child) {
      node.children = node.children.filter(item => item !== child);
      return child;
    },
    setAttribute(name, value) { node.attrs[name] = value; },
    getAttribute(name) { return name in node.attrs ? node.attrs[name] : null; },
    addEventListener() {},
    get firstChild() { return node.children[0] || null; },
  };
  node.classList = {
    add(name) {
      const current = node.className.split(" ").filter(Boolean);
      if (!current.includes(name)) current.push(name);
      node.className = current.join(" ");
    },
    contains(name) { return node.className.split(" ").includes(name); },
  };
  return node;
}

const elements = {
  app: makeNode("main"),
  project: makeNode("h1"),
  stamp: makeNode("span"),
  "theme-toggle": makeNode("button"),
  "watch-toggle": makeNode("button"),
  snapshot: makeNode("script"),
};

const document = {
  body: makeNode("body"),
  head: makeNode("head"),
  documentElement: makeNode("html"),
  getElementById(id) { return elements[id] || null; },
  createElement: makeNode,
  querySelector(selector) {
    if (selector.indexOf('http-equiv="refresh"') === -1) return null;
    return document.head.children.find(
      node => node.attrs && node.attrs["http-equiv"] === "refresh") || null;
  },
};

const storage = {};
global.document = document;
global.window = global;
global.localStorage = {
  getItem: key => (key in storage ? storage[key] : null),
  setItem: (key, value) => { storage[key] = String(value); },
};

const [labelsPath, clientPath, snapshotPath, watchSeconds] = process.argv.slice(2);
if (!labelsPath || !clientPath || !snapshotPath) {
  console.error("paths needed: <labels.js> <board.js> <snapshot.json> [watch-seconds]");
  process.exit(2);
}
elements.snapshot._text = fs.readFileSync(snapshotPath, "utf8");
document.body.setAttribute("data-watch-seconds", watchSeconds || "5");

eval(fs.readFileSync(labelsPath, "utf8") + "\n" + fs.readFileSync(clientPath, "utf8"));

function dump(node, depth) {
  const text = node._text === null ? ""
    : ' "' + node._text.replace(/\n/g, " ").slice(0, 70) + '"';
  console.log("  ".repeat(depth) + "<" + node.tagName +
              (node.className ? " class=" + node.className : "") + text + ">");
  node.children.forEach(child => dump(child, depth + 1));
}

dump(elements.app, 0);
console.log("--- theme: " + document.documentElement.getAttribute("data-theme"));
console.log("--- stored: " + JSON.stringify(storage));
console.log("--- refresh meta: " + JSON.stringify(
  document.head.children
    .filter(node => node.attrs && node.attrs["http-equiv"] === "refresh")
    .map(node => node.attrs.content)));