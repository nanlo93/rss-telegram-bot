#!/usr/bin/env bash
# 봇 상태 디렉터리(기본 data/, DATA_DIR로 지정 가능)의 변경분을 커밋하고 원격에 푸시한다.
# - 변경이 없고 푸시할 커밋도 없으면 아무것도 하지 않는다.
# - 이전 시도에서 커밋만 되고 푸시되지 못한 커밋도 함께 푸시한다.
# - 상태 디렉터리만 커밋한다. (다른 파일이 이미 스테이징돼 있어도 포함하지 않음)
# - DATA_DIR이 저장소 밖이면 저장할 수 없으므로 비정상 종료한다.
# - 푸시가 끝내 실패하면 비정상 종료(1)해 호출자가 실패를 알 수 있게 한다.
# 봇이 체크포인트(write-ahead)마다 호출하고, 워크플로의 마지막 단계에서도 호출한다.
set -euo pipefail

# 호출한 디렉터리 기준으로 DATA_DIR을 절대 경로로 먼저 확정한다. (봇도 같은 기준으로 상태를 저장하므로,
# 저장소 루트로 이동한 뒤 해석하면 하위 디렉터리에서 실행했을 때 다른 디렉터리를 보게 된다)
data_dir="${DATA_DIR:-data}"
mkdir -p "$data_dir"
abs="$(cd "$data_dir" && pwd -P)"

top="$(cd "$(git rev-parse --show-toplevel)" && pwd -P)"
cd "$top"

# 진행 중인 rebase가 있으면:
# - CI(GitHub Actions)에서는 이전 체크포인트가 rebase 도중 강제 종료돼 남은 흔적이므로 정리한다.
# - 그 밖의 환경(로컬 실행)에서는 사용자가 직접 진행 중인 rebase일 수 있어 건드리지 않고 중단한다.
if [ -d "$(git rev-parse --git-path rebase-merge)" ] || [ -d "$(git rev-parse --git-path rebase-apply)" ]; then
  if [ "${GITHUB_ACTIONS:-}" = "true" ]; then
    git rebase --abort >/dev/null 2>&1 || true
  else
    echo "진행 중인 rebase가 있어 상태를 커밋하지 않습니다. 직접 마무리(--continue 또는 --abort)한 뒤 다시 실행하세요." >&2
    exit 1
  fi
fi
case "$abs/" in
  "$top"/?*) rel="${abs#"$top"/}" ;;
  *)
    echo "DATA_DIR($abs)이 저장소($top) 안의 하위 디렉터리가 아니라 커밋할 수 없습니다." >&2
    exit 1
    ;;
esac

# Git이 무시하는 경로면 변경이 있어도 status에 나타나지 않아, 저장하지 못한 채 성공으로 끝나게 된다. 명시적으로 실패 처리.
for name in feeds.json state.json; do
  if git check-ignore -q -- "$rel/$name"; then
    echo "$rel/$name 이(가) .gitignore 대상이라 커밋할 수 없습니다." >&2
    exit 1
  fi
done

# 명령 실패(인덱스 오류 등)가 '변경 없음'으로 오해되지 않도록 조건문 밖에서 실행한다. (set -e)
changes="$(git status --porcelain -- "$rel")"
if [ -n "$changes" ]; then
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -- "$rel"
  git commit -q -m "chore: 구독 상태 갱신" -- "$rel"
fi

branch="$(git rev-parse --abbrev-ref HEAD)"

# 푸시할 커밋이 없으면 종료 (upstream 정보가 없으면 푸시를 시도한다)
if git rev-parse -q --verify '@{u}' >/dev/null 2>&1 && [ "$(git rev-list --count '@{u}..HEAD')" = "0" ]; then
  exit 0
fi

for attempt in 1 2 3; do
  # --autostash: 로컬에서 실행할 때 작업 트리에 다른 변경이 있어도 rebase가 막히지 않게 함 (상태 외 파일은 건드리지 않고 복원)
  if git pull -q --rebase --autostash origin "$branch" && git push -q origin "HEAD:$branch"; then
    exit 0
  fi
  git rebase --abort >/dev/null 2>&1 || true
  echo "상태 푸시 실패 (시도 $attempt/3)" >&2
  sleep $((attempt * ${COMMIT_STATE_RETRY_SLEEP:-3}))
done

echo "상태를 원격에 저장하지 못했습니다." >&2
exit 1
