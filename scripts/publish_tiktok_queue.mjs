#!/usr/bin/env node
import { access, mkdir, readFile, readdir, rename } from "node:fs/promises";
import { spawn } from "node:child_process";
import { dirname, extname, join, parse, resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { fileURLToPath } from "node:url";

const SCRIPT_DIR = dirname(fileURLToPath(import.meta.url));
const PUBLISHER = join(SCRIPT_DIR, "publish_tiktok.mjs");

function parseArgs(args) {
  let folder = "queue";
  let delayMinutes = 60;

  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === "--folder") {
      folder = args[++i];
      if (!folder) throw new Error("--folder requires a path");
    } else if (args[i] === "--delay-minutes") {
      const value = Number(args[++i]);
      if (!Number.isInteger(value) || value < 0) {
        throw new Error("--delay-minutes must be a non-negative integer");
      }
      delayMinutes = value;
    } else if (args[i] !== "--help" && args[i] !== "-h") {
      throw new Error(`Unknown option: ${args[i]}`);
    }
  }

  return { folder: resolve(folder), delayMinutes };
}

function usage() {
  console.log(
    "Usage: npm run publish:tiktok:queue -- [--folder queue] [--delay-minutes 60]",
  );
}

function captionPathFor(videoPath) {
  return `${videoPath.slice(0, -extname(videoPath).length)}.txt`;
}

async function captionFor(videoPath) {
  const captionPath = captionPathFor(videoPath);
  try {
    return (await readFile(captionPath, "utf8")).trim();
  } catch (error) {
    if (error.code !== "ENOENT") throw error;
    return parse(videoPath).name.replace(/[_-]+/g, " ");
  }
}

function runPublisher(videoPath, caption) {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(
      process.execPath,
      [PUBLISHER, videoPath, "--caption", caption, "--auto-confirm"],
      { stdio: "inherit", env: process.env },
    );
    child.once("error", rejectRun);
    child.once("exit", (code, signal) => {
      if (code === 0) {
        resolveRun();
      } else {
        rejectRun(
          new Error(
            `Публикация остановлена: процесс завершился с кодом ${code ?? signal}`,
          ),
        );
      }
    });
  });
}

function sleep(ms) {
  return new Promise((resolveSleep) => setTimeout(resolveSleep, ms));
}

async function main() {
  if (process.argv.slice(2).some((arg) => arg === "--help" || arg === "-h")) {
    usage();
    return;
  }

  const options = parseArgs(process.argv.slice(2));
  const publishedDir = join(options.folder, "published");
  await mkdir(options.folder, { recursive: true });
  let videos = (await readdir(options.folder, { withFileTypes: true }))
    .filter((entry) => entry.isFile() && extname(entry.name).toLowerCase() === ".mp4")
    .map((entry) => join(options.folder, entry.name))
    .sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }));

  if (videos.length === 0) {
    console.log(`В ${options.folder} нет MP4 для публикации.`);
    return;
  }

  await access(PUBLISHER);
  await mkdir(publishedDir, { recursive: true });

  for (const videoPath of videos) {
    const archivedVideo = join(publishedDir, parse(videoPath).base);
    try {
      await access(archivedVideo);
      throw new Error(`Файл уже есть в архиве публикаций: ${archivedVideo}`);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }

    const captionPath = captionPathFor(videoPath);
    try {
      await access(captionPath);
    } catch (error) {
      if (error.code === "ENOENT") continue;
      throw error;
    }

    const archivedCaption = join(publishedDir, parse(captionPath).base);
    try {
      await access(archivedCaption);
      throw new Error(`Файл уже есть в архиве публикаций: ${archivedCaption}`);
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
  }

  console.log(`Очередь: ${videos.length} видео; интервал: ${options.delayMinutes} мин.`);
  for (const videoPath of videos) {
    console.log(`  - ${parse(videoPath).base} — ${await captionFor(videoPath)}`);
  }

  const rl = createInterface({ input: stdin, output: stdout });
  try {
    const confirmation = (await rl.question(
      'Введите START, чтобы начать автоматическую публикацию: ',
    )).trim();
    if (confirmation !== "START") {
      console.log("Очередь отменена.");
      return;
    }
  } finally {
    rl.close();
  }

  for (let i = 0; i < videos.length; i += 1) {
    const videoPath = videos[i];
    const caption = await captionFor(videoPath);
    console.log(`\n[${i + 1}/${videos.length}] Публикую ${parse(videoPath).base}`);
    await runPublisher(videoPath, caption);

    await rename(videoPath, join(publishedDir, parse(videoPath).base));
    const captionPath = captionPathFor(videoPath);
    try {
      await rename(captionPath, join(publishedDir, parse(captionPath).base));
    } catch (error) {
      if (error.code !== "ENOENT") throw error;
    }
    console.log(`Готово; исходный MP4 перемещён в ${publishedDir}.`);

    if (i < videos.length - 1 && options.delayMinutes > 0) {
      console.log(`Следующая публикация через ${options.delayMinutes} мин.`);
      await sleep(options.delayMinutes * 60 * 1000);
    }
  }

  console.log("\nОчередь опубликована.");
}

main().catch((error) => {
  console.error(`Ошибка очереди TikTok: ${error.message}`);
  process.exitCode = 1;
});
