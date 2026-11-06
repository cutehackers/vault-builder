# LLM Wiki 사용법

이 문서는 LLM Wiki를 실제로 사용하는 순서를 설명합니다.

목표는 단순합니다.

```text
원본 자료는 raw/에 보관하고,
검증된 지식 문서만 wiki/에 남긴다.
```

내부 구조와 처리 원리는 [LLM-WIKI.md](LLM-WIKI.md)를 참고하세요. 이 문서는 사용자가 무엇을 실행하고, 어떤 결과를 확인하면 되는지에 집중합니다.

## 1. 5분 빠른 시작

Claude Code를 모두 종료한 뒤 아래 명령 한 줄로 설치합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash
```

이 명령은 git과 Python 3.10 이상이 있는지 확인하고, builder checkout을 `$HOME/.local/share/vault-builder`에 받고, `$HOME/.local/bin/vault-builder` 명령을 만든 뒤 `vault-builder setup`을 실행합니다. `setup`은 vault 설치와 hook 설치, 저장소 연결, MCP 서버 등록, doctor 검사를 차례로 합니다(17.6). 다른 도구는 설치하지 않습니다. 질문마다 Enter를 누르면 개인 vault `$HOME/vault`와, 현재 폴더가 git 저장소일 때 그 저장소의 project vault `$HOME/vaults/<저장소 폴더 이름>`이 생기고 연결됩니다.

명령이 끝나면 Claude Code를 다시 시작하고, `/mcp` 목록에 `vault-wiki`가 연결되어 있는지 확인합니다. 다음 저장소는 `vault-builder link --repo <경로>` 명령으로 연결합니다(17.3).

질문 없이 설치하려면 옵션을 `bash -s --` 뒤에 붙입니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash -s -- --repo /path/to/my-app --yes
```

설치 명령의 다른 옵션과 환경 변수는 `| bash -s -- --help`로 봅니다.

#### vault 폴더만 만들기

hook, 저장소 연결 없이 vault 폴더만 만들려면 `bootstrap.sh`를 씁니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash
cd vault
python3 tools/wiki/cli.py health
```

원하는 이름으로 만들 수도 있습니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash -s -- ~/my-vault
cd ~/my-vault
python3 tools/wiki/cli.py lint --report
```

이미 이 저장소를 열어 둔 상태라면, 먼저 현재 vault가 정상인지 확인합니다.

```bash
python3 tools/wiki/cli.py lint --report
```

정상이라면 다음과 비슷한 출력이 나옵니다.

```text
No issues found.
Report written: /.../scratch/reports/YYYY-MM-DD-lint.md
```

새 LLM Wiki를 따로 만들려면 이 저장소 루트에서 실행합니다.

```bash
./init-vault.sh ~/my-vault
cd ~/my-vault
python3 tools/wiki/cli.py lint --report
```

`init-vault.sh`는 인자 없이 실행하면 bootstrap과 동일하게 기본 `vault/` 폴더를 생성합니다. 이미 초기화된 vault를 덮어쓰지 않으며, 대상 폴더에 `wiki/index.md` 또는 `tools/wiki/cli.py`가 있으면 중단합니다.

Stenc 고정 포맷 spec/plan 문서는 선택 기능입니다. 기본 bootstrap과 기본 `init-vault.sh`는 Stenc 없이 vault를 생성합니다. Stenc 문서와 helper 도구까지 포함하려면 명시적으로 opt-in합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash -s -- --with-stenc ~/my-vault
./init-vault.sh --with-stenc ~/my-vault
```

## 1.1 기존 Vault를 안전하게 audit/migration하기

One Body Owner, Many References: `vault-builder`는 구조와 migration 계약을
소유하고, 생성된 Vault는 raw 증거와 durable wiki 지식을 소유합니다.
`.vault-builder/manifest.json`은 운영 metadata이며 durable knowledge가 아닙니다.
항상 최신 builder checkout에서 audit하고, 외부 plan을 사람이 검토한 뒤에만 apply합니다.

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
`review-required` 항목의 사람 검토가 필요함을 뜻합니다. apply는 wiki 지식을
자동 병합하거나 `wiki/index.md`/`wiki/maps/`를 재생성하지 않습니다. 실패 후
복구가 필요하다고 도구가 안내할 때만 명시적으로 `recover`를 실행합니다. Vault
Graph는 읽기 전용 소비자이며 Vault를 수정하거나 migration하지 않습니다.
recovery journal, preparation authority, cleanup tombstone가 하나라도 남아 있으면
apply는 target write 전에 중단하고 outstanding migration ID를 모두 정렬해 정확한
`recover` 명령을 출력합니다.
apply와 recover는 전체 실행 동안 target root의 process lock을 배타적으로
유지합니다. 이미 migration 명령이 실행 중이면 새 명령은 target write 없이
실패하며, process crash 시 lock은 운영체제가 해제합니다.

apply가 `Migration committed, cleanup pending`을 보고하면 두 gate와 version 1
manifest commit은 이미 끝난 상태입니다. 이때는 audit이나 apply를 다시 실행하지
말고, 출력된 migration ID로 cleanup-only 복구를 실행합니다.

```bash
python3 tools/vault_migration/cli.py recover \
  --target /path/to/vault \
  --migration-id <printed-migration-id>
```

이 상태의 `recover`는 남은 marker와 recovery tombstone만 정리하며 관리 파일이나
manifest를 legacy bytes로 되돌리지 않습니다. 같은 명령을 다시 실행해도 version 1
상태를 유지합니다.

실패한 apply가 `Automatic rollback: completed; cleanup pending`을 보고하면 prior
Vault bytes와 mode 복원은 이미 검증된 상태입니다. 같은 `recover` 형식으로
preparation/rollback journal cleanup만 재개합니다. 이 경로는 version 1 commit을
되돌리지 않는 committed cleanup 경로와 구분됩니다.

## 2. 단순 사용 모델

LLM Wiki는 내부적으로 여러 품질 단계를 거치지만, 사용자는 세 가지 요청만 기억하면 됩니다.

| 사용자가 하려는 일 | 권장 skill/workflow | 사용자가 말하는 방식 |
|---|---|---|
| 새 원본을 wiki에 반영 | `wiki-ingest` | `raw/sources/example.md를 wiki에 반영해주세요.` |
| 기존 wiki page를 수정 | `wiki-update` | `wiki/concepts/example.md를 최신 원본 기준으로 업데이트해주세요.` |
| wiki 기준으로 질문 | `wiki-query` | `현재 wiki 기준으로 ingest와 lint의 관계를 설명해주세요.` |
| 대화 세션 원문 보관 | `session-capture` | `이 세션을 vault에 보관해주세요.` (17.7절) |

현재 저장소의 결정론적 도구는 `tools/wiki/cli.py` 아래에 있습니다. 위 skill/workflow는 사람이 기억하기 쉬운 권장 사용 방식이며, 내부에서는 기존 CLI를 조합합니다.

현재 구현된 안정 명령은 `ingest-source`, `source-registry`, `bulk-ingest`, `publish-draft`, `publish-batch`, `review`, `merge scan`, `maps build`, `metrics`, `health`, `apply-draft`, `lint`, `hash-source`, `validate-page`, `memory-scan`, `sessions capture/status/git-check`, `workflow ingest/update/query`입니다. `wiki-ingest`, `wiki-update`, `wiki-query` skill 문서는 `.agents/skills/` 아래에 있으며, agent가 아래 workflow를 일관되게 따르기 위한 repo-local 지침입니다.

```text
wiki-ingest
-> ingest-source
-> draft 생성
-> publish-draft --report
```

```text
wiki-update
-> 대상 page와 관련 source 확인
-> human-lock 확인
-> 변경 draft 생성
-> publish-draft --report
```

```text
wiki-query
-> wiki/index.md 확인
-> 관련 page/source 읽기
-> 답변 작성
-> 필요하면 report 또는 draft로 남김
```

중요한 원칙은 하나입니다. Skill은 의미 판단과 draft 생성을 맡고, 영구 변경과 검증은 CLI가 맡습니다.

```text
Skill = cognition
CLI = mutation and validation
Wiki/Git = durable state
```

## 3. 기본 처리 순서

처음에는 아래 순서만 기억하면 됩니다. 새 원본의 내용을 wiki에 반영한 뒤 질문하려면 이 순서가 기본입니다.

1. 원본 파일을 `raw/sources/`에 넣습니다.
2. `ingest-source`로 source page를 만듭니다.
3. agent에게 원본에서 durable page를 만들도록 요청합니다.
4. agent가 만든 draft를 `publish-draft --report`로 검증, 적용, lint합니다.
5. 필요하면 `health`로 전체 상태를 다시 확인합니다.
6. 검증된 wiki를 기준으로 질문합니다.
7. 변경 내용을 확인하고 Git commit으로 남깁니다.

한 줄로 쓰면 다음과 같습니다.

```text
원본 등록 -> wiki page draft 생성 -> publish-draft -> health -> wiki에 질문
```

예시:

```bash
mkdir -p raw/sources
cp ~/Downloads/example.md raw/sources/example.md
python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report
python3 tools/wiki/cli.py lint --report
```

그 다음 agent에게 이렇게 요청할 수 있습니다.

```text
raw/sources/example.md를 읽고 핵심 concept page와 workflow page가 필요하면 draft로 만들어주세요.
기존 wiki/index.md를 먼저 확인하고 중복 page는 만들지 마세요.
```

## 4. 먼저 읽을 파일

처음 사용하는 사람은 이 순서로 읽으면 됩니다.

1. `README.md` - 가장 짧은 시작 안내
2. `docs/usage.md` - 실제 사용 순서
3. `docs/LLM-WIKI.md` - architecture 원본과 내부 처리 원리
4. `AGENTS.md` - agent가 반드시 지켜야 할 짧은 계약
5. `docs/VAULT_PROMPTS.md` - 운영, 관리, 품질 게이트용 프롬프트 모음
6. `docs/USER_PROMPTS.md` - 사용자가 스킬을 활용해 wiki 가치를 끌어내는 프롬프트 모음
7. `wiki/overview.md` - 현재 wiki의 요약
8. `wiki/index.md` - durable wiki page 목록

agent가 상세 규칙을 확인해야 할 때는 `docs/agent/OPERATING-SCHEMA.md`를 읽습니다. draft 작성 규칙은 `docs/agent/DRAFTS.md`와 `tools/wiki/templates/draft-upsert-page.json`, `tools/wiki/templates/draft-batch-upsert-pages.json`에 있습니다.

## 5. 디렉터리 역할

| 경로 | 사용자가 알아야 할 점 |
|---|---|
| `raw/sources/` | 원본 자료를 넣는 곳입니다. agent가 임의로 수정하지 않습니다. |
| `raw/assets/` | 이미지, 스크린샷, 첨부 파일을 둡니다. |
| `raw/imports/` | 아직 정리되지 않은 가져오기 자료를 임시로 둡니다. |
| `raw/sessions/` | 대화 세션 원문 보관소입니다. `sessions capture` 명령만 새 파일을 추가하고, 기존 파일은 누구도 고치거나 지우지 않습니다. source page는 만들지 않습니다(17.7절). |
| `wiki/` | 검증된 durable knowledge가 저장되는 곳입니다. |
| `wiki/sources/` | 원본 자료 하나당 source page가 생성됩니다. |
| `wiki/concepts/` | 반복해서 쓰이는 개념, 원칙, 패턴입니다. |
| `wiki/entities/` | 사람, 조직, 제품, 프로젝트, 도구입니다. |
| `wiki/systems/` | 시스템, 아키텍처, 서비스, agent 구조입니다. |
| `wiki/workflows/` | 반복 가능한 절차입니다. |
| `wiki/decisions/` | 중요한 결정과 근거입니다. |
| `wiki/maps/` | 주제별 읽기 경로와 지식 지도입니다. |
| `scratch/drafts/` | LLM이 만든 JSON 초안입니다. 아직 최종 지식이 아닙니다. |
| `scratch/reports/` | lint, ingest, draft 적용 결과 보고서입니다. |
| `scratch/review/` | 사람이 판단해야 할 항목을 둡니다. |
| `tools/wiki/` | 검증, 해시, ingest, draft 적용, MCP 도구입니다. |

## 6. 원본 등록하기

원본은 먼저 `raw/sources/`에 넣습니다.

```bash
mkdir -p raw/sources
cp ~/Downloads/example.md raw/sources/example.md
```

그 다음 source page를 생성합니다.

```bash
python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report
```

제목과 요약을 직접 지정할 수도 있습니다.

```bash
python3 tools/wiki/cli.py ingest-source raw/sources/example.md \
  --title "Example Source" \
  --summary "Example source for testing LLM Wiki ingestion." \
  --report
```

성공하면 다음이 만들어지거나 갱신됩니다.

| 결과 | 설명 |
|---|---|
| `wiki/sources/example.md` | 원본에 대응되는 source page입니다. |
| `canonical_source` | 원본 파일 경로입니다. |
| `raw_sha256` | 원본 변경 여부를 확인하기 위한 해시입니다. |
| `wiki/index.md` | Sources 섹션에 source page가 추가됩니다. |
| `wiki/log.md` | ingest 기록이 추가됩니다. |
| `scratch/reports/...ingest-example.md` | 실행 결과 보고서입니다. |

주의할 점:

- `ingest-source`는 source page만 등록합니다.
- 원본에서 개념, 결정, workflow를 뽑는 일은 agent의 semantic draft 단계입니다.
- source page가 생겼다고 모든 지식 추출이 끝난 것은 아닙니다.

현재 raw/source-page inventory를 확인하려면 다음 명령을 사용합니다.

```bash
python3 tools/wiki/cli.py source-registry --report
```

이 명령은 `raw/` 아래 파일과 `wiki/sources/` source page를 비교해 `registered`, `unregistered`, `drift`, `missing-source` 상태를 보고합니다. 보고서는 `scratch/reports/`에 작성됩니다.

여러 source file을 한 번에 등록하려면 `bulk-ingest`를 사용합니다.

```bash
python3 tools/wiki/cli.py bulk-ingest raw/sources/a.md raw/sources/b.md --report
```

또는 현재 `raw/` 아래의 모든 source 후보를 대상으로 실행할 수 있습니다.

```bash
python3 tools/wiki/cli.py bulk-ingest --all-raw --report
```

`bulk-ingest`도 semantic extraction을 수행하지 않습니다. 각 raw file에 대해 `ingest-source`와 같은 source registration만 수행하고, source별 outcome을 report에 남깁니다.

## 7. 원본에서 wiki page 만들기

원본을 durable wiki page로 만들 때는 agent에게 명확히 요청하는 것이 좋습니다.

좋은 요청 예시:

```text
raw/sources/example.md를 ingest한 뒤, 이 자료에서 재사용 가능한 concept, system, workflow, decision을 찾아주세요.
기존 wiki/index.md를 먼저 확인하고, 중복 page를 만들지 말고, 필요한 변경은 scratch/drafts/에 JSON draft로 작성해주세요.
```

검토 중심 요청:

```text
raw/sources/example.md에서 기존 wiki 내용과 충돌하는 claim이 있는지 확인하고,
충돌이 있으면 관련 page의 Contradictions / Tensions section에 반영할 draft를 만들어주세요.
```

보고서 중심 요청:

```text
raw/sources/example.md를 읽고 어떤 wiki page를 만들거나 갱신해야 하는지 scratch/reports/에 ingest report로 정리해주세요.
아직 wiki page는 수정하지 마세요.
```

agent가 page를 직접 쓰는 대신 `scratch/drafts/`에 draft를 만들도록 요청하는 것이 기본입니다. 최종 반영은 `publish-draft`로 검증, 적용, lint를 한 번에 수행합니다.

여러 durable page를 한 작업 단위로 함께 반영해야 하면 `tools/wiki/templates/draft-batch-upsert-pages.json`를 기준으로 batch draft를 만들고 `publish-batch`를 사용합니다.

```bash
python3 tools/wiki/cli.py publish-batch scratch/drafts/example-batch.json --report
```

`publish-batch`는 전체 dry-run 검증 후 모든 page를 적용하고, post-lint 실패 시 target pages, `wiki/index.md`, `wiki/log.md`를 이전 상태로 되돌립니다. Batch draft의 durable operation 기록은 top-level `log` 하나에 둡니다. `pages[]` 안의 개별 page draft에는 `log`를 넣지 않습니다.

## 8. Draft 검증하고 적용하기

Draft는 LLM이 만든 page 변경안입니다. 위치는 보통 `scratch/drafts/*.json`입니다.

템플릿은 여기에 있습니다.

```text
tools/wiki/templates/draft-upsert-page.json
```

최소 예시는 다음과 같습니다.

```json
{
  "version": 1,
  "operation": "upsert-page",
  "path": "wiki/concepts/example.md",
  "frontmatter": {
    "title": "Example",
    "type": "concept",
    "status": "active",
    "created": "2026-05-29",
    "updated": "2026-05-29",
    "owner": "agent",
    "summary": "Example concept page.",
    "source_count": 0,
    "tags": [],
    "related": [],
    "confidence": "medium",
    "quality": {
      "provenance": "none",
      "links": "unchecked",
      "contradictions": "none",
      "review_required": false
    }
  },
  "body": "# Example\n\nDraft body.\n",
  "index": {
    "section": "Concepts",
    "target": "concepts/example",
    "summary": "Example concept page."
  },
  "log": {
    "event_type": "repair",
    "title": "Apply Example Draft",
    "items": ["Added [[concepts/example]]."]
  }
}
```

권장 방식은 `publish-draft`입니다. 이 명령은 먼저 현재 wiki가 깨끗한지 확인하고, dry-run 검증을 수행한 뒤 적용합니다. 적용 후 lint가 실패하면 page, index, log 변경을 되돌리고 lint report를 남깁니다.

```bash
python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report
```

문제를 분리해서 보고 싶으면 저수준 명령을 직접 실행할 수 있습니다. 먼저 파일을 쓰지 않고 검증합니다.

```bash
python3 tools/wiki/cli.py apply-draft scratch/drafts/example.json --dry-run
```

검증 결과만 따로 확인한 뒤 durable하게 반영하려면 다시 `publish-draft`를 실행합니다. 적용 후 전체 wiki를 다시 검사합니다.

```bash
python3 tools/wiki/cli.py lint --report
```

주의: 일반 사용에서는 `publish-draft --report`를 사용하세요. `apply-draft`는 기본적으로 validation-only이며 파일을 쓰지 않습니다. `publish-draft`는 적용 전후 lint와 실패 시 rollback까지 포함합니다.

## 9. Wiki에 질문하기

사용자는 자연어로 질문하면 됩니다. 좋은 질문은 agent가 어떤 범위를 봐야 하는지 알려줍니다.

기존 wiki에 대해서는 언제든 질문할 수 있습니다. 다만 새 원본의 내용을 반영한 답변이 필요하다면, 먼저 `ingest-source`, draft 생성, `publish-draft --report`를 끝낸 뒤 질문하는 것이 기본 순서입니다.

질문 결과를 어떻게 남길지는 세 가지 방식 중 하나로 요청하면 됩니다.

| 방식 | 언제 사용하나 | 결과 |
|---|---|---|
| `answer-only` | 지금 답만 필요할 때 | 대화 답변만 생성합니다. |
| `answer-with-report` | 근거와 판단 흔적을 남기고 싶을 때 | `scratch/reports/YYYY-MM-DD-query-<slug>.md`에 query report를 남깁니다. |
| `answer-and-capture` | 답변을 wiki 지식으로 축적하고 싶을 때 | `scratch/drafts/`에 draft를 만들고 검증 후 wiki에 반영합니다. |

예시:

```text
현재 wiki 기준으로 LLM Wiki의 핵심 품질 원칙을 요약해주세요.
근거가 되는 wiki page와 source page도 함께 알려주세요.
```

```text
wiki 안에서 ingest와 lint가 어떤 관계인지 설명해주세요.
답변이 재사용 가능하면 workflow 또는 concept draft로 남겨주세요.
```

```text
answer-with-report 방식으로, 현재 wiki에서 source drift를 어떻게 처리하는지 설명해주세요.
```

```text
answer-and-capture 방식으로, 이 답변을 concepts 또는 workflows page로 남길 가치가 있으면 draft를 만들어주세요.
```

```text
이 vault에서 아직 정리되지 않은 open question이나 human review 항목을 찾아주세요.
```

agent는 보통 `wiki/index.md`에서 시작해 관련 page를 읽고, 필요하면 `wiki/sources/`와 `raw/sources/`를 확인합니다. 답변이 나중에도 쓸 가치가 있으면 `scratch/drafts/` 또는 `scratch/reports/`로 남기도록 요청하세요.

결정론적 query capture command는 agent가 작성한 synthesis를 report나 draft scaffold로 고정합니다.

```bash
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-with-report --report
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-and-capture --draft scratch/drafts/query-answer.json --report
```

`answer-with-report`는 question, answer summary, consulted pages/sources, confidence, contradictions, reusable capture recommendation을 report에 남깁니다. `answer-and-capture`는 publish 가능한 draft scaffold도 함께 만듭니다. 실제 durable 반영은 여전히 `publish-draft` gate를 통과해야 합니다.

## 10. Lint 실행과 실패 대응

전체 wiki 품질을 검사하려면 다음을 실행합니다.

```bash
python3 tools/wiki/cli.py lint --report
```

특정 page만 검사하려면 다음을 실행합니다.

```bash
python3 tools/wiki/cli.py validate-page wiki/overview.md
```

자주 보는 문제와 대응 방법은 다음과 같습니다.

| 문제 코드 | 의미 | 사용자가 할 일 |
|---|---|---|
| `missing-frontmatter` | page가 YAML frontmatter 없이 시작합니다. | page type에 맞는 frontmatter를 추가합니다. |
| `invalid-frontmatter` | YAML 문법이 깨졌습니다. | 들여쓰기, 따옴표, 리스트 형식을 확인합니다. |
| `missing-field` | 필수 metadata가 없습니다. | 누락된 필드를 채웁니다. |
| `invalid-field-type` | 필드 타입이 잘못됐습니다. | `source_count`는 숫자, `tags`와 `related`는 리스트로 둡니다. |
| `broken-link` | `[[...]]` 링크 대상이 없습니다. | 링크를 고치거나 대상 page를 만듭니다. |
| `orphan-page` | index나 map에서 도달할 수 없습니다. | `wiki/index.md`나 관련 map에 링크를 추가합니다. |
| `missing-provenance-signal` | 출처 수준은 높게 표시했지만 본문에 근거가 없습니다. | `Evidence:`, `Source:`, claim table 등 근거를 추가합니다. |
| `missing-claim-evidence-table` | `quality.provenance: claim`인데 claim table이 없습니다. | `Claim`, `Status`, `Evidence` 열을 가진 표를 추가합니다. |
| `invalid-claim-status` | claim table의 status 값이 허용 범위를 벗어났습니다. | `stated`, `inferred`, `contested`, `deprecated` 중 하나를 사용합니다. |
| `source-count-evidence-mismatch` | `source_count`와 claim evidence의 고유 근거 수가 맞지 않습니다. | 중복을 정리하거나 `source_count`를 실제 근거 수에 맞춥니다. |
| `claim-evidence-source-unregistered` | raw evidence가 등록된 source page나 inline SHA-256 없이 사용되었습니다. | 먼저 `ingest-source`로 source page를 만들거나 `raw/...#sha256=...` 형식으로 근거를 명시합니다. |
| `claim-evidence-source-hash-invalid` | claim evidence가 연결한 source hash가 현재 raw와 맞지 않습니다. | source drift를 확인하고 관련 page를 재검토합니다. |
| `session-file-invalid-path` | `raw/sessions/` 안 파일의 경로나 이름이 세션 파일 형식에 맞지 않습니다. `.md`가 아닌 파일도 포함합니다. 점으로 시작하는 파일은 검사하지 않습니다. | 수집 명령이 만든 파일이 아니면 사람이 판단해 보관소 밖으로 옮깁니다. agent는 옮기거나 지우지 않습니다. |
| `session-file-invalid-frontmatter` | 세션 파일의 frontmatter가 없거나, 필수 필드가 없거나, `schema` 값을 모르거나, 경로의 session id·part 값이 frontmatter 값과 다릅니다. | Git 이력에서 처음 커밋한 내용으로 되돌립니다. |
| `session-file-hash-mismatch` | 다시 계산한 본문 해시가 frontmatter의 `body_sha256` 값과 다릅니다. 누군가 세션 파일 본문을 고쳤습니다. | Git 이력에서 처음 커밋한 내용으로 되돌립니다. 세션 파일은 손으로 고치지 않습니다. |
| `session-part-duplicate` | 같은 agent, 같은 session id에 같은 part 번호의 파일이 둘 있습니다. | 어느 파일이 수집 명령의 결과인지 사람이 판단합니다. |
| `session-source-page-forbidden` | `wiki/sources/` source page의 `canonical_source` 값이 `raw/sessions/` 안을 가리킵니다. | 그 source page를 지웁니다. 세션 파일은 source page 없이 근거로 인용합니다. |
| `claim-evidence-session-invalid` | claim evidence가 보관소 검사에 실패한 세션 파일을 가리킵니다. | 먼저 그 세션 파일의 lint 오류를 해결합니다. |
| `source-drift` | 원본 파일의 현재 해시가 source page와 다릅니다. | 원본 변경이 의도된 것인지 확인하고 source page와 관련 wiki page를 재검토합니다. |
| `invalid-log-heading` | log heading 형식이 맞지 않습니다. | `## [YYYY-MM-DD] event-type | Short Title` 형식으로 고칩니다. |
| `unmatched-human-lock` | human lock 시작/끝 marker가 맞지 않습니다. | `<!-- human-locked:start -->`와 `<!-- human-locked:end -->` 쌍을 맞춥니다. |

`lint`가 error를 보고하면 durable update가 완료된 상태로 보지 않습니다. 먼저 문제를 고치고 다시 `lint --report`를 실행하세요.

## 11. 원본 변경 여부 확인하기

원본 파일의 SHA-256을 직접 계산하려면 다음을 실행합니다.

```bash
python3 tools/wiki/cli.py hash-source raw/sources/example.md
```

source page에 `canonical_source`와 `raw_sha256`이 있으면 `lint`가 자동으로 drift를 확인합니다.

```bash
python3 tools/wiki/cli.py lint --report
```

`source-drift`가 나오면 원본이 바뀐 것입니다. 이때는 기존 wiki page의 요약, claim, decision이 여전히 맞는지 다시 봐야 합니다.

## 12. Human Lock 사용하기

사람이 직접 보호하고 싶은 page나 section에는 marker를 씁니다.

전체 page 보호:

```markdown
<!-- human-locked -->
```

section 보호:

```markdown
<!-- human-locked:start -->
이 구간은 agent가 직접 수정하지 않습니다.
<!-- human-locked:end -->
```

agent에게는 이렇게 요청하세요.

```text
human-locked section은 직접 수정하지 말고, 필요한 변경은 scratch/reports/에 proposed patch로 남겨주세요.
```

현재 `apply-draft`와 `publish-draft`는 page-level human lock이 있는 target page를 직접 덮어쓰지 않습니다. Section-level lock도 기존 locked range의 exact content preservation을 검사합니다. 즉 `<!-- human-locked:start -->`와 `<!-- human-locked:end -->` 사이의 내용이 조금이라도 바뀌면 draft 검증과 publish가 실패합니다.

## 13. Contradiction 처리하기

자료끼리 충돌하면 하나를 조용히 선택하지 않습니다.

agent에게 이렇게 요청하세요.

```text
충돌하는 주장을 양쪽 근거와 함께 정리하고,
관련 page의 Contradictions / Tensions section에 반영할 draft를 만들어주세요.
중요한 판단이 필요하면 scratch/review/에 human review 항목으로 남겨주세요.
```

기본 처리 순서:

1. 충돌하는 문장을 찾습니다.
2. 양쪽 source를 기록합니다.
3. 충돌 유형을 구분합니다.
4. 관련 page에 `Contradictions / Tensions` section을 둡니다.
5. 중요하거나 반복되는 claim이면 claim page를 제안합니다.
6. 중요한 결정에 영향이 있으면 `scratch/review/`로 보냅니다.

Human review queue를 직접 만들고 닫으려면 다음 명령을 사용합니다.

```bash
python3 tools/wiki/cli.py review create --type contradiction --summary "Example Review" --related wiki/overview.md --context "Why human judgment is needed."
python3 tools/wiki/cli.py review list
python3 tools/wiki/cli.py review resolve scratch/review/example-review.md --status accepted --resolution "Accepted with rationale."
```

`review create`의 type은 `contradiction`, `merge`, `contested-claim`, `human-lock` 중 하나입니다. `review resolve`는 status를 `accepted`, `rejected`, `resolved` 중 하나로 바꾸고 `wiki/log.md`에 관련 page와 resolution을 기록합니다.

중복 page 후보를 찾을 때는 merge scan을 실행합니다.

```bash
python3 tools/wiki/cli.py merge scan --report --create-review
```

이 명령은 같은 title이나 alias를 공유하는 후보를 report에 남기고, `--create-review`가 있으면 merge review item을 만듭니다. 자동 병합, 자동 redirect, 자동 deprecate는 하지 않습니다.

## 14. Navigation Map 갱신하기

커지는 wiki는 `wiki/index.md`만으로 충분하지 않습니다. Generated map은 topic, source, decision, unresolved review, lifecycle 관점을 별도 page로 분리합니다.

```bash
python3 tools/wiki/cli.py maps build --report
```

CI/release gate에서는 stale map을 막기 위해 check mode를 사용합니다.

```bash
python3 tools/wiki/cli.py maps build --check --report
```

`maps build`는 `wiki/maps/topic-map.md`, `wiki/maps/source-map.md`, `wiki/maps/decision-map.md`, `wiki/maps/review-map.md`, `wiki/maps/lifecycle-map.md`를 deterministic하게 생성하고, index/log를 갱신합니다.

## 15. Maintenance Metrics 확인하기

장기 운영 상태는 `metrics` report로 확인합니다.

```bash
python3 tools/wiki/cli.py metrics --report
python3 tools/wiki/cli.py metrics --check --report
python3 tools/wiki/cli.py metrics --check --policy tools/wiki/metrics-policy.json --report
```

이 report는 pending review, contested claim rows, stale sources, orphan pages, claim-level provenance 미적용 page, deprecated linked pages, last health report를 한 번에 보여줍니다. `--check`는 `tools/wiki/metrics-policy.json`의 threshold를 기준으로 실패하며, 일시적으로 허용할 운영 부채가 있으면 별도 policy 파일을 지정할 수 있습니다. 값이 `null`인 항목은 gate에서 빠지고 경고로만 출력됩니다. 기본 policy는 대기 중인 review와 contested claim을 경고로만 보여주고, review 없이 남은 contested claim과 14일보다 오래 대기한 review만 실패로 처리합니다. 사람이 판단할 일이 하나 생겼다고 vault 전체가 실패하지 않게 하기 위해서입니다. 명시한 `--policy` 파일이 없으면 실패합니다. Metrics는 상태를 보여줄 뿐 자동 repair나 judgment를 수행하지 않습니다.

## 16. MCP 사용하기

install(17.6)은 Claude Code의 user scope MCP 설정 파일에 서버 항목 `mcpServers.vault-wiki` 하나만 등록합니다. 모든 vault가 이 서버 하나를 함께 씁니다. 그래서 어느 저장소 세션에서든 `/mcp` 목록에는 vault 서버 `vault-wiki` 하나만 보이고, 도구 이름은 `mcp__vault-wiki__<도구>` 형태입니다. vault 안에 `.mcp.json` 파일은 만들지 않습니다.

서버 항목은 `<설정 폴더>/vault-builder/llm_wiki_router.py` 라우터를 실행합니다. 라우터는 도구 호출을 받을 때마다 대상 vault를 정하고, 그 vault 폴더에서 그 vault의 `tools/wiki/cli.py`를 실행합니다. 그래서 각 vault는 자기 tools 버전으로 검사받습니다. 라우터는 호출 사이에 아무것도 기억하지 않고, 호출마다 연결 설정(17.1)을 다시 읽습니다. 따라서 `link` 명령(17.3)으로 연결을 바꾸면 Claude Code를 다시 시작하지 않아도 다음 호출부터 적용됩니다.

#### `vault` 인자와 읽기 범위

모든 도구에는 선택 인자 `vault`(연결 설정의 vault 이름)와 `cwd`(절대 경로)가 있습니다.

- **닿는 vault:** 세션 폴더가 vault 폴더이면 그 vault 하나입니다. 아니면 세션 저장소에 연결된 vault 전체입니다(17.3). 둘 다 없으면 닿는 vault는 없습니다. 라우터는 호출마다 세션 폴더로 이 값을 다시 구합니다. 호출에 `cwd` 값을 주면 그 폴더를 세션 폴더로 씁니다.
- **읽기 호출:** 닿는 vault만 읽습니다. 읽기 호출의 `vault` 값은 닿는 vault 이름이어야 합니다. 연결 설정에는 있지만 닿지 않는 이름을 주면 실행하지 않고, 닿는 vault 이름 목록과 `link` 안내를 담은 오류를 돌려줍니다. 그 vault를 읽으려면 먼저 `link` 명령으로 저장소에 연결합니다.
- `vault` 값이 없는 읽기 호출은 닿는 vault가 하나일 때 그 vault를 씁니다. 닿는 vault가 0개이거나 2개 이상이면 고를 수 있는 이름 목록을 담은 오류를 돌려줍니다.
- `wiki_search` 도구는 `vault` 값이 없으면 닿는 vault 전체를 찾습니다. 결과는 vault 이름 순서로 `# vault <이름> (<역할>)` 머리 줄 아래에 붙고, `limit` 값은 vault마다 적용됩니다. 닿는 vault가 없으면 검색하지 않고 vault 이름 목록과 `link` 안내를 담은 오류를 돌려줍니다.
- **쓰기 호출:** `vault` 인자가 꼭 있어야 합니다. 값은 연결 설정의 어떤 vault 이름이든 됩니다. 닿는 vault가 아니어도 되고, 역할이 `team`인 vault도 받습니다. 쓰는 내용은 대상 vault의 `publish-draft` 검증이 검사합니다. 라우터는 별도의 승인 단계를 두지 않고, Claude Code의 도구별 권한 확인을 따릅니다.
- **경로 인자:** `path`, `paths`, `source`, `target`, `draft`, `report` 값은 대상 vault 기준 상대 경로여야 합니다. 절대 경로, `..`로 vault 밖을 가리키는 경로, vault 밖으로 이어지는 symlink는 실행하기 전에 거부합니다. 그래서 `vault` 인자로 고른 vault 밖에는 쓸 수 없습니다.

#### 도구 분류

| 분류 | 도구 |
|---|---|
| 항상 읽기 | `wiki_search`, `wiki_read_page`, `wiki_validate_page`, `wiki_hash_source`, `wiki_review_list` |
| 쓰기 옵션이 없을 때만 읽기 | `wiki_lint`(`report` 없음), `wiki_source_registry`(`report` 없음), `wiki_apply_draft`(`report` 없음), `wiki_merge_scan`(`report`와 `create_review` 없음), `wiki_maps_build`(`dry_run` 또는 `check` 값이 true) |
| 항상 쓰기 | `wiki_health`, `wiki_metrics`, `wiki_ingest_source`, `wiki_bulk_ingest`, `wiki_publish_draft`, `wiki_publish_batch`, `wiki_review_create`, `wiki_review_resolve`, `wiki_workflow_ingest`, `wiki_workflow_update_preflight`, `wiki_workflow_update_publish`, `wiki_workflow_query_prepare`, `wiki_workflow_query_capture`, `wiki_workflow_query_publish` |

표에 없는 조합은 쓰기 호출로 봅니다. 예를 들어 `report` 값을 준 `wiki_lint` 호출은 `vault` 인자가 필요합니다.

#### 수동 실행

vault 안의 `tools/wiki/mcp_server.py` 파일은 그 vault 하나만 다루는 stdio MCP server입니다. install은 이 파일을 등록하지 않습니다. 다른 MCP client에서 vault 하나를 직접 연결할 때만 씁니다.

```bash
python3 tools/wiki/mcp_server.py
```

- 이 명령은 일반 CLI처럼 결과를 출력하고 바로 종료하는 명령이 아닙니다.
- MCP client가 stdio로 연결해 tool call을 보낼 때 사용합니다.
- 터미널에서 직접 실행하면 입력을 기다리는 것처럼 보일 수 있습니다.

제공되는 MCP tool:

| MCP tool | 역할 |
|---|---|
| `wiki_search` | title, summary, tags, 본문에서 키워드로 page를 찾습니다. deprecated page와 superseded claim은 기본으로 빼고, `include_deprecated`를 주면 이력까지 찾습니다. 생성된 map, index, log page는 찾지 않습니다. CLI: `search`. |
| `wiki_read_page` | page 하나 또는 heading 하나의 section을 읽습니다. CLI: `read`. |
| `wiki_validate_page` | page 하나를 검증합니다. |
| `wiki_lint` | 전체 wiki를 lint합니다. |
| `wiki_hash_source` | raw source의 SHA-256을 계산합니다. |
| `wiki_ingest_source` | raw source를 source page로 등록합니다. |
| `wiki_source_registry` | raw/source-page inventory를 보고합니다. |
| `wiki_bulk_ingest` | 여러 raw source를 등록합니다. |
| `wiki_apply_draft` | JSON draft를 파일 쓰기 없이 검증합니다. |
| `wiki_publish_draft` | JSON draft를 검증, 적용, lint합니다. |
| `wiki_publish_batch` | 여러 page draft를 transaction 단위로 publish합니다. |
| `wiki_review_create` | human-review item을 생성합니다. |
| `wiki_review_list` | human-review queue를 조회합니다. |
| `wiki_review_resolve` | human-review item을 resolve하고 log에 기록합니다. |
| `wiki_merge_scan` | duplicate/merge 후보를 report로 남깁니다. |
| `wiki_maps_build` | generated navigation maps를 build/check합니다. |
| `wiki_metrics` | maintenance metrics dashboard를 생성합니다. |
| `wiki_health` | 전체 wiki 상태를 점검하고 기본 lint report를 씁니다. |
| `wiki_workflow_ingest` | raw source 등록 checkpoint를 실행하고 semantic extraction pending 상태를 보고합니다. |
| `wiki_workflow_update_preflight` | 기존 page update 전 lock, link, related 상태를 report로 남깁니다. |
| `wiki_workflow_update_publish` | target page와 draft target을 확인한 뒤 `publish-draft`로 반영합니다. |
| `wiki_workflow_query_prepare` | query report scaffold를 만듭니다. |
| `wiki_workflow_query_capture` | query 답변과 재사용 가능한 capture draft scaffold를 만듭니다. |
| `wiki_workflow_query_publish` | reusable query capture draft를 `publish-draft`로 반영합니다. |

라우터와 MCP wrapper는 CLI를 얇게 감싸는 역할입니다. 같은 작업은 vault 폴더에서 CLI로도 실행할 수 있습니다.

## 17. 여러 vault를 저장소에 연결하기 (project memory)

Claude Code 세션에서 나온 결정, 교정, 남은 일은 세션이 끝나면 Claude Code auto memory(이하 L1 메모리)에만 남습니다. 이 절의 기능은 그 내용을 vault에 검증된 지식으로 옮기고, 다른 저장소의 세션에서 필요할 때 요약의 필요한 부분만 불러오게 합니다.

```text
L1 메모리
-> memory-cleanup skill: vault의 raw/와 wiki/로 옮김
-> maps build: project마다 요약 페이지 wiki/maps/projects/<project id>.md 생성
-> /project-memory: 현재 저장소에 연결된 vault들의 요약 페이지에서 요청한 섹션만 출력
```

세션을 시작할 때 요약을 자동으로 넣지는 않습니다. 평소 세션에 드는 비용은 skill 목록에 보이는 한 줄(약 50 token)뿐입니다. 대신 요약은 Claude나 사용자가 불러올 때만 보입니다.

이 절의 `vault-builder ...` 명령은 설치 명령(1절)이 만든 `$HOME/.local/bin/vault-builder` 파일로 실행합니다. 이 파일은 builder checkout의 `tools/vault_migration/cli.py`를 실행하므로, builder checkout 폴더에서 `vault-builder` 대신 `python3 tools/vault_migration/cli.py`로 같은 명령을 실행할 수도 있습니다.

### 17.1 설정 폴더와 연결 설정 파일

vault 밖 파일은 모두 설정 폴더 아래에 둡니다. 환경 변수 `CLAUDE_CONFIG_DIR` 값이 있으면 그 폴더가 설정 폴더이고, 없으면 `$HOME/.claude` 폴더입니다. 아래에서 `<설정 폴더>` 표기는 이 폴더를 뜻합니다.

| 경로 | 내용 |
|---|---|
| `<설정 폴더>/vault-builder/vault-map.json` | 연결 설정. 어떤 vault가 있고 어떤 저장소가 어느 vault를 쓰는지 적습니다. |
| `<설정 폴더>/skills/project-memory/SKILL.md` | 불러오기 skill `project-memory` |
| `<설정 폴더>/skills/project-memory/scripts/recall.sh` | 불러오기 skill이 실행하는 recall 스크립트 |

세 파일은 여러 vault가 함께 씁니다. install이 처음 만들고, 마지막 vault를 uninstall할 때만 지웁니다.

연결 설정 형식은 다음과 같습니다.

```json
{
  "version": 2,
  "vaults": {
    "personal": {"path": "/path/to/vault", "role": "personal"},
    "team": {"path": "/path/to/team-vault", "role": "team"},
    "app": {"path": "/path/to/app-vault", "role": "project"}
  },
  "projects": {
    "my-app": {
      "root": "/path/to/my-app",
      "vaults": ["personal", "team", "app"],
      "remote": "git@github.com:example/my-app.git"
    }
  }
}
```

| key | 뜻 |
|---|---|
| `version` | 파일 형식 version입니다. 지금은 `2` 값을 씁니다. 읽는 쪽은 `1`과 `2`를 모두 받고, install, uninstall, `link`, `unlink` 명령은 항상 `2` 값으로 씁니다. |
| `vaults` | vault 이름별 항목입니다. `path` 값은 vault 폴더의 절대 경로이고, `role` 값은 비어 있지 않은 문자열입니다. 알려진 역할은 17.2 표에 있습니다. |
| `projects` | project id별 항목입니다. `root` 값은 저장소 main worktree root의 절대 경로이고, `vaults` 값은 이 저장소가 쓰는 vault 이름 목록입니다. 선택 key `remote` 값은 `link` 명령을 실행할 때의 `origin` remote URL 원문입니다. |

저장소가 어느 project 항목에 해당하는지는 17.3의 project 찾기 순서(root 경로, remote URL, 폴더 이름)로 정합니다. `link` 명령으로 새 항목을 만들면 project id는 기본으로 저장소 root 폴더 이름입니다. 같은 저장소의 `git worktree` 폴더에서도 project id는 같습니다.

저장소 연결은 이 파일을 손으로 고치지 않고 `link`, `unlink` 명령(17.3)으로 하고, vault 역할(17.2)은 `set-role` 명령으로 바꿉니다. 여러 저장소에 연결된 vault의 `project` 없는 페이지를 모든 연결 저장소의 요약에 넣으려면 `vault-builder set-sharable --vault <이름> --value true` 명령으로 `sharable` 값을 정합니다(17.5). 상한 값 같은 다른 설정 key는 두지 않습니다.

### 17.2 vault 역할

역할은 memory-cleanup skill이 note를 어느 vault로 보낼지 정할 때 씁니다. 이름 없는 `link`(17.3)는 역할을 보고 어느 vault를 연결할지 고릅니다.

알려진 역할은 builder의 `tools/vault_migration/roles.py` 표와 같습니다.

| 역할 | 공유 여부 | 설명 | 들어가는 note |
|---|---|---|---|
| `personal` | 공유 | 한 사람의 note. 모든 저장소가 함께 씁니다. | 다른 저장소에서도 쓸 개인 지식. 해당 역할의 vault가 연결되어 있지 않을 때 note도 이 vault로 갑니다. |
| `team` | 공유 | 팀이 함께 쓰는 지식. 모든 저장소가 함께 씁니다. | 팀이 합의한 내용. 기본 branch에 직접 commit하지 않고 PR로만 반영합니다. |
| `project` | 저장소 하나 | 저장소 하나의 이력과 세션 원문 | 그 저장소의 결정과 이력 |

역할 값은 비어 있지 않은 문자열이면 무엇이든 됩니다. install, `set-role`, `setup`, `link` 명령은 표에 없는 역할(예: `client`)도 받고, 그 vault를 공유 vault로 다룹니다. 코드에서 동작이 달라지는 역할은 `project` 하나입니다. 세션 원문 저장(17.7)과 기본 쓰기 vault 자동 지정(17.3)이 이 역할만 봅니다. 새 역할을 알려진 역할로 더하려면 `roles.py`에 한 줄, 이 표에 한 줄을 더합니다.

install은 새로 등록하는 vault를 `--role` 옵션 값으로 연결 설정에 더합니다. 옵션이 없으면 역할은 `personal`입니다. 이미 등록된 vault는 `--role` 옵션을 줄 때만 역할이 바뀝니다. 등록된 vault의 역할은 `vault-builder set-role --vault <이름> --role <역할>` 명령으로 바꿉니다.

### 17.3 저장소 연결하기

저장소 연결은 `vault-builder link`, `vault-builder unlink`, `vault-builder list` 명령으로 합니다. 연결 설정 JSON 파일을 손으로 고치지 않아도 됩니다.

```bash
vault-builder link --repo /path/to/my-app --dry-run
vault-builder link --repo /path/to/my-app
vault-builder link personal team app --repo /path/to/my-app
vault-builder unlink personal --repo /path/to/my-app
vault-builder list --repo /path/to/my-app
```

`link [VAULT ...] [--repo PATH] [--project ID] [--dry-run]`

- vault 이름을 여러 개 줄 수 있습니다. 연결 설정을 한 번 읽고 한 번 써서 모든 vault를 연결합니다. 같은 이름을 두 번 주어도 한 번만 더합니다.
- 이름은 연결 설정 `vaults` 항목에 있어야 합니다. 없는 이름이 하나라도 있으면 아무것도 쓰지 않고, 이름 목록을 출력하고 exit 1로 끝납니다.
- 이름을 주지 않으면 공유 vault를 모두 연결합니다. 공유 vault는 역할이 17.2 표에서 "공유"인 vault와 표에 없는 역할의 vault입니다. 역할이 `project`인 vault는 한 저장소의 이력이라 이름으로만 연결합니다. 그래서 이름 없는 `link` 뒤에 이 저장소에 project vault가 없으면 `link: Link a project vault by name: vault-builder link <name> --repo <root> (project vaults: ...)` 줄로 연결 명령을 안내합니다. 공유 vault가 하나도 없으면 같은 안내를 담아 exit 1로 끝납니다.
- 연결한 vault의 역할이 17.2 표에 없으면 공유 vault로 연결했다는 줄을 출력합니다.
- 연결 뒤 project가 vault를 2개 이상 쓰고 그중 역할 `project` vault가 정확히 하나이면, 그 vault를 기본 쓰기 vault(`default_write_vault`)로 정합니다. 이미 정한 값은 바꾸지 않습니다. 직접 정하려면 `vault-builder set-default-write --project <id> --vault <이름>` 명령을 씁니다.
- `--repo` 옵션이 없으면 현재 폴더의 저장소를 씁니다. git 저장소 밖이면 exit 1로 끝납니다.
- 아래 project 찾기 순서로 이미 연결된 project를 찾으면 그 project의 `vaults` 목록에 vault를 더합니다. 이때 `root` 값을 현재 저장소 root로, `remote` 값을 현재 `origin` URL로 고칩니다. `origin` remote가 없으면 `remote` 값은 그대로 둡니다.
- 찾지 못하면 새 project 항목을 만듭니다. project id는 `--project` 옵션 값이고, 옵션이 없으면 저장소 root 폴더 이름입니다. 그 id를 다른 저장소가 이미 쓰고 있으면 거부하므로 `--project` 옵션으로 다른 id를 줍니다.
- `--project` 옵션을 주었는데 project 찾기가 다른 id를 찾으면 거부하고 찾은 id를 알려 줍니다.
- 역할이 `team`인 vault도 연결할 수 있습니다. 연결하면 그 저장소 세션에서 MCP로 읽을 수 있는 vault(16절의 닿는 vault)가 늘어납니다.

`unlink <vault> [--repo PATH] [--dry-run]`

- 찾은 project의 `vaults` 목록에서 vault를 뺍니다. 목록이 비면 그 project 항목을 지웁니다.
- project를 찾지 못했거나 vault가 이미 목록에 없으면 그 사실을 한 줄로 출력하고 exit 0으로 끝납니다.

`list [--repo PATH]`

- 읽기 전용입니다. vault마다 이름, 역할, 경로, 경로 존재 여부를 출력하고, project마다 id, root, remote, vault 목록을 출력합니다.
- 마지막 줄에는 `--repo` 저장소(옵션이 없으면 현재 폴더)가 어느 project에 어느 단계로 연결되었는지, 또는 연결되지 않았는지를 출력합니다.

세 명령 모두 연결 설정이 없으면 "먼저 install을 실행하라"는 줄을 출력하고 exit 1로 끝납니다. MCP 설정 파일과 저장소 안의 파일은 바꾸지 않습니다.

#### project 찾기 순서

`link`, `unlink`, `list` 명령, recall 스크립트, `memory-scan --repo` 명령, MCP 라우터는 같은 순서로 저장소의 project 항목을 찾습니다. 먼저 저장소의 main worktree root를 구합니다. 같은 저장소의 `git worktree` 폴더도 같은 root가 됩니다.

1. root 경로: `projects[*].root` 값이 현재 저장소 root와 같은 항목
2. remote URL: 저장소에 `origin` remote가 있으면 그 URL과 `projects[*].remote` 값이 같은 항목. `git@host:path`, `https://host/path` 형태와 끝의 `.git` 차이는 무시하고 비교합니다.
3. 폴더 이름: project id가 저장소 root 폴더 이름과 같은 항목. 단, 그 항목의 `remote` 값과 저장소 `origin` URL이 서로 다르면 제외합니다.

한 단계에서 하나가 맞으면 그 항목을 씁니다. 0개면 다음 단계로 갑니다. 2개 이상이면 추측하지 않고 "여러 project가 맞는다"는 이유와 id 목록을 출력합니다. 그래서 저장소 폴더 이름을 바꾸거나 다른 이름으로 clone해도 remote URL로 연결이 유지됩니다. 이름이 같고 `origin` remote도 없는 두 저장소는 구분하지 못합니다.

2단계나 3단계로 찾았는데 연결 설정의 `root` 값이 현재 위치와 다르면 "`link`를 다시 실행하라"는 줄이 출력에 붙습니다. `link` 명령을 다시 실행하면 `root` 값이 현재 위치로 바뀝니다.

연결한 뒤 그 vault에서 memory-cleanup skill을 실행하면 요약 페이지가 생깁니다. 요약 페이지가 생기기 전에는 `/project-memory`가 "요약 페이지 없음" 한 줄만 출력합니다.

### 17.4 L1 메모리 정리하기 (memory-cleanup, memory-scan)

L1 메모리는 `<설정 폴더>/projects/<key>/memory/` 폴더입니다. `<key>` 부분은 저장소 root 절대 경로의 영숫자가 아닌 문자를 모두 `-`로 바꾼 값입니다. `MEMORY.md` 파일은 목록이고, 나머지 note 파일은 주제당 하나입니다.

먼저 읽기 전용 명령으로 대상을 확인합니다. 연결 설정에 등록된 vault 폴더에서 실행합니다.

```bash
python3 tools/wiki/cli.py memory-scan
python3 tools/wiki/cli.py memory-scan --project my-app --json
python3 tools/wiki/cli.py memory-scan --repo /path/to/my-app --json
```

`--project ID` 옵션은 연결 설정의 project id로 바로 찾습니다. `--repo PATH` 옵션은 그 폴더의 저장소를 17.3의 project 찾기 순서로 찾습니다. 두 옵션은 함께 쓸 수 없습니다.

`memory-scan` 명령은 이 vault에 연결된 project마다 L1 note 목록, note별 sha256과 type, 그리고 같은 project에 연결된 모든 vault에 이미 들어왔는지(`imported` 또는 `new`)를 출력합니다. 파일은 하나도 쓰지 않습니다. 연결 설정이 없거나 이 vault가 `vaults` 항목에 없으면 이유 한 줄과 함께 exit 1로 끝납니다. 이 경우 17.1과 17.3을 따라 연결 설정을 먼저 만듭니다.

정리는 vault 세션에서 agent에게 요청합니다.

```text
메모리 정리해주세요.
```

agent는 `.agents/skills/memory-cleanup/SKILL.md` 절차를 따릅니다.

1. `memory-scan` 결과에서 이미 들어온 note는 건너뜁니다. 별도 상태 파일은 만들지 않습니다.
2. 새 note는 역할 기준으로 대상 vault를 하나 골라 `raw/imports/claude-memory/<project id>--<note 이름>--<sha256 앞 8자>.md` 경로로 bytes 그대로 복사합니다. 내용이 바뀐 note는 새 raw 파일이 되고, 이전 파일은 그대로 남습니다.
3. `ingest-source`, draft 작성, `publish-draft --report` 순서로 반영합니다. project 지식에는 frontmatter `project: <project id>` 값을 넣습니다. 결정은 `type: decision` 페이지로, 사용자 교정과 작업 규칙은 `tags` 목록에 `correction` 또는 `rule` 값이 있는 페이지로 만듭니다.
4. 기존 지식과 충돌하는 note는 publish하지 않습니다. 나머지를 모두 반영하고 commit한 뒤 contradiction review를 만들고, 같은 세션에서 사용자에게 판단을 묻습니다. 판단을 미루면 review가 남고, 가장 오래된 review가 14일을 넘으면 `metrics --check`가 실패합니다.
5. 팀 vault는 `memory-cleanup/<YYYY-MM-DD>` branch에서 작업하고 `gh pr create` 명령으로 PR을 엽니다.
6. 바뀐 vault마다 `maps build --report` 명령과 `scripts/release_gate.sh` 스크립트를 실행합니다. 개인 vault와 project vault는 gate가 통과하면 commit합니다.

L1 메모리 파일은 읽기만 하고 수정, 이동, 삭제하지 않습니다.

요약 페이지 `wiki/maps/projects/<project id>.md` 파일은 `maps build` 명령이 만들고, 섹션 3개를 항상 이 순서로 가집니다.

| 섹션 | heading | 들어가는 것 |
|---|---|---|
| `open` | `## Open Work` | `wiki/inbox.md` item 가운데 `Project: <project id>` 줄이 있고 `Status` 값이 `unprocessed` 또는 `deferred`인 남은 일 |
| `decisions` | `## Current Decisions` | `type` 값이 `decision`이고 deprecated가 아닌 페이지 |
| `corrections` | `## Corrections and Rules` | `tags` 목록에 `correction` 또는 `rule` 값이 있는 페이지 |

섹션 하나는 추정 token 750 이하, 페이지 본문 전체는 1,500 이하입니다. 추정 token은 한글 글자 수를 2로, 나머지 글자 수를 4로 나눈 값의 합입니다. 넘는 섹션은 끝 줄부터 생략하고 생략한 항목 수를 한 줄로 적습니다.

### 17.5 요약 불러오기 (/project-memory)

연결된 저장소의 Claude Code 세션에서 불러오기 skill을 직접 호출합니다.

```text
/project-memory
/project-memory open
/project-memory decisions
/project-memory corrections
```

| 인자 | 출력 |
|---|---|
| 없음 | 섹션 3개 전부. 전체 추정 token 1,500 이하 |
| `open` | 남은 일 |
| `decisions` | 현재 결정 |
| `corrections` | 교정과 작업 규칙 |

요약에 들어가는 페이지는 vault마다 아래 규칙으로 정합니다(`docs/contracts/vault-map-v2.md`).

1. frontmatter `project` 값이 이 저장소의 project id인 페이지와 남은 일은 요약 페이지 `wiki/maps/projects/<project id>.md`에서 읽습니다.
2. `project`가 없는 페이지와 남은 일은 그 vault를 연결한 project가 이 저장소 하나뿐일 때 넣습니다.
3. 그 vault를 연결한 project가 둘 이상이면 연결 설정의 `sharable` 값이 `true`일 때만 넣습니다. 값이 없으면 넣지 않습니다. `vault-builder set-sharable --vault <이름> --value true|false` 명령으로 정하고, `--clear` 옵션으로 지웁니다.

2와 3의 항목은 recall 스크립트가 그 vault의 읽기 전용 명령 `python3 tools/wiki/cli.py maps no-project-summary`를 실행해 읽고, 같은 섹션의 1 항목 뒤에 붙입니다. 이 명령이 없는 이전 vault 도구이면 그 사실을 한 줄로 알리고 1 항목만 출력합니다. `maps build`가 쓰는 요약 페이지는 연결 설정과 상관없이 같습니다.

섹션 하나만 요청하면 출력은 추정 token 750 이하입니다. 연결된 vault가 여러 개면 vault마다 vault 이름과 요약 페이지 경로를 담은 머리 줄이 붙습니다. 합계가 상한을 넘으면 상한을 vault 수로 나눠 vault마다 줄 단위로 자르고, 잘렸다는 줄을 하나 붙입니다. 각 줄 끝의 `wiki/...` 경로는 근거 페이지이며, 필요할 때만 열어 읽습니다.

Claude는 spec, 설계, 계획을 쓸 때나 지난 결정과 남은 일을 물을 때 이 skill을 스스로 호출할 수 있습니다. 다만 호출 여부는 Claude가 판단하므로 보장되지 않습니다. 필요하면 위처럼 직접 호출하세요. skill은 recall 스크립트 실행만 미리 허용하므로 호출할 때 권한 확인이 뜨지 않습니다.

recall 스크립트는 세션의 현재 작업 폴더로 저장소를 찾습니다. 세션 안에서 다른 저장소로 `cd` 명령으로 이동한 뒤 호출하면 그 저장소의 요약이 나옵니다.

출력할 내용이 없으면 이유 한 줄을 출력하고 exit 0으로 끝납니다.

| 상황 | 출력 |
|---|---|
| git 저장소 밖 | git 저장소가 아니라는 줄 |
| 연결되지 않은 저장소 | 찾지 못한 이유 한 줄과 `link: run vault-builder link <vault> --repo <root> (vaults: a, b)` 형식의 연결 명령 한 줄(17.3) |
| 연결 설정이 없거나 손상됨 | 그 사실을 담은 줄 |
| 연결된 vault 폴더나 요약 페이지가 없음 | 그 vault를 건너뛰었다는 줄 |
| 요청한 섹션 heading이 요약 페이지에 없음 | 그 vault의 섹션이 없다는 줄 |
| 인자가 `open`, `decisions`, `corrections`, 없음 외의 값 | 사용법 한 줄, exit 2 |

### 17.6 setup, install, uninstall, doctor

불러오기 skill, recall 스크립트, 라우터, hook, 연결 설정, MCP 서버 항목은 `vault-builder` 명령으로 설치하고 지웁니다. 이 명령은 Python 3.10 이상으로 실행됩니다.

setup, install, uninstall은 user scope MCP 설정 파일 `$HOME/.claude.json` 파일을 고칩니다(`CLAUDE_CONFIG_DIR` 값이 있으면 `<설정 폴더>/.claude.json` 파일). Claude Code는 실행 중에 이 파일을 자주 씁니다. 파일 교체는 원자적이라 파일이 깨지지는 않지만, 두 쓰기가 겹치면 한쪽 변경이 사라질 수 있습니다. 이 경합을 피하려면 아래 순서로 실행합니다.

1. Claude Code를 모두 종료합니다. 터미널의 `claude` 세션과 IDE 안의 Claude Code도 포함합니다.
2. 터미널에서 setup, install 또는 uninstall 명령을 실행합니다.
3. Claude Code를 다시 시작합니다. install 뒤라면 `/mcp` 목록에서 vault 서버가 `vault-wiki` 하나인지 확인합니다.
4. doctor 명령을 실행합니다. setup은 마지막 단계에서 doctor를 직접 실행합니다.

```bash
vault-builder setup --dry-run
vault-builder setup --vault team="$HOME/team-vault" --role team=team --repo /path/to/my-app
vault-builder install "$HOME/vault" --name personal --with-hooks --dry-run
vault-builder install "$HOME/vault" --name personal --with-hooks
vault-builder doctor
vault-builder doctor "$HOME/vault"
vault-builder uninstall "$HOME/vault" --dry-run
vault-builder uninstall "$HOME/vault"
vault-builder uninstall --all
```

`--dry-run` 옵션은 계획만 출력하고 아무 파일도 쓰지 않습니다.

`init-vault.sh`는 Python 3.10 이상을 고른 뒤 같은 install 명령을 실행합니다. `bash init-vault.sh /path/to/vault --name personal`처럼 `--name` 옵션을 그대로 넘길 수 있습니다. 이미 vault인 폴더는 init으로 다시 만들 수 없고, 기존 vault 등록은 install로 합니다.

`setup [--vault NAME=PATH]... [--role NAME=ROLE]... [--repo PATH]... [--project-vault NAME=PATH] [--default-write NAME] [--yes] [--dry-run]`

설치 명령(1절)이 마지막에 실행하는 명령입니다. 아래 4단계를 차례로 실행하고, 단계마다 `setup: [N/4]` 줄을 출력합니다. vault-builder 파일과 설정만 설치하고 다른 도구는 설치하지 않습니다.

1. 사전 검사. 파일을 쓰지 않습니다. 옵션, 연결 설정, `--repo` 저장소를 확인하고 계획을 출력합니다. 그다음 manifest가 있는 vault마다 audit을 실행합니다. manifest schema가 예전 것이거나 audit 계획에 `create`, `replace` 항목이 있는 vault는 업그레이드할 vault로 계획에 함께 출력합니다. 어느 vault든 audit에 `review-required`나 `invalid` 항목이 있으면, 그런 경로를 모든 vault에 걸쳐 한 번에 출력하고 아무것도 쓰지 않고 멈춥니다. apply가 이런 항목이 있는 계획을 거부하기 때문입니다. 경로마다 audit 이유와 고치는 방법을 함께 출력합니다. vault-builder 관리 파일이 배포된 어느 버전과도 다르면, 고친 내용이 필요할 때 vault 밖에 복사해 두고 출력된 `cp` 명령으로 vault-builder 버전을 다시 넣은 뒤 다시 실행합니다. 관리 파일이 없어 audit이 실패할 때도 같은 `cp` 명령을 출력합니다. 위키 페이지가 source 페이지 규칙을 어기면 페이지를 고칩니다. 더 새 vault-builder가 쓴 manifest이면 vault-builder를 업데이트합니다. vault마다 install의 `--dry-run` 검사를 실행합니다. 업그레이드할 vault는 schema와 audit 검사만 빼고 나머지 검사를 같은 방법으로 받습니다. 하나라도 실패하면 아무것도 쓰지 않고 멈춥니다. 마지막으로 대화 모드에서는 진행할지 묻고, Claude Code가 실행 중인지 확인합니다.
2. vault마다 업그레이드가 필요하면 audit 계획을 apply하고(1.1절의 apply와 같은 동작이며 실패하면 vault 파일을 되돌립니다), 이어서 hook을 포함해 install을 실행합니다(`install --with-hooks`). 업그레이드는 vault-builder 관리 파일과 manifest만 바꾸고 wiki 페이지와 raw source는 그대로 둡니다. 그래서 `setup`으로 설치하면 hook이 항상 설치됩니다. 환경 변수는 그대로 넘기므로, `VAULT_GRAPH_BIN`이나 `PATH`에서 vg를 찾으면 그 경로를 `vault-hooks.json`에 적습니다. vg를 설치하는 도구가 그 경로를 `VAULT_GRAPH_BIN`에 넣고 setup을 실행하면 hook이 그 vg를 씁니다.
3. 저장소마다 `link`를 한 번 실행해 공유 vault 전부와 그 저장소의 project vault를 연결합니다. 기본 쓰기 vault가 정해지지 않으면 대화 모드에서는 묻고, 그 밖에는 `--default-write` 값을 쓰거나 `vault-builder set-default-write` 명령을 안내합니다.
4. 경로 없이 doctor를 실행합니다.

- `setup`이 다루는 vault는 연결 설정에 이미 있는 vault 전부와 새 vault입니다. 이미 있는 vault는 install이 vault 밖 단계만 실행합니다. 새 vault는 `--vault`로 준 vault, 연결 설정이 비어 있고 `personal` 역할 vault도 주지 않았을 때 만드는 `$HOME/vault`의 `personal` vault, 아직 연결되지 않은 `--repo` 저장소마다 만드는 project vault(`$HOME/vaults/<저장소 폴더 이름>`)입니다. `--vault`와 `--role`로 역할 `project` vault를 주면 project vault를 따로 만들지 않고 그 vault를 연결합니다. project vault의 경로와 이름은 `--project-vault NAME=PATH`로 바꿉니다. 이 옵션은 `--repo`가 하나일 때만 씁니다.
- `--role NAME=ROLE`은 vault마다 역할을 정합니다. 없으면 새 vault는 `personal`이고, 이미 있는 vault는 지금 역할을 유지합니다(17.2).
- 같은 명령을 다시 실행해도 결과가 같습니다. 중간에 실패하면 원인을 고치고 같은 명령을 다시 실행합니다. vault를 나중에 더할 때도 위 예시처럼 새 vault만 넘깁니다.
- 대화 모드는 터미널(`/dev/tty`)이 있고 `--yes`가 없을 때입니다. `--vault`, `--repo`, `--project-vault`를 하나도 주지 않으면 개인 vault 경로, 연결할 저장소, project vault 생성 여부, 다른 vault 추가 여부를 차례로 묻습니다. 실행 전에는 항상 계획을 보여 주고 진행할지 묻습니다.
- vg를 찾지 못하면 `vault-hooks.json`에 vg 경로가 적히지 않고, hook은 recall 스크립트와 직접 세션 수집을 씁니다.
- 종료 코드: 0은 성공, 1은 실패(사전 검사, install, link 실패, doctor가 문제를 찾음), 2는 옵션이 빠졌거나 잘못됨(터미널이 없는데 `--yes`와 `--dry-run`이 모두 없을 때 포함), 3은 Claude Code가 실행 중이라 멈춤입니다. 대화 모드는 Claude Code가 실행 중이면 종료할 때까지 Enter를 기다립니다.

`install <path> [--name NAME] [--role ROLE] [--with-stenc] [--with-hooks] [--dry-run]`

- 대상 폴더가 없거나 비어 있으면 새 vault를 만듭니다. 임시 폴더에서 vault를 조립하고 lint로 검사한 뒤 한 번에 대상 경로로 옮깁니다. 중간에 실패하면 대상 폴더는 생기지 않습니다.
- 대상이 이미 vault이면 vault 밖 파일만 설치합니다. 이때 audit 결과에 `create`, `replace`, `review-required`, `invalid` 항목이 하나라도 있으면 거부합니다. `vault-builder setup`을 실행하면 사람 검토 항목이 없는 vault는 업그레이드한 뒤 설치합니다(17.6). 직접 하려면 1.1절의 audit/apply를 먼저 실행하세요.
- uninstall한 vault(manifest는 없고 `wiki/index.md` 파일은 있는 폴더)는 install 한 번으로 다시 설치됩니다. install이 audit 계획을 만들고, `review-required`나 `invalid` 항목이 없으면 apply와 같은 방법으로 managed 파일과 manifest를 되돌린 뒤 vault 밖 파일을 설치합니다. 계획에 그런 항목이 있으면 경로 목록과 audit/apply 안내를 출력하고 아무것도 쓰지 않습니다. `--dry-run` 옵션을 주면 audit 계획 요약과 vault 밖 계획만 출력합니다. vault 밖 설치가 실패하면 vault 밖 변경만 되돌리고, 되돌린 managed 파일과 manifest는 남습니다. 이때 install을 다시 실행하면 vault 밖 설치부터 이어갑니다.
- 불러오기 skill, recall 스크립트, 라우터 `<설정 폴더>/vault-builder/llm_wiki_router.py`를 설치합니다. 이미 있는 파일이 builder가 만든 것이 아니면 교체하지 않고 목록에 적습니다.
- 연결 설정이 없으면 만들고, 이 vault를 `vaults` 항목에 `--role` 값(없으면 `personal`)으로 더합니다. 저장소 연결은 `link` 명령(17.3)으로 합니다.
- `--with-hooks` 옵션을 주면 `<설정 폴더>/settings.json`의 `hooks.SessionStart`, `hooks.PreCompact`, `hooks.SessionEnd`에 launcher `<설정 폴더>/vault-builder/vault_hooks.py`를 실행하는 항목을 하나씩 더합니다. 그리고 `<설정 폴더>/vault-builder/vault-hooks.json`에 vg 절대 경로를 적습니다. vg는 환경 변수 `VAULT_GRAPH_BIN` 값, 없으면 `PATH`에서 찾습니다. vg를 찾지 못하면 vg 경로 없이 설치하고 한 줄로 알립니다. 이때 `vault-hooks.json`에 적힌 vg가 더 이상 실행 파일이 아니면(vg를 지운 경우) 그 파일을 지웁니다. 아직 실행되는 vg 경로는 그대로 둡니다. install 명령의 기본값은 hook 없음이고, `setup`은 항상 이 옵션을 붙여 install을 실행합니다.
- MCP 설정 파일의 `mcpServers` 객체 아래에 서버 항목 `vault-wiki` 하나를 둡니다. 값은 `{"command": <interpreter>, "args": ["-I", <라우터 경로>]}` 형태입니다. 모든 vault가 이 항목 하나를 함께 쓰므로, 두 번째 vault부터는 항목이 이미 같으면 MCP 설정 파일을 다시 쓰지 않습니다. 다른 key와 다른 서버 항목은 바꾸지 않습니다.
- interpreter 경로는 install을 실행한 Python과 같은 파일을 가리키는 경로 가운데 patch version(`3.12.4` 같은 형태)이 없는 경로를 고릅니다. 그런 경로가 없으면 실행한 경로를 그대로 쓰고, Python을 올리면 install을 다시 실행하라는 알림을 출력합니다. 서버 항목과 recall 스크립트의 `PYTHON=` 줄에 같은 경로를 씁니다.
- 이미 있는 `vault-wiki` 항목이 이 설정 폴더의 라우터를 가리키지 않으면 다른 프로그램의 항목으로 보고 아무것도 쓰지 않고 거부합니다.
- 이전 이름의 서버 항목 `llm-wiki`가 있고 `args` 값이 `["-I", <이 설정 폴더의 라우터 경로>]`이면 builder가 만든 항목입니다. install은 같은 한 번의 쓰기로 이 항목을 지우고 `vault-wiki` 항목을 씁니다. `--dry-run`은 이 이름 변경을 출력합니다. `args` 값이 다른 `llm-wiki` 항목은 다른 프로그램의 것으로 보고 그대로 둡니다. 이름이 바뀌면 도구 이름도 `mcp__llm-wiki__<도구>`에서 `mcp__vault-wiki__<도구>`로 바뀝니다. 권한 허용 규칙이나 skill에 옛 도구 이름이 있으면 직접 고치고, Claude Code를 다시 시작합니다.
- 이전 버전 builder가 만든 MCP 등록(vault 이름이 붙은 서버 항목이나 vault 안의 MCP 설정 파일)이 있으면 고치지 않고 거부합니다. 이전 버전 builder로 uninstall한 뒤 이 버전으로 install합니다.
- `--name` 옵션이 없으면 대상 폴더 이름을 vault 이름으로 씁니다. 폴더 이름이 `^[A-Za-z0-9][A-Za-z0-9_.-]*$` 형식이 아니면 거부하고 `--name` 옵션을 요구합니다.
- `CLAUDE_CONFIG_DIR` 폴더는 `$HOME` 안에 있어야 합니다. vault 밖 기록은 `$HOME` 기준 상대 경로로만 적기 때문입니다.
- vault 밖 변경 중 실패하면 바꾼 파일을 backup으로 되돌립니다.
- 설치한 vault 밖 파일과 서버 항목은 `.vault-builder/manifest.json`의 vault 밖 기록에 남깁니다. 라우터와 서버 항목은 여러 vault가 함께 쓰는 파일로 기록합니다. `CLAUDE_CONFIG_DIR` 값이 있으면 그 폴더 아래에 설치합니다.

`uninstall <path> [--dry-run]`

- vault 안에서는 기록된 hash와 같은 managed 파일과 manifest를 지웁니다. `.gitignore` 파일은 남깁니다. 사용자가 고친 managed 파일은 남기고 경로 목록을 출력합니다.
- `raw/`, `wiki/`, `scratch/`, `.git` 폴더와 manifest에 없는 파일은 어떤 경우에도 지우지 않습니다. commit도 하지 않습니다.
- 다른 vault가 남으면 연결 설정의 `vaults` 항목과 모든 `projects` 항목의 `vaults` 목록에서 이 vault를 빼고, `vaults` 목록이 빈 project 항목을 지웁니다. 서버 항목, 라우터, skill, recall 스크립트는 그대로 둡니다.
- 마지막 vault이면 서버 항목, 라우터, 불러오기 skill, recall 스크립트, 연결 설정을 지우고, 비게 된 `<설정 폴더>/vault-builder/` 폴더와 `<설정 폴더>/skills/project-memory/` 폴더를 지웁니다. builder가 만든 이전 이름의 `llm-wiki` 항목이 남아 있으면 함께 지웁니다. `mcpServers` key는 비어도 남깁니다. hook도 이때 지웁니다. `settings.json`에서는 launcher를 실행하는 hook 항목만 빼고, launcher 파일과 `vault-hooks.json`은 install이 쓴 내용과 같을 때만 지웁니다.
- 공유 파일은 기록된 hash와 같거나 지금 builder가 설치하는 형태와 같을 때만 지웁니다. 서버 항목은 `command` 값이 다른 interpreter여도 `args` 값이 `["-I", <라우터 경로>]`이면 같은 형태로 보고, recall 스크립트는 `PYTHON=` 줄만 다르면 같은 형태로 봅니다. 다른 vault를 다른 Python으로 install하면 이 공유 파일이 다시 쓰이기 때문입니다. 그 밖의 내용이면 남기고 경로를 출력합니다.
- 이전 버전 builder로 설치한 vault이면 아무것도 쓰지 않고 거부합니다. 이전 버전의 uninstall을 사용합니다.
- vault 밖 변경이 중간에 실패하면 모두 되돌리고 vault 안은 건드리지 않습니다.
- uninstall 뒤 같은 vault를 다시 쓰려면 install만 다시 실행합니다. 연결 설정이 지워졌다면 `link` 명령도 다시 실행합니다.

`uninstall --all [--dry-run]`

- 연결 설정의 vault마다 차례로 위 `uninstall`을 실행합니다. 그래서 마지막 vault에서 공유 파일, 서버 항목 `vault-wiki`, hook이 지워집니다. 각 vault의 `raw/`, `wiki/`는 남습니다.
- 한 vault가 실패하면 그 vault와 오류를 출력하고 멈춥니다. 남은 vault, `vault-builder` 명령, builder checkout은 그대로 둡니다.
- 모든 vault를 지운 뒤 `$HOME/.local/bin/vault-builder` 파일을 지웁니다. 설치 명령이 쓴 표시 줄이 없는 파일은 사용자 파일로 보고 지우지 않습니다.
- 마지막으로 그 파일에 적힌 builder checkout을 지웁니다. 기본 위치 `$HOME/.local/share/vault-builder`의 checkout만 지우고, `VAULT_BUILDER_HOME`으로 고른 checkout이나 다른 위치의 checkout은 남깁니다.
- vault-builder가 설치하지 않은 항목(다른 도구의 MCP 서버 항목과 실행 파일)은 지우지 않습니다. 설치 명령에 `--uninstall`을 붙이면(`curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh | bash -s -- --uninstall`) `vault-builder uninstall --all`을 실행하고, vault 폴더는 남긴 채 그 경로를 출력합니다.

`doctor [<path>]`

경로를 주면 그 vault 하나를 검사합니다. 경로를 생략하면 연결 설정의 vault 전부를 검사하고, 문제 줄 앞에 vault 이름과 경로를 붙입니다. 여러 vault가 함께 쓰는 항목은 한 번만 검사합니다.

manifest, 수정된 managed 파일, 불러오기 skill과 recall 스크립트(실행 권한, `PYTHON=` 줄의 interpreter, git 밖 폴더에서의 실행 결과), 연결 설정, git version(2.31 이상)을 검사하고 문제 목록을 출력합니다. 사용자가 고친 managed 파일도 문제로 셉니다. MCP 등록은 아래를 검사합니다.

- 서버 항목 `vault-wiki`가 있고 `args` 값이 `["-I", <라우터 경로>]`인지
- builder가 만든 이전 이름의 서버 항목 `llm-wiki`가 남아 있지 않은지. 남아 있으면 `vault-builder setup`으로 이름을 바꿉니다.
- 라우터 파일이 있고 내용이 builder 원본이나 기록된 hash와 같은지
- 서버 항목의 `command` 값이 Python 3.10 이상으로 실행되는지
- 서버 항목의 `command`와 `args` 값으로 라우터를 실제로 시작해 30초 안에 `initialize`와 `tools/list` 요청에 응답하고, 도구 24개를 모두 돌려주는지
- 이전 버전 builder가 만든 MCP 등록이 남아 있지 않은지

hook, vg, `vault-builder` 명령은 아래를 검사합니다.

- `settings.json`에 SessionStart, PreCompact, SessionEnd launcher 항목이 하나씩 있는지. hook이 하나도 설치되지 않았으면 문제가 아니라 알림(`Note:`)으로 출력합니다.
- launcher 파일이 있고 builder 원본과 같은지
- `vault-hooks.json`의 `vg` 값이 절대 경로이고 실행 파일인지. 이 파일이나 `vg` 값이 없으면 hook이 recall 스크립트와 직접 세션 수집을 쓴다고 알림으로 출력합니다.
- 그 vg가 `vg --help`로 10초 안에 exit 0으로 끝나는지
- `$HOME/.local/bin/vault-builder` 파일에 표시 줄이 있고, 적힌 checkout이 builder checkout이고, 실행 권한이 있는지. 파일이 없으면 알림으로 출력합니다.

문제 줄은 `Problem: ... Fix: <명령>` 형식이고, 끝에 고치는 명령 하나가 붙습니다. 대부분 `vault-builder setup`입니다. 문제가 없으면 exit 0, 있으면 exit 1입니다. 서버 항목의 interpreter 경로에 patch version이 들어 있고 고를 수 있는 다른 경로가 있으면 install을 다시 실행하라는 알림만 출력하고 exit 값에는 넣지 않습니다. doctor는 어떤 파일도 쓰지 않습니다.

### 17.7 세션 원문 보관하기 (sessions)

Claude Code는 대화 파일(transcript)을 기본 30일 뒤에 지웁니다. `sessions capture` 명령은 그 전에 대화 원문을 project vault의 `raw/sessions/` 폴더로 옮겨 둡니다. 보관한 원문은 검색 도구가 읽을 수 있고, wiki claim의 근거로 바로 인용할 수 있습니다.

보관 규칙은 다음과 같습니다.

- 세션 원문은 저장소에 연결된 역할 `project` vault에만 들어갑니다. 역할이 `personal`이나 `team`인 vault에는 어떤 경우에도 쓰지 않습니다.
- 수집 명령은 새 파일만 추가합니다. 기존 세션 파일은 사람도 agent도 고치거나, 이름을 바꾸거나, 지우지 않습니다.
- 세션 파일에는 source page를 만들지 않습니다. `ingest-source` 명령과 `bulk-ingest` 명령은 세션 파일 경로를 거부합니다. `bulk-ingest --all-raw`는 세션 파일을 건너뜁니다.
- 보관하는 내용은 사용자 발화, agent 답변 본문, 서브에이전트 대화, 도구 이름입니다. thinking 블록과 도구 입력·출력은 남기지 않습니다. 알려진 형식의 비밀값은 `[REDACTED:<종류>]` 표시로 바꿉니다. 목록에 없는 형식의 비밀값은 남을 수 있습니다.
- 수집 명령은 commit하지 않습니다. 세션 파일 commit은 사람이 합니다.

#### project vault 연결하기

수집 명령은 저장소에 역할 `project` vault가 정확히 하나 연결되어 있어야 파일을 씁니다. 설치 명령(1절)과 `vault-builder setup --repo <저장소>`는 연결되지 않은 저장소마다 project vault를 만들어 연결합니다(17.6). install은 `--role` 옵션이 없으면 새 vault를 역할 `personal` 값으로 등록하므로(17.2), install로 직접 만들 때는 아래 순서를 따릅니다.

1. project vault를 만듭니다: `vault-builder install /path/to/app-vault --name app --role project`
2. 저장소를 그 vault에 연결합니다: `vault-builder link app --repo /path/to/my-app`
3. `vault-builder list --repo /path/to/my-app` 명령으로 연결을 확인합니다.

이미 `personal` 역할로 등록한 vault는 `vault-builder set-role --vault app --role project` 명령으로 역할을 바꿉니다. project vault가 없는 저장소를 `link`하면 명령은 세션 원문이 저장되지 않는다는 경고 한 줄을 출력합니다.

수집 명령은 대상 vault의 manifest contract가 3 이상일 때만 씁니다. 지금 builder로 새로 install한 vault는 contract 3입니다. 이전 builder로 만든 vault라면 1.1절의 audit과 apply를 사람이 먼저 실행합니다. agent는 apply를 실행하지 않습니다.

#### `sessions capture`

`sessions capture [--transcript PATH] [--session-id ID] [--cwd PATH] [--trigger PreCompact|SessionEnd|manual] [--vault PATH] [--json]`

`sessions capture --normalized PATH [--rerender] [--cwd PATH] [--trigger ...] [--vault PATH] [--json]`

입력은 세 형태입니다.

- 훅 형태: `--transcript`와 `--normalized` 옵션이 없으면 stdin에서 Claude Code 훅 JSON 한 개를 읽습니다. 이 JSON의 `session_id`, `transcript_path`, `cwd`, `hook_event_name` 값을 씁니다. `hook_event_name` 값이 trigger가 됩니다. 훅은 이 명령을 인자 없이 호출하면 됩니다.
- 수동 형태: `--transcript PATH` 옵션으로 transcript 파일을 지정합니다. session id는 `--session-id` 값이고, 없으면 파일 이름에서 확장자를 뺀 값입니다. 작업 폴더는 `--cwd` 값이고, 없으면 transcript 첫 항목의 `cwd` 값, 그것도 없으면 현재 폴더입니다. trigger 기본값은 `manual`입니다.
- 정규화 형태: `--normalized PATH` 옵션으로 vault-graph가 만든 정규화 세션 파일(`vg-session-jsonl/1`)을 넘깁니다. 파일 형식은 `vault-session/v2`이고 닫힌 교환만 씁니다(`docs/agent/OPERATING-SCHEMA.md` 16.1절 "Session file v2"). session id와 agent는 정규화 파일 첫 줄에서 읽습니다. stdin에 훅 JSON이 있으면 그 `hook_event_name` 값을 trigger로, `cwd` 값을 작업 폴더로 쓰고, 훅 형태처럼 어떤 실패에서도 exit 0으로 끝납니다. 훅 JSON이 없으면 작업 폴더는 `--cwd` 값, 없으면 정규화 파일의 `cwd` 값입니다. `--rerender` 옵션은 이전 part의 이어 쓸 위치를 무시하고 닫힌 교환 전체를 새 part로 씁니다. 기존 파일은 바꾸지 않습니다. `--rerender`를 `--normalized` 없이 쓰거나, `--normalized`를 `--transcript`나 `--session-id`와 함께 쓰면 exit 2로 끝납니다. 모르는 옵션도 exit 2입니다.

```bash
python3 tools/wiki/cli.py sessions capture \
  --transcript "$HOME/.claude/projects/<key>/<session id>.jsonl" \
  --cwd /path/to/my-app
```

이 명령은 어느 vault 폴더에서 실행해도 됩니다. 대상 vault는 작업 폴더의 저장소를 17.3의 project 찾기 순서로 찾은 뒤, 그 project에 연결된 역할 `project` vault로 정합니다. `--vault PATH` 옵션은 역할 `project` vault가 둘 이상일 때 그중 하나를 고르는 데만 씁니다.

한 번 실행하면 대상 vault에 `raw/sessions/<agent>/<YYYY-MM-DD>-<session id>-part<N>.md` 파일 하나를 새로 만듭니다. 같은 세션을 다시 수집하면 이전 수집 뒤의 턴만 다음 part 번호 파일로 씁니다. 이전 part 파일은 바꾸지 않습니다. `wiki/`, index, log, `scratch/`는 바꾸지 않고, git 명령도 실행하지 않습니다.

쓰지 않는 경우와 종료 코드는 다음과 같습니다. 훅 형태 입력은 어떤 실패에서도 exit 0으로 끝나고 이유를 stderr에 적습니다. 세션 종료나 compaction을 막지 않기 위해서입니다.

| 상황 | 수동 형태 종료 코드 |
|---|---|
| 새 턴이 없음 | 0, `no new turns` 출력 |
| 저장소에 역할 `project` vault가 없음 | 0, 이유와 이 절의 연결 방법 안내 |
| 역할 `project` vault가 둘 이상이고 `--vault` 옵션이 없음 | 0, 후보 vault 이름 |
| 대상 vault manifest가 없거나 contract가 3 미만 | 0, migration을 먼저 실행하라는 안내 |
| `--vault` 경로가 그 project의 역할 `project` vault가 아님 | 1 |
| transcript 파일이 없거나 읽을 수 없음, session id 형식 오류 | 1 |
| 정규화 파일이 없거나 형식(`vg-session-jsonl/1`)에 맞지 않음, agent가 `claude-code`가 아님 | 1 |
| 연결 설정이 없거나 손상됨, 저장소를 찾지 못함, 여러 project가 맞음 | 1 |
| 이전 part 파일의 frontmatter에서 이어 쓸 위치를 읽을 수 없음 | 1 |
| 같은 part 번호를 다른 수집이 먼저 만들었고 한 번 재시도도 실패함, 디스크 쓰기 실패 | 1 |

#### `sessions status`

`sessions status [--json]`

세션 파일 수, 마지막 수집 시각(`collected_at`), 보관소 검사 실패 수, 아직 commit하지 않은 세션 파일 수를 출력합니다. 마지막 값은 vault가 git 작업 트리일 때 `git status --porcelain --untracked-files=all -- raw/sessions` 결과로 세고, git이 아니면 `unknown`을 출력합니다. 읽기 전용이고 항상 exit 0입니다.

commit하기 전에 세션 파일이 지워지면 어떤 검사도 잡지 못합니다. 이 명령으로 commit하지 않은 파일 수를 확인하고 commit합니다.

#### `sessions git-check`

`sessions git-check`

vault가 git 작업 트리가 아니면 `skipped`를 출력하고 exit 0으로 끝납니다. git 작업 트리이면 현재 branch 이력에서 `raw/sessions/` 아래에 추가된 적 있는 경로와 처음 추가된 내용을 모읍니다. 그 경로가 작업 트리에 없거나 내용이 처음 추가된 내용과 다르면 경로마다 한 줄을 출력하고 exit 1로 끝납니다. 파일을 되살리면 다시 통과합니다. `scripts/release_gate.sh`는 metrics 단계 뒤에 vault 루트에서 이 명령을 실행합니다.

#### 세션 파일 검사와 인용

- `lint` 명령은 세션 파일마다 경로 형식, frontmatter 필수 필드, 본문 해시(`body_sha256`)를 검사합니다. 실패 코드는 10절 표의 `session-` 코드입니다. 파일 형식은 `docs/agent/OPERATING-SCHEMA.md` 16.1절에 있습니다.
- `health` 출력 마지막 줄은 `Sessions: <수> files, last collected <시각|none>, hash mismatches <수>` 형식입니다. `metrics` report에도 세 값이 나오고, `metrics --check` 명령은 해시 불일치가 1 이상일 때만 실패합니다. 세션 파일 수가 늘어나는 것만으로는 실패하지 않습니다.
- `source-registry` 보고서는 세션 파일을 `unregistered`가 아니라 `session`(검사 실패 시 `session-invalid`) 상태로 보여 줍니다. Source Map에는 `session-invalid` 파일만 나오므로, 새로 수집해도 `maps build --check`가 실패하지 않습니다.
- wiki claim은 `raw/sessions/...` 경로를 source page 없이 근거로 인용할 수 있습니다. `#sha256=<파일 해시>`는 선택입니다. 붙이면 파일 해시를 비교하고, 안 붙이면 그 세션 파일이 보관소 검사를 통과했는지만 봅니다.

## 18. Git 사용하기

LLM Wiki는 Git으로 durable state를 보존합니다.

권장 commit 예시:

```text
ingest: add source page for example paper
wiki: update compiled knowledge concept
lint: repair broken wiki links
decision: record markdown knowledge store decision
schema: update agent operating contract
```

큰 작업은 작게 나눠 commit하는 것이 좋습니다.

예시:

```bash
git status --short
scripts/release_gate.sh
# Stage the files listed by git status --short.
git add -- wiki/example.md scratch/reports/example.md
git commit -m "wiki: update example concept"
```

`git add` 대상은 `git status --short`에 나온 실제 변경 파일입니다. Wiki content만 바꾼 경우에는 보통 `wiki/`, `scratch/reports/`, `scratch/drafts/`, `scratch/review/`가 포함될 수 있고, workflow/tooling 개선에서는 `docs/`, `tools/`, `scripts/`, `.github/`도 함께 stage해야 합니다.

`scripts/release_gate.sh`는 검사 범위를 세 단계 중 하나로 고릅니다. 단계는 `--tier check|unit|full` 인자로 정하고, 출력 첫 줄에 `[release-gate] tier: check`처럼 고른 단계를 씁니다.

| 단계 | 실행 내용 | 인자 없이 실행할 때 |
|---|---|---|
| `check` | `lint`, `maps build --check`, `metrics --check`, `sessions git-check`, whitespace 검사. test는 실행하지 않습니다 | vault의 기본값 |
| `unit` | `check`에 더해 `full_tier_only` 표시가 없는 test를 실행합니다 | builder 저장소의 기본값 |
| `full` | `check`에 더해 test 전부를 실행합니다 | 없음. builder 릴리스 전에 직접 실행합니다 |

gate는 모든 단계를 vault 루트에서 실행하고 report 파일을 쓰지 않습니다. Git 작업 트리에서는 실행 전후를 비교해, 추적 파일이 바뀌거나 ignore되지 않은 새 파일이 생기면 "Release gate changed tracked output"으로 실패합니다. `health`는 gate에 포함되지 않으므로 필요하면 따로 실행합니다. Stenc 문서 검증은 선택 기능이므로 필요한 경우에만 `WIKI_ENABLE_STENC=1 scripts/release_gate.sh`로 실행합니다.

같은 gate는 `.github/workflows/qa.yml`에서 인자 없이 실행됩니다. 그래서 vault의 CI는 `check`만 실행합니다. builder 저장소에는 `.github/workflows/qa-full.yml`이 더 있어 tag push, 수동 실행, 설치·migration 관련 파일을 바꾼 PR에서 `--tier full`을 실행합니다. 이 파일은 vault에 설치되지 않습니다.

## 19. 전체 명령 요약

| 목적 | 명령 |
|---|---|
| release gate 실행 (vault는 `check`, builder는 `unit`) | `scripts/release_gate.sh` |
| release gate의 test 전부 실행 | `scripts/release_gate.sh --tier full` |
| 전체 wiki 검사 | `python3 tools/wiki/cli.py lint --report` |
| 전체 wiki 상태 점검 | `python3 tools/wiki/cli.py health` |
| page 하나 검사 | `python3 tools/wiki/cli.py validate-page wiki/overview.md` |
| 원본 해시 계산 | `python3 tools/wiki/cli.py hash-source raw/sources/example.md` |
| 원본 등록 | `python3 tools/wiki/cli.py ingest-source raw/sources/example.md --report` |
| 원본/source inventory 확인 | `python3 tools/wiki/cli.py source-registry --report` |
| 여러 원본 등록 | `python3 tools/wiki/cli.py bulk-ingest raw/sources/a.md raw/sources/b.md --report` |
| 원본 등록 workflow checkpoint | `python3 tools/wiki/cli.py workflow ingest --source raw/sources/example.md --report` |
| draft 검증, 적용, lint | `python3 tools/wiki/cli.py publish-draft scratch/drafts/example.json --report` |
| multi-page draft transaction publish | `python3 tools/wiki/cli.py publish-batch scratch/drafts/example-batch.json --report` |
| draft 검증만 수행 | `python3 tools/wiki/cli.py apply-draft scratch/drafts/example.json --dry-run` |
| update preflight | `python3 tools/wiki/cli.py workflow update --target wiki/concepts/example.md --preflight --report` |
| query report scaffold | `python3 tools/wiki/cli.py workflow query --question "..." --prepare-report` |
| query answer report | `python3 tools/wiki/cli.py workflow query --question "..." --mode answer-with-report --report` |
| query answer capture draft | `python3 tools/wiki/cli.py workflow query --question "..." --mode answer-and-capture --draft scratch/drafts/query-answer.json --report` |
| human review item 생성 | `python3 tools/wiki/cli.py review create --type contradiction --summary "..." --related wiki/overview.md --context "..."` |
| human review queue 조회 | `python3 tools/wiki/cli.py review list` |
| human review item resolve | `python3 tools/wiki/cli.py review resolve scratch/review/example.md --status accepted --resolution "..."` |
| merge candidate scan | `python3 tools/wiki/cli.py merge scan --report --create-review` |
| navigation map 생성 | `python3 tools/wiki/cli.py maps build --report` |
| navigation map stale check | `python3 tools/wiki/cli.py maps build --check --report` |
| maintenance metrics report | `python3 tools/wiki/cli.py metrics --report` |
| maintenance metrics gate | `python3 tools/wiki/cli.py metrics --check --report` |
| MCP server 실행 | `python3 tools/wiki/mcp_server.py` |
| 세션 원문 수집 (훅은 인자 없이 stdin JSON으로 호출) | `python3 tools/wiki/cli.py sessions capture --transcript /path/to/session.jsonl` |
| 세션 보관소 상태 확인 (읽기 전용) | `python3 tools/wiki/cli.py sessions status` |
| 커밋된 세션 파일 삭제·변경 검사 | `python3 tools/wiki/cli.py sessions git-check` |
| L1 메모리 정리 대상 확인 (읽기 전용) | `python3 tools/wiki/cli.py memory-scan --json` |
| 첫 설치 (vault, hook, 저장소 연결, MCP 등록) | `curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh \| bash` |
| 설치 명령으로 전부 제거 (vault 폴더는 남김) | `curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/install.sh \| bash -s -- --uninstall` |
| vault 추가, 빠진 부분 다시 설치 | `vault-builder setup --vault team="$HOME/team-vault" --role team=team --repo /path/to/my-app` |
| 저장소를 공유 vault 전부에 연결 | `vault-builder link --repo /path/to/my-app` |
| 저장소를 vault 여러 개에 연결 | `vault-builder link personal team app --repo /path/to/my-app` |
| 저장소 연결 해제 | `vault-builder unlink personal --repo /path/to/my-app` |
| 연결 상태 확인 (읽기 전용) | `vault-builder list --repo /path/to/my-app` |
| 기본 쓰기 vault 정하기 | `vault-builder set-default-write --project my-app --vault app` |
| vault 역할 바꾸기 | `vault-builder set-role --vault app --role project` |
| 여러 저장소가 쓰는 vault의 `project` 없는 페이지를 모든 연결 저장소 요약에 넣기 | `vault-builder set-sharable --vault personal --value true` |
| project 요약 불러오기 (Claude Code 세션) | `/project-memory [open\|decisions\|corrections]` |
| vault 밖 파일 설치 | `vault-builder install /path/to/vault --name personal --with-hooks` |
| 모든 vault와 hook, vg 점검 (읽기 전용) | `vault-builder doctor` |
| vault 하나 점검 (읽기 전용) | `vault-builder doctor /path/to/vault` |
| vault 밖 파일 삭제 | `vault-builder uninstall /path/to/vault` |
| 모든 vault의 vault 밖 파일, `vault-builder` 명령, builder checkout 삭제 | `vault-builder uninstall --all` |

`vault-builder ...` 명령은 builder checkout 폴더에서 `python3 tools/vault_migration/cli.py ...`로도 실행할 수 있습니다.

## 20. 완료 기준

wiki 작업은 다음 조건을 만족해야 완료입니다.

- `raw/` 파일을 임의로 수정하지 않았습니다.
- source page가 필요한 경우 생성되었습니다.
- `canonical_source`와 `raw_sha256`이 맞습니다.
- durable page는 required frontmatter를 갖습니다.
- 중요한 claim에는 provenance가 있습니다.
- `wiki/index.md`가 최신입니다.
- `wiki/log.md`가 append되었습니다.
- 보고서가 `scratch/reports/`에 있습니다.
- human lock을 보존했습니다.
- contradiction을 지우지 않고 기록했습니다.
- `scripts/release_gate.sh`가 통과합니다.
