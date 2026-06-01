#!/usr/bin/env bash
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOCS_DIR='docs/stenc'
PORT=""
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: ./open-docs.sh [options]

Open this project's Stenc static docs and stop it with Enter.

Options:
  --docs-dir <path>      Docs path inside this project. Defaults to the installed docs path.
  --port <number>        Preferred local port. Defaults to the first free port from 4321.
  --dry-run              Print resolved paths without starting the static server.
  -h, --help             Show this help.
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help)
      usage
      exit 0
      ;;
    --docs-dir)
      DOCS_DIR="${2:-}"
      if [[ -z "${DOCS_DIR}" ]]; then
        echo "Missing value for --docs-dir" >&2
        exit 2
      fi
      shift 2
      ;;
    --port)
      PORT="${2:-}"
      if [[ -z "${PORT}" ]]; then
        echo "Missing value for --port" >&2
        exit 2
      fi
      shift 2
      ;;
    --dry-run)
      DRY_RUN=1
      shift
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

if [[ "${DOCS_DIR}" = /* ]]; then
  DOCS_PATH="${DOCS_DIR}"
else
  DOCS_PATH="${PROJECT_ROOT}/${DOCS_DIR}"
fi

if [[ "${DRY_RUN}" -eq 1 ]]; then
  echo "projectRoot=${PROJECT_ROOT}"
  echo "docsPath=${DOCS_PATH}"
  exit 0
fi

if ! command -v node >/dev/null 2>&1; then
  echo "node is required to open the Stenc static docs." >&2
  exit 1
fi

STENC_SKILLS_ROOT="${CODEX_SKILLS_DIR:-${HOME}/.codex/skills}"
STENC_SETUP_PROJECT_JS="${STENC_SETUP_PROJECT_JS:-${STENC_SKILLS_ROOT}/stenc/scripts/setup-project.js}"
if [[ ! -f "${STENC_SETUP_PROJECT_JS}" ]]; then
  echo "Stenc renderer not found: ${STENC_SETUP_PROJECT_JS}" >&2
  echo "Install Stenc first: stenc install --docs-dir ${DOCS_DIR}" >&2
  exit 1
fi

node "${STENC_SETUP_PROJECT_JS}"   --project-root "${PROJECT_ROOT}"   --docs-dir "${DOCS_DIR}"   --render-only   --skip-open-docs-script

if [[ "${STENC_OPEN_DOCS_PRECHECK_ONLY:-0}" -eq 1 ]]; then
  echo "Stenc docs regenerated at ${DOCS_PATH}"
  exit 0
fi

if [[ ! -f "${DOCS_PATH}/index.html" ]]; then
  echo "Stenc static docs not found: ${DOCS_PATH}" >&2
  echo "Run setup first, for example:" >&2
  echo "  stenc install --docs-dir ${DOCS_DIR}" >&2
  exit 1
fi

if [[ -z "${PORT}" ]]; then
  PORT="$(node - <<'NODE'
const net = require("node:net");

function canListen(port) {
  return new Promise((resolve) => {
    const server = net.createServer();
    server.once("error", () => resolve(false));
    server.once("listening", () => {
      server.close(() => resolve(true));
    });
    server.listen(port, "127.0.0.1");
  });
}

(async () => {
  for (let port = 4321; port < 4400; port += 1) {
    if (await canListen(port)) {
      console.log(port);
      return;
    }
  }
  process.exit(1);
})();
NODE
)"
fi

URL="http://127.0.0.1:${PORT}/"
(
  cd "${DOCS_PATH}"
  node -e "const http=require('node:http'),fs=require('node:fs'),path=require('node:path');const root=process.cwd();const port=Number(process.argv[1]);const types={'.html':'text/html; charset=utf-8','.css':'text/css; charset=utf-8','.js':'text/javascript; charset=utf-8','.json':'application/json; charset=utf-8'};http.createServer((req,res)=>{const url=new URL(req.url,'http://127.0.0.1');let file=path.join(root,decodeURIComponent(url.pathname));if(!file.startsWith(root)){res.writeHead(403);res.end('Forbidden');return;}if(fs.existsSync(file)&&fs.statSync(file).isDirectory())file=path.join(file,'index.html');if(!fs.existsSync(file)){res.writeHead(404);res.end('Not found');return;}res.writeHead(200,{'Content-Type':types[path.extname(file)]||'application/octet-stream'});fs.createReadStream(file).pipe(res);}).listen(port,'127.0.0.1');" "${PORT}"
) &
SERVER_PID=$!

cleanup() {
  if kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    kill "${SERVER_PID}" >/dev/null 2>&1 || true
    wait "${SERVER_PID}" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT INT TERM

for _ in $(seq 1 80); do
  if ! kill -0 "${SERVER_PID}" >/dev/null 2>&1; then
    echo "Stenc static server failed to start." >&2
    exit 1
  fi
  if curl -fsS "${URL}" >/dev/null 2>&1; then
    break
  fi
  sleep 0.25
done

if command -v open >/dev/null 2>&1; then
  open "${URL}"
fi

echo "Stenc docs running at ${URL}"
echo "Press Enter to stop."
IFS= read -r _
