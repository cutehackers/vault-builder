#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

function fail(message) {
  console.error(message);
  process.exit(1);
}

function optionValue(name, fallback) {
  const index = process.argv.indexOf(name);
  if (index === -1) {
    return fallback;
  }
  const value = process.argv[index + 1];
  if (!value || value.startsWith("--")) {
    fail(`Missing value for ${name}`);
  }
  return value;
}

function walkJsonFiles(dir) {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const fullPath = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      files.push(...walkJsonFiles(fullPath));
    } else if (entry.isFile() && entry.name.endsWith(".json")) {
      files.push(fullPath);
    }
  }
  return files;
}

function escapeHtml(value) {
  return String(value || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function ensureDir(dir) {
  fs.mkdirSync(dir, { recursive: true });
}

function page(title, body) {
  return `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>${escapeHtml(title)}</title>
  <link rel="stylesheet" href="/styles.css">
</head>
<body>
  <main>
${body}
  </main>
</body>
</html>
`;
}

function collectionFor(docType) {
  if (docType === "spec") return "specs";
  if (docType === "plan") return "plans";
  if (docType === "decision") return "decisions";
  if (docType === "agent-context") return "agent-context";
  return null;
}

function docSummary(doc) {
  return [
    `<h1>${escapeHtml(doc.title)}</h1>`,
    `<p>${escapeHtml(doc.description)}</p>`,
    "<dl>",
    `<dt>Status</dt><dd>${escapeHtml(doc.status)}</dd>`,
    `<dt>Owner</dt><dd>${escapeHtml(doc.owner)}</dd>`,
    `<dt>Updated</dt><dd>${escapeHtml(doc.updatedAt || doc.createdAt || "")}</dd>`,
    "</dl>",
  ].join("\n");
}

function writeStaticDocs(docsRoot, docs) {
  ensureDir(docsRoot);
  fs.writeFileSync(
    path.join(docsRoot, "styles.css"),
    "body{font-family:system-ui,-apple-system,sans-serif;margin:0;color:#202124;background:#fff}main{max-width:920px;margin:0 auto;padding:40px 24px}a{color:#0b57d0}li{margin:8px 0}dt{font-weight:700;margin-top:12px}dd{margin-left:0;color:#444}\n"
  );

  const byCollection = new Map();
  for (const doc of docs.filter((item) => item.docType)) {
    const collection = collectionFor(doc.docType);
    if (!collection) continue;
    if (!byCollection.has(collection)) byCollection.set(collection, []);
    byCollection.get(collection).push(doc);
    const targetDir = path.join(docsRoot, collection, doc.slug);
    ensureDir(targetDir);
    fs.writeFileSync(path.join(targetDir, "index.html"), page(doc.title, docSummary(doc)));
  }

  const collectionLinks = [];
  for (const [collection, items] of byCollection.entries()) {
    items.sort((a, b) => String(a.slug).localeCompare(String(b.slug)));
    ensureDir(path.join(docsRoot, collection));
    const links = items
      .map((doc) => `<li><a href="./${escapeHtml(doc.slug)}/">${escapeHtml(doc.title)}</a></li>`)
      .join("\n");
    fs.writeFileSync(
      path.join(docsRoot, collection, "index.html"),
      page(collection, `<h1>${escapeHtml(collection)}</h1>\n<ul>\n${links}\n</ul>`)
    );
    collectionLinks.push(`<li><a href="./${escapeHtml(collection)}/">${escapeHtml(collection)}</a></li>`);
  }

  const site = docs.find((item) => !item.docType) || { title: "Project Docs", description: "" };
  fs.writeFileSync(
    path.join(docsRoot, "index.html"),
    page(site.title, `<h1>${escapeHtml(site.title)}</h1>\n<p>${escapeHtml(site.description)}</p>\n<ul>\n${collectionLinks.join("\n")}\n</ul>`)
  );
}

const projectRoot = path.resolve(optionValue("--project-root", process.cwd()));
const docsDir = optionValue("--docs-dir", "docs/stenc");
const docsRoot = path.resolve(projectRoot, docsDir);
const contentRoot = path.join(docsRoot, "content");

if (!fs.existsSync(projectRoot)) {
  fail(`Project root does not exist: ${projectRoot}`);
}
if (!fs.existsSync(contentRoot)) {
  fail(`Stenc content directory does not exist: ${contentRoot}`);
}

const jsonFiles = walkJsonFiles(contentRoot);
if (jsonFiles.length === 0) {
  fail(`No Stenc JSON content found under ${contentRoot}`);
}

const docs = [];
for (const file of jsonFiles) {
  try {
    docs.push(JSON.parse(fs.readFileSync(file, "utf8")));
  } catch (error) {
    fail(`Invalid Stenc JSON content ${file}: ${error.message}`);
  }
}

writeStaticDocs(docsRoot, docs);

console.log(`Prepared Stenc static docs at ${docsRoot}`);
console.log(`Validated content files: ${jsonFiles.length}`);
