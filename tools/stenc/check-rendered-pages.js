#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

function fail(message) {
  console.error(message);
  process.exit(1);
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, "utf8"));
  } catch (error) {
    fail(`Invalid JSON in ${file}: ${error.message}`);
  }
}

function renderedPageFor(docsRoot, doc) {
  if (doc.docType === "spec") {
    return path.join(docsRoot, "specs", doc.slug, "index.html");
  }
  if (doc.docType === "plan") {
    return path.join(docsRoot, "plans", doc.slug, "index.html");
  }
  if (doc.docType === "decision") {
    return path.join(docsRoot, "decisions", doc.slug, "index.html");
  }
  if (doc.docType === "agent-context") {
    return path.join(docsRoot, "agent-context", doc.slug, "index.html");
  }
  return null;
}

function jsonFilesIn(dir) {
  if (!fs.existsSync(dir)) {
    return [];
  }
  return fs.readdirSync(dir)
    .filter((name) => name.endsWith(".json"))
    .map((name) => path.join(dir, name));
}

const docsRoot = path.resolve(process.argv[2] || "docs/stenc");
const contentRoot = path.join(docsRoot, "content");

for (const required of [
  path.join(docsRoot, "index.html"),
  path.join(docsRoot, "styles.css"),
  path.join(contentRoot, "site.json"),
]) {
  if (!fs.existsSync(required)) {
    fail(`Missing Stenc output: ${required}`);
  }
}

const docs = [
  ...jsonFilesIn(path.join(contentRoot, "specs")),
  ...jsonFilesIn(path.join(contentRoot, "plans")),
  ...jsonFilesIn(path.join(contentRoot, "decisions")),
  ...jsonFilesIn(path.join(contentRoot, "agent-context")),
].map(readJson);

let checked = 1;
for (const doc of docs) {
  const rendered = renderedPageFor(docsRoot, doc);
  if (!rendered) {
    continue;
  }
  if (!fs.existsSync(rendered)) {
    fail(`Missing rendered page for ${doc.id || doc.slug}: ${rendered}`);
  }
  checked += 1;
}

console.log(`Stenc rendered page check passed: ${docsRoot}`);
console.log(`Rendered pages checked: ${checked}`);
