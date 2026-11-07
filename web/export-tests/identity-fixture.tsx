// Test-only real React lifecycle fixture. Never part of main.tsx or its routes.
import React from "react";
import { createRoot } from "react-dom/client";
import { flushSync } from "react-dom";
import { EvidenceExport } from "../src/EvidenceExport";
const root = createRoot(document.getElementById("fixture")!);
let identity = { username: "ui-authored-reviewer", workspace: "00000000-0000-4000-8000-000000000001", caseId: "00000000-0000-4000-8000-000000000002" };
Object.assign(window, {
  mountExport(change = {}) {
    identity = { ...identity, ...change };
    flushSync(() => root.render(<EvidenceExport key={JSON.stringify(identity)} {...identity} version={9} roles={["reviewer"]} csrf="authored-csrf" />));
  },
  unmountExport() { flushSync(() => root.render(null)); },
});
(window as any).mountExport();
