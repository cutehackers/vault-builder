# Vault Operating Prompts

이 문서는 LLM Wiki를 운영하고 관리할 때 쓰는 프롬프트 모음입니다.

목표는 단순히 `ingest`나 `update`를 요청하는 것이 아니라, 현재 vault의 품질 경계인 source registry, semantic draft, publish gate, provenance, review queue, generated maps, maintenance metrics, release gate를 일관되게 사용하게 만드는 것입니다.

## 사용 원칙

- `raw/`는 수정하지 말라고 명시합니다.
- durable 변경은 `scratch/drafts/`의 JSON draft와 `publish-draft` 또는 `publish-batch`를 거치게 합니다.
- 의미 있는 변경은 `wiki/index.md`, `wiki/log.md`, `scratch/reports/`까지 확인하게 합니다.
- 충돌, 병합, human-lock, 중요한 판단은 `scratch/review/`로 분리하게 합니다.
- 완료 요청에는 `scripts/release_gate.sh`를 포함합니다.

## 공통 운영 프롬프트

```text
AGENTS.md와 wiki/index.md를 먼저 읽고, 현재 vault 구조 기준으로 작업해주세요.
raw/는 수정하지 말고, durable 변경은 scratch/drafts/ JSON draft와 publish-draft 또는 publish-batch gate를 통해서만 반영해주세요.
변경이 의미 있으면 wiki/index.md, wiki/log.md, scratch/reports/까지 포함하고, 완료 전 scripts/release_gate.sh를 실행해주세요.
```

## Source Registry 점검

```text
현재 raw/source-page inventory를 점검해주세요.
source-registry --report 기준으로 registered, unregistered, drift, missing-source 상태를 나누고,
semantic extraction이 필요한 raw source 후보를 우선순위와 함께 정리해주세요.
아직 wiki page는 수정하지 마세요.
```

## Bulk Ingest 준비

```text
raw/ 아래 모든 source 후보를 bulk-ingest 대상으로 검토해주세요.
중복 source, slug-collision 가능성, source-drift 위험을 먼저 보고하고,
안전한 항목만 bulk-ingest --all-raw --report로 등록해주세요.
semantic extraction은 별도 draft 단계로 남겨주세요.
```

## 단일 Source 풍부한 Ingest

```text
raw/sources/<source>.md를 wiki에 반영해주세요.
먼저 source page 등록 상태를 확인하고, wiki/index.md와 관련 page를 읽어 중복을 피해주세요.
이 source에서 concept, entity, system, workflow, decision, claim, contradiction, open question을 추출하고,
필요한 durable page 변경은 scratch/drafts/에 작성한 뒤 publish-draft --report로 반영해주세요.
```

## 여러 Source 묶음 구축

```text
다음 source들을 하나의 지식 구축 단위로 처리해주세요: <raw paths>.
각 source를 등록한 뒤, 공통 개념/시스템/결정/타임라인/비교 축을 찾아주세요.
여러 page가 함께 바뀌어야 하면 batch draft를 만들고 publish-batch --report로 transaction처럼 반영해주세요.
```

## 보고서만 먼저 받기

```text
raw/sources/<source>.md를 읽고 어떤 wiki page를 만들거나 갱신해야 하는지 ingest report로만 정리해주세요.
생성 후보, 업데이트 후보, 충돌 후보, human review 후보, 필요한 source evidence를 나눠주세요.
아직 draft publish나 wiki 수정은 하지 마세요.
```

## Page Taxonomy 설계

```text
현재 wiki/index.md 기준으로 <topic>을 어떤 page taxonomy로 구축하면 좋을지 제안해주세요.
concept, entity, system, workflow, decision, comparison, timeline, claim 중 무엇이 필요한지 나누고,
기존 page 재사용/업데이트와 신규 page 생성을 구분해주세요.
```

## Concept / System / Decision 추출

```text
<source 또는 topic>에서 재사용 가능한 concept, system, workflow, decision을 분리해주세요.
각 후보마다 왜 별도 page가 필요한지, 관련 source evidence, 관련 기존 page, confidence를 적어주세요.
승인 가능한 항목은 draft로 만들고 publish-draft --report까지 진행해주세요.
```

## Semantic Draft-first 구축

```text
<topic 또는 source>를 durable wiki page로 만들기 전에 scratch/drafts/에 JSON draft만 먼저 작성해주세요.
draft에는 frontmatter, body provenance, index entry, log entry를 포함해주세요.
apply-draft --dry-run으로 검증하고, 아직 publish는 하지 마세요.
```

## Batch Draft 구축

```text
<topic>에 필요한 여러 page 변경을 하나의 semantic operation으로 묶어주세요.
tools/wiki/templates/draft-batch-upsert-pages.json 구조를 사용하고,
top-level log 하나만 두며 pages[] 안에는 page-level log를 넣지 마세요.
publish-batch --dry-run으로 먼저 검증한 뒤 결과를 보고해주세요.
```

## Claim-level Provenance 강화

```text
wiki/<page>.md의 중요한 claim들을 점검해주세요.
stated, inferred, contested, deprecated로 분류하고, Claim | Status | Evidence table을 보강할 draft를 만들어주세요.
raw evidence는 등록된 source page 또는 sha256 연결이 있는지 확인해주세요.
```

## Provenance Audit

```text
현재 wiki에서 provenance가 약한 page를 찾아주세요.
source_count, primary_sources, body evidence, claim table이 서로 맞는지 보고하고,
수정 가능한 항목은 draft로, 판단이 필요한 항목은 scratch/review/로 분리해주세요.
```

## Query answer-only

```text
현재 wiki 기준으로 <question>에 답해주세요.
wiki/index.md에서 시작하고, 관련 durable page와 source page를 근거로 사용해주세요.
stated fact, inference, contested claim, missing evidence를 구분해서 답해주세요.
```

## Query answer-with-report

```text
answer-with-report 방식으로 <question>에 답해주세요.
Pages Consulted, Sources Consulted, Confidence, Contradictions, Reusable Capture Recommendation을 포함한 query report를 scratch/reports/에 남겨주세요.
```

## Query answer-and-capture

```text
answer-and-capture 방식으로 <question>에 답해주세요.
답변이 재사용 가능한 concept, workflow, comparison, decision, map 가치가 있으면 capture draft를 만들고,
검증 가능한 경우 publish-draft --report까지 진행해주세요.
```

## 비교 Page 만들기

```text
현재 wiki 기준으로 <A>와 <B>를 비교해주세요.
차이점, 공통점, authority/evidence, 적용 상황, open questions를 정리하고,
재사용 가치가 있으면 wiki/comparisons/에 comparison page draft를 만들어 publish-draft --report로 반영해주세요.
```

## Timeline 만들기

```text
현재 source와 wiki page를 기준으로 <topic/project>의 timeline을 만들어주세요.
날짜별 사건, source evidence, 변경된 결정, superseded된 관점, 남은 불확실성을 포함하고,
필요하면 wiki/timelines/ page draft로 남겨주세요.
```

## Target Update

```text
wiki/<target-page>.md를 최신 source 기준으로 업데이트해주세요.
frontmatter, related links, provenance, contradictions, index/log 영향을 함께 점검해주세요.
human-locked section은 보존하고, 안전한 변경만 draft + publish-draft --report로 반영해주세요.
```

## Human Lock 보호 업데이트

```text
wiki/<target-page>.md에 human-locked content가 있는지 확인해주세요.
잠긴 내용은 수정하지 말고, 필요한 변경은 scratch/reports/에 proposed patch로 남겨주세요.
안전하게 수정 가능한 주변 section만 draft로 처리해주세요.
```

## Contradiction 처리

```text
<topic 또는 page>에서 source 간 충돌하는 claim을 찾아주세요.
양쪽 evidence, 충돌 유형, 결정 영향도를 정리하고,
관련 page의 Contradictions / Tensions section draft와 필요한 scratch/review item을 만들어주세요.
```

## Human Review Queue 운영

```text
현재 scratch/review/와 wiki/maps/review-map.md 기준으로 unresolved review item을 점검해주세요.
각 항목의 type, related page, evidence, 필요한 human decision, 다음 action을 정리해주세요.
resolve 가능한 항목은 어떤 resolution 문구가 필요한지도 제안해주세요.
```

## Merge 후보 점검

```text
merge scan --report --create-review 기준으로 중복 page 후보를 찾아주세요.
자동 병합하지 말고, alias/title/source overlap, 의미 차이, merge 위험을 정리한 뒤 review item으로 남겨주세요.
```

## Navigation Map 갱신

```text
현재 wiki가 커진 상태를 기준으로 maps build --report를 실행해주세요.
topic-map, source-map, decision-map, review-map, lifecycle-map이 현재 index와 맞는지 확인하고,
stale map이 있으면 maps build --check --report 결과까지 보고해주세요.
```

## Maintenance Metrics 운영

```text
metrics --check --report로 현재 wiki 운영 상태를 점검해주세요.
pending review, contested claim rows, stale sources, orphan pages, provenance coverage, deprecated links, last health report를 요약하고,
가장 먼저 처리할 maintenance 작업 5개를 제안해주세요.
```

## Source Drift 점검

```text
현재 source-drift가 있는지 lint --report와 source-registry --report 기준으로 점검해주세요.
drift가 있다면 어떤 raw source가 바뀌었는지, 어떤 source page와 durable page를 재검토해야 하는지 정리해주세요.
raw file은 수정하지 말고, 필요한 재검토 작업만 report 또는 review item으로 남겨주세요.
```

## Release Gate 완료 요청

```text
이번 wiki 변경이 완료 가능한 상태인지 검증해주세요.
lint --report, health, maps build --check --report, metrics --check --report를 확인하고,
최종적으로 scripts/release_gate.sh를 실행해주세요.
실패하면 원인, 관련 file, 수정 draft 또는 review item으로 나눠 보고해주세요.
```

## 주간 Wiki 운영 리뷰

```text
이번 주 wiki 운영 리뷰를 해주세요.
새 source, 업데이트된 durable page, query capture, unresolved review, source drift, stale maps, metrics risk, release gate 상태를 요약하고,
다음 주에 ingest/update/review해야 할 우선순위를 제안해주세요.
```

## 도메인 구축 마스터 프롬프트

```text
<domain/topic>을 이 vault의 LLM-Wiki 구조로 체계적으로 구축해주세요.
source registry 점검 -> 관련 source ingest -> taxonomy 설계 -> concept/system/workflow/decision/comparison/timeline 후보 추출 -> claim provenance table 구성 -> contradiction/review 분리 -> batch draft 작성 -> publish-batch --report -> maps build -> metrics --check -> scripts/release_gate.sh 순서로 진행해주세요.
각 단계마다 변경 파일, 보고서, 남은 human decision을 명확히 알려주세요.
```

## 품질 복구 마스터 프롬프트

```text
현재 vault를 maintenance 관점에서 복구해주세요.
lint --report, health, source-registry --report, merge scan --report --create-review, maps build --check --report, metrics --check --report를 순서대로 확인하고,
자동으로 고칠 수 있는 구조 문제는 draft로, judgment가 필요한 문제는 scratch/review/로 분리해주세요.
마지막에 scripts/release_gate.sh로 완료 가능 여부를 검증해주세요.
```
