import type { BackendStatus } from '@shared/backend'

const STATES = {
  starting: { dot: 'bg-paper-500', label: 'Engine starting' },
  ready: { dot: 'bg-ok', label: 'Engine ready' },
  error: { dot: 'bg-signal', label: 'Engine stopped' }
} as const

export function BackendStatusPill({ status }: { status: BackendStatus }): React.JSX.Element {
  const state = STATES[status.state]
  return (
    <div className="space-y-2">
      <div className="px-2" role="status">
        <div className="flex items-center gap-2.5">
          <span className={`h-1.5 w-1.5 shrink-0 rounded-full ${state.dot}`} aria-hidden="true" />
          <span className="whitespace-nowrap font-mono text-[11px] tracking-wide text-paper-300">
            {state.label}
          </span>
        </div>
        {status.state === 'ready' && (
          <p className="mt-1 whitespace-nowrap pl-4 font-mono text-[10px] text-paper-700">
            v{status.version} · port {status.port}
          </p>
        )}
      </div>

      {status.state === 'error' && (
        <div className="space-y-2 rounded-md border border-line-strong bg-ink-850 p-3 text-xs text-paper-100">
          <p className="break-words leading-relaxed">{status.message}</p>
          <button
            type="button"
            onClick={() => void window.api.backend.restart()}
            className="font-mono text-[11px] uppercase tracking-wider text-paper-300 hover:text-paper-50"
          >
            Restart engine →
          </button>
        </div>
      )}
    </div>
  )
}
