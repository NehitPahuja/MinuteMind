import type { BackendStatus } from '@shared/backend'
import { BackendStatusPill } from './BackendStatusPill'

export type Page = 'meetings' | 'record' | 'ask' | 'settings'

const NAV: { id: Exclude<Page, 'record'>; label: string }[] = [
  { id: 'meetings', label: 'Meetings' },
  { id: 'ask', label: 'Ask' },
  { id: 'settings', label: 'Settings' }
]

interface SidebarProps {
  page: Page
  onNavigate: (page: Page) => void
  status: BackendStatus
}

export function Sidebar({ page, onNavigate, status }: SidebarProps): React.JSX.Element {
  return (
    <aside className="flex w-64 shrink-0 flex-col border-r border-line bg-ink-900 px-4 pb-4 pt-7">
      <div className="mb-9 px-2">
        <p className="display text-[26px] leading-none text-paper-50">
          Minute<span className="marker italic">Mind</span>
        </p>
        <p className="eyebrow mt-2">Meeting notebook</p>
      </div>

      <button
        type="button"
        onClick={() => onNavigate('record')}
        aria-current={page === 'record' ? 'page' : undefined}
        className={`group mb-8 flex items-center gap-3 rounded-md border px-3 py-2.5 text-left transition-colors ${
          page === 'record'
            ? 'border-signal/50 bg-signal-soft'
            : 'border-line bg-ink-850 hover:border-line-strong'
        }`}
      >
        <span className="grid h-6 w-6 place-items-center rounded-full border border-signal/60">
          <span className="h-2.5 w-2.5 rounded-full bg-signal transition-transform group-hover:scale-110" />
        </span>
        <span className="text-sm font-medium text-paper-50">New recording</span>
      </button>

      <nav className="flex flex-1 flex-col gap-0.5" aria-label="Main">
        {NAV.map((item, i) => {
          const active = page === item.id
          return (
            <button
              key={item.id}
              type="button"
              onClick={() => onNavigate(item.id)}
              aria-current={active ? 'page' : undefined}
              className={`relative flex items-baseline gap-3 rounded-md px-2 py-2 text-left text-sm transition-colors ${
                active ? 'text-paper-50' : 'text-paper-500 hover:text-paper-100'
              }`}
            >
              {active && (
                <span
                  className="absolute -left-4 top-1/2 h-4 w-0.5 -translate-y-1/2 bg-marker"
                  aria-hidden="true"
                />
              )}
              <span className="font-mono text-[11px] text-paper-700">
                {String(i + 1).padStart(2, '0')}
              </span>
              {item.label}
            </button>
          )
        })}
      </nav>

      <div className="border-t border-line pt-4">
        <BackendStatusPill status={status} />
      </div>
    </aside>
  )
}
