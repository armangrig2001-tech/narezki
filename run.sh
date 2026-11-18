#!/usr/bin/env bash
# Запуск нарезчика. Если в корне есть sample.mp4 — режем его (игровой исходник),
# иначе генерируем тестовый исходник и режем его.
set -euo pipefail
cd "$(dirname "$0")"
OUT=${OUT:-output/clip.mp4}
if [[ -f sample.mp4 ]]; then
  echo ">>> Исходник: sample.mp4 (игровой)"
  SRC=sample.mp4
  # Для реального стрима задайте регионы вебки/таймера: CAM=x:y:w:h TIMER=x:y:w:h
  python3 narezchik.py "$SRC" -o "$OUT" ${CAM:+--cam "$CAM"} ${TIMER:+--timer "$TIMER"}
else
  echo ">>> sample.mp4 не найден — генерирую тестовый исходник"
  mkdir -p work
  [[ -f work/test_source.mp4 ]] || ./make_test_source.sh work/test_source.mp4
  python3 narezchik.py work/test_source.mp4 -o "$OUT"
fi

if [[ "${PUBLISH_TIKTOK:-0}" == "1" ]]; then
  echo ">>> Подготовка публикации в TikTok"
  publish_args=("$OUT")
  if [[ -n "${TIKTOK_CAPTION:-}" ]]; then
    publish_args+=(--caption "$TIKTOK_CAPTION")
  fi
  node scripts/publish_tiktok.mjs "${publish_args[@]}"
fi
