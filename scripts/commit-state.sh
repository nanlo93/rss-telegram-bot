#!/usr/bin/env bash
# data/ 의 변경분을 커밋하고 원격에 푸시한다.
# - 변경이 없고 푸시할 커밋도 없으면 아무것도 하지 않는다.
# - 이전 시도에서 커밋만 되고 푸시되지 못한 커밋도 함께 푸시한다.
# - 푸시가 끝내 실패하면 비정상 종료(1)해 호출자가 실패를 알 수 있게 한다.
# 봇이 체크포인트(write-ahead)마다 호출하고, 워크플로의 마지막 단계에서도 호출한다.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [ -n "$(git status --porcelain -- data)" ]; then
  git config user.name "github-actions[bot]"
  git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
  git add -- data
  git commit -q -m "chore: 구독 상태 갱신"
fi

branch="$(git rev-parse --abbrev-ref HEAD)"

# 푸시할 커밋이 없으면 종료 (upstream 정보가 없으면 푸시를 시도한다)
if git rev-parse -q --verify '@{u}' >/dev/null 2>&1 && [ "$(git rev-list --count '@{u}..HEAD')" = "0" ]; then
  exit 0
fi

for attempt in 1 2 3; do
  if git pull -q --rebase origin "$branch" && git push -q origin "HEAD:$branch"; then
    exit 0
  fi
  git rebase --abort >/dev/null 2>&1 || true
  echo "상태 푸시 실패 (시도 $attempt/3)" >&2
  sleep $((attempt * ${COMMIT_STATE_RETRY_SLEEP:-3}))
done

echo "상태를 원격에 저장하지 못했습니다." >&2
exit 1
