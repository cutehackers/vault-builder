# LLM Wiki 처리 구조

이 문서는 LLM Wiki를 사용할 때 내부적으로 어떤 일이 일어나는지 설명합니다.

`docs/usage.md`가 "무엇을 실행하면 되는가"를 설명한다면, 이 문서는 "그 실행이 왜 필요하고 내부에서 어떻게 처리되는가"를 설명합니다.

핵심 목표는 다음과 같습니다.

```text
LLM이 만든 문장을 바로 지식으로 저장하지 않고,
원본, 초안, 검증, 색인, 로그를 거쳐 품질이 확인된 문서만 wiki에 남긴다.
```

LLM Wiki는 단순한 RAG 폴더가 아닙니다. 원본에서 필요한 부분을 그때그때 검색해 답하는 방식에 머물지 않고, 반복해서 사용할 가치가 있는 지식을 `wiki/` 아래의 문서로 축적합니다.

## 1. 큰 그림

사용자가 원본을 넣고 문서를 만들거나 질문을 하면, LLM Wiki는 보통 아래 흐름을 따릅니다.

```text
원본 자료
-> source 등록
-> LLM의 의미 추출
-> JSON draft 작성
-> 결정론적 검증
-> wiki page 반영
-> index/log/report 갱신
-> lint로 품질 확인
-> Git history에 보존
```

각 단계는 담당하는 일이 다릅니다.

| 단계 | 내부에서 일어나는 일 |
|---|---|
| 원본 보관 | 사람이 제공한 파일을 `raw/`에 둡니다. 원본은 수정하지 않습니다. |
| source 등록 | 원본 파일의 위치와 해시를 기록한 source page를 만듭니다. |
| 의미 추출 | LLM이 개념, 주장, 결정, workflow, 모순, 질문을 읽어냅니다. |
| draft 작성 | LLM 결과를 바로 저장하지 않고 JSON 초안으로 만듭니다. |
| 검증 | 코드가 frontmatter, 링크, 출처 신호, 경로, human lock을 검사합니다. |
| 반영 | 검증된 내용만 `wiki/`의 durable page로 저장합니다. |
| 색인과 로그 | `wiki/index.md`와 `wiki/log.md`를 갱신합니다. |
| 보고서 | `scratch/reports/`에 실행 결과와 점검 흔적을 남깁니다. |

이 구조의 핵심은 역할 분리입니다.

- LLM은 읽고, 해석하고, 제안합니다.
- CLI 도구는 파일을 쓰기 전에 구조와 품질을 검사합니다.
- 사람은 중요한 판단과 최종 결정을 합니다.

## 2. 권장 Skill Workflow Layer

사람이 모든 저수준 명령을 기억하지 않도록, 최종 권장 사용 모델은 세 가지 skill/workflow로 단순화합니다.

| Workflow | 사용자 의도 | 내부 처리 |
|---|---|---|
| `wiki-ingest` | 새 원본을 wiki에 반영 | source 등록, 기존 page 확인, draft 생성, 검증, 적용, lint |
| `wiki-update` | 기존 wiki page 변경 | 대상 page/source 확인, human lock 확인, 변경 draft 생성, 검증, 적용, lint |
| `wiki-query` | wiki 기준 질문 | index에서 관련 page/source를 찾고 답변하며, 필요하면 report 또는 draft로 남김 |

현재 구현된 안정 CLI는 `ingest-source`, `source-registry`, `bulk-ingest`, `publish-draft`, `publish-batch`, `review`, `merge scan`, `maps build`, `metrics`, `health`, `apply-draft`, `lint`, `hash-source`, `validate-page`, `workflow ingest/update/query`입니다. `wiki-ingest`, `wiki-update`, `wiki-query` skill 문서는 `agents/skills/` 아래에 있습니다.

책임 경계는 다음과 같습니다.

```text
Skill = cognition
CLI = mutation and validation
MCP = CLI semantics wrapper
Wiki/Git = durable state
```

Skill은 의미 판단, 중복 확인, contradiction 발견, draft/report 작성을 담당합니다. Durable wiki page 변경은 CLI validation path를 통과해야 합니다.

## 3. 주요 용어

LLM Wiki 문서에서 자주 나오는 용어를 쉽게 풀면 다음과 같습니다.

| 용어 | 쉬운 설명 |
|---|---|
| routing | 새 문서가 `concepts/`, `systems/`, `workflows/` 중 어디에 들어가야 하는지 정하는 일입니다. 예를 들어 도구 설명은 `entities/`, 반복 절차는 `workflows/`, 아키텍처 설명은 `systems/`로 보냅니다. |
| linting | 문서가 wiki 규칙을 지키는지 자동으로 검사하는 일입니다. 깨진 링크, 빠진 frontmatter, 잘못된 해시, 로그 형식 오류 등을 찾습니다. |
| indexing | 사람이 읽고 agent가 찾을 수 있도록 `wiki/index.md`에 문서 목록을 등록하는 일입니다. 문서가 있어도 index나 map에서 도달할 수 없으면 사실상 고립된 문서가 됩니다. |
| quality triage | 모든 문제를 같은 심각도로 보지 않고, 자동 수정 가능한 문제와 사람 판단이 필요한 문제를 나누는 일입니다. 예를 들어 깨진 링크는 도구가 찾을 수 있지만, 상충하는 주장은 사람이 판단해야 할 수 있습니다. |
| provenance | 어떤 문장이나 주장이 어디에서 왔는지 추적할 수 있게 하는 출처 정보입니다. |
| durable knowledge | 검증을 통과해 `wiki/`에 저장된 재사용 가능한 지식입니다. |
| semantic draft | LLM이 만든 의미 기반 초안입니다. 아직 최종 지식이 아니며 `scratch/drafts/`에 둡니다. |
| source drift | `raw/`의 원본 파일 내용이 예전에 기록한 해시와 달라진 상태입니다. 원본이 바뀌면 기존 source page의 근거도 다시 확인해야 합니다. |
| human lock | 사람이 보호한 page나 section입니다. agent가 임의로 덮어쓰면 안 됩니다. |
| contradiction | source나 page 사이의 내용 충돌입니다. LLM Wiki는 충돌을 조용히 지우지 않고 표시합니다. |

## 4. 저장 위치가 나뉘는 이유

LLM Wiki는 파일 위치 자체를 품질 경계로 사용합니다.

```text
raw/              원본 자료
scratch/drafts/   LLM이 만든 초안
scratch/reports/  실행 결과와 점검 보고서
scratch/review/   사람 판단이 필요한 항목
wiki/             검증된 지식 문서
tools/wiki/       검증과 반영을 수행하는 도구
docs/             사용법과 설계 설명
```

이 분리는 단순 정리가 아닙니다.

- `raw/`는 원본이므로 agent가 임의로 고치지 않습니다.
- `scratch/drafts/`는 제안이므로 source of truth로 인용하지 않습니다.
- report와 generated draft scaffold는 각각 `scratch/reports/`, `scratch/drafts/` 경계를 벗어나지 않습니다.
- `wiki/`는 사람이 읽고 agent가 재사용하는 compiled knowledge입니다.
- `scratch/reports/`는 나중에 무엇이 바뀌었는지 확인하는 audit trail입니다.
- `tools/wiki/`는 LLM이 놓칠 수 있는 구조적 오류를 코드로 잡습니다.

## 5. 문서 생성 요청이 처리되는 과정

사용자가 "이 원본을 wiki 문서로 만들어줘"라고 요청하면 내부 흐름은 두 층으로 나뉩니다.

첫 번째 층은 원본 등록입니다.

```text
raw/sources/example.md
-> ingest-source
-> wiki/sources/example.md
-> wiki/index.md 갱신
-> wiki/log.md 기록
-> scratch/reports/...ingest...md 생성
```

이 단계는 원본 파일을 지식 본문으로 해석하지 않습니다. 대신 원본이 wiki에 등록되었다는 사실, 원본 경로, 원본 해시, 기본 source page를 만듭니다.

두 번째 층은 의미 추출과 문서화입니다.

```text
원본과 기존 wiki 읽기
-> 관련 개념과 기존 page 찾기
-> 새 page 생성 또는 기존 page 갱신 결정
-> scratch/drafts/*.json 작성
-> publish-draft로 검증/적용
-> wiki page 반영
-> index/log/report 갱신
-> lint 실행
```

이 단계에서 LLM은 원본을 읽고 durable knowledge로 남길 내용을 제안합니다. 하지만 최종 파일 쓰기와 전체 품질 확인은 `publish-draft`가 맡습니다.

## 6. Ingest 내부 처리

`ingest-source`는 원본 파일을 wiki의 source page로 등록하는 기능입니다.

내부 처리 순서는 다음과 같습니다.

```text
입력 경로 확인
-> raw/ 아래 파일인지 확인
-> 파일 존재 여부 확인
-> SHA-256 해시 계산
-> source title과 slug 결정
-> 기존 source page와 canonical_source/raw_sha256 비교
-> 같은 source/hash면 unchanged로 종료
-> source-drift, slug-collision, human lock이면 덮어쓰기 중단
-> wiki/sources/<slug>.md 생성
-> canonical_source와 raw_sha256 기록
-> wiki/index.md Sources 섹션 갱신
-> wiki/log.md에 ingest 기록 추가
-> 요청 시 scratch/reports/에 보고서 작성
```

중요한 점은 `ingest-source`가 의미 추출을 하지 않는다는 것입니다.

예를 들어 PDF나 markdown을 등록할 수는 있지만, 그 안에서 "중요한 개념 5개"를 자동으로 뽑아 durable page로 만들지는 않습니다. 그 일은 LLM의 semantic draft 단계에서 수행됩니다.

이렇게 나누는 이유는 품질 때문입니다. 파일 경로, 해시, 로그, index 갱신은 결정론적으로 처리해야 합니다. 반면 요약, 주장 분리, 모순 해석은 LLM이 더 잘하는 의미 작업입니다.

## 7. Semantic Draft 내부 처리

LLM이 wiki page를 만들거나 고칠 때는 먼저 JSON draft를 만듭니다.

draft에는 대체로 다음이 들어갑니다.

| 항목 | 의미 |
|---|---|
| `version` | draft 형식 버전입니다. 현재는 `1`입니다. |
| `operation` | 수행할 작업입니다. 현재는 `upsert-page`입니다. |
| `path` | 만들거나 갱신할 `wiki/...md` 경로입니다. |
| `frontmatter` | 문서의 타입, 상태, 요약, 품질 상태입니다. |
| `body` | 실제 markdown 본문입니다. |
| `index` | index에 추가할 항목입니다. |
| `log` | log에 추가할 작업 기록입니다. |

낮은 수준의 `apply-draft --dry-run`는 이 draft를 읽고 다음 순서로 검증합니다.

```text
JSON 읽기
-> version과 operation 확인
-> target path가 wiki/ 아래인지 확인
-> target이 markdown 파일인지 확인
-> frontmatter schema 검사
-> provenance signal 검사
-> human lock marker 검사
-> Obsidian link 검사
-> 기존 target page가 page-level human lock인지 확인
-> 파일 쓰기 없이 종료
```

여기서 중요한 방어선은 경로 제한입니다. draft가 `wiki/` 밖의 파일을 수정하려고 하면 실패합니다. LLM이 만든 초안이 실수로 `raw/`, `docs/`, `tools/`를 바꾸지 못하게 하기 위한 장치입니다.

권장 진입점인 `publish-draft`는 이 낮은 수준 검증 앞뒤에 전체 vault lint를 붙입니다. 현재 wiki가 이미 깨져 있으면 적용을 중단하고, 적용 후 새 문제가 생기면 page, index, log 변경을 되돌립니다.

여러 page를 같은 의미 단위로 반영할 때는 `publish-batch`가 같은 gate를 사용합니다. Batch draft는 각 page의 `path`, `frontmatter`, `body`, `index`를 검증하고, operation 기록은 top-level `log` 하나로만 남깁니다.

## 8. Routing 내부 처리

Routing은 "문서를 어디에 둘 것인가"를 정하는 과정입니다.

LLM Wiki는 기본적으로 다음 기준을 사용합니다.

| 내용 | 기본 위치 |
|---|---|
| 원본 하나의 요약과 출처 | `wiki/sources/` |
| 반복해서 쓰이는 아이디어나 원칙 | `wiki/concepts/` |
| 사람, 조직, 제품, 프로젝트, 도구 | `wiki/entities/` |
| 앱, 서비스, 아키텍처, agent 구조 | `wiki/systems/` |
| 반복 가능한 절차 | `wiki/workflows/` |
| 선택과 근거가 있는 결정 | `wiki/decisions/` |
| 읽기 경로와 주제 지도 | `wiki/maps/` |

Routing이 중요한 이유는 검색 품질 때문입니다. 같은 주제가 매번 다른 위치에 생기면 중복 문서가 늘어나고, 이후 agent가 어떤 page를 신뢰해야 할지 알기 어려워집니다.

따라서 새 page를 만들기 전에는 먼저 `wiki/index.md`와 관련 폴더를 확인해야 합니다. 이미 같은 개념이 있으면 새 문서를 만들기보다 기존 문서를 갱신하는 것이 기본입니다.

## 9. Frontmatter 내부 역할

Frontmatter는 문서 본문을 대신하는 지식 저장소가 아닙니다. 내부 처리에 필요한 metadata입니다.

주요 역할은 네 가지입니다.

| 역할 | 설명 |
|---|---|
| routing | `type`을 보고 page 종류를 알 수 있습니다. |
| linting | 필수 필드, 타입, enum 값이 맞는지 검사할 수 있습니다. |
| indexing | `title`, `summary`, `tags`, `related`를 이용해 문서를 찾기 쉽게 만듭니다. |
| quality triage | `quality` block을 보고 출처, 링크, 모순, 리뷰 필요 상태를 판단합니다. |

예를 들어 다음 값은 도구가 검사할 수 있습니다.

```yaml
type: concept
status: active
source_count: 2
tags: []
related: []
quality:
  provenance: section
  links: unchecked
  contradictions: none
  review_required: false
```

하지만 claim별 근거, 모순 설명, 결정의 tradeoff 같은 내용은 frontmatter에 넣지 않습니다. 그런 내용은 본문 section이나 표에 남겨야 사람이 읽고 판단할 수 있습니다.

`quality.provenance: claim`인 page는 본문에 claim evidence table을 둡니다.

```markdown
| Claim | Status | Evidence |
|---|---|---|
| Durable claim text. | stated | `raw/sources/example.md` |
```

도구는 row별 status가 `stated`, `inferred`, `contested`, `deprecated` 중 하나인지, evidence가 raw/source/wiki/human instruction 근거를 가리키는지, `source_count`가 고유 evidence 수와 맞는지 검사합니다. Raw evidence는 등록된 source page의 `raw_sha256`와 연결되거나 inline `#sha256=...` 값을 가져야 합니다.

## 10. Indexing 내부 처리

`wiki/index.md`는 wiki의 목차이자 agent의 탐색 출발점입니다.

문서가 생성되거나 중요한 문서가 갱신되면 index는 다음 역할을 합니다.

```text
새 page target 확인
-> 해당 section 찾기
-> 이미 등록된 page인지 확인
-> 없으면 한 줄 요약과 함께 추가
```

Indexing은 단순한 목록 관리가 아닙니다.

- 사용자는 index를 보고 현재 wiki의 문서 범위를 파악합니다.
- agent는 query나 ingest 전에 index에서 관련 page를 찾습니다.
- lint는 index나 map에서 도달할 수 없는 page를 orphan page로 보고합니다.

즉, index에 없는 durable page는 나중에 잊힐 가능성이 큽니다.

## 11. Log 내부 처리

`wiki/log.md`는 append-only 작업 기록입니다.

모든 의미 있는 변경은 다음 형식으로 기록되어야 합니다.

```markdown
## [YYYY-MM-DD] event-type | Short Title
```

허용되는 event type은 다음과 같습니다.

```text
ingest, query, lint, repair, decision, schema-change, manual-note
```

Log는 사람이 "언제 왜 바뀌었는가"를 추적하게 해줍니다. 또한 lint는 log heading 형식을 검사해 agent가 임의 형식으로 기록을 남기지 않도록 합니다.

## 12. Lint 내부 처리

`lint`는 wiki 전체를 읽고 구조적 품질을 검사합니다.

내부 흐름은 다음과 같습니다.

```text
wiki/**/*.md 찾기
-> 각 markdown page 파싱
-> YAML frontmatter 파싱
-> schema 검사
-> Obsidian link 해석
-> provenance signal 검사
-> claim-level evidence table 검사
-> human lock marker 검사
-> source hash drift 검사
-> log heading 검사
-> index coverage 검사
-> lint report 생성
```

현재 lint가 확인하는 대표 항목은 다음과 같습니다.

| 검사 | 문제를 찾는 이유 |
|---|---|
| missing frontmatter | 문서를 routing, indexing, quality triage에 사용할 수 없습니다. |
| invalid frontmatter | YAML이 깨지면 도구가 문서를 안정적으로 읽을 수 없습니다. |
| missing required field | 문서 품질 상태를 일관되게 판단할 수 없습니다. |
| invalid enum/type | `status`, `type`, `quality` 값이 제멋대로 늘어나는 것을 막습니다. |
| broken link | Obsidian 링크가 존재하지 않는 page를 가리키는 문제를 찾습니다. |
| orphan page | index나 map에서 도달할 수 없는 durable page를 찾습니다. |
| missing provenance signal | 출처 수준이 높다고 표시했지만 본문에 근거 신호가 없는 문제를 찾습니다. |
| claim-level provenance error | claim table, status, evidence, source_count, source hash 연결 문제를 찾습니다. |
| source hash drift | source page가 기록한 원본 해시와 현재 원본 해시가 다른지 확인합니다. |
| invalid log heading | 작업 기록이 parseable한 형식인지 확인합니다. |
| human lock marker error | 보호 구간의 시작과 끝이 맞는지 확인합니다. |

Lint가 하지 않는 일도 중요합니다.

- 어떤 주장이 사실인지 자동 판정하지 않습니다.
- 모순을 자동으로 해결하지 않습니다.
- 문서를 자동으로 병합하지 않습니다.
- 사람의 결정을 대신하지 않습니다.

Lint는 "판단"이 아니라 "품질 위험을 보이게 만드는 장치"입니다.

## 13. Query 응답 내부 처리

사용자가 wiki에 질문하면 agent는 일반적으로 다음 순서로 답합니다.

```text
wiki/index.md 읽기
-> 관련 page 후보 찾기
-> 관련 wiki page 읽기
-> 필요한 경우 source page나 raw source 확인
-> 여러 page를 종합해 답변 작성
-> 확실한 사실과 추론 구분
-> 출처와 불확실성 표시
-> 재사용 가치가 있으면 draft로 남길지 제안
```

Query 처리에서 중요한 원칙은 "검색 결과 나열"이 아니라 "축적된 wiki를 기반으로 한 종합"입니다.

예를 들어 사용자가 "이 시스템의 핵심 설계 원칙이 뭐야?"라고 물으면 agent는 단일 page를 요약하는 데 그치지 않고, 관련 concept, decision, workflow, source page를 함께 읽어 결론을 정리해야 합니다.

답변이 반복해서 쓸 가치가 있으면 다음 단계가 이어집니다.

```text
query answer
-> scratch/reports/...query...md 또는 scratch/drafts/*.json
-> publish-draft
-> wiki page 반영
-> log에 query 또는 repair 기록
```

이렇게 하면 좋은 답변이 일회성 대화에 사라지지 않고 wiki의 durable knowledge가 됩니다.

결정론적 command는 answer 내용을 생성하지 않고, agent가 만든 synthesis를 report나 draft scaffold로 고정합니다.

```bash
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-with-report --report
python3 tools/wiki/cli.py workflow query --question "..." --mode answer-and-capture --draft scratch/drafts/query-answer.json --report
```

`answer-with-report`는 question, answer summary, consulted pages/sources, confidence, contradictions, reusable capture recommendation을 report에 남깁니다. `answer-and-capture`는 같은 report와 함께 `publish-draft`를 통과할 수 있는 reusable answer draft scaffold를 생성합니다.

## 14. Quality Triage 내부 처리

Quality triage는 문제를 발견했을 때 "자동으로 처리할지, 사람에게 넘길지"를 나누는 과정입니다.

자동 처리에 가까운 문제:

- index에 빠진 명백한 page entry
- 깨진 내부 링크의 단순 오타
- frontmatter의 명백한 누락값
- log 형식의 단순 오류
- report 생성 누락

사람 판단이 필요한 문제:

- contested claim
- 오래된 source와 최신 source의 충돌
- 중요한 decision 변경
- page merge가 의미를 바꾸는 경우
- human-locked section 수정
- source 자체가 바뀐 이유를 판단해야 하는 경우

사람 판단이 필요한 항목은 `scratch/review/`에 두거나 보고서에 명확히 남겨야 합니다.

현재 review queue는 다음 명령으로 관리합니다.

```bash
python3 tools/wiki/cli.py review create --type contradiction --summary "Example Review" --related wiki/overview.md --context "Why human judgment is needed."
python3 tools/wiki/cli.py review list
python3 tools/wiki/cli.py review resolve scratch/review/example-review.md --status accepted --resolution "Accepted with rationale."
```

Review item type은 `contradiction`, `merge`, `contested-claim`, `human-lock`입니다. Status는 `pending`에서 시작해 `accepted`, `rejected`, `resolved` 중 하나로 닫힙니다. Resolve는 review item의 frontmatter와 본문 resolution을 갱신하고 `wiki/log.md`에 관련 page 링크와 결론을 남깁니다.

Page lifecycle metadata는 `aliases`, `redirects`, `supersedes`, `superseded_by`를 사용합니다. 이 필드들은 list여야 하며, `redirects`, `supersedes`, `superseded_by` target은 존재하는 wiki page를 가리켜야 합니다. Deprecated page는 반드시 `superseded_by`를 가져야 합니다.

중복 후보는 다음 명령으로 탐지합니다.

```bash
python3 tools/wiki/cli.py merge scan --report --create-review
```

이 명령은 같은 normalized title이나 alias를 공유하는 page group을 report로 남기고, 요청 시 `type: merge` review item을 만듭니다. 실제 병합, redirect, deprecate 처리는 자동으로 하지 않습니다.

## 15. Contradiction 처리

Contradiction은 단순 오류가 아닐 수 있습니다. source의 시점, 범위, 용어, 사람의 선호가 달라져 생길 수 있습니다.

따라서 내부 처리는 다음 순서를 따릅니다.

```text
충돌하는 문장 식별
-> 각 문장의 근거 source 확인
-> 충돌 유형 분류
-> 관련 page의 Contradictions / Tensions section 갱신
-> 중요하거나 반복되면 claim page 제안
-> 필요 시 scratch/review/로 human judgment 요청
-> log에 기록
```

LLM Wiki는 이전 내용을 조용히 삭제하지 않습니다. 오래된 관점이 왜 바뀌었는지, 어떤 source가 새 판단을 만들었는지 남겨야 합니다.

## 16. Human Lock 처리

Human lock은 사람이 보호한 내용을 agent가 임의로 바꾸지 못하게 하는 장치입니다.

Page 전체 보호:

```markdown
<!-- human-locked -->
```

Section 보호:

```markdown
<!-- human-locked:start -->
보호할 내용
<!-- human-locked:end -->
```

내부 처리는 다음과 같습니다.

```text
page body에서 marker 검색
-> start/end 순서 검사
-> unmatched marker 검사
-> page-level lock이면 apply-draft/publish-draft 차단
-> section-level lock이면 기존 locked range의 exact content preservation 확인
-> 필요한 변경은 report나 review item으로 제안
```

현재 도구는 page-level lock을 `apply-draft`와 `publish-draft`에서 직접 차단합니다. Section-level lock은 marker 오류를 lint로 검사하고, draft 적용 시 기존 locked range와 새 draft의 locked range가 완전히 같은지도 확인합니다.

## 17. Source Hash와 Drift 처리

Source page에는 원본 파일의 해시를 저장할 수 있습니다.

```yaml
canonical_source: "raw/sources/example.md"
raw_sha256: "..."
```

Lint는 source page를 검사할 때 다음을 수행합니다.

```text
canonical_source 확인
-> raw/ 아래 경로인지 확인
-> 원본 파일 존재 여부 확인
-> 현재 SHA-256 계산
-> frontmatter의 raw_sha256과 비교
-> 다르면 source-drift issue 보고
```

Source drift가 발생하면 기존 wiki 내용이 여전히 맞는지 다시 확인해야 합니다. 원본이 바뀌었다는 사실만으로 자동 수정하지 않는 이유는, 바뀐 내용이 사소한 편집인지 의미 변화인지 도구가 판단할 수 없기 때문입니다.

## 18. MCP 처리 구조

`tools/wiki/mcp_server.py`는 CLI 기능을 agent가 tool처럼 호출할 수 있게 감싸는 wrapper입니다.

제공되는 MCP tool은 CLI와 대응됩니다.

현재 제공되는 MCP tool:

| MCP tool | 내부 CLI 기능 |
|---|---|
| `wiki_validate_page` | page 하나를 검증합니다. |
| `wiki_lint` | 전체 wiki를 lint합니다. |
| `wiki_hash_source` | raw source 해시를 계산합니다. |
| `wiki_ingest_source` | source page를 등록합니다. |
| `wiki_source_registry` | raw 파일과 source page inventory를 비교해 report를 생성합니다. |
| `wiki_bulk_ingest` | 여러 raw source를 반복 등록하고 source별 outcome report를 생성합니다. |
| `wiki_apply_draft` | JSON draft를 파일 쓰기 없이 검증합니다. |
| `wiki_health` | `health` facade로 lint report를 생성하고 상태를 확인합니다. |
| `wiki_publish_draft` | `publish-draft` facade로 pre-lint, dry-run, apply, post-lint를 실행하고 실패 시 page/index/log 변경을 되돌립니다. |
| `wiki_publish_batch` | `publish-batch` facade로 multi-page draft를 transaction 단위로 검증, 적용, lint, rollback합니다. |
| `wiki_review_create` | `scratch/review/`에 pending human-review item을 만듭니다. |
| `wiki_review_list` | review queue item과 status count를 조회합니다. |
| `wiki_review_resolve` | review item을 accepted/rejected/resolved로 닫고 `wiki/log.md`에 resolution을 기록합니다. |
| `wiki_merge_scan` | 중복 후보를 탐지해 merge proposal report를 만들고 선택적으로 merge review item을 생성합니다. |
| `wiki_maps_build` | generated navigation map을 build, dry-run, check mode로 생성하거나 검증합니다. |
| `wiki_metrics` | review, source, provenance, lifecycle, lint health 관점의 maintenance metrics를 생성하고 policy threshold로 check합니다. |
| `wiki_workflow_ingest` | raw source 등록 checkpoint를 실행하고 semantic extraction pending 상태를 보고합니다. |
| `wiki_workflow_update_preflight` | update 전 target page, lock, related/link 상태를 report로 남깁니다. |
| `wiki_workflow_update_publish` | target page와 draft target을 확인하고 `publish-draft`로 반영합니다. |
| `wiki_workflow_query_prepare` | query report scaffold를 생성합니다. |
| `wiki_workflow_query_capture` | answer-with-report 또는 answer-and-capture mode로 query report와 draft scaffold를 생성합니다. |
| `wiki_workflow_query_publish` | reusable query capture draft를 `publish-draft`로 반영합니다. |

MCP wrapper는 새로운 판단 로직을 만들지 않습니다. 같은 CLI 기능을 agent workflow에서 호출하기 쉽게 노출할 뿐입니다. 따라서 사람이 CLI로 실행하든 agent가 MCP tool로 실행하든 품질 경계는 동일하게 유지됩니다.

## 19. 사용자가 시도할 수 있는 기능과 내부 흐름

아래는 사용자 관점의 기능과 내부 처리 흐름을 함께 정리한 것입니다.

| 사용자가 하려는 일 | 내부 처리 흐름 |
|---|---|
| 새 vault 만들기 | `init-vault.sh`가 기본 디렉터리, 문서, 도구를 복사하고 기존 vault 덮어쓰기를 방지합니다. |
| 새 원본을 wiki에 반영하기 | 권장 workflow는 `wiki-ingest`입니다. agent가 `ingest-source`, draft 생성, `publish-draft`를 조합해 수행합니다. |
| 기존 wiki page 변경하기 | 권장 workflow는 `wiki-update`입니다. 대상 page/source, human lock, contradiction을 확인한 뒤 draft를 검증해 반영합니다. |
| 원본 등록하기 | `ingest-source`가 raw 경로를 확인하고, 해시를 계산하고, source page, index, log, report를 만듭니다. |
| 원본/source inventory 확인하기 | `source-registry`가 `raw/` 파일과 `wiki/sources/` page를 비교해 registered, unregistered, drift, missing-source 상태를 보고합니다. |
| 여러 원본 등록하기 | `bulk-ingest`가 여러 raw source에 대해 source registration을 반복하고 source별 outcome을 report로 남깁니다. |
| 원본 등록 workflow checkpoint | `workflow ingest`가 source 등록을 실행하고 의미 추출은 agent draft 작업으로 남았음을 report에 명시합니다. |
| 원본에서 지식 문서 만들기 | agent가 기존 index와 관련 page를 읽고, semantic draft를 만든 뒤, `publish-draft`가 검증, 적용, lint를 수행합니다. |
| 여러 지식 문서를 한 번에 반영하기 | `publish-batch`가 batch draft의 모든 target page를 dry-run 검증하고, post-lint 실패 시 전체 target/index/log 변경을 rollback합니다. |
| 사람 판단 queue 남기기 | `review create/list/resolve`가 contradiction, merge, contested claim, human-lock 항목을 추적하고 resolve 결과를 log에 연결합니다. |
| 중복/병합 후보 찾기 | `merge scan`이 duplicate candidates를 report로 남기고 필요하면 merge review item을 생성합니다. 자동 병합은 하지 않습니다. |
| navigation map 갱신하기 | `maps build`가 topic, source, decision, review, lifecycle generated maps를 만들고 `--check`로 stale map을 막습니다. |
| maintenance metrics 확인하기 | `metrics`가 pending review, contested claim, stale source, orphan, provenance coverage, deprecated link, last health report를 보여주고 `tools/wiki/metrics-policy.json` 기준으로 check mode를 실행합니다. |
| 초안만 검증하기 | `apply-draft --dry-run`이 파일을 쓰지 않고 schema, link, provenance, human lock을 검사합니다. |
| 전체 wiki 점검하기 | `lint`가 모든 wiki page를 읽고 구조, 링크, index, log, hash, human lock 문제를 보고합니다. |
| page 하나만 점검하기 | `validate-page`가 해당 page의 schema, provenance, human lock, link를 검사합니다. |
| 원본 변경 여부 확인하기 | `hash-source` 또는 `lint`가 SHA-256을 계산해 기록된 해시와 비교합니다. |
| wiki에 질문하기 | 권장 workflow는 `wiki-query`입니다. agent가 index에서 시작해 관련 page와 source를 읽고, `answer-only`, `answer-with-report`, `answer-and-capture` 중 요청된 방식으로 처리합니다. |
| query report scaffold 만들기 | `workflow query --prepare-report`가 고정된 보고서 섹션을 만든 뒤, agent가 내용을 채웁니다. |
| query answer capture 만들기 | `workflow query --mode answer-with-report/answer-and-capture`가 supplied synthesis를 report나 draft scaffold로 남깁니다. |
| 모순 처리하기 | agent가 충돌 내용을 기록하고, 필요하면 claim page나 review item으로 분리합니다. |
| agent 도구로 연결하기 | MCP wrapper가 CLI 기능을 tool 형태로 노출해 같은 품질 검사를 agent workflow에서 사용하게 합니다. |

## 20. 품질 경계 요약

LLM Wiki의 품질은 "LLM이 좋은 답을 쓴다"에만 의존하지 않습니다.

품질은 다음 경계들이 함께 작동할 때 유지됩니다.

```text
raw/ 불변성
-> source hash
-> skill workflow triage
-> frontmatter schema
-> semantic draft
-> publish-draft 검증/적용
-> index/log 갱신
-> lint report
-> release gate
-> human review
-> Git history
```

최종 원칙은 간단합니다.

```text
LLM은 지식을 제안한다.
도구는 구조와 품질을 검증한다.
사람은 중요한 판단을 승인한다.
검증된 결과만 wiki와 Git에 남긴다.
```
