import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const source = readFileSync(resolve("functions/data/[[path]].js"), "utf8");
for (const required of [
  'key.startsWith("control/")',
  'key.startsWith("recovery/")',
  'return new Response("Not found", { status: 404 })',
]) {
  if (!source.includes(required)) {
    throw new Error(`Data route guard missing: ${required}`);
  }
}
console.log("Data route guard passed: control and recovery prefixes are private.");
