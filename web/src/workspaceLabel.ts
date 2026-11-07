export function workspaceLabel(workspaceId: string | undefined): string {
  const id = workspaceId?.trim();
  if (!id) return "Workspace";
  return `Workspace ${id.slice(0, 8)}`;
}
