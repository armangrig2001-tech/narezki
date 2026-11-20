#!/usr/bin/env python3
"""Find, render, and optionally publish strong moments from GTA VI Twitch VODs."""
import argparse
from concurrent.futures import Future, ThreadPoolExecutor
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import narezchik
from find_twitch_clips import TwitchError, fetch_clips, get_token, helix, iso


NAREZCHIK = ROOT / "narezchik.py"
PUBLISHER_QUEUE = ROOT / "scripts" / "publish_tiktok_queue.mjs"
VARIANTS = (
    ("account-1", "classic", 40),
    ("account-2", "framed", 35),
    ("account-3", "cinematic", 30),
)
WINDOW_SECONDS = 40
CHAT_LAUGHTER = re.compile(
    r"(?:\blul\b|\blol\b|\bkekw+\b|\blmao\b|\bomegalul\b|"
    r"ах{2,}|х[ао]{2,}|рж[уё]|ору|сме[её]т|xd+)",
    re.IGNORECASE,
)
TRANSCRIPT_LAUGHTER = re.compile(
    r"(?:ах{2,}|х[ао]{2,}|смех|сме[её]т|смешно|ха-ха|хаха)",
    re.IGNORECASE,
)
INSIGHT_CUE = re.compile(
    r"(?:на самом деле|вот почему|я понял|я понял[а]?|оказывается|"
    r"суть в том|секрет|лайфхак|интересный факт|запомните|"
    r"actually|here'?s why|the point is|turns out)",
    re.IGNORECASE,
)


def parse_time(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def fetch_vods(user_id, started, ended, client_id, token):
    vods, cursor, pages = [], None, 0
    while True:
        params = {
            "user_id": user_id,
            "type": "archive",
            "first": 100,
            "sort": "time",
        }
        if cursor:
            params["after"] = cursor
        response = helix("videos", params, client_id, token)
        data = response.get("data", [])
        if not data:
            break
        older_than_window = False
        for vod in data:
            created = parse_time(vod["created_at"])
            if created < started:
                older_than_window = True
                break
            if created <= ended and vod.get("user_id") == user_id:
                vods.append(vod)
        cursor = (response.get("pagination") or {}).get("cursor")
        pages += 1
        if older_than_window or not cursor or pages >= 50:
            break
    return vods


def category_title_matches(title, category):
    title = title.casefold()
    category = category.casefold()
    gta_patterns = {
        "grand theft auto vi": r"\b(?:gta\s*(?:6|vi)|grand\s+theft\s+auto\s*(?:6|vi))\b",
        "grand theft auto v": r"\b(?:gta\s*(?:5|v)|grand\s+theft\s+auto\s*(?:5|v))\b",
    }
    pattern = gta_patterns.get(category)
    if pattern:
        return bool(re.search(pattern, title))
    phrase = r"\s+".join(re.escape(word) for word in category.split())
    return bool(re.search(rf"(?<!\w){phrase}(?!\w)", title))


def select_game_vods(vods, game_id, category):
    selected = []
    for vod in vods:
        vod_game_id = vod.get("game_id")
        if vod_game_id:
            if str(vod_game_id) == str(game_id):
                selected.append(vod)
        elif category_title_matches(vod.get("title", ""), category):
            selected.append(vod)
    return selected


def select_game_clips(clips, game_id):
    return [clip for clip in clips if clip.get("game_id") == game_id]


def run_checked(command):
    subprocess.run(command, check=True)


def is_readable_video(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True,
    )
    return result.returncode == 0 and result.stdout.strip()


def download_video(item, kind, work_dir):
    video_id = str(item["id"])
    target_dir = work_dir / kind
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{video_id}.mp4"
    if target.exists():
        if is_readable_video(target):
            print(f"Использую скачанный ранее файл: {target}")
            return target
        raise RuntimeError(f"Найден повреждённый локальный файл, удалите его вручную: {target}")

    url = item.get("url") if kind == "clips" else f"https://www.twitch.tv/videos/{video_id}"
    if not url:
        raise RuntimeError(f"Twitch не вернул URL для {kind} {video_id}")
    print(f"Скачиваю {kind}: {item.get('title', video_id)}")
    run_checked([
        "yt-dlp", "--no-warnings", "--no-progress", "--no-part",
        "--format", "bestvideo+bestaudio/best", "--merge-output-format", "mp4",
        "--output", str(target), url,
    ])
    if not target.exists() or not is_readable_video(target):
        raise RuntimeError(f"yt-dlp завершился, но файл не прошёл проверку: {target}")
    return target


def locate_chat_downloader(requested=None):
    executable = requested or os.environ.get("TWITCHDOWNLOADERCLI")
    if executable:
        resolved = shutil.which(executable) or executable
        if Path(resolved).is_file() and os.access(resolved, os.X_OK):
            return str(Path(resolved).resolve())
        raise RuntimeError(
            f"TwitchDownloaderCLI не найден или не исполняемый: {executable}"
        )
    resolved = shutil.which("TwitchDownloaderCLI")
    if not resolved:
        raise RuntimeError(
            "Для анализа Twitch-чата установите TwitchDownloaderCLI и добавьте его в PATH "
            "или задайте TWITCHDOWNLOADERCLI"
        )
    return resolved


def download_chat(item, kind, work_dir, cli):
    chat_dir = work_dir / "chat"
    chat_dir.mkdir(parents=True, exist_ok=True)
    chat_path = chat_dir / f"{kind}-{item['id']}.json"
    if chat_path.exists():
        return chat_path
    run_checked([
        cli, "chatdownload", "--id", str(item["id"]), "--output", str(chat_path),
        "--collision", "Overwrite",
    ])
    if not chat_path.exists() or chat_path.stat().st_size == 0:
        raise RuntimeError(f"TwitchDownloaderCLI не создал файл чата: {chat_path}")
    return chat_path


def is_chat_laughter(text):
    return bool(CHAT_LAUGHTER.search(text))


def load_chat_signals(chat_path, duration):
    try:
        import ijson
    except ImportError as error:
        raise RuntimeError(
            "Не установлен пакет ijson; выполните pip install -r requirements.txt"
        ) from error

    length = max(1, int(np.ceil(duration)))
    messages = np.zeros(length, dtype=float)
    laughs = np.zeros(length, dtype=float)
    with open(chat_path, "rb") as stream:
        for comment in ijson.items(stream, "comments.item"):
            try:
                offset = float(comment["content_offset_seconds"])
            except (KeyError, TypeError, ValueError):
                continue
            index = int(offset)
            if index < 0 or index >= length:
                continue
            body = (comment.get("message") or {}).get("body") or ""
            messages[index] += 1
            if is_chat_laughter(body):
                laughs[index] += 1
    return messages, laughs


def normalized(values):
    values = np.asarray(values, dtype=float)
    if not values.size:
        return values
    low, high = np.percentile(values, [5, 95])
    if high - low < 1e-9:
        return np.zeros_like(values)
    return np.clip((values - low) / (high - low), 0, 1)


def rolling_chat_bursts(messages, width=10):
    prefix = np.concatenate(([0.0], np.cumsum(messages)))
    left = np.maximum(0, np.arange(len(messages)) - width // 2)
    right = np.minimum(len(messages), left + width)
    return prefix[right] - prefix[left]


def srt_segments_for_window(segments, start, end):
    return [
        segment for segment in segments
        if segment["end"] > start and segment["start"] < end
    ]


def write_candidate_srt(segments, path):
    if segments:
        narezchik.write_srt(segments, str(path))
        return path
    return None


def find_chunk_candidates(
    audio_rms,
    motion,
    chat_bursts,
    chat_laughs,
    transcript_segments,
    chunk_start,
    core_start,
    core_end,
    duration,
    min_score,
    min_separation,
):
    count = min(len(audio_rms), len(motion))
    if count < 30:
        return []
    audio_score = normalized(audio_rms)
    motion_score = normalized(motion)
    chunk_end = chunk_start + count
    chat_score = chat_bursts[
        max(0, int(chunk_start)):min(len(chat_bursts), int(chunk_end))
    ]
    chat_offset = max(0, int(chunk_start))
    chat_laugh_slice = chat_laughs[chat_offset:chat_offset + count]
    if len(chat_score) < count:
        chat_score = np.pad(chat_score, (0, count - len(chat_score)))
    if len(chat_laugh_slice) < count:
        chat_laugh_slice = np.pad(chat_laugh_slice, (0, count - len(chat_laugh_slice)))

    transcript_laugh = np.zeros(count, dtype=bool)
    transcript_insight = np.zeros(count, dtype=bool)
    for segment in transcript_segments:
        text = segment["text"]
        first = max(0, int(segment["start"] - chunk_start))
        last = min(count, max(first + 1, int(np.ceil(segment["end"] - chunk_start))))
        if first >= count:
            continue
        if TRANSCRIPT_LAUGHTER.search(text):
            transcript_laugh[first:last] = True
        if INSIGHT_CUE.search(text):
            transcript_insight[first:last] = True

    laughter = (chat_laugh_slice > 0) | transcript_laugh
    peaks = []
    last_start = max(0, count - 30)
    for local_start in range(0, last_start + 1, 5):
        local_end = min(count, local_start + WINDOW_SECONDS)
        global_start = chunk_start + local_start
        global_end = min(duration, chunk_start + local_end)
        center = (global_start + global_end) / 2
        if center < core_start or center >= core_end or global_end - global_start < 30:
            continue

        sl = slice(local_start, local_end)
        audio = float(np.mean(audio_score[sl]))
        motion_value = float(np.mean(motion_score[sl]))
        chat_value = float(np.max(chat_score[sl])) if len(chat_score[sl]) else 0.0
        has_laughter = bool(np.any(laughter[sl]))
        has_insight = bool(np.any(transcript_insight[sl]))
        score = (
            0.25 * audio + 0.15 * motion_value + 0.30 * chat_value
            + 0.20 * has_laughter + 0.10 * has_insight
        )
        evidence = sum((
            audio >= 0.35,
            motion_value >= 0.35,
            chat_value >= 0.35,
            has_laughter,
            has_insight,
        ))
        if score >= min_score and evidence >= 2:
            peaks.append({
                "start": global_start,
                "end": global_end,
                "center": center,
                "score": score,
            })

    peaks.sort(key=lambda candidate: (-candidate["score"], candidate["center"]))
    selected = []
    for candidate in peaks:
        if all(
            abs(candidate["center"] - current["center"]) >= min_separation
            for current in selected
        ):
            selected.append(candidate)
    return sorted(selected, key=lambda candidate: candidate["center"])


def scan_vod_candidates(
    source,
    duration,
    chat_messages,
    chat_laughs,
    no_whisper,
    chunk_seconds,
    min_score,
    min_separation,
):
    bursts = normalized(rolling_chat_bursts(chat_messages))
    core_seconds = chunk_seconds - 80
    core_start = 0
    recent_centers = []
    while core_start < duration:
        core_end = min(duration, core_start + core_seconds)
        scan_start = max(0, core_start - 40)
        scan_end = min(duration, core_end + 40)
        scan_length = scan_end - scan_start
        print(
            f"Анализирую {narezchik.fmt_tc(scan_start)} — "
            f"{narezchik.fmt_tc(scan_end)}..."
        )
        rms = narezchik.audio_rms_per_sec(source, scan_length, scan_start)
        motion = narezchik.motion_per_sec(source, scan_length, scan_start)
        segments = []
        if not no_whisper:
            segments = narezchik.transcribe_window(source, scan_start, scan_end)
        candidates = find_chunk_candidates(
            rms,
            motion,
            bursts,
            chat_laughs,
            segments,
            scan_start,
            core_start,
            core_end,
            duration,
            min_score,
            min_separation,
        )
        for candidate in candidates:
            recent_centers = [
                center for center in recent_centers
                if candidate["center"] - center < min_separation
            ]
            if any(
                abs(candidate["center"] - center) < min_separation
                for center in recent_centers
            ):
                continue
            recent_centers.append(candidate["center"])
            candidate["transcript"] = srt_segments_for_window(
                segments, candidate["start"], candidate["end"]
            )
            yield candidate
        core_start = core_end


def render_candidate(item, source, output_dir, work_dir, candidate, number, no_whisper):
    video_id = str(item["id"])
    basename = (
        f"twitch-vod-{video_id}-moment-{number:05d}-"
        f"at-{int(candidate['center']):08d}"
    )
    existing = []
    missing = []
    for account, style, duration in VARIANTS:
        filename = f"{basename}-{style}.mp4"
        final_video = output_dir / account / filename
        archived_video = output_dir / account / "published" / filename
        if final_video.exists():
            if not is_readable_video(final_video):
                raise RuntimeError(f"Найден повреждённый готовый файл: {final_video}")
            if not final_video.with_suffix(".txt").exists():
                raise RuntimeError(f"Для готового видео нет подписи: {final_video}")
            existing.append((account, style, duration, final_video))
        elif archived_video.exists():
            if not is_readable_video(archived_video):
                raise RuntimeError(f"Найден повреждённый архивный файл: {archived_video}")
            if not archived_video.with_suffix(".txt").exists():
                raise RuntimeError(f"Для архивного видео нет подписи: {archived_video}")
            existing.append((account, style, duration, archived_video))
        else:
            missing.append((account, style, duration, final_video))
    if existing and missing:
        raise RuntimeError(
            f"Для момента #{number} найдены только часть вариантов; "
            "проверьте очереди account-1..3 вручную"
        )
    if existing:
        print(f"Момент #{number} уже смонтирован, пропускаю.")
        return [
            {
                "id": video_id,
                "moment": number,
                "title": item.get("title", ""),
                "source": str(source),
                "account": account,
                "variant": style,
                "start_sec": round(max(
                    0.0,
                    min(candidate["center"] - duration / 2,
                        candidate["source_duration"] - duration),
                ), 3),
                "score": round(candidate["score"], 3),
                "output": str(path),
            }
            for account, style, duration, path in existing
        ]

    stage_dir = work_dir / "staging" / f"{video_id}-{number:05d}"
    stage_dir.mkdir(parents=True, exist_ok=True)
    srt_path = write_candidate_srt(
        candidate["transcript"], stage_dir / f"{basename}.srt"
    )
    staged = []

    for account, style, duration in VARIANTS:
        start = max(0.0, min(
            candidate["center"] - duration / 2,
            max(0.0, candidate["source_duration"] - duration),
        ))
        end = min(candidate["source_duration"], start + duration)
        output = stage_dir / f"{basename}-{style}.mp4"
        command = [
            sys.executable, str(NAREZCHIK), str(source), "-o", str(output),
            "--min", "30", "--max", "40", "--layout", "full",
            "--variant", style, "--duration", str(duration),
            "--start-sec", f"{start:.3f}", "--end-sec", f"{end:.3f}",
        ]
        if srt_path:
            command.extend(["--srt", str(srt_path)])
        if no_whisper:
            command.append("--no-whisper")
        run_checked(command)
        staged.append((account, style, output))

    published = []
    for account, style, staged_video in staged:
        account_dir = output_dir / account
        account_dir.mkdir(parents=True, exist_ok=True)
        final_video = account_dir / staged_video.name
        if final_video.exists():
            raise RuntimeError(f"Готовый файл уже существует: {final_video}")

        for suffix in (".json", ".txt"):
            staged_sidecar = staged_video.with_suffix(suffix)
            if suffix == ".txt":
                staged_sidecar.write_text(
                    str(item.get("title") or "").strip(), encoding="utf-8"
                )
            if staged_sidecar.exists():
                os.replace(staged_sidecar, final_video.with_suffix(suffix))
        os.replace(staged_video, final_video)
        published.append({
            "id": video_id,
            "moment": number,
            "title": item.get("title", ""),
            "source": str(source),
            "account": account,
            "variant": style,
            "start_sec": round(max(
                0.0,
                min(candidate["center"] - duration / 2,
                    candidate["source_duration"] - duration),
            ), 3),
            "score": round(candidate["score"], 3),
            "output": str(final_video),
        })
    print(
        f"Готов момент #{number}: {narezchik.fmt_tc(candidate['center'])}, "
        f"оценка {candidate['score']:.2f}"
    )
    return published


def process_video(item, kind, source, output_dir, variant, report_path, no_whisper):
    video_id = str(item["id"])
    account, style, duration = variant
    account_dir = output_dir / account
    account_dir.mkdir(parents=True, exist_ok=True)
    output = account_dir / f"twitch-{kind[:-1]}-{video_id}-{style}.mp4"
    caption_path = output.with_suffix(".txt")
    if output.exists():
        if not is_readable_video(output):
            raise RuntimeError(f"Найден повреждённый готовый файл, удалите его вручную: {output}")
        existing_report = output.with_suffix(".json")
        if not existing_report.exists():
            raise RuntimeError(
                f"Для готовой версии нет отчёта с выбранным моментом: {existing_report}"
            )
        if not caption_path.exists():
            caption_path.write_text(str(item.get("title") or "").strip(), encoding="utf-8")
        print(f"Уже обработан, пропускаю: {output}")
        return output, existing_report

    command = [
        sys.executable, str(NAREZCHIK), str(source), "-o", str(output),
        "--min", "30", "--max", "40",
        "--layout", "full",
        "--variant", style,
        "--duration", str(duration),
    ]
    if report_path and report_path.exists():
        command.extend(["--reuse-report", str(report_path)])
    if no_whisper:
        command.append("--no-whisper")
    run_checked(command)
    caption_path.write_text(str(item.get("title") or "").strip(), encoding="utf-8")
    return output, output.with_suffix(".json")


def probe_duration(path):
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def start_publisher(output_dir, work_dir, delay_minutes):
    if not shutil.which("node"):
        raise RuntimeError("Для публикации через браузер нужен Node.js в PATH")
    done_file = work_dir / f"publisher-{uuid.uuid4().hex}.done"
    confirmation = input(
        "Автоматически публиковать готовые видео в TikTok по очереди? "
        "Введите START для подтверждения: "
    ).strip()
    if confirmation != "START":
        raise RuntimeError("Публикация отменена пользователем")
    child = subprocess.Popen([
        "node", str(PUBLISHER_QUEUE),
        "--folder", str(output_dir),
        "--accounts", "3",
        "--delay-minutes", str(delay_minutes),
        "--watch",
        "--done-file", str(done_file),
        "--auto-start",
    ])
    return child, done_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--channel", default="strogo")
    parser.add_argument("--category", default="Grand Theft Auto VI")
    parser.add_argument("--hours", type=float, default=24)
    parser.add_argument("--max-clips", type=int, default=1)
    parser.add_argument("--max-vods", type=int, default=1)
    parser.add_argument("--source", choices=("both", "clips", "vods"), default="vods")
    parser.add_argument("--out-dir", default="queue")
    parser.add_argument("--work-dir", default="work/twitch")
    parser.add_argument("--chat-cli", help="путь к TwitchDownloaderCLI")
    parser.add_argument("--chunk-seconds", type=int, default=300,
                        help="длина анализируемого чанка")
    parser.add_argument("--min-score", type=float, default=0.45,
                        help="минимальная оценка сильного момента (0..1)")
    parser.add_argument("--min-separation", type=int, default=90,
                        help="минимальный интервал между найденными моментами")
    parser.add_argument("--publish", action="store_true",
                        help="сразу публиковать готовые варианты через очередь TikTok")
    parser.add_argument("--delay-minutes", type=int, default=60,
                        help="пауза между TikTok-публикациями")
    parser.add_argument("--no-whisper", action="store_true",
                        help="не распознавать речь и не использовать текстовые сигналы")
    args = parser.parse_args()

    if (args.hours <= 0 or args.max_clips < 0 or args.max_vods < 0
            or args.chunk_seconds < 200 or args.min_separation < WINDOW_SECONDS
            or not 0 <= args.min_score <= 1 or args.delay_minutes < 0):
        parser.error(
            "--hours должен быть положительным; лимиты и интервал неотрицательными; "
            "--chunk-seconds не меньше 200, --min-separation не меньше 40"
        )
    if not args.no_whisper and importlib.util.find_spec("whisper") is None:
        parser.error(
            "Whisper не установлен; выполните pip install -r requirements.txt "
            "или задайте --no-whisper"
        )
    for tool in ("ffmpeg", "ffprobe", "yt-dlp"):
        if not shutil.which(tool):
            parser.error(f"{tool} не найден; установите его и повторите запуск")

    client_id = os.environ.get("TWITCH_CLIENT_ID", "").strip()
    client_secret = os.environ.get("TWITCH_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        parser.error("задайте TWITCH_CLIENT_ID и TWITCH_CLIENT_SECRET")

    now = datetime.now(timezone.utc)
    started = now - timedelta(hours=args.hours)
    started_iso, ended_iso = iso(started), iso(now)
    token = get_token(client_id, client_secret)
    users = helix("users", {"login": args.channel}, client_id, token).get("data", [])
    if not users:
        raise TwitchError("api", f"канал '{args.channel}' не найден")
    user_id = users[0]["id"]

    games = helix("games", {"name": args.category}, client_id, token).get("data", [])
    if not games:
        raise TwitchError("api", f"категория '{args.category}' не найдена в Twitch")
    game_id = games[0]["id"]

    clips, vods = [], []
    if args.source in ("both", "clips"):
        clips = select_game_clips(
            fetch_clips(user_id, started_iso, ended_iso, client_id, token),
            game_id,
        )
        clips.sort(key=lambda item: item["created_at"], reverse=True)
        clips = clips[:args.max_clips]
    if args.source in ("both", "vods"):
        vods = select_game_vods(
            fetch_vods(user_id, started, now, client_id, token),
            game_id,
            args.category,
        )
        vods.sort(key=lambda item: item["created_at"], reverse=True)
        vods = vods[:args.max_vods]

    print(f"Найдено: {len(clips)} клипов и {len(vods)} VOD.")
    if not clips and not vods:
        print("Подходящих видео за выбранный период нет.")
        return

    work_dir = Path(args.work_dir).resolve()
    output_dir = Path(args.out_dir).resolve()
    work_dir.mkdir(parents=True, exist_ok=True)
    output_dir.mkdir(parents=True, exist_ok=True)
    chat_cli = locate_chat_downloader(args.chat_cli) if vods else None
    publisher = None
    done_file = None
    if args.publish:
        if not PUBLISHER_QUEUE.exists():
            raise RuntimeError(f"Не найден TikTok queue publisher: {PUBLISHER_QUEUE}")
        publisher, done_file = start_publisher(output_dir, work_dir, args.delay_minutes)

    results = []
    try:
        for kind, items in (("clips", clips), ("vods", vods)):
            for item in items:
                source = download_video(item, kind, work_dir)
                if kind == "clips":
                    report_path = None
                    for variant in VARIANTS:
                        output, report_path = process_video(
                            item, kind, source, output_dir, variant,
                            report_path, args.no_whisper,
                        )
                        results.append({
                            "type": "clip",
                            "id": item["id"],
                            "title": item.get("title", ""),
                            "source": str(source),
                            "account": variant[0],
                            "variant": variant[1],
                            "output": str(output),
                        })
                    continue

                chat_path = download_chat(item, kind, work_dir, chat_cli)
                duration = probe_duration(source)
                messages, laughs = load_chat_signals(chat_path, duration)
                candidates = scan_vod_candidates(
                    source,
                    duration,
                    messages,
                    laughs,
                    args.no_whisper,
                    args.chunk_seconds,
                    args.min_score,
                    args.min_separation,
                )
                futures: list[Future] = []
                with ThreadPoolExecutor(max_workers=1) as renderer:
                    for number, candidate in enumerate(candidates, 1):
                        if publisher and publisher.poll() is not None:
                            raise RuntimeError(
                                f"Очередь публикации завершилась с кодом {publisher.returncode}"
                            )
                        candidate["source_duration"] = duration
                        print(
                            f"Найден момент #{number}: "
                            f"{narezchik.fmt_tc(candidate['center'])}, "
                            f"оценка {candidate['score']:.2f}; начинаю монтаж"
                        )
                        futures.append(renderer.submit(
                            render_candidate,
                            item, source, output_dir, work_dir,
                            candidate, number, args.no_whisper,
                        ))
                        completed = [future for future in futures if future.done()]
                        for future in completed:
                            results.extend(future.result())
                            futures.remove(future)
                    for future in futures:
                        results.extend(future.result())

        report_path = work_dir / "report.json"
        report_path.write_text(json.dumps({
            "channel": args.channel,
            "category": args.category,
            "window": {"started_at": started_iso, "ended_at": ended_iso},
            "selection": {
                "min_score": args.min_score,
                "min_separation_seconds": args.min_separation,
                "chunk_seconds": args.chunk_seconds,
            },
            "processed": results,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"Готовые вертикальные видео добавлены в {output_dir}; отчёт: {report_path}")
    finally:
        if publisher:
            done_file.write_text("done\n", encoding="utf-8")
            return_code = publisher.wait()
            if return_code:
                raise RuntimeError(f"Очередь TikTok завершилась с кодом {return_code}")


if __name__ == "__main__":
    try:
        main()
    except (TwitchError, subprocess.CalledProcessError, OSError, RuntimeError) as error:
        print(f"Ошибка обработки Twitch: {error}", file=sys.stderr)
        sys.exit(1)
