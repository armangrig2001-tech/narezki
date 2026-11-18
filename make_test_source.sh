#!/usr/bin/env bash
# Генерирует тестовый исходник test_source.mp4 (НЕ игровое видео):
# 1920x1080, 30 fps, 120 с. Слои: «геймплей» (движущийся паттерн),
# «вебка стримера» в правом нижнем углу, таймер в левом верхнем углу,
# звук: тихий фон + «хайп-сегмент» 52–88 с (громче и динамичнее).
# Дополнительно пишет test_source.srt — тестовые субтитры с таймкодами
# (фикстура для проверки прожига субтитров, не результат распознавания речи).
set -euo pipefail
OUT=${1:-test_source.mp4}
DUR=120
FONT=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf

ffmpeg -hide_banner -loglevel error -y \
  -f lavfi -i "testsrc2=size=1920x1080:rate=30:duration=$DUR" \
  -f lavfi -i "mandelbrot=size=480x270:rate=30,trim=duration=$DUR" \
  -f lavfi -i "sine=frequency=110:sample_rate=48000:duration=$DUR" \
  -f lavfi -i "anoisesrc=color=pink:sample_rate=48000:duration=$DUR:amplitude=0.6" \
  -f lavfi -i "sine=frequency=660:sample_rate=48000:duration=$DUR" \
  -filter_complex "
    [0:v]hue=h='if(between(t,52,88),t*90,0)':s='if(between(t,52,88),1.6,0.7)',
         drawbox=x='mod(t*if(between(t,52,88),900,120),1920)':y=500:w=160:h=160:color=red@0.9:t=fill,
         drawtext=fontfile=$FONT:text='GAMEPLAY (TEST)':x=(w-tw)/2:y=h-120:fontsize=56:fontcolor=white:borderw=3[g];
    [1:v]drawtext=fontfile=$FONT:text='FACECAM':x=(w-tw)/2:y=20:fontsize=40:fontcolor=white:borderw=3,
         drawbox=x=0:y=0:w=iw:h=ih:color=yellow:t=6[cam];
    [g][cam]overlay=x=W-w-24:y=H-h-24,
      drawbox=x=20:y=20:w=380:h=80:color=black@0.7:t=fill,
      drawtext=fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf:text='%{pts\:hms}':x=36:y=36:fontsize=44:fontcolor=lime[v];
    [2:a]volume=0.15[bed];
    [3:a]volume='if(between(t,52,88),0.9*(0.6+0.4*sin(2*PI*3*t)),0.03)':eval=frame[noise];
    [4:a]volume='if(between(t,52,88),0.5*gt(sin(2*PI*2*t),0),0)':eval=frame[beep];
    [bed][noise][beep]amix=inputs=3:normalize=0,alimiter=limit=0.9[a]" \
  -map "[v]" -map "[a]" -c:v libx264 -preset veryfast -crf 23 -pix_fmt yuv420p \
  -c:a aac -b:a 160k -ac 2 "$OUT"

cat > "${OUT%.mp4}.srt" <<'SRT'
1
00:00:50,000 --> 00:00:53,000
Так, сейчас начинается погоня

2
00:00:53,500 --> 00:00:57,000
Жми, жми, жми!

3
00:00:58,000 --> 00:01:02,500
Они уже на хвосте, уходим через мост

4
00:01:04,000 --> 00:01:08,000
ВОТ ЭТО ПРЫЖОК!

5
00:01:10,000 --> 00:01:15,000
Сирены всё ближе, сворачиваю в переулок

6
00:01:17,000 --> 00:01:22,000
Ещё чуть-чуть до укрытия

7
00:01:24,000 --> 00:01:28,500
Ушли! Звёзды гаснут, чат, вы видели?

8
00:01:30,000 --> 00:01:33,000
Ладно, едем дальше
SRT
echo "OK: $OUT (+ ${OUT%.mp4}.srt)"
