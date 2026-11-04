# LLM Wiki

LLM Wiki는 사람과 LLM agent가 함께 쓰는 quality-gated markdown wiki입니다.

원본 자료와 durable knowledge를 분리하고, agent가 만든 draft가 vault의 품질
경계를 통과했을 때만 영구 wiki 지식으로 남도록 결정론적 검증을 사용합니다.

## 설치

Claude Code를 종료한 뒤 아래 명령 한 줄을 실행합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash
```

이 주소는 `scripts/install.sh`를 더한 변경이 `main` branch에 merge된 뒤부터
동작합니다. 그 전에는 GitHub가 404를 돌려줍니다.

이 명령은 git, curl, Python 3.10 이상이 있는지 확인하고, builder checkout을
`$HOME/.local/share/vault-builder`에 받고, `$HOME/.local/bin/vault-builder`
명령을 만든 뒤 `vault-builder setup`을 실행합니다. setup은 `vg`(Vault Graph)를
설치하고, Vault를 만들고, Vault hook을 설치하고, 저장소를 연결하고, Claude Code에
`llm-wiki`와 `vault-graph` MCP 서버를 등록하고, 첫 색인을 만든 뒤
`vault-builder doctor`로 검사합니다. uv가 없으면 설치할지 먼저 묻습니다. 질문마다
Enter를 누르면 개인 Vault가 `$HOME/vault`에 생기고, 현재 폴더가 git 저장소이면
그 저장소의 project Vault가 `$HOME/vaults/<저장소 폴더 이름>`에 생깁니다. Claude
Code가 실행 중이면 이 명령은 `$HOME/.claude.json`을 쓰기 전에 멈춥니다.

명령이 끝나면 다음을 합니다.

1. Claude Code를 다시 시작합니다.
2. Claude Code에서 `/mcp`를 실행하고 `llm-wiki`와 `vault-graph`가 연결되어
   있는지 확인합니다.

다음 저장소는 아래 명령으로 연결합니다.

```bash
vault-builder link --repo /path/to/other-repo
```

Vault 이름을 주지 않으면 `link`는 공유 Vault 전부(역할 `personal`, `team`, 알려진
역할 표에 없는 역할)를 연결합니다. project Vault는 이름으로 연결합니다:
`vault-builder link <이름> --repo /path/to/other-repo`. 나중에 Vault를 더할 때는
`vault-builder setup --vault team="$HOME/team-vault" --role team=team --repo /path/to/app`처럼
새 Vault만 넘깁니다.

Vault Graph를 설치하려면 `github.com/cutehackers/vault-graph` 저장소의 SSH 읽기
권한이 필요합니다. 권한이 없으면 vault-builder만 설치합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash -s -- --without-vault-graph
```

이 명령은 `vg`와 `vault-graph` 서버 없이 Vault, hook, 저장소 연결, `llm-wiki` MCP
서버만 설치합니다. 나중에 같은 명령을 `--without-vault-graph` 없이 다시 실행하면
Vault Graph가 더해집니다.

다른 옵션은 `bash -s --` 뒤에 붙입니다. 예를 들어 `--repo /path/to/app --yes`를
붙이면 질문 없이 실행합니다. `--help`는 모든 옵션과 환경 변수를 보여 줍니다.
`--uninstall`은 모든 Vault의 builder 파일, hook, 두 MCP 서버, `vg`,
`vault-builder` 명령, builder checkout을 지웁니다. 각 Vault의 `raw/`와 `wiki/`,
그리고 `$HOME/.vault-graph`는 남습니다. 자세한 내용은
[docs/usage.md](docs/usage.md) 1절과 17절을 봅니다.

## Vault 폴더만 만들기

`vg`, hook, 저장소 연결, MCP 서버 없이 Vault 폴더만 만들려면 `bootstrap.sh`를
씁니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash
```

이 명령은 `cutehackers/vault-builder`를 내려받고, 현재 폴더 아래에 바로 사용할 수
있는 `vault/` 폴더를 만듭니다.

## Vault 사용하기

vault를 열고 포함된 agent skill을 사용합니다.

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
- 완료 여부는 `scripts/release_gate.sh` gate로 확인합니다. 이 gate는 wiki 작업이
  완료되었다고 판단하기 전에 agent와 CI가 처리합니다.
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
