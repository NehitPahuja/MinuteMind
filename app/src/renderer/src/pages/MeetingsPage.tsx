import { PageHeader, PhaseTag } from '../components/PageHeader'

const today = new Intl.DateTimeFormat('en-GB', {
  weekday: 'short',
  day: '2-digit',
  month: 'short',
  year: 'numeric'
}).format(new Date())

export function MeetingsPage({ onRecord }: { onRecord: () => void }): React.JSX.Element {
  return (
    <>
      <PageHeader
        eyebrow="Library"
        title="Your meetings"
        lede="Every recording lands here with its transcript, your notes and the minutes."
        aside={<p className="font-mono text-xs uppercase tracking-wider text-paper-500">{today}</p>}
      />

      <section className="rounded-lg border border-dashed border-line-strong bg-ink-900/70 px-8 py-14 text-center">
        <p className="display text-3xl italic text-paper-100">Nothing recorded yet.</p>
        <p className="mx-auto mt-3 max-w-md text-sm leading-relaxed text-paper-500">
          Start a recording before your next call. MinuteMind captures your microphone and the other
          side of the call, all on this PC.
        </p>
        <div className="mt-7 flex items-center justify-center gap-3">
          <button
            type="button"
            onClick={onRecord}
            className="inline-flex items-center gap-2 rounded-md bg-paper-100 px-4 py-2 text-sm font-medium text-ink-950 transition-colors hover:bg-paper-50"
          >
            <span className="h-2 w-2 rounded-full bg-signal" aria-hidden="true" />
            New recording
          </button>
          <PhaseTag phase={1} />
        </div>
      </section>
    </>
  )
}
