# vault-map.json version 2 계약

`vault-map.json`은 어떤 Vault가 있고 어느 저장소가 어느 Vault에 연결되는지 적는 파일이다. 위치는 `$HOME/.claude/vault-builder/vault-map.json`이고, `CLAUDE_CONFIG_DIR`가 있으면 `$CLAUDE_CONFIG_DIR/vault-builder/vault-map.json`이다.

vault-builder가 이 형식을 소유한다. llm-wiki router, `recall.sh`, vault-graph는 이 문서대로 읽는다.

## 쓰는 곳

- `install`: Vault 항목을 더한다.
- `link`, `unlink`: 프로젝트 항목과 그 `vaults` 목록을 바꾼다.
- `set-role --vault <name> --role <role>`: Vault의 `role`을 바꾼다.
- `set-default-write --project <id> (--vault <name> | --clear)`: 프로젝트의 `default_write_vault`를 정하거나 지운다.

모든 쓰기는 모르는 필드를 지우지 않는다. 같은 폴더에 임시 파일을 쓴 뒤 이름을 바꿔 파일을 교체한다. 내용이 같으면 파일을 다시 쓰지 않는다. 쓸 때 `version`은 2가 된다.

## 필드

```json
{
  "version": 2,
  "vaults": {
    "personal": {"path": "/abs/path/to/vault", "role": "personal"},
    "team": {"path": "/abs/path/to/team-vault", "role": "team"}
  },
  "projects": {
    "my-repo": {
      "root": "/abs/path/to/my-repo",
      "vaults": ["personal", "team"],
      "remote": "git@github.com:me/my-repo.git",
      "default_write_vault": "team"
    }
  }
}
```

| 필드 | 형식 | 필수 | 뜻 |
| --- | --- | --- | --- |
| `version` | 정수 1 또는 2 | 예 | 형식 버전. 읽는 쪽은 1과 2를 받는다. 쓰는 쪽은 2로 쓴다. |
| `vaults` | 객체 | 예 | Vault 이름에서 Vault 항목으로 가는 표. |
| `vaults.<name>.path` | 문자열 | 예 | Vault 폴더의 절대 경로. |
| `vaults.<name>.role` | 비어 있지 않은 문자열 | 예 | Vault 종류. 문서상 값은 `personal`, `project`, `team`이다. 다른 값도 받는다. install은 `personal`로 만든다. |
| `projects` | 객체 | 예 | 프로젝트 id에서 프로젝트 항목으로 가는 표. |
| `projects.<id>.root` | 문자열 | 예 | 저장소 main worktree의 절대 경로. |
| `projects.<id>.vaults` | 문자열 목록 | 예 | 이 프로젝트가 읽을 수 있는 Vault 이름. |

## 선택 필드

| 필드 | 형식 | 없을 때 | 뜻 |
| --- | --- | --- | --- |
| `projects.<id>.remote` | 문자열 | 원격 비교 단계를 건너뛴다 | `link` 때의 `origin` URL. |
| `projects.<id>.default_write_vault` | 문자열 | 기본 저장 Vault가 없다 | 이 프로젝트의 기본 저장 Vault. 값은 그 프로젝트 `vaults` 목록 안의 이름이어야 한다. |

`default_write_vault` 규칙:

- `set-default-write`는 프로젝트 `vaults`에 없는 이름을 거부한다.
- `link`와 `set-role`은 값을 자동으로 정할 수 있다. 세 조건이 모두 맞을 때만 정한다. 프로젝트에 연결된 Vault가 둘 이상이다. `default_write_vault`가 아직 없다. 연결된 Vault 중 role이 `project`인 Vault가 정확히 하나다. 그러면 그 Vault를 기본 저장 Vault로 정하고 한 줄을 출력한다. 이미 있는 값은 바꾸지 않는다. `link --dry-run`은 출력만 하고 파일을 쓰지 않는다.
- `unlink`로 기본 저장 Vault를 프로젝트에서 빼면 `default_write_vault`도 지운다.
- llm-wiki router는 이 필드를 무시한다. 쓰기 도구는 여전히 `vault` 인자를 받아야 한다.
- vault-graph는 값이 문자열이 아니거나 프로젝트 `vaults`에 없으면 그 필드만 무시하고 경고한다. 파일 전체를 버리지 않는다.

읽는 쪽은 모르는 필드를 오류로 보지 않고 무시한다.

## 프로젝트 찾기 규칙

저장소가 어느 프로젝트인지 정하는 규칙은 llm-wiki router의 `find_project`(`tools/vault_migration/assets/llm-wiki-router/llm_wiki_router.py`)가 소유한다. `link`, `unlink`, `list`는 이 함수를 불러 쓴다. `recall.sh`와 vault-graph처럼 따로 구현한 쪽은 이 함수와 같은 결과를 내야 한다.

순서는 아래와 같다. 하나만 맞는 첫 단계가 이긴다. 한 단계에서 둘 이상 맞으면 멈추고 그 id들을 알린다.

1. root: 저장소 main worktree 경로가 `root`와 같다.
2. remote: 저장소 `origin` URL을 정규화한 값이 `remote`를 정규화한 값과 같다.
3. name: 프로젝트 id가 저장소 폴더 이름과 같다. 저장소와 항목 둘 다 원격이 있으면 이 단계에서 맞지 않는다.

## 버전 규칙

형식을 깨면 `version`을 올린다. 형식을 깬다는 것은 아래 중 하나다.

- 필수 필드를 더하거나 뺀다.
- 기존 필드의 형식이나 뜻을 바꾼다.
- 지금 읽는 쪽이 받는 파일을 오류로 만들거나 다르게 해석하게 만든다.

선택 필드를 더하는 것은 형식을 깨지 않는다. 없을 때의 동작이 지금과 같아야 하고, 이 문서의 선택 필드 표에 적어야 한다. `default_write_vault`는 이 규칙으로 version 2 안에서 더했다.
