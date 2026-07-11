import { ClipboardCheck, MessageSquareText } from "lucide-react";
import type { WorkspaceId } from "../types";

type WorkspaceNavProps = {
  active: WorkspaceId;
  onSelect: (workspace: WorkspaceId) => void;
};

const ENTRIES: {
  id: WorkspaceId;
  label: string;
  Icon: typeof MessageSquareText;
}[] = [
  { id: "chat", label: "Chat", Icon: MessageSquareText },
  { id: "review", label: "Review Queue", Icon: ClipboardCheck }
];

/** Top-level workspace switcher: local state only, no routing. */
export function WorkspaceNav({ active, onSelect }: WorkspaceNavProps) {
  return (
    <nav
      aria-label="Workspace"
      className="flex shrink-0 items-center gap-0.5 rounded-lg border border-line bg-raised p-0.5"
    >
      {ENTRIES.map(({ id, label, Icon }) => {
        const isActive = active === id;
        return (
          <button
            className={`flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs font-medium focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ink/40 ${
              isActive
                ? "bg-surface text-ink shadow-sm"
                : "text-muted hover:text-ink"
            }`}
            key={id}
            onClick={() => onSelect(id)}
            type="button"
            aria-current={isActive ? "page" : undefined}
          >
            <Icon size={14} />
            {label}
          </button>
        );
      })}
    </nav>
  );
}

export default WorkspaceNav;
