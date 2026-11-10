import type { BackendStatus } from '@shared/backend'

const STYLES = {
  starting: { dot: 'bg-amber-400 animate-pulse', label: 'Starting backend…' },
  ready: { dot: 'bg-emerald-400', label: 'Backend connected' },
  error: { dot: 'bg-rose-500', label: 'Backend error' }
} as const

export function BackendStatusPill({ status }: { status: BackendStatus }): React.JSX.Element {
  const style = STYLES[status.state]
  return (
    <div className="space-y-2">
      <div
        className="flex items-center gap-2 rounded-full border border-white/10 bg-white/5 px-3 py-1.5 text-xs text-slate-300"
        role="status"
        title={status.state === 'ready' ? `v${status.version} on port ${status.port}` : undefined}
      >
        <span className={`h-2 w-2 shrink-0 rounded-full ${style.dot}`} aria-hidden="true" />
        {style.label}
      </div>

      {status.state === 'error' && (
        <div className="space-y-2 rounded-lg border border-rose-500/30 bg-rose-500/10 p-2 text-xs text-rose-200">
          <p className="break-words">{status.message}</p>
          <button
            type="button"
            onClick={() => void window.api.backend.restart()}
            className="rounded-md bg-rose-500/20 px-2 py-1 font-medium hover:bg-rose-500/30"
          >
            Retry
          </button>
        </div>
      )}
    </div>
  )
}
