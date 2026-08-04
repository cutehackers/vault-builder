# LLM Wiki

LLM Wiki는 사람과 LLM agent가 함께 쓰는 quality-gated markdown wiki입니다.

원본 자료와 durable knowledge를 분리하고, agent가 만든 draft가 vault의 품질
경계를 통과했을 때만 영구 wiki 지식으로 남도록 결정론적 검증을 사용합니다.

## 간단한 설치

아래 한 줄로 새 vault를 설치합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash
```

이 명령은 `cutehackers/vault-builder`를 내려받고, 현재 폴더 아래에 바로 사용할 수
있는 `vault/` 폴더를 만듭니다.

설치 후에는 vault를 열고 포함된 agent skill을 사용합니다.

- `wiki-ingest`: 원본 자료를 durable wiki page로 정리합니다.
- `wiki-update`: 신뢰할 수 있는 원본을 기준으로 기존 페이지를 갱신합니다.
- `wiki-query`: 컴파일된 wiki를 기준으로 질문에 답하고, 재사용할 가치가 있는
  결과를 필요한 경우 남깁니다.

Stenc 고정 포맷 spec/plan은 기본 설치에서 제외됩니다. 해당 workflow를
의도적으로 유지할 vault에서만 활성화하면 됩니다.

## 기존 Vault 마이그레이션

One Body Owner, Many References: `vault-builder`는 생성 구조, 관리 파일 계약,
마이그레이션 정책을 소유하고, Vault는 raw 증거와 durable wiki 지식을 소유합니다.
새 Vault의 `.vault-builder/manifest.json`은 운영 metadata일 뿐 지식이 아닙니다.
기존 Vault는 최신 builder checkout에서 audit한 뒤 외부 plan을 검토하고 적용합니다.

```bash
cd /path/to/vault-builder

python3 tools/vault_migration/cli.py audit \
  --target /path/to/vault \
  --plan-out /tmp/vault-migration-plan.json

python3 tools/vault_migration/cli.py apply \
  --target /path/to/vault \
  --plan /tmp/vault-migration-plan.json
```

종료 코드 `0`은 차단 항목 없음, `1`은 실패, `2`는 읽기 전용 audit 성공이지만
`review-required` 검토가 필요함을 뜻합니다. apply는 wiki 지식을 병합하거나
index/map을 재생성하지 않습니다. Vault Graph는 읽기 전용 소비자이며 Vault를
수정하거나 migration하지 않습니다.
남은 migration recovery artifact가 하나라도 있으면 모든 apply는 target write 전에
차단되고, outstanding migration별 정확한 `recover --migration-id` 명령을 출력합니다.
apply와 recover는 전체 실행 동안 하나의 target-wide process lock도 유지하므로 동시
migration 명령은 target을 쓰지 않고 실패합니다.

apply가 `Migration committed, cleanup pending`을 출력하면 version 1 파일과
manifest는 이미 commit된 상태입니다. 출력된 정확한 `recover --migration-id`
명령을 실행하세요. 이 복구는 cleanup만 완료하며 legacy bytes로 rollback하지
않습니다.

실패한 apply가 `Automatic rollback: completed; cleanup pending`을 출력하면 이전
Vault bytes 복원은 이미 끝났습니다. 출력된 `recover` 명령으로 durable preparation
또는 rollback journal 정리만 완료하세요.

## 예제 프롬프트

아래 문장을 agent에게 그대로 요청하면 됩니다. 파일 이름만 현재 vault에 맞게
바꾸세요.

```text
wiki-ingest skill을 사용해서 raw/sources/project-notes.md를 wiki에 반영해주세요.
중요한 개념, 결정사항, 후속 질문을 durable wiki page로 정리하고 검증까지 완료해주세요.
```

```text
wiki-update skill을 사용해서 wiki/concepts/product-strategy.md를 최신 원본 기준으로 업데이트해주세요.
관련 source를 확인하고, human-locked 구간은 보존하고, 모순이 있으면 review 항목으로 남겨주세요.
```

```text
wiki-query skill을 사용해서 현재 wiki 기준으로 "이번 프로젝트의 핵심 결정과 남은 리스크는 무엇인가요?"에 답해주세요.
답변에 사용한 wiki page와 source를 함께 알려주고, 재사용할 가치가 있으면 wiki에 남길 draft도 제안해주세요.
```

## 동작 방식

vault는 원본 자료와 검증된 지식을 분리합니다.

```text
raw source
-> LLM semantic draft
-> deterministic validation
-> durable wiki page
-> index, links, logs, reports
```

핵심 규칙은 단순합니다. LLM이 만든 내용은 검증을 통과하기 전까지 제안일
뿐입니다.

## 꼭 알아둘 폴더

| 경로               | 의미                                                                   |
| ------------------ | ---------------------------------------------------------------------- |
| `raw/`             | 원본 자료입니다. 명시적으로 요청하지 않는 한 agent가 고치면 안 됩니다. |
| `wiki/`            | 검증을 통과한 durable compiled knowledge입니다.                        |
| `scratch/drafts/`  | 게시되기 전의 wiki 변경 제안입니다.                                    |
| `scratch/reports/` | 검증, 유지보수, ingest, query report가 남는 곳입니다.                  |
| `scratch/review/`  | 사람의 판단이 필요한 항목을 모아두는 곳입니다.                         |
| `.agents/skills/`   | ingest, update, query를 위한 사용자-facing workflow입니다.             |
| `tools/wiki/`      | skill이 사용하는 검증 및 게시 내부 구현입니다.                         |
| `docs/agent/`      | agent를 위한 상세 운영 규칙입니다.                                     |

## 기억할 원칙

- LLM이 만든 내용은 검증을 통과하기 전까지 제안일 뿐입니다.
- 중요한 claim은 원본 자료나 기존 durable wiki page에 근거해야 합니다.
- `human-locked` page와 section은 보존해야 합니다.
- 모순은 조용히 해결하지 말고 기록해야 합니다.
- 완료 여부는 `AGENTS.md`에 정의된 결정론적 wiki 검사 명령으로 확인합니다. 이
  검사는 wiki 작업이 완료되었다고 판단하기 전에 agent와 CI가 실행합니다.
- 사용자는 skill을 공개 인터페이스로 사용하고, 결정론적 도구는 그 뒤의 구현
  계층으로 보면 됩니다.

## 더 자세한 문서

- [docs/usage.md](docs/usage.md) - 한국어 사용 가이드.
- [docs/LLM-WIKI.md](docs/LLM-WIKI.md) - canonical architecture와 vault 구조.
- [docs/USER_PROMPTS.md](docs/USER_PROMPTS.md) - 일반 사용자를 위한 LLM Wiki 활용 프롬프트.
- [docs/VAULT_PROMPTS.md](docs/VAULT_PROMPTS.md) - vault 운영과 유지보수용 프롬프트.
- [docs/agent/OPERATING-SCHEMA.md](docs/agent/OPERATING-SCHEMA.md) - 상세 agent
  운영 계약.

---

English: [README.md](README.md)
