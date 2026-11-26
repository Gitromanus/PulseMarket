/**
 * Запуск Electron с чистым окружением.
 *
 * Если терминал унаследовал ELECTRON_RUN_AS_NODE=1 (так бывает в VS Code),
 * Electron стартует как обычный Node и падает на `app` = undefined.
 * Поэтому переменную убираем перед спавном.
 */
import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const require = createRequire(import.meta.url);
const electronBinary = require("electron");

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;

const child = spawn(electronBinary, ["."], { stdio: "inherit", env });
child.on("exit", (code) => process.exit(code ?? 0));
