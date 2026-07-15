import assert from "node:assert/strict";
import test from "node:test";

import { useProjects } from "./useProjects.js";

test("loadProjects clears a selection that no longer exists", async () => {
  const state = useProjects(async () => [{ id: 2, name: "remaining" }]);
  state.selectedProjectId.value = 1;
  await state.loadProjects();
  assert.equal(state.selectedProjectId.value, null);
  assert.equal(state.projects.value.length, 1);
});

test("persistProject selects POST or PATCH from form mode", async () => {
  const calls = [];
  const state = useProjects(async (path, options) => { calls.push([path, options]); return { id: 4 }; });
  await state.persistProject({ name: "new" }, "create");
  state.selectedProjectId.value = 4;
  await state.persistProject({ name: "changed" }, "edit");
  assert.equal(calls[0][0], "/api/projects");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[1][0], "/api/projects/4");
  assert.equal(calls[1][1].method, "PATCH");
});
