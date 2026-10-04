import assert from "node:assert/strict";
import { mkdtempSync, mkdirSync, readFileSync, rmSync, symlinkSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { test } from "node:test";
import { mockupPreviewPlugin } from "./mockupPreviewPlugin.ts";

function fixture(t) {
  const root = mkdtempSync(path.join(tmpdir(), "mockup-discovery-"));
  t.after(() => rmSync(root, { recursive: true, force: true }));
  const put = (name) => {
    const file = path.join(root, "src/components/mockups", name);
    mkdirSync(path.dirname(file), { recursive: true });
    writeFileSync(file, "export default function Fixture() { return null; }\n");
    return file;
  };
  const plugin = mockupPreviewPlugin();
  plugin.configResolved({ root });
  return {
    root, put, plugin,
    source: () => readFileSync(path.join(root, "src/.generated/mockup-components.ts"), "utf8"),
  };
}

test("discovers nested TSX files and excludes helper files, helper folders, and directories", async (t) => {
  const f = fixture(t);
  f.put("Zebra.tsx");
  f.put("nested/Alpha.tsx");
  f.put("_Helper.tsx");
  f.put("_helpers/Hidden.tsx");
  f.put("nested/_helpers/Hidden.tsx");
  f.put("nested/_Private.tsx");
  f.put("Other.jsx");
  f.put(".Hidden.tsx");
  f.put(".hidden/Secret.tsx");
  mkdirSync(path.join(f.root, "src/components/mockups/Directory.tsx"));
  await f.plugin.buildStart();
  const source = f.source();
  assert.match(source, /Zebra\.tsx/);
  assert.match(source, /nested\/Alpha\.tsx/);
  assert.doesNotMatch(source, /Helper|Hidden|Private|Other|Directory|Secret/);
  assert.match(source, /import\("\.\.\/components\/mockups\/nested\/Alpha\.tsx"\)/);
});

test("continues to discover symlinked files and folders while ignoring broken links", async (t) => {
  const f = fixture(t);
  const original = f.put("Original.tsx");
  f.put("nested/Nested.tsx");
  const mockups = path.dirname(original);
  symlinkSync(original, path.join(mockups, "Linked.tsx"));
  symlinkSync(path.join(mockups, "nested"), path.join(mockups, "linked-directory"));
  symlinkSync(path.join(mockups, "missing.tsx"), path.join(mockups, "Broken.tsx"));
  symlinkSync(mockups, path.join(mockups, "cycle"));
  await f.plugin.buildStart();
  assert.match(f.source(), /Linked\.tsx/);
  assert.match(f.source(), /linked-directory\/Nested\.tsx/);
  assert.doesNotMatch(f.source(), /Broken\.tsx/);
  assert.doesNotMatch(f.source(), /cycle\//);
});

test("rescans additions and removals and keeps generated output deterministic", async (t) => {
  const f = fixture(t);
  const removed = f.put("First.tsx");
  await f.plugin.buildStart();
  const original = f.source();
  await f.plugin.buildStart();
  assert.equal(f.source(), original);
  rmSync(removed);
  f.put("Added.tsx");
  await f.plugin.buildStart();
  assert.match(f.source(), /Added\.tsx/);
  assert.doesNotMatch(f.source(), /First\.tsx/);
});