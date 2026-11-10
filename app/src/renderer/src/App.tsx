import { useState } from 'react'
import { BackendStatusPill } from './components/BackendStatusPill'
import { useBackendStatus } from './hooks/useBackend'

type Page = 'meetings' | 'record' | 'ask' | 'settings'

const NAV: { id: Page; label: string; phase: string }[] = [
  { id: 'meetings', label: 'Meetings', phase: 'Phase 1' },
  { id: 'record', label: 'Record', phase: 'Phase 1' },
  { id: 'ask', label: 'Ask', phase: 'Phase 6' },
  { id: 'settings', label: 'Settings', phase: 'Phase 2' }
]

function App(): React.JSX.Element {
  const status = useBackendStatus()
  const [page, setPage] = useState<Page>('meetings')
  const current = NAV.find((n) => n.id === page)!

  return (
    <div className="flex h-screen bg-[#0b0d17] text-slate-100">
      <aside className="flex w-60 shrink-0 flex-col border-r border-white/10 bg-[#0f1220] p-4">
        <div className="mb-8 flex items-center gap-2 px-2">
          <span className="grid h-8 w-8 place-items-center rounded-lg bg-violet-500/20 text-sm font-bold text-violet-300">
            M
          </span>
          <span className="text-lg font-semibold tracking-tight">MinuteMind</span>
        </div>

        <nav className="flex flex-1 flex-col gap-1" aria-label="Main">
          {NAV.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => setPage(item.id)}
              aria-current={page === item.id ? 'page' : undefined}
              className={`rounded-lg px-3 py-2 text-left text-sm transition-colors ${
                page === item.id
                  ? 'bg-violet-500/15 text-violet-200'
                  : 'text-slate-400 hover:bg-white/5 hover:text-slate-200'
              }`}
            >
              {item.label}
            </button>
          ))}
        </nav>

        <BackendStatusPill status={status} />
      </aside>

      <main className="flex flex-1 flex-col items-center justify-center gap-2 p-8 text-center">
        <h1 className="text-2xl font-semibold">{current.label}</h1>
        <p className="text-sm text-slate-400">Coming in {current.phase}.</p>
      </main>
    </div>
  )
}

export default App
