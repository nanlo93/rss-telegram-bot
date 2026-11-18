#!/usr/bin/env bash
# data/ 의 변경분을 커밋하고 원격에 푸시한다. 변경이 없으면 아무것도 하지 않는다.
# 봇이 체크포인트(write-ahead)마다 호출하고, 워크플로의 마지막 단계에서도 호출한다.
set -euo pipefail

cd "$(git rev-parse --show-toplevel)"

if [ -z "$(git status --porcelain -- data)" ]; then
  exit 0
fi

git config user.name "github-actions[bot]"
git config user.email "41898282+github-actions[bot]@users.noreply.github.com"
git add -- data
git commit -q -m "chore: 구독 상태 갱신"
git pull -q --rebase
git push -q
