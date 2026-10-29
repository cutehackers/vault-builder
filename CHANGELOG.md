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
