# LLM Wiki

LLM Wiki는 사람과 LLM agent가 함께 쓰는 quality-gated markdown wiki입니다.

원본 자료와 durable knowledge를 분리하고, agent가 만든 draft가 vault의 품질
경계를 통과했을 때만 영구 wiki 지식으로 남도록 결정론적 검증을 사용합니다.

## 설치

Claude Code를 종료한 뒤 아래 명령 한 줄을 실행합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash
```

이 명령은 git과 Python 3.10 이상이 있는지 확인하고, builder checkout을
`$HOME/.local/share/vault-builder`에 받고, `$HOME/.local/bin/vault-builder`
명령을 만든 뒤 `vault-builder setup`을 실행합니다. setup은 Vault를 만들고, Vault
hook을 설치하고, 저장소를 연결하고, Claude Code에 `vault-wiki` MCP 서버를 등록한 뒤
`vault-builder doctor`로 검사합니다. 다른 도구는 설치하지 않습니다. 질문마다
Enter를 누르면 개인 Vault가 `$HOME/vault`에 생기고, 현재 폴더가 git 저장소이면
그 저장소의 project Vault가 `$HOME/vaults/<저장소 폴더 이름>`에 생깁니다.
이전 vault-builder로 만든 Vault는 같은 실행에서 업그레이드합니다. wiki 페이지와
raw source는 그대로 남습니다. vault-builder가 관리하는 파일을 직접 고쳤으면
setup은 그 파일 목록을 출력하고 아무것도 쓰기 전에 멈춥니다. Claude
Code가 실행 중이면 이 명령은 `$HOME/.claude.json`을 쓰기 전에 멈춥니다.

명령이 끝나면 다음을 합니다.

1. Claude Code를 다시 시작합니다.
2. Claude Code에서 `/mcp`를 실행하고 `vault-wiki`가 연결되어 있는지 확인합니다.

다음 저장소는 아래 명령으로 연결합니다.

```bash
vault-builder link --repo /path/to/other-repo
```

Vault 이름을 주지 않으면 `link`는 공유 Vault 전부(역할 `personal`, `team`, 알려진
역할 표에 없는 역할)를 연결합니다. project Vault는 이름으로 연결합니다:
`vault-builder link <이름> --repo /path/to/other-repo`. 나중에 Vault를 더할 때는
`vault-builder setup --vault team="$HOME/team-vault" --role team=team --repo /path/to/app`처럼
새 Vault만 넘깁니다.

다른 옵션은 `bash -s --` 뒤에 붙입니다. 예를 들어 `--repo /path/to/app --yes`를
붙이면 질문 없이 실행합니다. `--help`는 모든 옵션과 환경 변수를 보여 줍니다.
`--uninstall`은 모든 Vault의 builder 파일, hook, `vault-wiki` MCP 서버,
`vault-builder` 명령, builder checkout을 지웁니다. 각 Vault의 `raw/`와 `wiki/`는
남습니다. 자세한 내용은
[docs/usage.md](docs/usage.md) 1절과 17절을 봅니다.

## Vault 폴더만 만들기

hook, 저장소 연결, MCP 서버 없이 Vault 폴더만 만들려면 `bootstrap.sh`를
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
- `session-capture`: 요청하면 지금 agent 세션을 project Vault에 보관합니다.

Stenc 고정 포맷 spec/plan은 기본 설치에서 제외됩니다. 해당 workflow를
의도적으로 유지할 vault에서만 활성화하면 됩니다.

## 기능

아래 기능은 모두 같은 네 항목으로 설명합니다. 하는 일, 실행 시점, 명령, 쓰는
위치입니다. `<설정 폴더>`는 환경 변수 `CLAUDE_CONFIG_DIR` 값이고, 값이 없으면
`$HOME/.claude`입니다. `vault-builder` 명령은 어느 폴더에서나 실행합니다.
`python3 tools/wiki/cli.py` 명령은 Vault 폴더 안에서 실행합니다. 자세한 동작은
[docs/usage.md](docs/usage.md) 16절과 17절에 있습니다.

### setup과 doctor

- 하는 일: `setup`은 Vault를 모두 만들거나 업그레이드하고, hook을 설치하고,
  저장소를 연결하고, MCP 서버를 등록한 뒤 `doctor`를 실행합니다. `doctor`는
  Vault, MCP 서버 항목, 라우터, hook, `vault-builder` 명령을 검사하고 문제마다
  고치는 명령 하나를 출력합니다.
- 실행 시점: 수동입니다. 설치 명령이 `setup`을 한 번 실행합니다. `setup`은
  `$HOME/.claude.json`을 쓰므로 Claude Code를 종료한 뒤 실행합니다.
- 명령: `vault-builder setup`, `vault-builder doctor`
- 쓰는 위치: `setup`은 Vault 폴더, `<설정 폴더>/vault-builder/`,
  `<설정 폴더>/settings.json`의 hook 항목, `$HOME/.claude.json`의
  `mcpServers.vault-wiki`를 씁니다. `doctor`는 아무것도 쓰지 않고, 문제를 찾으면
  exit 1로 끝납니다.

### Vault 역할

- 하는 일: Vault마다 역할이 있습니다. `tools/vault_migration/roles.py`가 아는
  역할은 세 개입니다. `personal`은 한 사람의 note이고 `team`은 팀이 함께 쓰는
  지식입니다. 둘 다 공유 Vault라서 이름 없는 `link`가 모든 저장소에 연결합니다.
  `project`는 저장소 하나의 이력과 세션 원문을 담고, 이름을 줄 때만 연결됩니다.
  표에 없는 역할도 받고, 그런 Vault는 공유 Vault로 다룹니다.
- 실행 시점: 수동
- 명령: `vault-builder set-role --vault <이름> --role <역할>`
- 쓰는 위치: `<설정 폴더>/vault-builder/vault-map.json`의 `vaults.<이름>.role`

### 저장소 연결

- 하는 일: 저장소가 어느 Vault를 쓰는지 적습니다. 저장소는 root 경로, `origin`
  remote URL, 폴더 이름 순서로 찾습니다. `set-default-write`는 한 project에서
  vault-graph가 기억을 쓸 Vault를 정합니다. `set-sharable`은 project 두 개
  이상에 연결된 Vault가 `project` 필드 없는 페이지를 모든 project와 나눌지
  정합니다. 이 두 값은 vault-graph가 읽고, MCP 라우터는 읽지 않습니다.
- 실행 시점: 수동입니다. `setup`도 저장소마다 `link`를 실행합니다.
- 명령: `vault-builder link [<vault> ...] --repo <경로>`,
  `vault-builder unlink <vault> --repo <경로>`, `vault-builder list`,
  `vault-builder set-default-write --project <id> (--vault <이름> | --clear)`,
  `vault-builder set-sharable --vault <이름> (--value true|false | --clear)`
- 쓰는 위치: `<설정 폴더>/vault-builder/vault-map.json`만 씁니다. `list`는
  아무것도 쓰지 않습니다.

### 세션 hook

- 하는 일: hook 실행기 `<설정 폴더>/vault-builder/vault_hooks.py`가 Claude Code
  이벤트 세 개에서 실행됩니다. SessionStart에서 `session-start`는
  `vg hook session-start`를 실행하고, exit 0이면 그 출력을 세션에 넣습니다. vg가
  없거나 실패하면 아무것도 넣지 않습니다. PreCompact와 SessionEnd에서 `capture`는
  `vg hook capture`를 실행합니다. vg가 없거나 실패하면 `capture --direct`로
  넘어가서, 세션 저장소에 연결된 `project` Vault 하나의 `sessions capture`를
  실행합니다. hook은 세션을 막지 않습니다.
- 실행 시점: 자동입니다. `setup` 또는 `vault-builder install <경로> --with-hooks`
  뒤부터 실행됩니다.
- 명령: 직접 실행할 명령은 없습니다. vg 경로는 `VAULT_GRAPH_BIN`,
  `<설정 폴더>/vault-builder/vault-hooks.json`, `PATH` 순서로 찾습니다.
- 쓰는 위치: 실행기는 직접 쓰지 않습니다. 수집은 `sessions capture`(다음 절)로
  씁니다.

### 세션 수집

- 하는 일: 세션 대화 원문(사용자 발화, agent 답변, 서브에이전트 대화, 도구
  이름)을 새 part 파일로 보관합니다. thinking 블록과 도구 입력·출력은 남기지
  않습니다. 알려진 형식의 비밀값은 `[REDACTED:<종류>]`로 바꿉니다. 같은 세션을
  다시 수집하면 새 턴만 다음 part 파일로 씁니다.
- 실행 시점: PreCompact와 SessionEnd hook에서 자동으로 실행되고,
  `session-capture` skill로 수동 실행할 수도 있습니다.
- 명령: `python3 tools/wiki/cli.py sessions capture --transcript <경로>`.
  `sessions status`는 파일 수와 commit하지 않은 파일 수를 보여 줍니다.
  `sessions git-check`는 commit된 세션 파일이 바뀌거나 지워졌으면 실패합니다.
- 쓰는 위치: 저장소에 연결된 `project` Vault 하나의
  `raw/sessions/<agent>/<YYYY-MM-DD>-<session id>-part<N>.md`입니다. `personal`이나
  `team` Vault에는 쓰지 않고, 기존 파일을 고치지 않고, commit하지 않습니다.
  저장소에 `project` Vault가 없거나 두 개 이상이면 쓰지 않습니다.

### 세션 사실 첨부

- 하는 일: 세션 part 파일 하나 옆에 사실 첨부를 더합니다. 쓰기 전에 사실 JSON을
  part 파일과 대조합니다(hash, session id, 교환 id, 날짜, 길이, 비밀값 규칙).
- 실행 시점: 수동입니다. vault-graph가 세션에서 사실을 뽑은 뒤 이 명령을 부릅니다.
- 명령: `python3 tools/wiki/cli.py sessions attach-facts --session <part> --facts <json>`
- 쓰는 위치: `raw/sessions/<agent>/<part 이름에서 .md를 뺀 것>.facts.md`입니다.
  덮어쓰지 않습니다. 첨부가 이미 있으면 `exists`를 냅니다. 검사가 실패하면
  `rejected`와 이유 코드를 내고 exit 1로 끝나며 파일을 쓰지 않습니다.

### 기억 검토

- 하는 일: agent가 추론한 기억은 사람이 승인할 때까지 wiki에 넣지 않습니다.
  `review propose`는 draft를 검사하고 pending `memory-proposal` 항목으로 둡니다.
  `review resolve --status accepted`는 그 draft를 발행합니다. `--status rejected`나
  `--status resolved`는 wiki 페이지를 바꾸지 않고 항목만 닫습니다.
- 실행 시점: `review propose`는 vault-graph나 agent가 추론한 기억을 저장할 때
  실행합니다. `review list`와 `review resolve`는 사람이 실행합니다.
- 명령: `python3 tools/wiki/cli.py review propose --draft <draft> --summary <요약>`,
  `python3 tools/wiki/cli.py review list`,
  `python3 tools/wiki/cli.py review resolve <항목> --status accepted|rejected|resolved --resolution <이유>`
- 쓰는 위치: `propose`는 `scratch/review/`에 항목과 draft 사본을 씁니다.
  `resolve`는 항목을 고치고 `wiki/log.md`에 한 줄을 더합니다. wiki 페이지는
  `accepted`일 때만 씁니다.

### MCP 라우터

- 하는 일: MCP 서버 `vault-wiki` 하나가 모든 Vault를 다룹니다. 이 서버는 라우터
  `<설정 폴더>/vault-builder/llm_wiki_router.py`를 실행합니다. 라우터는 도구
  호출마다 `vault-map.json`을 다시 읽고 대상 Vault의 `tools/wiki/cli.py`를
  실행합니다. 읽기는 세션 저장소에 연결된 Vault나 세션 폴더인 Vault만 합니다.
  쓰기는 `vault` 인자가 꼭 있어야 하고 `vault-map.json`의 어떤 Vault에도 쓸 수
  있습니다. 경로 인자는 그 Vault 안의 상대 경로여야 합니다.
- 실행 시점: 자동입니다. Claude Code가 서버를 시작하고, 도구 호출
  (`mcp__vault-wiki__<도구>`)마다 라우터가 한 번 실행됩니다.
- 명령: 직접 실행할 명령은 없습니다. `setup`이 등록하고, `/mcp`로 `vault-wiki` 연결을 확인합니다.
- 쓰는 위치: 라우터는 직접 쓰지 않습니다. 쓰기 도구는 대상 Vault의 CLI와
  `publish-draft` 검사를 거쳐 그 Vault를 바꿉니다.

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
| `raw/sessions/`    | 세션 원문과 사실 첨부입니다. 추가만 하고, project Vault에만 생깁니다.  |
| `wiki/`            | 검증을 통과한 durable compiled knowledge입니다.                        |
| `scratch/drafts/`  | 게시되기 전의 wiki 변경 제안입니다.                                    |
| `scratch/reports/` | 검증, 유지보수, ingest, query report가 남는 곳입니다.                  |
| `scratch/review/`  | 사람의 판단이 필요한 항목을 모아두는 곳입니다.                         |
| `.agents/skills/`  | ingest, update, query, 세션 보관을 위한 사용자-facing workflow입니다.  |
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
