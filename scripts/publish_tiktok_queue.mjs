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
  let accounts = 1;
  let profileDir = "work/tiktok-profiles";
  let watch = false;
  let doneFile;
  let pollSeconds = 3;
  let autoStart = false;

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
    } else if (args[i] === "--accounts") {
      accounts = Number(args[++i]);
      if (!Number.isInteger(accounts) || accounts < 1 || accounts > 10) {
        throw new Error("--accounts must be an integer from 1 to 10");
      }
    } else if (args[i] === "--profile-dir") {
      profileDir = args[++i];
      if (!profileDir) throw new Error("--profile-dir requires a path");
    } else if (args[i] === "--watch") {
      watch = true;
    } else if (args[i] === "--done-file") {
      doneFile = args[++i];
      if (!doneFile) throw new Error("--done-file requires a path");
    } else if (args[i] === "--poll-seconds") {
      pollSeconds = Number(args[++i]);
      if (!Number.isInteger(pollSeconds) || pollSeconds < 1) {
        throw new Error("--poll-seconds must be a positive integer");
      }
    } else if (args[i] === "--auto-start") {
      autoStart = true;
    } else if (args[i] !== "--help" && args[i] !== "-h") {
      throw new Error(`Unknown option: ${args[i]}`);
    }
  }

  if (watch && !doneFile) {
    throw new Error("--watch requires --done-file");
  }

  return {
    folder: resolve(folder),
    delayMinutes,
    accounts,
    profileDir: resolve(profileDir),
    watch,
    doneFile: doneFile ? resolve(doneFile) : null,
    pollSeconds,
    autoStart,
  };
}

function usage() {
  console.log(
    "Usage: npm run publish:tiktok:queue -- [--folder queue] [--delay-minutes 60] [--accounts 3] [--watch --done-file PATH]",
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

function runPublisher(videoPath, caption, profileDir) {
  return new Promise((resolveRun, rejectRun) => {
    const child = spawn(
      process.execPath,
      [PUBLISHER, videoPath, "--caption", caption, "--profile-dir", profileDir, "--auto-confirm"],
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

async function exists(path) {
  try {
    await access(path);
    return true;
  } catch (error) {
    if (error.code === "ENOENT") return false;
    throw error;
  }
}

async function discoverTasks(options) {
  const accountQueues = [];
  for (let account = 1; account <= options.accounts; account += 1) {
    const accountFolder = options.accounts === 1
      ? options.folder
      : join(options.folder, `account-${account}`);
    await mkdir(accountFolder, { recursive: true });
    const videos = (await readdir(accountFolder, { withFileTypes: true }))
      .filter((entry) => entry.isFile() && extname(entry.name).toLowerCase() === ".mp4")
      .map((entry) => join(accountFolder, entry.name))
      .sort((a, b) => a.localeCompare(b, undefined, { numeric: true, sensitivity: "base" }));
    accountQueues.push({ account, folder: accountFolder, videos });
  }

  const maxVideos = Math.max(0, ...accountQueues.map(({ videos }) => videos.length));
  const tasks = [];
  for (let index = 0; index < maxVideos; index += 1) {
    for (const accountQueue of accountQueues) {
      const videoPath = accountQueue.videos[index];
      if (!videoPath || !(await exists(captionPathFor(videoPath)))) continue;
      tasks.push({
        videoPath,
        account: accountQueue.account,
        publishedDir: join(accountQueue.folder, "published"),
        profileDir: options.accounts === 1
          ? process.env.TIKTOK_PROFILE_DIR || "work/tiktok-profile"
          : join(options.profileDir, `account-${accountQueue.account}`),
      });
    }
  }
  return tasks;
}

async function assertNotPublished(task) {
  await mkdir(task.publishedDir, { recursive: true });
  const archivedVideo = join(task.publishedDir, parse(task.videoPath).base);
  if (await exists(archivedVideo)) {
    throw new Error(`Файл уже есть в архиве публикаций: ${archivedVideo}`);
  }

  const captionPath = captionPathFor(task.videoPath);
  const archivedCaption = join(task.publishedDir, parse(captionPath).base);
  if (await exists(archivedCaption)) {
    throw new Error(`Файл подписи уже есть в архиве публикаций: ${archivedCaption}`);
  }
}

async function main() {
  if (process.argv.slice(2).some((arg) => arg === "--help" || arg === "-h")) {
    usage();
    return;
  }

  const options = parseArgs(process.argv.slice(2));
  await mkdir(options.folder, { recursive: true });
  const initialTasks = await discoverTasks(options);
  if (initialTasks.length === 0 && !options.watch) {
    console.log(`В ${options.folder} нет MP4 для публикации.`);
    return;
  }

  await access(PUBLISHER);

  console.log(
    `Очередь: ${initialTasks.length} готовых видео для ${options.accounts} аккаунтов; ` +
    `интервал: ${options.delayMinutes} мин.`,
  );
  for (const task of initialTasks) {
    console.log(
      `  - account-${task.account}: ${parse(task.videoPath).base} — ` +
      `${await captionFor(task.videoPath)}`,
    );
  }
  if (options.watch) {
    console.log("Режим наблюдения: новые готовые MP4 будут добавляться в очередь автоматически.");
  }

  if (!options.autoStart) {
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
  } else {
    console.log("Публикация подтверждена.");
  }

  const handled = new Set();
  let published = 0;
  let nextAllowedAt = 0;
  while (true) {
    const tasks = (await discoverTasks(options))
      .filter((task) => !handled.has(task.videoPath));
    if (tasks.length === 0) {
      if (!options.watch || await exists(options.doneFile)) break;
      await sleep(options.pollSeconds * 1000);
      continue;
    }

    for (const task of tasks) {
      handled.add(task.videoPath);
      await assertNotPublished(task);
      const waitMs = nextAllowedAt - Date.now();
      if (waitMs > 0) {
        console.log(`Следующая публикация через ${Math.ceil(waitMs / 60000)} мин.`);
        await sleep(waitMs);
      }
      const caption = await captionFor(task.videoPath);
      published += 1;
      console.log(
        `\n[${published}] account-${task.account}: ` +
        `публикую ${parse(task.videoPath).base}`,
      );
      await runPublisher(task.videoPath, caption, task.profileDir);

      await rename(task.videoPath, join(task.publishedDir, parse(task.videoPath).base));
      const captionPath = captionPathFor(task.videoPath);
      if (await exists(captionPath)) {
        await rename(captionPath, join(task.publishedDir, parse(captionPath).base));
      }
      console.log(`Готово; исходный MP4 перемещён в ${task.publishedDir}.`);
      nextAllowedAt = Date.now() + options.delayMinutes * 60 * 1000;
    }
  }

  console.log(`\nОчередь опубликована. Видео: ${published}.`);
}

main().catch((error) => {
  console.error(`Ошибка очереди TikTok: ${error.message}`);
  process.exitCode = 1;
});
