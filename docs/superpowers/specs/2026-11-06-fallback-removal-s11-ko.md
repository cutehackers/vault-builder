# 요청S11 대체 경로 제거: 바꿀 곳 목록

**Date:** 2026-11-06
**Status:** 메모만 있다. 구현하지 않았다. vault-graph spec `2026-11-05-session-memory-indexing` §4.4.9, 묶음 S12에서 사람이 제거를 한 번 더 확인한 뒤 시작한다.
**보낸 쪽:** vault-graph spec §4.5 요청S11

## 1. 제거 조건

- vault-graph 기억 평가(spec §4.4.10)의 지표1(대체·폐기 기억 주입 수)과 지표2(`sharable`이 false인 Vault의 다른 프로젝트 기억 주입 비율)가 0이다.
- 사람이 S12 시작 때 제거를 확인했다.
- 제거 뒤 vault-graph가 없는 사용자는 세션 시작 회상과 기억 정리를 받지 못한다. ingest, update, query, 검증, 세션 수집은 그대로 동작한다.

## 2. 지울 것

| 대상 | 위치 | 할 일 |
|---|---|---|
| `memory-cleanup` 스킬 | `.agents/skills/memory-cleanup/SKILL.md`, `.claude/skills/memory-cleanup/SKILL.md` | 두 파일을 지운다. `AGENTS.md`의 Task Routing 문단과 Directory Map 표의 스킬 목록, `docs/agent/OPERATING-SCHEMA.md`, `docs/usage.md` 17.4를 고친다. `memory-scan` 명령(`tools/wiki/lib/memory_scan.py`)은 vault-graph가 읽기 전용으로 쓰므로 남긴다. |
| `maps build` 프로젝트 요약 | `tools/wiki/lib/maps.py`의 `_project_summary_pages`, `_render_project_summary`, `_stale_project_map_paths`, `_assert_generated_project_map`, `render_no_project_summary`, 관련 상수 | 프로젝트 요약 생성과 `maps no-project-summary` 명령을 지운다. 일반 탐색 지도(topic, source, decision, review, lifecycle)는 남긴다. 이미 있는 `wiki/maps/projects/*.md`를 지울지, 남겨 두고 lint에서 무시할지 정해야 한다. |
| `recall.sh`와 `/project-memory` 스킬 | `tools/vault_migration/assets/project-memory/` | asset 폴더를 지운다. `installing.py`(recall 스크립트와 스킬 복사, 인터프리터 자리 표시 치환), `uninstalling.py`(설치본 판별과 삭제, doctor의 git 2.31 검사), `onboarding.py`의 안내를 고친다. 이미 설치된 사용자 폴더의 `skills/project-memory/`는 uninstall과 같은 규칙(설치한 바이트와 같을 때만)으로 지운다. |
| 훅 실행기의 `recall.sh` 대체 | `tools/vault_migration/assets/vault-hooks/vault_hooks.py`의 `session_start` | vg가 없거나 실패하면 `recall.sh` 대신 빈 출력으로 끝낸다. 수집 대체(`capture --direct`)는 남긴다. |
| `sharable` | `docs/contracts/vault-map-v2.md`, `tools/vault_migration/map_settings.py` | vault-graph가 계속 읽으므로 필드와 `set-sharable` 명령은 남긴다. 대체 경로 설명만 지운다. |

## 3. 함께 바꿀 테스트

- `builder_tests/test_vault_migration.py`의 `ProjectMemoryRecallTest`와 recall asset 검사, `builder_tests/test_recall_scope.py`
- `builder_tests/test_vault_hooks_launcher.py`의 session-start 대체 검사(`test_vg_failure_prints_recall_output` 등)
- `builder_tests/test_doctor_hooks.py`, `builder_tests/test_onboarding_setup.py`, `builder_tests/test_uninstall_all.py`의 recall.sh 줄
- `tests/test_wiki_tools.py`의 `ProjectSummaryMapTest`, `tests/test_project_scope.py`

## 4. 버전

관리 파일 목록(`tools/wiki/lib/vault_manifest.py`의 `MANAGED_EXACT_PATH_SCOPE`)에서 스킬 파일을 빼면 contract가 바뀐다. 명령(`maps no-project-summary`)과 스킬을 없애므로 `CHANGELOG.md` 규칙상 MAJOR다. 새 contract 파일과 audit, apply 경로가 필요하다.
