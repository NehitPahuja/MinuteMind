import { PageHeader, PhaseTag } from '../components/PageHeader'

export function RecordPage(): React.JSX.Element {
  return (
    <>
      <PageHeader
        eyebrow="Capture"
        title="New recording"
        lede="Records your microphone and your PC's sound as separate tracks, so you and the other side of the call are always told apart."
      />

      <section className="grid gap-6 lg:grid-cols-[1fr_280px]">
        <div className="flex flex-col items-center justify-center rounded-lg border border-line bg-ink-900 px-8 py-16">
          <button
            type="button"
            disabled
            aria-label="Start recording (available in Phase 1)"
            className="grid h-24 w-24 place-items-center rounded-full border-2 border-signal/50 bg-ink-850 opacity-70"
          >
            <span className="h-9 w-9 rounded-full bg-signal" />
          </button>
          <p className="mt-6 font-mono text-3xl tabular-nums text-paper-300">00:00:00</p>
          <div className="mt-4 flex items-center gap-2">
            <span className="text-sm text-paper-500">Recording controls arrive in</span>
            <PhaseTag phase={1} />
          </div>
        </div>

        <aside className="space-y-4">
          {['Microphone', 'PC sound'].map((label) => (
            <div key={label} className="rounded-lg border border-line bg-ink-900 p-4">
              <p className="eyebrow mb-3">{label}</p>
              <div className="flex h-8 items-end gap-[3px]" aria-hidden="true">
                {Array.from({ length: 24 }, (_, i) => (
                  <span
                    key={i}
                    className="w-full rounded-[1px] bg-line"
                    style={{ height: '20%' }}
                  />
                ))}
              </div>
            </div>
          ))}
          <p className="px-1 text-xs leading-relaxed text-paper-500">
            Let everyone know you&apos;re recording. In many places it&apos;s required by law.
          </p>
        </aside>
      </section>
    </>
  )
}
