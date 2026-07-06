import { Loader2 } from "lucide-react";

/**
 * Streaming progress line. After the initial "Starting request..." placeholder
 * the stage text comes from the backend's real status events ("Searching local
 * documents...", "Composing answer...", "Searching external context...") —
 * stages are never invented client-side.
 */
export function LoadingIndicator({ status }: { status?: string | null }) {
  return (
    <div className="flex items-center gap-2 text-sm text-muted">
      <Loader2 size={15} className="animate-spin text-grounded" />
      {status ?? "Preparing answer..."}
    </div>
  );
}

export default LoadingIndicator;
