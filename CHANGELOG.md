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
