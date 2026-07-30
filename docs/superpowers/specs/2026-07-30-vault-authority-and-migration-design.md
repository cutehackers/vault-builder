# Vault Authority And Migration Design

작성일: 2026-07-30

상태: 승인됨

## 1. 목적

Vault의 정보 중복 문제를 사용자별 Vault에서 직접 고치지 않는다.
`vault-builder`가 생성 구조, 정보 역할, 검증 규칙, 기존 Vault의 안전한
업그레이드 경로를 소유한다.

이 설계는 Vault Graph의 코드 인덱스와 하네스 확장보다 먼저 완료해야 하는
`Round 0-VB — Vault Authority Contract`를 정의한다. 후속
`Round 0-G — Vault Graph Projection Hygiene`는 이 계약을 읽기 전용으로
소비한다.

핵심 원칙은 다음과 같다.

> 하나의 의미 본문은 권한 계층에서 한 번만 소유하고, 나머지 파일과
> 파생 시스템은 안정된 경로와 provenance 관계로 그 본문을 참조한다.

## 2. 문제

`vault-builder`는 `init-vault.sh`를 통해 문서, 도구, 스키마, 테스트, 초기
wiki 구조를 새 Vault에 복사한다. 생성된 Vault는 그 이후 독립 저장소가
되므로 `vault-builder`를 수정해도 기존 Vault는 자동으로 갱신되지 않는다.

현재 구조에는 다음 위험이 있다.

- source page가 raw 원문을 다시 서술하며 두 번째 본문처럼 커질 수 있다.
- 동일 주제의 durable wiki 본문이 여러 페이지에 반복될 수 있다.
- index, maps, log, reports가 durable knowledge와 같은 검색 후보로 취급될
  수 있다.
- 빌더 관리 파일과 사용자 수정 파일을 구분할 버전 및 해시 기록이 없다.
- 기존 Vault를 갱신하는 공식적인 audit와 migration 경로가 없다.

구조 문제를 개별 Vault에서 수동 수정하면 새로운 Vault에서 문제가
반복되고 사용자별 구조가 서로 달라진다. 반대로 빌더 파일을 일괄
덮어쓰면 사용자 지식과 로컬 운영 변경을 손상할 수 있다.

## 3. 범위

### 포함

- Vault 정보 역할과 본문 소유권 계약
- 신규 Vault에 적용할 생성 계약 버전과 관리 파일 manifest
- manifest가 없는 기존 Vault를 위한 읽기 전용 audit
- 명시적으로 승인된 비파괴 migration
- 빌더 관리 파일의 변경 여부 판정
- dry-run, 충돌 보고, 실패 복구, 적용 후 검증
- 향후 Vault Graph가 소비할 역할 및 provenance 신호

### 제외

- raw 원문 자동 수정, 이동, 병합, 이름 변경, 삭제
- 사용자 wiki 본문의 자동 재작성 또는 중복 페이지 자동 병합
- 사용자가 수정한 빌더 파일의 자동 덮어쓰기
- scratch report의 자동 삭제 또는 장기 보존 정책 강제
- Vault Graph의 저장소, FTS, vector, graph, retrieval 변경
- Vault Graph가 Vault migration을 실행하는 기능
- 코드 인덱스 또는 코딩 하네스 통합

## 4. 저장소 책임

### vault-builder

`vault-builder`는 다음 항목의 유일한 설계 소유자다.

- 새 Vault의 디렉터리와 기본 파일 구조
- frontmatter와 provenance 계약
- source page, generated view, log, report의 역할 규칙
- 결정적 검증 도구와 release gate
- 빌더 관리 파일 목록과 생성 계약 버전
- 기존 Vault audit와 migration 도구

### 생성된 Vault

생성된 Vault는 사용자의 durable knowledge 저장소다.

- raw 원본과 durable wiki 본문을 소유한다.
- 빌더 계약을 복사해 로컬에서 검증할 수 있다.
- 사용자 변경과 Git history를 독립적으로 소유한다.
- 빌더가 명시적인 migration 없이 파일을 원격으로 변경하지 않는다.

### vault-graph

Vault Graph는 읽기 전용 projection이다.

- Vault의 파일 역할과 provenance를 읽는다.
- 빌더 manifest의 계약 버전을 상태 진단에 사용할 수 있다.
- Vault 파일이나 manifest를 생성, 수정, migration하지 않는다.
- 중복 후보를 보고할 수 있지만 durable merge를 결정하지 않는다.

## 5. 정보 역할과 본문 소유권

역할 판정은 우선 path, 기존 frontmatter `type`, provenance 필드를 사용한다.
모든 문서에 새로운 `source_role` 필드를 일괄 추가하지 않는다. 기존 신호로
역할이 모호한 사례가 실제로 확인될 때만 명시적 필드를 후속 계약으로
추가한다.

| 역할 | 예시 | 본문 소유 | 자동 migration |
| --- | --- | ---: | ---: |
| 원본 증거 | `raw/` | 예 | 금지 |
| 지속 지식 | canonical `wiki/` page | 예 | 금지 |
| 출처 manifest | `wiki/sources/` | 아니오 | 본문 변경 금지 |
| 생성 탐색 뷰 | `wiki/index.md`, `wiki/maps/` | 아니오 | 별도 재생성 |
| 운영 이력 | `wiki/log.md` | event만 | 변경 금지 |
| 임시 감사 산출물 | `scratch/reports/` | 제한적 | 변경·삭제 금지 |
| 빌더 운영 metadata | `.vault-builder/manifest.json` | 지식 아님 | 도구가 관리 |
| 빌더 도구·계약 | `tools/`, `scripts/`, 기본 운영 문서 | 지식 아님 | 안전 판정 후 가능 |

### 원본 증거

- `raw/`는 수집 당시 원문을 보존한다.
- 오류가 있어도 migration이 원문을 고치지 않는다.
- 수정된 해시나 source drift는 보고 대상으로만 남긴다.

### 지속 지식

- 재사용되는 한 주제의 상세 본문은 하나의 canonical wiki page가 소유한다.
- 다른 durable page는 전체 설명을 복사하지 않고 링크, 짧은 요약, 관계를
  사용한다.
- 중복 후보는 review item으로 보고하며 자동 병합하지 않는다.

### 출처 manifest

- `wiki/sources/`는 raw 본문의 복사본이 아니다.
- canonical path, raw hash, source kind, 등록 상태, 짧은 indexable summary를
  유지한다.
- 상세한 source takeaways가 필요하면 별도 canonical wiki page로 승격하고
  `derived_from`으로 연결한다.

### 생성 탐색 뷰

- `wiki/index.md`와 `wiki/maps/`는 frontmatter, 링크, registry state에서
  결정적으로 재생성할 수 있어야 한다.
- 렌더링된 제목과 짧은 summary는 탐색 projection이며 별도 durable claim이
  아니다.
- migration은 이 파일을 조용히 다시 쓰지 않고 재생성 계획을 제시한다.

### 운영 이력과 보고서

- `wiki/log.md`는 event type, 대상, revision, 결과와 링크를 기록한다.
- durable 본문을 log entry에 복사하지 않는다.
- `scratch/reports/`는 당시의 검사 증거이며 기본 지식 후보가 아니다.
- report의 보존과 승격은 사용자가 결정한다.

## 6. 버전형 생성 계약

새 Vault에는 `.vault-builder/manifest.json`을 생성한다. 이 파일은 지식
본문을 저장하지 않고 다음 운영 metadata만 보관한다.

```json
{
  "contract_version": 1,
  "builder_revision": "<git revision or release>",
  "generated_at": "YYYY-MM-DDTHH:MM:SSZ",
  "managed_files": {
    "tools/wiki/cli.py": {
      "sha256": "<generated file hash>",
      "policy": "replace-if-unmodified"
    }
  }
}
```

`managed_files`에는 업그레이드 가능한 빌더 파일만 포함한다. 다음 경로는
manifest의 교체 대상에 넣지 않는다.

- `raw/**`
- 사용자 durable wiki page
- `wiki/log.md`
- `scratch/**`
- 프로젝트별 문서와 사용자 추가 파일

contract version 1의 허용 범위는 해당 catalog에 기록된 finite exact path
set이다. `tools/wiki/` prefix만으로 새 경로를 허용하지 않으며, catalog 생성과
Vault-side shared manifest validator는 같은 exact set을 사용한다.

초기 legacy migration을 위해 구현 시점의 기존 `main` 생성 결과를
contract version 0 baseline catalog로 고정한다. manifest가 없는 Vault라도
대상 파일의 해시가 version 0의 알려진 해시와 일치하면 수정되지 않은
빌더 파일로 판정할 수 있다.

이후 contract catalog는 버전별 관리 파일 해시와 migration 가능 범위를
보존한다. catalog는 지식 내용의 버전이 아니라 빌더가 관리하는 운영 파일의
호환성 계약이다.

## 7. Migration 인터페이스와 흐름

migration은 기존 Vault에 들어 있는 오래된 CLI가 아니라 최신
`vault-builder` checkout에서 실행한다. 기존 도구가 스스로를 먼저
업그레이드해야 하는 순환 의존을 피하기 위함이다.

공개 흐름은 최신 `vault-builder` checkout에서 실행하는 두 단계다.

```bash
python3 tools/vault_migration/cli.py audit \
  --target /path/to/vault \
  --plan-out /path/outside/vault/migration-plan.json

python3 tools/vault_migration/cli.py apply \
  --target /path/to/vault \
  --plan /path/outside/vault/migration-plan.json
```

`audit`는 plan을 표준 출력에 렌더링한다. `--plan-out`은 선택 사항이며
target Vault 밖의 경로만 허용한다. `apply`는 audit가 생성한 JSON plan을
필수 입력으로 받는다.

`--plan-out`과 `--report`는 한 실행에서 함께 사용할 수 없다. 서로 다른 두
경로를 이식 가능하게 원자 교체한다고 가정하지 않고, 한 audit 실행은
소비 가능한 산출물 하나만 게시한다. 이 산출물은 create-only다. 완전히
기록하고 fsync한 임시 파일을 no-overwrite hard link로 게시하며, 최종 경로가
이미 있으면 덮어쓰지 않고 새 경로를 요구한다. hard stop은 최종 경로를
없거나 완전한 파일로만 남긴다. 현재 실행이 만든 임시 파일만 정리하며,
이름 모양만 같은 소유 불명의 sibling은 삭제하지 않는다.
`scratch/reports/`는 migration 대상 상태가 아니므로 report의 추가나 내용
변경은 plan action, target fingerprint, migration ID를 바꾸지 않는다.
migration 관리 파일, canonical wiki, raw, manifest처럼 실제 적용 판단에
필요한 상태는 계속 fingerprint와 apply 재검증 대상이다.

1. audit가 target topology와 contract version을 확인한다.
2. 모든 후보 파일의 현재 해시와 알려진 catalog 해시를 비교한다.
3. 변경 행동이 담긴 deterministic migration plan을 만든다.
4. 기본 실행은 plan 출력만 하며 target에 쓰지 않는다.
5. apply는 audit가 만든 동일 plan과 target precondition을 다시 확인한다.
6. 변경 가능한 빌더 파일만 staging 후 원자적으로 교체한다.
7. generated view는 별도 rebuild 단계로 남기고 자동 실행하지 않는다.
8. target의 lint, health, release gate를 실행한다.
9. 검증 실패 시 적용된 관리 파일을 원래 bytes로 복구한다.
10. 성공한 경우 새 manifest와 migration 결과를 기록한다.

optional report 출력은 사용자가 명시한 경우에만 `scratch/reports/` 아래에
쓴다. report 옵션이 없는 dry-run은 target에 어떠한 파일도 만들지 않는다.

## 8. 파일 판정 모델

audit는 각 후보를 다음 행동 중 하나로 분류한다.

| 행동 | 조건 | 결과 |
| --- | --- | --- |
| `unchanged` | target hash가 목표 hash와 같음 | 변경 없음 |
| `create` | 빌더 관리 파일이 target에 없음 | apply 시 생성 가능 |
| `replace` | 현재 hash가 manifest 또는 legacy catalog의 알려진 이전 hash와 일치 | apply 시 교체 가능 |
| `preserve` | 사용자 소유 경로 또는 migration 금지 역할 | 변경 없음 |
| `review-required` | 관리 후보지만 hash가 알려지지 않았거나 사용자가 수정함 | 자동 변경 금지 |
| `rebuild-suggested` | generated view가 새 계약과 맞지 않음 | 별도 명령 제시 |
| `invalid` | Vault topology, plan precondition, manifest가 손상됨 | apply 중단 |

판정 우선순위는 `preserve`가 가장 높다. 사용자 소유 경로는 해시가
빌더 파일과 우연히 같아도 교체 후보가 되지 않는다.

apply 직전에는 audit 이후 파일이 변경되지 않았는지 precondition hash를
다시 확인한다. 하나라도 달라지면 전체 apply를 중단하고 새 audit를
요구한다.

## 9. 안전성과 복구

- target 경로가 Vault root인지 확인하지 못하면 중단한다.
- symlink를 따라 Vault 밖의 파일을 쓰지 않는다.
- path traversal이 포함된 catalog나 plan을 거부한다.
- migration plan은 target 절대 경로 대신 Vault-relative path를 기록한다.
- apply 대상 원본 bytes와 행동 journal을 target의
  `.vault-builder/recovery/<migration-id>/`에 보존한다.
- 각 기존 파일은 지원되는 POSIX 플랫폼의 native atomic exchange로
  교체하고, 교체 경계에서 밀려난 파일 identity가 audit 상태와 같은지
  확인한다. 없는 파일은 no-overwrite hard link로 게시한다. 지원되지 않는
  플랫폼에서는 안전하지 않은 fallback 대신 apply를 중단한다.
- source와 destination parent directory descriptor를 validation부터
  exchange, fsync까지 유지하여 parent symlink 교체가 쓰기 대상을 바꾸지
  못하게 한다. descriptor는 root부터 각 component를 no-follow로 열며,
  사용자 경로 전체를 다시 resolve하지 않는다.
- exchange 직후 중단되어 밀려난 파일이 durable staging에 남으면 recovery가
  cleanup 전에 그 내용을 판별한다. audit 상태가 아닌 외부 변경이면 원래
  leaf로 되돌리고 journal을 보존한다.
- 원래 없던 leaf를 rollback할 때는 검증 후 경로 unlink를 하지 않는다.
  native no-replace rename으로 journal quarantine에 옮긴 뒤 내용을 확인하여
  마지막 경계의 외부 변경을 보존한다.
- 프로세스 오류나 검증 실패 시 journal을 사용해 변경 파일을 복구한다.
- 성공한 migration은 recovery journal을 제거하고 결과 metadata만
  manifest에 기록한다.
- 실패 중 자동 복구를 완료하지 못하면 journal을 보존하고 수동 복구 명령을
  출력한다.
- 복구가 끝나기 전에는 manifest의 contract version을 올리지 않는다.
- `review-required`가 존재해도 안전한 파일만 부분 적용하지 않는다.
  사용자가 새 plan을 승인할 수 있도록 전체 apply를 중단한다.
- migration 도구는 네트워크 연결 없이 동작한다.

## 10. 오류 처리

다음 경우에는 target을 변경하지 않고 명확한 오류를 반환한다.

- target이 없거나 Vault topology가 아님
- manifest JSON 또는 catalog schema가 유효하지 않음
- 알려지지 않은 contract version
- audit 이후 target 파일이 변경됨
- 사용자 소유 경로가 plan의 쓰기 대상에 포함됨
- 쓰기 대상이 symlink이거나 Vault root 밖으로 해석됨
- staging 또는 atomic replace 실패
- lint, health, release gate 실패
- rollback 완료 여부를 확인할 수 없음

검증 실패 시 출력은 실패한 명령, 종료 코드, 복구 결과, 다시 audit하는
방법을 포함한다. 실패한 migration을 성공으로 manifest에 기록하지 않는다.

## 11. 테스트 전략

### 생성 계약

- 새 Vault에 최신 contract version과 manifest가 생성된다.
- manifest에는 허용된 managed path만 포함된다.
- 생성된 Vault의 lint, health, release gate가 통과한다.

### Audit

- audit 기본 실행은 target bytes와 파일 목록을 변경하지 않는다.
- version 0과 일치하는 legacy 파일을 `replace`로 판정한다.
- 사용자가 수정한 관리 파일을 `review-required`로 판정한다.
- raw, wiki body, log, scratch를 항상 `preserve`로 판정한다.
- 손상된 manifest와 Vault 밖 경로를 `invalid`로 거부한다.

### Apply

- 승인된 plan만 적용한다.
- audit 후 파일 변경을 감지하면 적용 전에 중단한다.
- 사용자 소유 파일은 적용 전후 hash가 동일하다.
- `review-required`가 포함된 plan은 아무 파일도 부분 적용하지 않는다.
- 중간 쓰기 실패와 release gate 실패에서 원본 bytes가 복구된다.
- 성공 후 manifest가 새 contract version과 실제 관리 파일 hash를
  기록한다.

### 호환성

- 신규 Vault와 legacy Vault 모두 동일한 정보 역할 검증을 받는다.
- generated view rebuild는 명시적 단계이며 migration apply의 숨은
  side effect가 아니다.
- Vault Graph 없이도 생성, audit, migration, 검증을 완료할 수 있다.

## 12. 완료 조건

Round 0-VB는 다음 조건을 모두 만족할 때 완료된다.

- 정보 역할과 본문 소유권이 문서와 결정적 검증에 반영된다.
- 신규 Vault가 versioned manifest를 가진다.
- legacy Vault의 변경되지 않은 빌더 파일과 사용자 수정 파일을 구분한다.
- audit는 기본적으로 완전한 read-only다.
- apply는 명시적 승인, precondition 확인, atomic replace, rollback을
  제공한다.
- raw와 사용자 wiki 본문의 적용 전후 hash가 동일하다.
- source page는 raw 본문을 소유하지 않는다.
- index, maps, log, reports는 durable knowledge와 구분된다.
- 신규 및 migration된 fixture가 전체 release gate를 통과한다.
- 실제 사용자 Vault 적용은 별도 승인과 audit 결과 검토 후 수행한다.

## 13. Round 0-G로의 인계

Round 0-VB가 완료되면 Vault Graph는 다음 안정된 입력을 사용할 수 있다.

- path와 frontmatter 기반 정보 역할
- `canonical_source`, `derived_from`, lifecycle 관계
- generated view와 audit report의 비권위 표시
- Vault 생성 계약 버전
- canonical wiki 중심의 기본 검색 정책

Vault Graph는 이 입력을 이용해 single-body projection과 provenance-family
검색 결과 축약을 구현한다. Vault Graph 구현은 Round 0-VB를 우회해 자체
Vault schema나 migration 권한을 만들지 않는다.
