# 릴리스 절차

vault-builder 릴리스를 내는 순서다. 사람이든 agent든 이 순서를 그대로 따른다. 버전 번호의 뜻과 올리는 규칙은 `CHANGELOG.md` 첫 절에 있다.

## 평소 작업할 때

- 사용자에게 보이는 변경을 merge할 때마다 `CHANGELOG.md`의 `## [Unreleased]` 아래 Added, Changed, Fixed, Removed 중 맞는 곳에 한 줄씩 적는다.
- 설치된 vault에 사람이 해야 할 일이 생기면 `### Upgrade`에 실행할 명령을 적는다.
- 수정 경위는 지금처럼 `docs/PATCH_LOG.md`에, 결정은 `docs/DECISIONS.md`에 적는다. 두 파일의 새 항목에는 `**Release:** X.Y.Z`(아직 릴리스 전이면 `Unreleased`) 줄을 둔다.

## 릴리스 순서

1. main에서 새 branch를 만든다. 이름은 `release/vX.Y.Z`다.
2. `CHANGELOG.md`의 `Unreleased` 내용을 보고 MAJOR, MINOR, PATCH를 정한다.
3. `VERSION`을 새 버전 한 줄로 바꾼다.
4. contract가 바뀌었으면 `tools/vault_migration/contracts/vN.json`을 새로 만들고 이전 contract 파일은 그대로 둔다. 바뀌지 않았으면 현재 카탈로그를 다시 만든다. `--revision`을 주지 않으면 `VERSION` 값이 들어간다.

   ```bash
   python3.12 tools/vault_migration/cli.py catalog --contract-version 2 --output tools/vault_migration/contracts/v2.json
   ```

5. `CHANGELOG.md`에서 `## [Unreleased]` 아래 내용을 `## [X.Y.Z] - YYYY-MM-DD`로 옮기고, 호환표 한 줄과 `### Upgrade` 절을 쓴다. `## [Unreleased]` 제목은 비운 채 남긴다.
6. release gate를 실행한다. `VERSION`, `CHANGELOG.md`의 최신 릴리스 제목, 카탈로그 `builder_revision`이 다르면 여기서 실패한다.

   ```bash
   bash scripts/release_gate.sh
   ```

7. commit하고 main에 병합한다. commit 메시지는 `chore(release): vX.Y.Z`다.
8. main의 릴리스 commit에 annotated tag를 달고 push한다.

   ```bash
   git tag -a vX.Y.Z -m "vault-builder X.Y.Z"
   ```

   ```bash
   git push origin main vX.Y.Z
   ```

9. 쓰는 vault마다 `CHANGELOG.md`의 Upgrade 절대로 올리고 doctor를 실행한다. doctor 첫 줄이 새 버전이고 `Problem:` 줄이 없으면 끝이다.

## 하지 않는 것

- 이미 단 tag를 옮기거나 지우지 않는다. 잘못 낸 릴리스는 다음 PATCH로 고친다.
- 같은 릴리스 번호로 카탈로그를 두 번 만들지 않는다. 관리 파일이 바뀌면 번호도 바뀐다.
