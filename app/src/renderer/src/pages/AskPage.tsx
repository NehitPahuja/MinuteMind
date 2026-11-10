import { PageHeader, PhaseTag } from '../components/PageHeader'

const EXAMPLES = [
  'What did we decide about the launch date?',
  'Who owns the budget follow-up?',
  'What number did Priya quote for hosting?'
]

export function AskPage(): React.JSX.Element {
  return (
    <>
      <PageHeader
        eyebrow="Recall"
        title={
          <>
            Ask your <span className="italic">meetings</span>
          </>
        }
        lede="Answers come straight from what was said, with a timestamp you can click to hear it."
      />

      <div className="flex items-center gap-3 rounded-lg border border-line bg-ink-900 px-4 py-3.5 opacity-80">
        <span className="font-mono text-paper-700" aria-hidden="true">
          ?
        </span>
        <input
          disabled
          placeholder="Ask anything about your recordings…"
          className="flex-1 bg-transparent text-[15px] text-paper-100 placeholder:text-paper-500 focus:outline-none"
        />
        <PhaseTag phase={6} />
      </div>

      <p className="eyebrow mb-3 mt-10">For example</p>
      <ul className="space-y-1">
        {EXAMPLES.map((q) => (
          <li key={q} className="display border-b border-line py-3 text-xl italic text-paper-300">
            {q}
          </li>
        ))}
      </ul>
    </>
  )
}
