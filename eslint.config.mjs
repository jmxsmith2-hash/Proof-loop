// ESLint for the window's page scripts (`npm run lint`).
//
// What is linted
//   * src/proofloop/app/static/js/*.js: the Loop files & Printers page's scripts. There is no
//     bundler: loop.html loads them with plain <script src> tags, in order, and they share one
//     global scope (a function or const declared in core.js is used by make.js, monitor.js ...).
//   * the inline <script> blocks of shell.html (the window), loop.html and the Check page's Jinja
//     template check_app.html.j2, through the small `inlineScripts` processor below (no
//     eslint-plugin-html: it can't read the template's `{{ prefix|tojson }}`).
//
// Shared names and `no-undef`
//   A file can use any top-level name of the other files, so `no-undef` alone would either flag all
//   of them or (with a blanket "ignore") miss typos. Instead the globals of each file are *generated*
//   here from the code itself: for every js file, the top-level function / class / const / let / var
//   names of all the OTHER js files are declared as globals. Nothing to keep up to date by hand: add
//   a function anywhere and every other file knows it. A misspelt name is still `no-undef`, and a name
//   declared in two files is `no-redeclare` (tests/app/test_page_scripts.py checks the same at runtime).
//   `const`, `function` and `class` names are read-only (assigning to one is `no-global-assign`).
//
// Rules left out on purpose
//   * `no-implicit-globals`: it forbids exactly what this page is built on (top-level functions in
//     a classic script), so it would flag every function.
//   * `no-unused-vars` is set to `vars: "local"`: a top-level function is "unused" in its own file
//     when only another file (or loop.html) calls it, so only locals and parameters are checked.
import { readdirSync, readFileSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import js from "@eslint/js";
import * as espree from "espree";
import globals from "globals";

const root = path.dirname(fileURLToPath(import.meta.url));
const JS = path.join(root, "src", "proofloop", "app", "static", "js");
const files = readdirSync(JS).filter((f) => f.endsWith(".js")).sort();

/** The top-level names a script declares: {name: "readonly" | "writable"}. */
function topLevelNames(file) {
  const ast = espree.parse(readFileSync(path.join(JS, file), "utf8"), { ecmaVersion: "latest", sourceType: "script" });
  const names = {};
  const patternNames = (p, out = []) => {
    if (!p) return out;
    if (p.type === "Identifier") out.push(p.name);
    else if (p.type === "ObjectPattern") p.properties.forEach((q) => patternNames(q.value ?? q.argument, out));
    else if (p.type === "ArrayPattern") p.elements.forEach((q) => patternNames(q, out));
    else if (p.type === "AssignmentPattern") patternNames(p.left, out);
    else if (p.type === "RestElement") patternNames(p.argument, out);
    return out;
  };
  for (const node of ast.body) {
    if (node.type === "FunctionDeclaration" || node.type === "ClassDeclaration") names[node.id.name] = "readonly";
    else if (node.type === "VariableDeclaration")
      for (const d of node.declarations)
        for (const n of patternNames(d.id)) names[n] = node.kind === "const" ? "readonly" : "writable";
  }
  return names;
}

const declared = Object.fromEntries(files.map((f) => [f, topLevelNames(f)]));

/** One config block per js file: the other files' top-level names are its globals. */
const sharedGlobals = files.map((file) => ({
  files: [`src/proofloop/app/static/js/${file}`],
  languageOptions: {
    globals: Object.assign({}, ...files.filter((f) => f !== file).map((f) => declared[f])),
  },
}));

/**
 * Lints the inline <script> blocks of an HTML file or Jinja template. Each block is padded with
 * blank lines (and spaces) so reported line and column numbers are the ones in the original file;
 * `{{ ... }}`, `{% ... %}` and `{# ... #}` become `null` / nothing. Blocks with a `src` or a
 * non-JavaScript `type` are skipped. No autofix (it could not be put back into the page).
 */
const inlineScripts = {
  meta: { name: "inline-scripts" },
  supportsAutofix: false,
  preprocess(text) {
    const blocks = [];
    const blank = (s) => s.replace(/[^\n]/g, "");
    const re = /<script\b([^>]*)>([\s\S]*?)<\/script>/gi;
    for (let m; (m = re.exec(text)); ) {
      const attrs = m[1];
      const type = /\btype\s*=\s*["']?([^"'\s>]+)/i.exec(attrs)?.[1];
      if (/\bsrc\s*=/i.test(attrs) || (type && !/javascript|module/i.test(type))) continue;
      const start = m.index + m[0].length - m[2].length - "</script>".length;
      const before = text.slice(0, start);
      const line = before.split("\n").length - 1;
      const col = start - (before.lastIndexOf("\n") + 1);
      const body = m[2]
        .replace(/\{\{[\s\S]*?\}\}/g, (t) => "null" + blank(t))
        .replace(/\{[%#][\s\S]*?[%#]\}/g, blank);
      blocks.push({ text: "\n".repeat(line) + " ".repeat(col) + body, filename: `inline-${blocks.length + 1}.js` });
    }
    return blocks;
  },
  postprocess: (messages) => messages.flat(),
};

export default [
  { ignores: ["node_modules/**", ".venv/**"] },
  js.configs.recommended,
  {
    languageOptions: {
      ecmaVersion: "latest",
      sourceType: "script",
      globals: { ...globals.browser },
    },
    linterOptions: { reportUnusedDisableDirectives: "error" },
    rules: {
      eqeqeq: ["error", "smart"],
      // `try { localStorage... } catch (e) {}` is deliberate here: storage can be blocked, the page works without it.
      "no-empty": ["error", { allowEmptyCatch: true }],
      "no-unused-vars": ["error", { vars: "local", args: "after-used", caughtErrors: "none" }],
    },
  },
  ...sharedGlobals,
  {
    files: ["**/*.html", "**/*.j2"],
    processor: inlineScripts,
  },
  {
    // shell.html: the server replaces this placeholder with the window's settings before serving it.
    files: ["**/shell.html/*.js"],
    languageOptions: { globals: { __PROOFLOOP_CONFIG__: "readonly" } },
    // `const frames = {...}` shadows window.frames, which is allowed (a replaceable property, unlike `top` or `location`).
    rules: { "no-redeclare": ["error", { builtinGlobals: false }] },
  },
];
