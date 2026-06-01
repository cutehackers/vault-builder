#!/usr/bin/env node
const fs = require("fs");
const path = require("path");

function fail(message) {
  console.error(message);
  process.exit(1);
}

const docPath = process.argv[2];
if (!docPath) {
  fail("usage: validate-stenc-doc.js <json-doc>");
}

let parsed;
try {
  parsed = JSON.parse(fs.readFileSync(docPath, "utf8"));
} catch (error) {
  fail(`Invalid JSON in ${docPath}: ${error.message}`);
}

if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
  fail(`Stenc document must be a JSON object: ${docPath}`);
}

const basename = path.basename(docPath);
if (basename === "site.json") {
  for (const field of ["title", "description"]) {
    if (typeof parsed[field] !== "string" || parsed[field].trim() === "") {
      fail(`site.json requires non-empty ${field}`);
    }
  }
  console.log(`Stenc validation passed: ${docPath}`);
  process.exit(0);
}

for (const field of ["schemaVersion", "docType", "id", "slug", "status", "title", "description", "owner"]) {
  if (parsed[field] === undefined || parsed[field] === null || parsed[field] === "") {
    fail(`Stenc document missing required field ${field}: ${docPath}`);
  }
}

if (parsed.schemaVersion !== 2) {
  fail(`Stenc document schemaVersion must be 2: ${docPath}`);
}

if (!["spec", "plan", "decision", "agent-context"].includes(parsed.docType)) {
  fail(`Unsupported Stenc docType ${parsed.docType}: ${docPath}`);
}

if (typeof parsed.slug !== "string" || !/^[a-z0-9][a-z0-9-]*$/.test(parsed.slug)) {
  fail(`Stenc slug must be lowercase kebab-case: ${docPath}`);
}

console.log(`Stenc validation passed: ${docPath}`);
