# vault-graph가 vault-builder에 요청하는 변경

**Date:** 2026-10-29
**Status:** 요청1~6은 구현됐고 요청7은 일부만 구현됐다(2026-11-02). 요청1·2는 커밋 `f562d77`, 요청3~7은 브랜치 `feat/jh/vault-graph-requests`의 그 뒤 커밋에 있고 아직 main에 병합하지 않았다. 각 요청의 상태는 2절 표의 마지막 열에 있다.
**보낸 쪽:** vault-graph spec `2026-10-29-local-graph-memory-layer` (vault-graph 저장소 `docs/specs/`)
**설계 근거:** vault-graph 저장소 `docs/html/2026-10-29-vault-memory-design.html`

## 1. 배경

vault-graph는 Vault 위의 로컬 검색·회상 계층(Local-Graph)으로 방향을 바꾼다. Vault는 기억의 원본이고, vault-graph는 Vault와 코드 저장소를 읽어 색인한 뒤 에이전트에게 출처가 붙은 기억을 토큰 예산 안에 돌려준다. vault-graph는 Vault 파일을 직접 쓰지 않는다.

이 방향은 Vault 쪽에 아직 없는 기능 몇 가지에 기대고 있다. 이 문서는 그 목록이다. 각 항목은 vault-builder가 받아들일지, 어떻게 만들지를 정해야 한다.

## 2. 요청

| 코드 | 요청 | 이유 | vault-graph 쪽 연결 | 상태 (2026-11-02) |
|---|---|---|---|---|
| 요청1 | Vault `AGENTS.md` 규칙1에 예외를 둔다. 세션 수집기가 `raw/sessions/`에 **새 파일을 추가하는 것만** 허용하고, 기존 파일 수정·이름 변경·삭제는 계속 금지한다. | 규칙1은 "사람이 요청하지 않으면 raw/를 바꾸지 않는다"이다. 자동 수집은 사람이 매번 요청하지 않는다. vault-graph 사용자가 이 예외를 승인했다(결정11). | 결정11 | 구현됨(`f562d77`). Vault `AGENTS.md` 규칙1에 `sessions capture`만 `raw/sessions/`에 새 파일을 더할 수 있다는 예외가 있다. |
| 요청2 | 세션 수집 도구를 Vault 도구(`tools/wiki`와 스킬)에 넣고 템플릿으로 배포한다. Claude Code JSONL을 턴 단위로 읽어 발화와 도구 이름만 남기고, 비밀값을 가린 뒤 Markdown과 frontmatter로 `raw/sessions/<agent>/<date>-<id>.md`에 쓴다. | `publish-draft`는 `wiki/` 밖에 쓰지 못한다(`tools/wiki/lib/draft.py:283`). 따라서 수집은 별도 전용 경로가 필요하다. Claude Code는 대화 파일을 기본 30일 뒤 지운다. | 결정5, 기능1 | 구현됨(`f562d77`). `tools/wiki/cli.py sessions capture`와 `session-capture` 스킬. 계약 3으로 배포했다. |
| 요청3 | 설치 단계에서 Claude Code 훅을 연결하는 선택 항목을 둔다. (1) PreCompact 또는 SessionEnd → 수집 도구. (2) SessionStart → `vg hook session-start`가 있으면 실행하고, 종료 코드 0이면 그 출력을 쓰고(빈 출력도 정상), 0이 아니면 `recall.sh`를 쓴다. | 훅이 `transcript_path`와 `source`를 넘겨준다. 지금 설치 단계에 훅이 없다. resume 처리는 vg가 `source`를 보고 정하므로 훅은 종료 코드만 본다. | 기능1, 결정10 | 구현됨(`2232c7f`, `a9fddb4`). `install --with-hooks`를 줄 때만 실행기 `vault_hooks.py`를 복사하고 `settings.json`의 SessionStart·PreCompact·SessionEnd에 항목을 하나씩 더한다. PreCompact와 SessionEnd 둘 다 수집한다(시간 제한 60초). vg가 없거나 실패하면 SessionStart는 `recall.sh`, 수집은 실행기가 직접 한다. |
| 요청4 | `publish-draft`를 다른 프로그램이 호출할 수 있는 형태로 문서화한다. 입력 draft 형식, 대상 Vault 지정 방법, 종료 코드와 오류 출력(특히 저장소 전체 lint가 이미 깨져 있을 때)을 고정한다. | vault-graph의 `remember`는 기록을 `publish-draft`에 맡긴다(결정8). 지금은 lint가 깨져 있으면 remember가 실패한다(`tools/wiki/cli.py:648-676`). | 결정8 | 구현됨(`eab420a`). `publish-draft --json`이 `vault-publish-result/v1` JSON 객체 하나를 낸다. 종료 코드 0은 published, 1은 rejected다. lint가 이미 깨져 있으면 `reason: lint-dirty-before`다. |
| 요청5 | `vault-map.json` 형식(version 2: version, vaults, projects)을 외부에서 읽는 계약으로 명시하고, 형식을 바꿀 때 버전을 올린다. | vault-graph는 자체 프로젝트 연결 파일을 버리고 `vault-map.json`만 읽는다(결정9). | 결정9 | 구현됨(`eddddb9`). 외부 계약 문서 `docs/contracts/vault-map-v2.md`. version 2 안에서 선택 필드 `default_write_vault`를 더했고, `set-role`·`set-default-write` 명령이 생겼다. |
| 요청6 | 기억 페이지 frontmatter에 스키마 버전(예: `vault-memory/v1`)을 붙이고, 두 필드를 더한다. `pin`(세션 시작 때 전문을 항상 넣을 기억), `memory_kind`(프로필, 선호, 결정, 에피소드 같은 기억 유형). `supersedes`·`superseded_by`는 이미 있으므로 그대로 쓴다. | 형식이 바뀌었는데 vault-graph가 모르면 기억을 잘못 읽는다(위험3). vault-graph 세션 시작 회상은 `pin` 기억을 고정 핵심으로, 나머지를 유형이 붙은 목록으로 넣는다(2026-10-30 결정). | 위험3, 질문10 | 구현됨(`c24d4a5`). `memory_schema: vault-memory/v1`, `pin`(불리언), `memory_kind`. `memory_kind` 값은 요청의 예시와 다르게 `rule`, `correction`, `decision`, `preference` 넷이다. 대체 없는 폐기용 status `retired`(`retired_reason`, `retired_at` 필수)도 더했다. |
| 요청7 | 정리 스킬에서 자동 기억의 확정 규칙을 정한다. 사용자가 직접 말한 사실(stated)은 검증을 통과하면 확정하고, LLM이 추론한 사실(inferred)은 검토 대기열을 거친다. | vault-graph는 검토 대기열(`scratch/review/`)을 색인하지 않으므로 승인 전에는 회상되지 않는다. vault-graph 쪽 사용자가 2026-10-30에 이 규칙을 채택했다(결정6). 실행은 정리 스킬이 맡는다. | 결정6, 기능2 | 일부 구현(`0c014bd`). 추론 기억이 거칠 검토 대기열은 생겼다. 검토 유형 `memory-proposal`, `review propose`, 승인하면 발행하는 `review resolve`다. `memory-cleanup` 스킬의 stated/inferred 확정 규칙은 바꾸지 않았다. |

## 3. 우선순위

요청1과 요청2가 먼저다. 둘이 없으면 vault-graph의 핵심 가치(세션 원문 대량 검색)가 성립하지 않는다. 요청4와 요청5는 vault-graph 1차 작업(remember, vault-map 읽기)과 같은 시기에 필요하다. 요청6과 요청7은 정리 기능을 만들 때 필요하다.

## 4. 바꾸지 않는 것

- `wiki/`에 쓰는 일은 계속 Vault 도구의 검증 경로(publish-draft 등)로만 한다.
- `raw/`의 기존 파일은 계속 불변이다.
- vault-graph는 어떤 Vault 파일도 직접 쓰지 않는다.
