import { app, BrowserWindow, dialog, ipcMain, type OpenDialogOptions } from "electron";
import { spawn, ChildProcess } from "node:child_process";
import { existsSync, readFileSync, unlinkSync, writeFileSync } from "node:fs";
import net from "node:net";
import path from "node:path";

let backend: ChildProcess | null = null;
let mainWindow: BrowserWindow | null = null;
let runtimeInfo: { port: number; token: string } | null = null;
let quitting = false;

function dataDir(): string {
  const base = process.env.PF_APP_DATA || process.env.APPDATA || process.env.HOME || ".";
  return path.join(base, "pf-automation");
}

function runtimePath(): string {
  return path.join(dataDir(), "runtime.json");
}

function backendDir(): string {
  if (app.isPackaged) return path.join(process.resourcesPath, "backend");
  return path.resolve(__dirname, "..", "..", "backend");
}

function pythonPath(dir: string): string {
  if (process.env.PF_PYTHON) return process.env.PF_PYTHON;
  const venv =
    process.platform === "win32"
      ? path.join(dir, ".venv", "Scripts", "python.exe")
      : path.join(dir, ".venv", "bin", "python");
  return existsSync(venv) ? venv : process.platform === "win32" ? "python" : "python3";
}

/** Собранное Nuitka ядро внутри установщика: resources/backend/pf-backend.exe. */
function packagedBackend(): string | null {
  if (!app.isPackaged) return null;
  const exe = path.join(process.resourcesPath, "backend", "pf-backend.exe");
  return existsSync(exe) ? exe : null;
}

/** Свободный порт: фиксированный 8765 конфликтует с осиротевшим ядром после падения. */
function freePort(): Promise<number> {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", reject);
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 8765;
      server.close(() => resolve(port));
    });
  });
}

/** Прошлый экземпляр мог оставить python-процесс: гасим его по pid из runtime.json. */
function killStaleBackend(): void {
  try {
    if (!existsSync(runtimePath())) return;
    const stale = JSON.parse(readFileSync(runtimePath(), "utf-8")) as { pid?: number };
    if (stale.pid && stale.pid !== process.pid) {
      if (process.platform === "win32") {
        spawn("taskkill", ["/pid", String(stale.pid), "/T", "/F"], { windowsHide: true });
      } else {
        process.kill(stale.pid, "SIGTERM");
      }
    }
  } catch {
    /* старый runtime.json не критичен */
  } finally {
    try {
      if (existsSync(runtimePath())) unlinkSync(runtimePath());
    } catch {
      /* ignore */
    }
  }
}

function startBackend(port: number): void {
  const exe = packagedBackend();
  const dir = exe ? path.dirname(exe) : backendDir();
  const command = exe ?? pythonPath(dir);
  const args = exe ? [] : ["-m", "app"];
  const browsers = path.join(dir, "browsers");
  const env: NodeJS.ProcessEnv = {
    ...process.env,
    PF_PORT: String(port),
    PYTHONIOENCODING: "utf-8",
  };
  // Браузеры, уложенные рядом с ядром при сборке (иначе — стандартный кэш Playwright).
  if (exe && existsSync(browsers)) env.PLAYWRIGHT_BROWSERS_PATH = browsers;
  const child = spawn(command, args, {
    cwd: dir,
    windowsHide: true,
    env,
    stdio: ["ignore", "pipe", "pipe"],
  });
  backend = child;

  const forward = (chunk: Buffer) => console.log("[backend]", chunk.toString().trimEnd());
  child.stdout?.on("data", forward);
  child.stderr?.on("data", forward);
  child.on("exit", (code) => console.log(`[backend] exited with ${code}`));
}

async function waitForRuntime(port: number, timeoutMs = 30000) {
  const started = Date.now();
  while (Date.now() - started < timeoutMs) {
    try {
      if (existsSync(runtimePath())) {
        const raw = JSON.parse(readFileSync(runtimePath(), "utf-8"));
        if (raw?.token && raw?.port) {
          // Проверяем, что отвечает именно наше ядро, а не чужой процесс.
          const res = await fetch(`http://127.0.0.1:${port}/api/health`, {
            headers: { "x-token": String(raw.token) },
          });
          if (res.ok) return { port, token: String(raw.token) };
        }
      }
    } catch {
      /* ядро ещё поднимается */
    }
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error("Ядро не запустилось: health-check не прошёл");
}

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 920,
    minWidth: 1100,
    minHeight: 700,
    backgroundColor: "#0d1117",
    title: "PulseMarket",
    show: false,
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
    },
  });

  mainWindow.setMenuBarVisibility(false);
  mainWindow.once("ready-to-show", () => mainWindow?.show());
  mainWindow.on("closed", () => {
    mainWindow = null;
  });
  mainWindow.webContents.on("console-message", (_event, level, message) =>
    console.log(`[renderer:${level}] ${message}`),
  );
  // PF_SHOT=path.png — диагностический снимок окна (используется при проверке сборки).
  mainWindow.webContents.on("did-finish-load", () => {
    const target = process.env.PF_SHOT;
    if (!target) return;
    setTimeout(async () => {
      const script = process.env.PF_SHOT_JS;
      if (script) {
        try {
          await mainWindow?.webContents.executeJavaScript(script);
          await new Promise((r) => setTimeout(r, 1200));
        } catch (error) {
          console.log("[shot] js error:", String(error));
        }
      }
      const text = await mainWindow?.webContents.executeJavaScript(
        "document.body.innerText.replace(/\\n+/g, ' | ').slice(0, 1200)",
      );
      console.log("[shot] text:", text);
      const image = await mainWindow?.webContents.capturePage();
      if (image) writeFileSync(target, image.toPNG());
      app.quit();
    }, Number(process.env.PF_SHOT_DELAY ?? 3500));
  });
  void mainWindow.loadFile(path.join(__dirname, "..", "dist", "index.html"));
  if (process.env.PF_DEVTOOLS) mainWindow.webContents.openDevTools({ mode: "detach" });
}

function stopBackend(): void {
  if (!backend || backend.killed || backend.pid === undefined) return;
  if (process.platform === "win32") {
    spawn("taskkill", ["/pid", String(backend.pid), "/T", "/F"], { windowsHide: true });
  } else {
    backend.kill("SIGTERM");
  }
  backend = null;
}

/** Мягкая остановка: сначала просим ядро завершить прогон, потом гасим процесс. */
async function gracefulShutdown(): Promise<void> {
  try {
    if (runtimeInfo) {
      await fetch(`http://127.0.0.1:${runtimeInfo.port}/api/run/stop`, {
        method: "POST",
        headers: { "x-token": runtimeInfo.token },
        signal: AbortSignal.timeout(3000),
      });
    }
  } catch {
    /* ядро уже недоступно */
  }
  stopBackend();
}

async function bootstrap(): Promise<void> {
  killStaleBackend();
  const port = await freePort();
  startBackend(port);
  try {
    runtimeInfo = await waitForRuntime(port);
  } catch (error) {
    console.error(error);
  }

  ipcMain.handle("app:info", () => ({
    apiBase: `http://127.0.0.1:${runtimeInfo?.port ?? port}`,
    token: runtimeInfo?.token ?? "",
    wsUrl: `ws://127.0.0.1:${runtimeInfo?.port ?? port}/ws?token=${runtimeInfo?.token ?? ""}`,
  }));

  ipcMain.handle("dialog:pick-excel", async () => {
    const options: OpenDialogOptions = {
      title: "Выберите файл Excel",
      properties: ["openFile"],
      filters: [
        { name: "Excel", extensions: ["xlsx", "xlsm"] },
        { name: "Все файлы", extensions: ["*"] },
      ],
    };
    const result = mainWindow
      ? await dialog.showOpenDialog(mainWindow, options)
      : await dialog.showOpenDialog(options);
    return result.canceled ? null : (result.filePaths[0] ?? null);
  });

  createWindow();

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
}

if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.focus();
  });

  app.whenReady().then(bootstrap);

  app.on("window-all-closed", () => {
    app.quit();
  });

  app.on("before-quit", (event) => {
    if (quitting) return;
    event.preventDefault();
    quitting = true;
    void gracefulShutdown().finally(() => app.exit(0));
  });
}
