// Point the docstale pi package at this checkout's pinned command. The package
// reads DOCSTALE_EXECUTABLE when it runs the hook, after every extension loads.
// See docs/DEVELOPMENT.md#documentation-reviews.
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(dirname(fileURLToPath(import.meta.url))));

if (!process.env.DOCSTALE_EXECUTABLE) {
  process.env.DOCSTALE_EXECUTABLE = join(
    root,
    ".pixi",
    "envs",
    "default",
    "bin",
    process.platform === "win32" ? "docstale.exe" : "docstale",
  );
}

export default function docstaleExecutable(): void {}
