# User Prompts for LLM Wiki

이 문서는 일반 사용자가 LLM Wiki의 가치를 가장 잘 이용하기 위해 쓸 수 있는 프롬프트 모음입니다.

사용자는 내부 명령을 모두 외울 필요가 없습니다. 아래 프롬프트는 `wiki-ingest`, `wiki-update`, `wiki-query` 스킬을 자연스럽게 호출하도록 작성되어 있습니다. agent는 필요할 때 source registration, semantic draft, publish gate, review queue, generated maps, metrics, release gate를 내부에서 사용합니다.

## 사용자가 기억할 기본 문장

```text
현재 vault 규칙을 지키면서 처리해주세요.
raw/는 수정하지 말고, 검증된 지식만 wiki/에 남겨주세요.
출처, 충돌, 남은 질문도 함께 정리해주세요.
```

## 새 자료를 Wiki에 반영하기

```text
raw/sources/<source>.md를 읽고 wiki에 반영해주세요.
중복 page를 만들지 말고, 기존 wiki와 연결되는 concept, system, workflow, decision이 있으면 정리해주세요.
중요한 claim은 출처와 함께 남겨주세요.
```

## 새 자료를 넣기 전에 영향 범위 알기

```text
raw/sources/<source>.md를 wiki에 반영하면 어떤 page가 생기거나 바뀔지 먼저 알려주세요.
생성 후보, 업데이트 후보, 충돌 가능성, 사람이 판단해야 할 항목을 나눠주세요.
아직 wiki는 수정하지 마세요.
```

## 여러 자료로 하나의 주제 만들기

```text
다음 자료들을 바탕으로 <topic> 지식 묶음을 만들어주세요: <raw paths>.
자료별 요약보다, 반복되는 개념, 시스템 구조, 의사결정, 차이점, 타임라인을 중심으로 wiki를 구성해주세요.
```

## 현재 Wiki 기준으로 질문하기

```text
현재 wiki 기준으로 <question>에 답해주세요.
근거가 되는 wiki page와 source가 있으면 함께 알려주고,
확실한 내용과 추론한 내용을 구분해주세요.
```

## 답변을 재사용 가능한 지식으로 남기기

```text
현재 wiki 기준으로 <question>에 답하고,
이 답변이 나중에도 쓸 만하면 concept, workflow, comparison, decision 중 알맞은 형태로 wiki에 남겨주세요.
출처와 검증 상태도 같이 관리해주세요.
```

## 프로젝트 온보딩 받기

```text
현재 wiki에서 <project/topic>을 처음 이해하는 사람이 읽어야 할 순서를 만들어주세요.
overview, 핵심 concept, system, workflow, decision, source를 읽기 경로로 정리하고,
부족한 page나 open question도 알려주세요.
```

## 현재 지식 지도 보기

```text
현재 wiki의 topic map, source map, decision map, review map, lifecycle map 관점에서 <topic>이 어디에 위치하는지 설명해주세요.
필요하면 navigation map도 최신 상태인지 확인해주세요.
```

## 개념 정리 요청

```text
현재 wiki 기준으로 <concept>를 설명해주세요.
정의, 왜 중요한지, 관련 시스템/결정/워크플로우, supporting source, open question을 포함해주세요.
재사용 가치가 있으면 concept page로 남겨주세요.
```

## 시스템 구조 이해하기

```text
현재 wiki 기준으로 <system>의 구조를 설명해주세요.
목적, 구성요소, 데이터/지식 흐름, 운영 규칙, 의존성, 위험 요소, 관련 decision을 정리해주세요.
부족한 근거가 있으면 missing evidence로 표시해주세요.
```

## Workflow 만들기

```text
<task/process>를 반복 가능한 workflow로 정리해주세요.
trigger, input, procedure, output, quality gate, failure handling을 포함하고,
wiki에 남길 가치가 있으면 workflow page로 만들어주세요.
```

## Decision 기록하기

```text
다음 결정을 wiki에 남겨주세요: <decision>.
배경, 선택지, 최종 결정, tradeoff, consequences, supporting source, supersedes/superseded_by 가능성을 정리해주세요.
판단이 아직 확정되지 않았다면 proposed decision으로 남겨주세요.
```

## 두 선택지 비교하기

```text
현재 wiki 기준으로 <A>와 <B>를 비교해주세요.
차이점, 공통점, 언제 무엇을 선택해야 하는지, 관련 evidence, 아직 판단이 필요한 부분을 정리해주세요.
나중에도 쓸 비교라면 comparison page로 남겨주세요.
```

## Timeline 만들기

```text
현재 wiki와 source 기준으로 <project/topic>의 변화 과정을 timeline으로 만들어주세요.
날짜별 사건, 바뀐 결정, superseded된 관점, 현재 유효한 방향을 구분해주세요.
```

## Open Question 찾기

```text
현재 wiki에서 <topic>과 관련된 open question을 찾아주세요.
source 부족, claim 충돌, 결정 미확정, outdated 가능성을 구분하고,
다음에 어떤 source를 추가하면 좋은지 제안해주세요.
```

## 충돌하는 주장 정리하기

```text
<topic>에 대해 서로 충돌하는 주장이 있는지 확인해주세요.
각 주장과 근거 source를 나란히 보여주고,
어떤 판단이 필요한지와 wiki에 어떻게 기록해야 하는지 알려주세요.
```

## 사람이 판단할 항목 보기

```text
현재 사람이 판단해야 하는 review item을 보여주세요.
각 항목이 왜 필요한지, 관련 page/source, 가능한 결정 옵션, 추천 다음 action을 정리해주세요.
```

## Human-locked 내용 안전하게 다루기

```text
wiki/<page>.md를 업데이트하고 싶습니다.
human-locked section은 절대 수정하지 말고, 필요한 변경은 제안 형태로만 남겨주세요.
안전하게 반영 가능한 부분과 제 승인 없이는 바꾸면 안 되는 부분을 나눠주세요.
```

## Wiki 품질 점검 요청

```text
현재 wiki의 건강 상태를 점검해주세요.
broken link, orphan page, provenance 부족, source drift, stale map, pending review, metrics risk를 요약하고,
제가 먼저 처리해야 할 항목을 우선순위로 알려주세요.
```

## 출처 신뢰도 점검

```text
<page/topic>의 중요한 주장들이 어떤 source에 근거하는지 점검해주세요.
직접 stated된 것, agent가 inferred한 것, contested된 것, evidence가 약한 것을 구분해주세요.
```

## 오래된 지식 찾기

```text
현재 wiki에서 오래되었거나 superseded되었을 가능성이 있는 내용을 찾아주세요.
관련 source date, decision status, supersedes/superseded_by, lifecycle map 기준으로 설명해주세요.
```

## 중복 Page 정리 요청

```text
현재 wiki에서 <topic>과 관련해 중복되거나 병합이 필요해 보이는 page가 있는지 찾아주세요.
자동 병합하지 말고, 후보와 이유, 위험, 사람이 결정해야 할 내용을 정리해주세요.
```

## 특정 Page 개선하기

```text
wiki/<page>.md를 더 좋은 durable wiki page로 개선해주세요.
summary, related links, provenance, claim table, contradictions, open questions, change notes를 점검하고,
필요한 변경은 검증 가능한 draft로 처리해주세요.
```

## Inbox 정리하기

```text
wiki/inbox.md에 있는 미정리 내용을 검토해주세요.
source로 옮겨야 할 것, concept/workflow/decision 후보, 질문으로 남겨야 할 것, 버려도 되는 것을 구분해주세요.
durable 변경은 제안한 뒤 진행해주세요.
```

## 팀 공유용 요약 만들기

```text
현재 wiki 기준으로 <topic/project>의 팀 공유용 요약을 만들어주세요.
핵심 결정, 현재 시스템 이해, 위험, open question, 다음 action을 짧게 정리하고,
근거 page도 함께 알려주세요.
```

## 주간 리뷰 요청

```text
이번 주 LLM Wiki 상태를 사용자 관점에서 리뷰해주세요.
새로 들어온 source, 새로 생긴 knowledge, 아직 검토가 필요한 항목, 품질 위험, 다음에 넣으면 좋은 자료를 요약해주세요.
```

## 최종 검증 요청

```text
방금 반영한 wiki 변경이 완료된 상태인지 확인해주세요.
검증 명령과 결과를 요약하고, 아직 release gate를 통과하지 못했다면 완료라고 말하지 말아주세요.
```

## Vault 가치 극대화 프롬프트

```text
<domain/topic>을 이 vault에서 장기적으로 재사용 가능한 지식 체계로 만들어주세요.
단순 요약이 아니라 source, concept, system, workflow, decision, comparison, timeline, claim evidence, open question, review item, navigation map까지 고려해주세요.
사용자가 읽을 수 있는 구조와 agent가 다음에 재사용할 수 있는 구조를 모두 만족하게 해주세요.
```
