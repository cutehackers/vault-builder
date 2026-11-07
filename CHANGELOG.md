# Changelog

vault-builder의 릴리스별 변경을 기록한다. 형식은 [Keep a Changelog](https://keepachangelog.com/ko/1.1.0/)를 따르고, 버전은 [SemVer](https://semver.org/lang/ko/)를 따른다.

## 버전 번호 읽는 법

이 저장소는 서로 다른 것을 세는 번호를 네 가지 쓴다.

| 번호 | 세는 것 | 기록 위치 |
| --- | --- | --- |
| 릴리스 버전 `X.Y.Z` | vault-builder 배포 | `VERSION`, git tag `vX.Y.Z`, 이 파일 |
| contract 버전 | vault에 설치되는 관리 파일 목록과 manifest 규칙 | `tools/vault_migration/contracts/vN.json` |
| manifest schema 버전 | `.vault-builder/manifest.json` 형식 | manifest의 `schema_version` |
| vault-map 버전 | `<설정 폴더>/vault-builder/vault-map.json` 형식 | vault-map의 `version` |

릴리스 1.0.0이 contract 2를 쓰는 것처럼 두 번호는 같지 않다. 카탈로그의 `builder_revision`과 vault manifest의 `builder_revision`에는 릴리스 버전이 들어간다. 그래서 manifest를 보면 그 vault를 어느 릴리스로 설치하거나 migration했는지 알 수 있다.

올리는 규칙:

- MAJOR: 설치된 vault에 사람이 손을 써야 하는 변경이다. contract 버전을 올리는 경우, 이전 설치를 거부하는 경우, 명령이나 MCP 도구를 없애거나 이름을 바꾸는 경우, 이전 builder가 새 manifest나 vault-map을 읽지 못하는 경우다.
- MINOR: 기존 설치와 함께 쓸 수 있는 새 기능이다. vault는 audit와 apply로 따라온다.
- PATCH: 버그 수정이다. 관리 파일이 바뀌면 vault는 audit와 apply로 따라온다.

릴리스 절차는 `docs/PUBLISH.md`에 있다.

## [Unreleased]

## [5.0.2] - 2026-11-08

예전 릴리스가 설치한 훅 실행기를 `setup`이 알 수 없는 파일로 보고 교체하지 않았다. 그래서 `doctor`가 `vault-builder setup`을 안내해도 문제가 계속 남았다.

| 릴리스 | contract | manifest schema | vault-map |
| --- | --- | --- | --- |
| 5.0.2 | 6 | 6 | 2 |

### Fixed

- `install --with-hooks`와 `setup`이 v3.0.0부터 v4.1.0까지의 훅 실행기, 그리고 5.0.0 전 main의 훅 실행기를 새 실행기로 교체한다. 바이트가 이 실행기들과 다른 파일은 전과 같이 그대로 둔다.

### Upgrade

- Claude Code를 종료하고 `vault-builder setup`을 다시 실행한다.

## [5.0.1] - 2026-11-07

vault-graph가 쓴 기억 페이지가 있는 vault는 `metrics --check`가 실패해서 migration apply가 되돌려졌다. 검사 기준을 고쳤다.

| 릴리스 | contract | manifest schema | vault-map |
| --- | --- | --- | --- |
| 5.0.1 | 6 | 6 | 2 |

### Fixed

- `metrics`가 기억 페이지(frontmatter `memory_schema`가 있는 페이지)를 `pages_without_claim_level_provenance`에 세지 않는다. 기억 페이지는 claim 표 대신 Evidence 절로 근거를 적는다.
- `metrics`가 index, map, log 페이지의 링크를 `deprecated_linked_pages`에 세지 않는다. 이 페이지들은 폐기된 페이지를 일부러 나열한다. 다른 페이지가 폐기된 페이지를 링크하면 전과 같이 센다.

### Upgrade

- Claude Code를 종료하고 `vault-builder setup`을 다시 실행하거나, vault마다 audit와 apply를 실행한다. 순서는 5.0.0 Upgrade 절과 같다.

## [5.0.0] - 2026-11-07

vault-graph 요청S11로 대체 경로를 없앴다. vault-builder는 `/project-memory` skill, `recall.sh`, `memory-cleanup` skill, 프로젝트 요약 페이지를 더 만들지 않고, 세션 시작 회상과 기억 정리는 vault-graph가 맡는다. vault-graph가 없는 사용자는 세션 시작 회상과 기억 정리를 받지 못하고, ingest, update, query, 검증, 세션 수집은 그대로 동작한다. 명령 `maps no-project-summary`와 skill을 없애고 contract를 6으로 올렸으므로 MAJOR로 올렸다.

| 릴리스 | contract | manifest schema | vault-map |
| --- | --- | --- | --- |
| 5.0.0 | 6 | 6 | 2 |

### Added

- `sessions capture --normalized <file> [--rerender] --json`이 vault-graph 정규화 세션 파일(`vg-session-jsonl/1`)을 새 세션 파일 형식 `vault-session/v2`로 쓴다. 닫힌 교환만 쓰고, part 번호는 v1과 v2를 이어서 매긴다. 이전 part가 v1이면 `last_turn_uuid` 뒤부터, v2이면 `last_exchange_id` 뒤부터 쓴다. 훅 실행기 `vault_hooks.py capture --direct --normalized <file> [--rerender]`도 같은 일을 하고, vault 도구가 `--normalized`를 모르면 exit 2로 끝낸다. 기존 v1 파일과 v1 수집은 그대로다.
- `vault-map.json`에 선택 필드 `vaults.<name>.sharable`과 설정 명령 `vault-builder set-sharable --vault <name> (--value true|false | --clear)`를 더한다. 필드가 없으면 false다.
- `import-source <file> --into raw/imports/<kind>/ [--json]`이 vault 밖 파일을 `raw/imports/<kind>/<stem>--<sha256 앞 8자><확장자>`에 바이트 그대로 복사하고 source page를 만든다. 같은 sha256의 source page가 있으면 아무것도 만들지 않고 기존 경로를 돌려준다. `AGENTS.md` 규칙1에 이 명령의 예외를 적는다.
- 페이지 종류 `project`를 더한다. project brief는 `wiki/projects/<project>.md`에만 두고 frontmatter `project`가 그 파일 이름과 같아야 하며, 어긋나면 검증이 `project-page-path`로 거부한다. `publish-draft`로 쓸 수 있다.

### Changed

- 폐기한 페이지를 frontmatter만 바꾸는 `patch-page`로 되살릴 수 있다. `status`가 retired가 아니어도 `restored_at`(날짜)과 `restored_reason`이 함께 있으면 `retired_reason`, `retired_at`을 이력으로 남길 수 있다. 전에는 lint가 `retired-field-without-retired-status`로 거부했다(vault-graph 요청S12).
- contract와 manifest schema를 6으로 올렸다. 관리 파일 목록에서 `memory-cleanup` skill 두 파일을 뺐다(`tools/vault_migration/contracts/v6.json`). `v5.json`은 릴리스 4.1.0 내용으로 고정한다. schema 5 이하 manifest와 contract 5 이하 migration journal은 두 파일을 계속 적을 수 있다.
- `docs/usage.md` 17절, `docs/contracts/vault-map-v2.md`, `docs/html/project-memory-structure.html`, `docs/html/vault-structure-map.html`에서 대체 경로(`/project-memory`, `recall.sh`, 프로젝트 요약 페이지, `memory-cleanup`) 설명을 지우거나 이전 동작 기록으로 표시했다. `sharable` 필드와 `set-sharable` 명령은 vault-graph가 읽으므로 그대로다.

### Removed

- 훅 실행기 `vault_hooks.py session-start`가 vg가 없거나 실패하거나 시간을 넘기면 아무것도 출력하지 않는다. 전에는 `recall.sh` 출력을 넣었다(vault-graph 요청S11). 세션 수집의 대체 경로(`capture --direct`)는 그대로다.
- install이 `/project-memory` skill과 recall 스크립트(`<설정 폴더>/skills/project-memory/`)를 설치하지 않는다. asset 폴더 `tools/vault_migration/assets/project-memory/`를 지웠다. 이전 install이 쓴 두 파일은 install(`setup` 포함)이 지우고 manifest 기록에서도 뺀다. 파일이 manifest에 기록된 hash나 릴리스된 asset과 같을 때만 지우고, 사용자가 고친 파일은 남긴 뒤 목록에 적는다. uninstall도 같은 규칙으로 남은 파일을 지운다. doctor는 두 파일을 검사하지 않고, git 2.31 검사도 하지 않는다. 남은 builder 파일이 있으면 `vault-builder setup`으로 고치라고 알린다.
- `maps build`가 프로젝트 요약 페이지(`wiki/maps/projects/<project id>.md`)를 만들지 않고, 명령 `maps no-project-summary`를 없앴다. topic, source, decision, review, lifecycle 지도는 그대로다. 이전 `maps build`가 만든 요약 페이지(frontmatter `generated_by: tools/wiki/cli.py maps build`이고 page lock이 없는 파일)는 다음 `maps build`가 지우고 `wiki/log.md`에 적는다. 그 폴더의 다른 파일은 사람이 쓴 페이지로 보고 그대로 둔다. `maps build --check`는 남은 요약 페이지 때문에 실패하지 않으므로, migration apply가 실행하는 vault gate도 통과한다.
- vault에 설치하던 `memory-cleanup` skill(`.agents/skills/memory-cleanup/SKILL.md`, `.claude/skills/memory-cleanup/SKILL.md`)을 지웠다. `AGENTS.md`의 안내도 뺐다. 읽기 전용 명령 `memory-scan`은 vault-graph가 쓰므로 남긴다.

### Upgrade

- Claude Code를 종료하고 설치 명령(`curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash`)이나 `vault-builder setup`을 다시 실행한다. 사람 검토 항목이 없는 vault는 audit와 apply로 contract 6이 되고, install이 builder가 만든 `<설정 폴더>/skills/project-memory/` 파일을 지운다. 직접 하려면 vault마다 아래 순서로 실행한 뒤 `vault-builder install <vault 경로>`를 실행한다.

  ```bash
  python3 tools/vault_migration/cli.py audit --target /path/to/vault --plan-out /tmp/vault-migration-plan.json
  ```

  ```bash
  python3 tools/vault_migration/cli.py apply --target /path/to/vault --plan /tmp/vault-migration-plan.json
  ```

- migration은 contract 6이 관리하지 않는 `memory-cleanup` skill 두 파일을 지우지 않는다. apply와 doctor가 끝나면 vault에서 지우고 commit한다.

  ```bash
  git -C /path/to/vault rm .agents/skills/memory-cleanup/SKILL.md .claude/skills/memory-cleanup/SKILL.md
  ```

  ```bash
  git -C /path/to/vault commit -m "chore: remove memory-cleanup skill retired by contract 6"
  ```

- 이전 `maps build`가 만든 `wiki/maps/projects/*.md`는 migration 뒤에도 남는다. vault 폴더에서 `python3 tools/wiki/cli.py maps build --report`를 한 번 실행하면 지워지고 `wiki/log.md`에 적힌다. 그 뒤 commit한다. 사람이 쓴 파일과 잠긴 파일은 남는다.
- 사용자가 고친 `<설정 폴더>/skills/project-memory/` 파일은 install이 남기고 경로를 출력한다. 필요 없으면 직접 지운다. doctor는 builder가 만든 파일이 남아 있을 때만 문제로 알린다.
- doctor 첫 줄이 `vault-builder 5.0.0 (contract 6)`이고 `Problem:` 줄이 없으면 끝이다.

## [4.1.0] - 2026-11-05

이전 릴리스로 만든 vault가 있어도 설치 명령 한 번으로 설치가 끝난다. 기존 설치와 함께 쓸 수 있는 새 동작이라 MINOR로 올렸다. contract와 manifest schema는 5 그대로다.

| 릴리스 | contract | manifest schema | vault-map |
| --- | --- | --- | --- |
| 4.1.0 | 5 | 5 | 2 |

### Changed

- `setup`이 예전 vault를 업그레이드한다. 사전 검사에서 manifest가 있는 vault마다 audit을 실행하고, manifest schema가 예전 것이거나 `create`, `replace` 항목이 있는 vault를 계획에 출력한 뒤 2단계에서 apply하고 install한다. 그래서 이전 릴리스로 만든 vault가 있어도 설치 명령 한 번으로 끝난다. wiki 페이지와 raw source는 바뀌지 않는다.
- 어느 vault든 audit에 `review-required`나 `invalid` 항목이 있으면 `setup`은 그 경로를 모든 vault에 걸쳐 한 번에 출력하고 아무것도 쓰지 않고 멈춘다. `--dry-run`도 같은 이유로 exit 1을 낸다. 경로마다 audit 이유와 고치는 방법을 출력한다. 바뀐 관리 파일이나 없어진 필수 관리 파일(`AGENTS.md` 등)에는 vault-builder 버전을 다시 넣는 `cp` 명령을 출력한다. source 규칙을 어긴 위키 페이지는 고치라고, 더 새 builder의 manifest는 vault-builder를 업데이트하라고 안내한다. 업그레이드할 vault도 쓰기 전에 install 사전 검사(schema와 audit 검사 제외)를 받는다.
- install이 schema가 예전 것이거나 migration이 필요한 vault를 거부할 때 `vault-builder setup`과 `vault-builder audit` 명령을 안내한다.

### Upgrade

- 설치 명령이나 `vault-builder setup`을 다시 실행한다. 사람 검토 항목이 없는 vault는 이 실행에서 업그레이드된다.

## [4.0.0] - 2026-11-05

vault-builder는 vault-graph 없이 설치되고 동작한다. vault-graph를 설치하고 설정하는 일은 vault-graph가 맡는다. vault-graph는 vault-builder가 필요하므로 vault-graph의 설치 명령이 vault-builder 설치 명령을 실행한다. 설치 명령의 옵션 `--without-vault-graph`와 `--install-uv`를 없앴으므로 MAJOR로 올렸다. contract와 manifest schema는 5 그대로다.

| 릴리스 | contract | manifest schema | vault-map |
| --- | --- | --- | --- |
| 4.0.0 | 5 | 5 | 2 |

### Removed

- `setup`이 vg를 설치하지 않는다. vault-graph 저장소 접근 검사(`git ls-remote`), `uv tool install`, `vg setup --agent claude --mcp`(MCP 등록과 첫 색인), 대화 모드의 vault-graph 설치 질문, 환경 변수 `VAULT_GRAPH_REPO`를 없앴다. 단계는 precheck, hook을 포함한 Vault 설치, 저장소 연결, doctor 네 개다.
- `setup`과 `install.sh`의 `--without-vault-graph` 옵션을 없앴다. 이 옵션을 주면 알 수 없는 옵션으로 멈춘다.
- `install.sh`가 uv를 확인하거나 설치하지 않는다. `--install-uv` 옵션을 없앴고, curl도 더 이상 필수 도구가 아니다.
- `install.sh --uninstall`이 `vg mcp unregister --agent claude`와 `uv tool uninstall vault-graph`를 실행하지 않고, `$HOME/.vault-graph`와 임베딩 모델 폴더를 출력하지 않는다.
- `doctor`가 `mcpServers.vault-graph` 항목을 검사하지 않는다. vault-builder가 설치하지 않은 항목이기 때문이다.

### Changed

- `setup`은 환경 변수를 그대로 install에 넘긴다. 그래서 `VAULT_GRAPH_BIN`이나 `PATH`에서 vg를 찾으면 hook이 그 vg를 쓰도록 `vault-hooks.json`에 적는다. vg를 설치하는 도구가 이 값을 넣고 setup을 실행하면 된다. vg가 없으면 hook은 recall 스크립트와 직접 세션 수집을 쓴다.
- `install --with-hooks`가 vg를 찾지 못했고 `vault-hooks.json`에 적힌 vg가 더 이상 실행 파일이 아니면 그 파일을 지운다. 그래서 vg를 지운 뒤 `vault-builder setup`을 다시 실행하면 doctor가 지워진 vg를 문제로 알리지 않는다. 아직 실행되는 vg 경로는 그대로 둔다.

### Upgrade

- 이미 설치한 vg, `mcpServers.vault-graph` 항목, `vault-hooks.json`의 vg 경로는 그대로 남는다. vault-graph를 지우려면 vault-graph의 제거 명령을 쓴다.
- `--without-vault-graph`를 붙여 설치 명령을 실행하던 스크립트는 이 옵션을 지운다.

## [3.0.0] - 2026-11-04

설치 명령 하나(`scripts/install.sh`, `vault-builder setup`)로 vg, Vault, hook, 저장소 연결, MCP 등록, 첫 색인을 끝낸다. 공유 MCP 서버 이름을 `llm-wiki`에서 `vault-wiki`로 바꿔 MCP 도구 이름이 `mcp__vault-wiki__*`가 되었으므로 MAJOR로 올렸다. contract와 manifest schema는 5다.

| 릴리스 | contract | manifest schema | vault-map |
| --- | --- | --- | --- |
| 3.0.0 | 5 | 5 | 2 |

### Added

- 설치 명령 `scripts/install.sh`를 더했다. `curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash` 한 줄로 uv 확인, builder checkout, `vault-builder` 명령 파일(`$HOME/.local/bin/vault-builder`), vg 설치, hook을 포함한 Vault 설치, 저장소 연결, MCP 등록, 첫 색인, doctor를 차례로 실행한다. Claude Code가 실행 중이면 `$HOME/.claude.json`을 쓰기 전에 멈춘다. 같은 명령을 다시 실행하면 빠진 것만 채운다.
- `setup` 명령을 더했다. `install.sh`가 부르는 단계(precheck, vg 설치, hook을 포함한 Vault 설치, 저장소 연결, `vg setup`의 MCP 등록과 첫 색인, doctor)를 builder checkout에서 직접 실행한다. `--vault NAME=PATH`, `--role NAME=ROLE`, `--repo PATH`, `--yes`, `--dry-run`, `--without-vault-graph`를 받는다. vg는 `uv tool install --force --python 3.12 git+https://github.com/cutehackers/vault-graph.git`로 설치하고, 주소는 `VAULT_GRAPH_REPO`로 바꿀 수 있다.
- `uninstall --all`을 더했다. `vault-map.json`의 모든 Vault를 uninstall한 뒤 `vault-builder` 명령 파일과 기본 builder checkout을 지운다. 각 Vault의 `raw/`와 `wiki/`는 남는다. `install.sh --uninstall`은 이어서 `vg mcp unregister --agent claude`와 `uv tool uninstall vault-graph`를 실행하고, 남긴 Vault, `$HOME/.vault-graph`, 임베딩 모델 폴더(기본 `$HOME/llm/models`)를 출력한다.
- 역할 목록(`tools/vault_migration/roles.py`)을 더했다. `personal`, `team`, `project` 세 역할을 알고, 목록에 없는 역할도 그대로 받아 공유 Vault로 다룬다.

### Changed

- `link`가 Vault 이름을 여러 개 받는다. 이름 없이 실행하면 공유 역할의 Vault를 모두 연결한다.
- `doctor`의 경로 인자를 뺄 수 있다. 경로 없이 실행하면 `vault-map.json`의 모든 Vault와 공유 항목을 한 번씩 검사한다. hook launcher 항목(SessionStart, PreCompact, SessionEnd), `vault-hooks.json`에 적힌 vg 경로와 `vg --help` 실행, `vault-builder` 명령 파일이 가리키는 checkout도 검사한다.

- vault에 설치되는 workflow 이름을 `.github/workflows/release-gate.yml`에서 `.github/workflows/qa.yml`로 바꿨다. 관리 파일 목록이 바뀌어 contract와 manifest schema를 5로 올렸다. node를 설치하지 않고, setup-python이 설치한 Python을 `PYTHON`으로 gate에 넘긴다.
- `scripts/release_gate.sh`에 `--tier check|unit|full` 인자를 더했다. `check`는 `lint`, `maps build --check`, `metrics --check`, `sessions git-check`, whitespace 검사만 하고 test를 실행하지 않는다. `unit`은 `full_tier_only` 표시가 없는 test를 더하고, `full`은 test 전부를 더한다. 인자가 없으면 vault는 `check`, builder 저장소는 `unit`을 실행한다. 출력 첫 줄에 `[release-gate] tier: <단계>`를 쓴다.
- gate는 저장소를 임시 폴더로 복사하지 않고 루트에서 실행하며 report 파일을 쓰지 않는다. Git 작업 트리에서 추적 파일이 바뀌거나 ignore되지 않은 새 파일이 생기면 "Release gate changed tracked output"으로 실패한다.
- gate에서 `health` 단계를 뺐다. `health` 명령 자체는 그대로다.
- gate의 test fingerprint 캐시(`llm-wiki-gate-tests.cache`)와 `WIKI_GATE_FULL`을 없앴다. `WIKI_GATE_FULL=1`을 설정해도 오류 없이 무시된다.
- builder 저장소에 `.github/workflows/qa-full.yml`을 더했다. tag push, 수동 실행, 설치·migration·gate·contract·workflow 파일을 바꾼 PR에서 Python 3.12 하나로 `--tier full`을 실행한다. 3.12는 vg 설치 Python, vault-graph CI, `qa.yml`과 같은 버전이다. 이 파일은 vault에 설치되지 않는다. builder는 `.github` 폴더 전체 대신 `qa.yml` 한 파일만 vault에 복사한다.
- install은 manifest schema가 5가 아닌 기존 vault를 audit 안내와 함께 거부한다. doctor는 schema 5를 기준으로 검사한다.
- MCP 서버 이름을 `llm-wiki`에서 `vault-wiki`로 바꿨다. `$HOME/.claude.json`의 항목은 `mcpServers.vault-wiki`, 라우터와 vault `tools/wiki/mcp_server.py`가 답하는 `serverInfo.name`은 `vault-wiki`이고, Claude Code의 MCP 도구 이름은 `mcp__llm-wiki__*`에서 `mcp__vault-wiki__*`로 바뀐다. 이 규칙의 "MCP 도구 이름을 바꾸는 경우"에 해당하는 MAJOR 변경이다. 라우터 파일 이름 `llm_wiki_router.py`는 그대로다.
- install은 이 설정 폴더의 라우터를 실행하는(`args`가 `["-I", <라우터 경로>]`인) `mcpServers.llm-wiki` 항목을 builder 항목으로 보고, 같은 한 번의 쓰기로 지우고 `mcpServers.vault-wiki`를 쓴다. `--dry-run`은 이 이름 변경을 출력한다. 다른 곳을 가리키는 `llm-wiki` 항목은 다른 프로그램의 것이므로 그대로 둔다. 거부는 이제 다른 프로그램의 `vault-wiki` 항목이 있을 때 일어난다. uninstall은 마지막 vault에서 `vault-wiki`와 남은 builder `llm-wiki` 항목을 함께 지우고, doctor는 남은 builder `llm-wiki` 항목을 `Fix: vault-builder setup` 문제로 알린다.

### Upgrade

- 기존 vault(contract 4, manifest schema 4)는 새 builder checkout에서 audit을 실행하고, plan을 검토한 뒤 apply를 실행한다. 순서는 `docs/usage.md` 1.1절을 따른다. `.github/workflows/qa.yml`은 `create`, contract 4 내용 그대로인 관리 파일(예: `scripts/release_gate.sh`)은 `replace`, 사람이 고친 관리 파일은 `review-required`로 나온다. apply는 manifest를 schema 5, contract 5로 새로 쓴다.

  ```bash
  python3 tools/vault_migration/cli.py audit --target /path/to/vault --plan-out /tmp/vault-migration-plan.json
  ```

  ```bash
  python3 tools/vault_migration/cli.py apply --target /path/to/vault --plan /tmp/vault-migration-plan.json
  ```

- apply 뒤 doctor를 실행한다. doctor 첫 줄이 `vault-builder <릴리스 버전> (contract 5)`이고 `Problem:` 줄이 없으면 끝이다.
- migration은 옛 workflow `.github/workflows/release-gate.yml`을 지우지 않는다. 그대로 두면 PR마다 CI가 두 번 돈다. apply와 doctor가 끝나면 vault에서 그 파일을 지우고 commit한다.

  ```bash
  git -C /path/to/vault rm .github/workflows/release-gate.yml
  ```

  ```bash
  git -C /path/to/vault commit -m "ci: remove release-gate.yml replaced by qa.yml"
  ```

- 사람이 고친 `release-gate.yml`도 migration은 건드리지 않는다. 고친 내용이 필요하면 `qa.yml`로 옮긴 뒤 지운다.
- 이미 contract 5로 설치한 vault는 audit에서 `README.md`, `README-kr.md`, `docs/usage.md`, `tests/test_wiki_tools.py`, `tools/wiki/mcp_server.py`가 `replace`로 나온다. 설치 안내 문서가 새 설치 명령으로 바뀌었고 MCP 서버 이름이 `vault-wiki`로 바뀌었기 때문이다. 사람이 고치지 않은 파일이므로 위와 같은 순서로 apply하면 된다. 고친 파일은 `review-required`로 나온다.
- 기존 사용자는 Claude Code를 종료하고 설치 명령(`curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash`)을 한 번 실행한 뒤 다시 시작하면 hook, vg 경로 기록, `vault-builder` 명령이 채워진다. 기존 Vault, 저장소 연결, 기본 쓰기 Vault는 그대로다. 소스에서 설치한 vg(`uv tool install -e .`)는 git 주소 설치로 바뀌므로, 개발용 vg를 유지하려면 `--without-vault-graph`를 준다.
- MCP 서버 이름 변경: 설치 명령, `vault-builder install` 또는 `vault-builder setup`을 다시 실행하면 `$HOME/.claude.json`의 builder `llm-wiki` 항목이 `vault-wiki`로 바뀐다. 권한 허용 규칙(`settings.json`의 `permissions.allow` 등)이나 skill이 `mcp__llm-wiki__...` 도구 이름을 적고 있으면 `mcp__vault-wiki__...`로 직접 고친다. 그 뒤 Claude Code를 다시 시작한다.
- 옮긴 vault에서 인자 없는 `scripts/release_gate.sh`는 `check` 단계만 실행한다. vault의 `tests/`를 실행하려면 `scripts/release_gate.sh --tier unit`을 쓴다.

## [2.0.0] - 2026-10-30

Claude Code 세션 원문을 vault의 `raw/sessions/`에 보관한다. 관리 파일 목록이 바뀌어 contract와 manifest schema를 3으로 올렸다.

| 릴리스 | contract | manifest schema | vault-map |
| --- | --- | --- | --- |
| 2.0.0 | 3 | 3 | 2 |

### Added

- 세션 보관소 `raw/sessions/`: 계약에 적힌 별도 원본 종류다. 세션 수집 명령만 이곳에 새 파일을 추가하고, 기존 세션 파일은 아무도 고치거나 옮기거나 지우지 않는다. 파일마다 frontmatter의 `body_sha256`에 본문 해시를 적는다.
- `sessions` 명령(`tools/wiki/cli.py`): `sessions capture`는 transcript를 읽어 사용자 발화, 에이전트 답변 본문, 서브에이전트 대화, 도구 이름을 세션 파일로 쓴다. thinking 블록과 도구 입력·출력은 남기지 않고, 정해진 형식의 비밀값은 `[REDACTED:<종류>]`로 가린다. 다시 수집하면 이전 수집 뒤의 턴만 새 구간 파일로 쓴다. `sessions status`는 세션 파일 수, 마지막 수집 시각, 검사 실패 수, 커밋되지 않은 세션 파일 수를 출력한다. `sessions git-check`는 커밋된 세션 파일이 지워지거나 바뀌었는지 Git 이력과 비교한다.
- `session-capture` skill: 사람이 세션 보관을 요청할 때 수집 명령을 실행하고 결과를 보고하는 절차다.

### Changed

- lint가 세션 보관소를 검사한다. 새 오류 코드는 `session-file-invalid-path`, `session-file-invalid-frontmatter`, `session-file-hash-mismatch`, `session-part-duplicate`, `session-source-page-forbidden`, `claim-evidence-session-invalid`다.
- wiki 주장은 source page 없이 세션 파일을 근거로 인용할 수 있다. `#sha256=`는 붙여도 되고 빼도 된다.
- source registry는 세션 파일을 `unregistered`로 세지 않고 `session` 또는 `session-invalid`로 표시한다. `ingest-source`와 `bulk-ingest`는 세션 파일을 거부한다.
- metrics와 health가 세션 파일 수, 마지막 수집 시각, 해시 불일치 수를 보고한다. `metrics --check`는 해시 불일치가 있을 때만 실패하고, 세션 수가 늘어나는 것으로는 실패하지 않는다.
- release gate가 metrics 단계 뒤에 `sessions git-check`를 실행한다. Git이 없는 vault에서는 건너뛴다.
- install은 manifest schema가 3이 아닌 기존 vault를 audit 안내와 함께 거부한다. doctor는 schema 3을 기준으로 검사한다.

### Upgrade

- 기존 vault(contract 2, manifest schema 2)는 2.0.0 builder checkout에서 audit을 실행하고, plan을 검토한 뒤 apply를 실행한다. 순서는 `docs/usage.md` 1.1절을 따른다. 새 관리 파일은 `create`, 1.0.0 내용 그대로인 관리 파일은 `replace`, 사람이 고친 관리 파일은 `review-required`로 나온다. apply는 manifest를 schema 3, contract 3, `builder_revision` 2.0.0으로 새로 쓰고 `external_records`는 그대로 옮긴다.

  ```bash
  python3 tools/vault_migration/cli.py audit --target /path/to/vault --plan-out /tmp/vault-migration-plan.json
  ```

  ```bash
  python3 tools/vault_migration/cli.py apply --target /path/to/vault --plan /tmp/vault-migration-plan.json
  ```

- apply 뒤 doctor를 실행한다. doctor 첫 줄이 `vault-builder 2.0.0 (contract 3)`이고 `Problem:` 줄이 없으면 끝이다.
- 1.0.0 builder는 schema 3 manifest를 읽지 못한다. 옮긴 vault에 1.0.0 checkout으로 audit, doctor, uninstall을 실행하면 "지원하지 않는 schema"로 실패한다. 옮긴 뒤에는 2.0.0 이상 checkout만 쓴다.
- 수집 명령은 프로젝트에 연결된 role `project` vault에만 쓴다. `personal`, `team` vault에는 쓰지 않는다. 프로젝트에 project vault가 없거나 둘 이상이면 수집 명령은 아무것도 쓰지 않고 종료 코드 0과 stderr 한 줄로 이유를 알린다. 세션을 보관하려면 project vault를 만들고 `link`로 그 프로젝트에 연결한다.
- 대상 project vault가 아직 contract 3 미만이거나 manifest가 없으면 수집 명령은 쓰지 않고 그 vault를 먼저 migration하라는 한 줄을 낸다. 위 audit과 apply로 그 vault를 옮긴다.
- `raw/sessions/` 디렉터리는 migration이 만들지 않는다. 수집 명령이 처음 쓸 때 만든다.
- 수집 명령은 세션 파일을 커밋하지 않는다. 커밋되지 않은 파일 수는 `sessions status`로 확인하고 사람이 커밋한다.

## [1.0.0] - 2026-10-29

첫 릴리스다. 2026-06-01부터 main에 쌓인 변경을 이 버전 하나로 묶는다.

| 릴리스 | contract | manifest schema | vault-map |
| --- | --- | --- | --- |
| 1.0.0 | 2 | 2 | 2 |

### Added

- wiki 도구(`tools/wiki/cli.py`): 검증, publish, 검색, lint, health, metrics, maps, review, workflow 명령.
- vault migration(`tools/vault_migration/cli.py`): catalog, manifest, audit, apply, recover, install, uninstall, doctor 명령.
- 프로젝트 메모리: 저장소별 요약 페이지, memory-cleanup skill, `/project-memory` skill과 recall 스크립트.
- 사용자 단위 MCP 서버 `llm-wiki` 하나. 상태 없는 라우터가 호출마다 대상 vault의 `tools/wiki/cli.py`를 실행한다. 읽기는 세션이 닿는 vault만, 쓰기는 `vault` 인자로 고른 vault에 한다.
- 저장소 연결 명령 `link`, `unlink`, `list`. 저장소는 root 경로, git remote, 폴더 이름 순서로 찾는다.
- 릴리스 버전 관리: `VERSION`, 이 파일, `docs/PUBLISH.md`. install과 doctor가 `vault-builder X.Y.Z (contract N)`을 출력한다. doctor는 vault manifest의 `builder_revision`이 builder 릴리스와 다르면 알림을 낸다.

### Changed

- 카탈로그와 manifest의 `builder_revision`이 `contract-v2` 대신 릴리스 버전(`1.0.0`)을 담는다.
- install은 vault `.mcp.json`을 만들지 않고 `mcpServers.llm-wiki` 하나만 등록한다. uninstall한 vault도 install 하나로 다시 설치한다.
- doctor는 파일을 쓰지 않고, 서버를 실제로 시작해 응답을 확인한다.

### Removed

- vault마다 등록하던 `mcpServers.llm-wiki-<이름>` 항목과 vault `.mcp.json`.

### Upgrade

- 이전 버전으로 설치한 vault(`llm-wiki-<이름>` 항목이 있는 설치): Claude Code를 모두 종료하고, 이전 버전 builder로 `uninstall`을 실행한 뒤, 이 버전으로 `install`과 저장소마다 `link`를 실행한다. 순서는 `docs/usage.md` 17.6절을 따른다.
- 이 버전으로 이미 설치했지만 manifest의 `builder_revision`이 `contract-v2`인 vault: 관리 파일은 같으므로 `audit`와 `apply`로 manifest만 1.0.0으로 맞춘다. doctor가 알림으로 알려 준다.
