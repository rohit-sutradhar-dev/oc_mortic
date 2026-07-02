import { copyFile, mkdir, readdir, rm } from "node:fs/promises";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const rootDir = join(dirname(fileURLToPath(import.meta.url)), "..");
const srcDir = join(rootDir, "src");
const distDir = join(rootDir, "dist");
const sourceFiles = (await readdir(srcDir)).filter((file) => file.endsWith(".js")).sort();

await rm(distDir, { recursive: true, force: true });
await mkdir(distDir, { recursive: true });

for (const file of sourceFiles) {
  await copyFile(join(srcDir, file), join(distDir, file));
}
