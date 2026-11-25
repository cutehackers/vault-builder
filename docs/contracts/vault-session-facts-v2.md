# `vault-session-facts/v2` 세션 사실 첨부 계약

근거: vault-graph spec `docs/specs/2026-11-25-memory-intent-layers/spec.md` D5와 §4.3 3c, 4a, 4b. v1 계약 `vault-session-facts/v1`(vault-graph `docs/contracts/vault-session-facts-v1.md`)을 넓힌 판이다.
쓰는 쪽: vault-builder Vault 도구 `tools/wiki/cli.py sessions attach-facts`(구현 `tools/wiki/lib/session_capture.py`).
읽는 쪽: vault-graph 색인(사실 조각, 개념 사전, 코드 앵커).

v2는 사실 한 줄에 종류, 개념 태그, 코드 앵커를 더한다. 날짜, 문장, 교환 id는 v1과 같다. 이 문서에 없는 내용(명령 출력, 종료 코드, 첨부 위치, 불변성, `exists` 우선, `git-check`)은 v1과 같다.

## 1. 명령

v1과 같다.

```text
python3 <vault>/tools/wiki/cli.py sessions attach-facts --session <part 경로> --facts <입력 JSON 경로> --json
```

- 입력 `schema`가 `vault-session-facts-input/1`이면 v1 첨부를, `vault-session-facts-input/2`이면 v2 첨부를 쓴다.
- v2를 모르는 옛 Vault는 `/2` 입력에 `rejected`(`bad_input`)를 낸다. vault-graph는 이때 같은 사실을 `/1` 입력으로 다시 보내거나, Vault를 올리라는 경고를 남긴다. 어느 쪽을 쓸지는 vault-graph가 정한다.

## 2. 입력 JSON

```json
{
  "schema": "vault-session-facts-input/2",
  "session_id": "45d576f0-4bf0-56dd-8201-703456b8e6f4",
  "source_sha256": "<part 파일 전체 바이트의 sha256, 소문자 hex>",
  "extractor": "llama-server",
  "model": "qwen3-8b",
  "facts": [
    {
      "date": "2026-11-25",
      "text": "사용자는 사실 계약 v2를 승인했다.",
      "exchange_id": "x0003",
      "kind": "decision",
      "concepts": ["사실 계약", "session-facts"],
      "anchors": {
        "files": ["tools/wiki/cli.py", "tools/wiki/lib/session_capture.py"],
        "commits": ["691176a1c7e0b0d0b5e6c1d2e3f4a5b6c7d8e9f0"]
      }
    },
    {"date": "2026-11-25", "text": "The user thanked the assistant.", "exchange_id": "x0004"}
  ]
}
```

최상위 키와 `date`, `text`, `exchange_id`는 v1 §2와 같다. 사실마다 아래 키를 더할 수 있다. 셋 다 없어도 된다.

| 키 | 형 | 누가 채우나 | 규칙 |
|---|---|---|---|
| `kind` | 문자열 | LLM | `decision`, `change`, `problem`, `preference`, `question` 중 하나 |
| `concepts` | 문자열 목록 | LLM | 0~8개, 각 1~64자, 앞뒤 공백·쉼표·제어 문자·줄 구분 문자 없음, 중복 없음 |
| `anchors.files` | 문자열 목록 | vault-graph(결정적) | 0~32개, 저장소 루트 기준 POSIX 상대 경로, 각 1~300자, 중복 없음 |
| `anchors.commits` | 문자열 목록 | vault-graph(결정적) | 0~16개, 소문자 hex 40자(SHA-1) 또는 64자(SHA-256), 중복 없음 |

- 사실 객체와 `anchors` 객체에 위 표에 없는 키가 있으면 `bad_input`이다. v1 입력은 지금처럼 모르는 키를 무시하고 v1 줄만 쓴다.
- 빈 목록은 받되 첨부에 적지 않는다.
- 제어 문자·줄 구분 문자는 Unicode 범주 `Cc`, `Zl`, `Zp`의 모든 문자다(`\t`, `\x0b`, `\x85`, U+2028 포함). v2 `text`는 v1 규칙에 더해 Python `str.splitlines()`가 둘 이상으로 나누는 문자(`\x0b`, `\x0c`, `\x1c`~`\x1e`, `\x85`, U+2028, U+2029)를 담지 않는다(`bad_text`). 그래서 첨부 본문을 `\n`으로 나누든 `splitlines()`로 나누든 줄이 같다.
- 상대 경로 규칙: `/`, `~`, `C:` 같은 드라이브로 시작하지 않는다. `\`, 쉼표, 제어 문자·줄 구분 문자, 앞뒤 공백이 없다. `/`로 나눈 조각에 빈 조각, `.`, `..`이 없다(`./a`, `a/../b`, `a//b`, `a/`는 거부).
- `anchors`는 LLM에 맡기지 않는다. vault-graph가 교환 메타 `files=`의 절대 경로를 git 루트 기준으로 바꾸고, 본문에서 찾은 commit을 `git cat-file -e <sha>^{commit}`으로 확인한 것만 넣는다. 확인되지 않은 commit은 넣지 않는다. Vault 명령은 저장소를 모르므로 commit이 실제로 있는지는 확인하지 않는다.
- 개념 태그의 대소문자와 동의어 정리는 vault-graph 개념 사전이 맡는다. Vault 명령은 형식만 본다.

## 3. 첨부 파일

frontmatter는 v1 §3과 같고 `schema`만 `vault-session-facts/v2`다.

```markdown
---
schema: vault-session-facts/v2
agent: claude-code
session_id: "45d576f0-4bf0-56dd-8201-703456b8e6f4"
part: 1
source: raw/sessions/claude-code/2026-11-25-45d576f0-4bf0-56dd-8201-703456b8e6f4-part1.md
source_sha256: "<입력의 source_sha256>"
extractor: llama-server
model: qwen3-8b
generated_at: "2026-11-25T03:00:00Z"
fact_count: 2
---

- 2026-11-25 사용자는 사실 계약 v2를 승인했다. (exchange x0003)
  - kind: decision
  - concepts: 사실 계약, session-facts
  - files: tools/wiki/cli.py, tools/wiki/lib/session_capture.py
  - commits: 691176a1c7e0b0d0b5e6c1d2e3f4a5b6c7d8e9f0
- 2026-11-25 The user thanked the assistant. (exchange x0004)
```

- 사실 줄은 v1과 같은 `- <date> <text> (exchange <exchange_id>)`다.
- 그 아래 두 칸 들여쓴 하위 줄이 `kind`, `concepts`, `files`, `commits` 순서로 온다. 값이 있는 것만 쓴다. 목록 값은 입력 순서대로 `, `(쉼표와 공백)로 잇는다. 쉼표를 값에서 금지하므로 `, `로 나누면 원래 목록이 나온다.
- `fact_count`는 사실 줄 수이고 하위 줄은 세지 않는다.

## 4. 검증

v1 §4의 이유 코드와 순서를 그대로 쓰고, 사실마다 v1 검사(`unknown_exchange`, `bad_text`, `bad_date`, `secret`) 뒤에 아래를 이 순서로 본다. `bad_input`은 v1처럼 입력을 읽을 때 입력 전체에서 보므로 사실별 검사보다 먼저 나온다.

| 이유 코드 | 조건 |
|---|---|
| `bad_input` | §2 표에 없는 키가 있거나, `kind`가 문자열이 아니거나, `concepts`·`files`·`commits`가 문자열 목록이 아니거나, `anchors`가 객체가 아니다 |
| `bad_kind` | `kind`가 다섯 값 중 하나가 아니다(대소문자 구분, 빈 문자열 거부) |
| `bad_concept` | 개념 태그가 §2 규칙에 맞지 않거나 8개를 넘거나 중복이다 |
| `bad_anchor` | 파일 경로가 상대 경로 규칙에 맞지 않거나, commit이 hex 40자·64자가 아니거나, 개수 상한을 넘거나 중복이다 |
| `secret` | 개념 태그나 파일 경로가 Vault 가림 규칙에 걸린다 |

## 5. vault-graph가 읽는 방법

- `schema`가 `vault-session-facts/v1` 또는 `vault-session-facts/v2`인 파일을 첨부로 본다. 둘 다 계속 색인한다. `vault-session-facts/`로 시작하는 다른 판은 v1 §5처럼 경고 하나를 남기고 건너뛴다.
- 사실 줄 하나가 사실 조각 하나다. `parent_key`와 시각은 v1과 같다.
- v2에서 사실 줄 바로 아래 `  - <키>: <값>` 줄은 그 사실에 붙는다. 키는 `kind`, `concepts`, `files`, `commits`다. 모르는 키의 하위 줄은 건너뛰고 색인 경고 하나를 남긴다. 하위 줄이 없는 사실은 종류·개념·앵커가 없는 사실이다.
- v1 첨부의 사실은 종류·개념·앵커가 없는 사실로 읽는다.
- 앵커 경로는 그 세션 part의 `cwd`가 속한 저장소 기준이다(§6 질문1).

## 6. 열린 질문

- 질문1: 앵커 파일 경로에 저장소 식별자가 없다. 한 교환이 두 저장소의 파일을 고치면 경로만으로는 어느 저장소인지 모른다. 지금 안은 part `cwd`의 git 저장소 기준으로 보고, 그 저장소 밖 파일은 vault-graph가 앵커에 넣지 않는 것이다. 여러 저장소가 필요하면 v3에서 `anchors.files` 항목을 `<repo id>:<path>`로 바꾸거나 `anchors.repo`를 더한다. 백필 전에 정해야 한다.
- 질문2: 심볼 앵커(spec 4a)는 v2에 없다. 코드 색인이 풀어낼 수 있을 때 `anchors.symbols`를 더하는 판을 따로 낸다.
