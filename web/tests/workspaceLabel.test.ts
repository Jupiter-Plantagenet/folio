import assert from "node:assert/strict";
import test from "node:test";
import { workspaceLabel } from "../src/workspaceLabel.ts";

test("tenant label uses only the current session workspace identifier", () => {
  assert.equal(
    workspaceLabel("11111111-1111-4111-8111-111111111111"),
    "Workspace 11111111",
  );
  assert.equal(workspaceLabel("team-a"), "Workspace team-a");
  assert.equal(workspaceLabel(undefined), "Workspace");
});
