#!/usr/bin/env bash
# Публикация скринера на GitHub Pages (ветка gh-pages, корень ветки = сайт).
#
# 1) при необходимости пересобирает дневной снапшот (--if-stale:
#    если сервер уже собрал сегодня в19:05 -- просто выгружаем готовое,
#    иначе собираем сами; пропущенные запуски догоняются при пробуждении)
# 2) пишет static/data.json
# 3) синхронизирует static/ в ветку gh-pages и пушит
#
# Запускается вручную, из launchd (см. README) или из любого скрипта.
set -euo pipefail
cd "$(dirname "$0")"          # корень git-репозитория (screener/)
PY=".venv/bin/python"; [ -x "$PY" ] || PY="python3"

"$PY" -m app.export --if-stale

WORK="$(mktemp -d)"
cleanup() {
  git worktree remove --force "$WORK" >/dev/null 2>&1 || rm -rf "$WORK"
  git worktree prune
}
trap cleanup EXIT

if git show-ref --verify --quiet refs/heads/gh-pages; then
  git worktree add -f "$WORK" gh-pages >/dev/null
elif git ls-remote --exit-code --heads origin gh-pages >/dev/null 2>&1; then
  git fetch origin gh-pages:gh-pages --quiet
  git worktree add -f "$WORK" gh-pages >/dev/null
else
  # первый запуск: ветки ещё нет -- создаём пустую
  git worktree add --detach "$WORK" >/dev/null
  git -C "$WORK" checkout --orphan gh-pages >/dev/null
  git -C "$WORK" rm -rfq . >/dev/null 2>&1 || true
fi

# актуальный сайт: только index.html + data.json
find "$WORK" -mindepth 1 -maxdepth 1 ! -name '.git' -exec rm -rf {} +
cp -R static/. "$WORK"/

if git -C "$WORK" diff --cached --quiet && \
   [ -z "$(git -C "$WORK" status --porcelain)" ]; then
  echo "publish: изменений нет"
  exit 0
fi
git -C "$WORK" add -A
git -C "$WORK" -c user.name="macromax2" \
                -c user.email="macromax2@users.noreply.github.com" \
                commit -q -m "публикация $(date '+%Y-%m-%d %H:%M') МСК"
# push с повтором: если ветку успел обновить другой публикатор
# (облачная сборка или локальный сервер) -- подтягиваем и повторяем
for i in 1 2 3; do
  if git -C "$WORK" push -q origin gh-pages 2>/dev/null; then
    echo "publish: готово -> https://macromax2.github.io/bond-screener-rf/"
    exit 0
  fi
  git -C "$WORK" pull --rebase -q origin gh-pages 2>/dev/null || true
  sleep 3
done
echo "publish: push не удался (попробуйте позже)" >&2
exit 1
