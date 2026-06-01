# LLM Wiki 사용법

이 문서는 LLM Wiki를 실제로 사용하는 순서를 설명합니다.

목표는 단순합니다.

```text
원본 자료는 raw/에 보관하고,
검증된 지식 문서만 wiki/에 남긴다.
```

내부 구조와 처리 원리는 [architecture.md](architecture.md)를 참고하세요. 이 문서는 사용자가 무엇을 실행하고, 어떤 결과를 확인하면 되는지에 집중합니다.

## 1. 5분 빠른 시작

GitHub에서 새 LLM Wiki vault를 바로 만들려면 아래 명령을 실행합니다.

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

`init-vault.sh`는 이미 초기화된 vault를 덮어쓰지 않습니다. 대상 폴더에 `wiki/index.md` 또는 `tools/wiki/cli.py`가 있으면 중단합니다.

Stenc 고정 포맷 spec/plan 문서는 선택 기능입니다. 기본 bootstrap과 기본 `init-vault.sh`는 Stenc 없이 vault를 생성합니다. Stenc 문서와 helper 도구까지 포함하려면 명시적으로 opt-in합니다.

```bash
curl -fsSL https://raw.githubusercontent.com/cutehackers/vault-builder/main/scripts/bootstrap.sh | bash -s -- --with-stenc ~/my-vault
./init-vault.sh --with-stenc ~/my-vault
```

## 2. 단순 사용 모델

LLM Wiki는 내부적으로 여러 품질 단계를 거치지만, 사용자는 세 가지 요청만 기억하면 됩니다.

| 사용자가 하려는 일 | 권장 skill/workflow | 사용자가 말하는 방식 |
|---|---|---|
| 새 원본을 wiki에 반영 | `wiki-ingest` | `raw/sources/example.md를 wiki에 반영해주세요.` |
| 기존 wiki page를 수정 | `wiki-update` | `wiki/concepts/example.md를 최신 원본 기준으로 업데이트해주세요.` |
| wiki 기준으로 질문 | `wiki-query` | `현재 wiki 기준으로 ingest와 lint의 관계를 설명해주세요.` |

현재 저장소의 결정론적 도구는 `tools/wiki/cli.py` 아래에 있습니다. 위 skill/workflow는 사람이 기억하기 쉬운 권장 사용 방식이며, 내부에서는 기존 CLI를 조합합니다.

현재 구현된 안정 명령은 `ingest-source`, `source-registry`, `bulk-ingest`, `publish-draft`, `publish-batch`, `review`, `merge scan`, `maps build`, `metrics`, `health`, `apply-draft`, `lint`, `hash-source`, `validate-page`, `workflow ingest/update/query`입니다. `wiki-ingest`, `wiki-update`, `wiki-query` skill 문서는 `agents/skills/` 아래에 있으며, agent가 아래 workflow를 일관되게 따르기 위한 repo-local 지침입니다.

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
3. `docs/architecture.md` - 내부 처리 원리
4. `AGENTS.md` - agent가 반드시 지켜야 할 짧은 계약
5. `wiki/overview.md` - 현재 wiki의 요약
6. `wiki/index.md` - durable wiki page 목록

agent가 상세 규칙을 확인해야 할 때는 `docs/agent/OPERATING-SCHEMA.md`를 읽습니다. draft 작성 규칙은 `docs/agent/DRAFTS.md`와 `tools/wiki/templates/draft-upsert-page.json`, `tools/wiki/templates/draft-batch-upsert-pages.json`에 있습니다.

## 5. 디렉터리 역할

| 경로 | 사용자가 알아야 할 점 |
|---|---|
| `raw/sources/` | 원본 자료를 넣는 곳입니다. agent가 임의로 수정하지 않습니다. |
| `raw/assets/` | 이미지, 스크린샷, 첨부 파일을 둡니다. |
| `raw/imports/` | 아직 정리되지 않은 가져오기 자료를 임시로 둡니다. |
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

이 report는 pending review, contested claim rows, stale sources, orphan pages, claim-level provenance 미적용 page, deprecated linked pages, last health report를 한 번에 보여줍니다. `--check`는 `tools/wiki/metrics-policy.json`의 threshold를 기준으로 실패하며, 일시적으로 허용할 운영 부채가 있으면 별도 policy 파일을 지정할 수 있습니다. 명시한 `--policy` 파일이 없으면 실패합니다. Metrics는 상태를 보여줄 뿐 자동 repair나 judgment를 수행하지 않습니다.

## 16. MCP 사용하기

MCP를 지원하는 agent는 `tools/wiki/mcp_server.py`를 stdio MCP server로 연결할 수 있습니다.

```bash
python3 tools/wiki/mcp_server.py
```

주의할 점:

- 이 명령은 일반 CLI처럼 결과를 출력하고 바로 종료하는 명령이 아닙니다.
- MCP client나 agent가 stdio로 연결해 tool call을 보낼 때 사용합니다.
- 터미널에서 직접 실행하면 입력을 기다리는 것처럼 보일 수 있습니다.

제공되는 MCP tool:

| MCP tool | 역할 |
|---|---|
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

MCP wrapper는 CLI를 얇게 감싸는 역할입니다. 같은 작업은 CLI로도 실행할 수 있습니다.

## 17. Git 사용하기

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

`scripts/release_gate.sh`는 임시 복사본에서 tests, lint, health, maps, metrics를 실행합니다. 검증 중 생기는 report나 test artifact가 현재 vault를 오염시키지 않으며, 같은 gate는 `.github/workflows/release-gate.yml`에서도 실행됩니다. Stenc 문서 검증은 선택 기능이므로 필요한 경우에만 `WIKI_ENABLE_STENC=1 scripts/release_gate.sh`로 실행합니다.

## 18. 전체 명령 요약

| 목적 | 명령 |
|---|---|
| 전체 release gate 실행 | `scripts/release_gate.sh` |
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

## 19. 완료 기준

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
