#!/usr/bin/env node
import { mkdir, access } from "node:fs/promises";
import { resolve } from "node:path";
import { createInterface } from "node:readline/promises";
import { stdin, stdout } from "node:process";
import { chromium } from "playwright";

const TIKTOK_UPLOAD_URL = "https://www.tiktok.com/tiktokstudio/upload";

function usage() {
  console.log(
    "Usage: node scripts/publish_tiktok.mjs <video.mp4> [--caption TEXT] [--profile-dir PATH] [--auto-confirm]",
  );
}

function parseArgs(args) {
  let videoPath;
  let caption = process.env.TIKTOK_CAPTION;
  let profileDir = process.env.TIKTOK_PROFILE_DIR || "work/tiktok-profile";
  let autoConfirm = false;

  for (let i = 0; i < args.length; i += 1) {
    if (args[i] === "--caption") {
      caption = args[++i];
      if (caption === undefined) throw new Error("--caption requires text");
    } else if (args[i] === "--profile-dir") {
      profileDir = args[++i];
      if (!profileDir) throw new Error("--profile-dir requires a path");
    } else if (args[i] === "--auto-confirm") {
      autoConfirm = true;
    } else if (args[i].startsWith("--")) {
      throw new Error(`Unknown option: ${args[i]}`);
    } else if (!videoPath) {
      videoPath = args[i];
    } else {
      throw new Error(`Unexpected argument: ${args[i]}`);
    }
  }

  if (!videoPath) throw new Error("A video file is required");
  return {
    videoPath: resolve(videoPath),
    caption,
    profileDir: resolve(profileDir),
    autoConfirm,
  };
}

async function ask(rl, prompt) {
  return (await rl.question(prompt)).trim();
}

async function waitForPublishSuccess(page) {
  const successText = page.getByText(
    /your video is being uploaded|video (?:was|has been) (?:uploaded|posted|published)|(?:upload|post|publish)(?:ed)? (?:successfully|complete)|видео (?:загружено|опубликовано)|публикация успешна|发布成功|视频已上传/i,
  ).first();
  await successText.waitFor({ state: "visible", timeout: 180000 });
}

async function main() {
  if (process.argv.slice(2).some((arg) => arg === "--help" || arg === "-h")) {
    usage();
    return;
  }

  let options;
  try {
    options = parseArgs(process.argv.slice(2));
    await access(options.videoPath);
  } catch (error) {
    usage();
    throw error;
  }

  const rl = createInterface({ input: stdin, output: stdout });
  let context;

  try {
    if (!options.caption) {
      options.caption = await ask(rl, "Подпись к видео (можно оставить пустой): ");
    }

    await mkdir(options.profileDir, { recursive: true, mode: 0o700 });
    context = await chromium.launchPersistentContext(options.profileDir, {
      headless: false,
      viewport: null,
      args: ["--start-maximized"],
    });

    const page = context.pages()[0] || await context.newPage();
    await page.goto(TIKTOK_UPLOAD_URL, { waitUntil: "domcontentloaded" });

    let fileInput = page.locator('input[type="file"]').first();
    try {
      await fileInput.waitFor({ state: "attached", timeout: 5000 });
    } catch {
      console.log("Войдите в TikTok в открывшемся браузере.");
      await ask(rl, "После входа нажмите Enter, чтобы продолжить: ");
      await page.goto(TIKTOK_UPLOAD_URL, { waitUntil: "domcontentloaded" });
      fileInput = page.locator('input[type="file"]').first();
      await fileInput.waitFor({ state: "attached", timeout: 120000 });
    }

    await fileInput.setInputFiles(options.videoPath);

    const captionBox = page.locator('[contenteditable="true"]:visible').first();
    await captionBox.waitFor({ state: "visible", timeout: 120000 });
    if (options.caption) await captionBox.fill(options.caption);

    if (options.autoConfirm) {
      console.log("Публикация подтверждена режимом очереди.");
    } else {
      console.log("Видео загружено. Проверьте его и подпись в браузере.");
      const confirmation = await ask(
        rl,
        'Чтобы опубликовать, введите PUBLISH. Любой другой ввод отменит публикацию: ',
      );
      if (confirmation.toUpperCase() !== "PUBLISH") {
        console.log("Публикация отменена; видео не отправлено.");
        return;
      }
    }

    const publishButton = page.getByRole("button", {
      name: /^(post|publish|опубликовать|опубликовать сейчас)$/i,
    });
    await publishButton.waitFor({ state: "visible", timeout: 30000 });
    await publishButton.click();

    if (options.autoConfirm) {
      console.log("Ожидаю подтверждение загрузки от TikTok...");
      await waitForPublishSuccess(page);
      console.log("TikTok подтвердил отправку видео.");
    } else {
      console.log("Кнопка публикации нажата. Проверьте результат в окне TikTok.");
      await ask(rl, "Нажмите Enter, чтобы закрыть браузер: ");
    }
  } finally {
    rl.close();
    if (context) await context.close();
  }
}

main().catch((error) => {
  console.error(`Ошибка публикации: ${error.message}`);
  process.exitCode = 1;
});
