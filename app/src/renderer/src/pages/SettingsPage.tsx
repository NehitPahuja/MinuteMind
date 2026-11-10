import { PageHeader, PhaseTag } from '../components/PageHeader'

const SECTIONS = [
  {
    title: 'Audio devices',
    body: 'Choose which microphone and speaker output to record.',
    phase: 1
  },
  {
    title: 'Transcription',
    body: 'Hardware profile, Whisper models and GPU or CPU processing.',
    phase: 2
  },
  {
    title: 'Speaker labels',
    body: 'Hugging Face token for telling voices apart.',
    phase: 4
  },
  {
    title: 'AI model',
    body: 'Built-in local model, or Claude with your own API key.',
    phase: 7
  }
]

export function SettingsPage(): React.JSX.Element {
  return (
    <>
      <PageHeader
        eyebrow="Preferences"
        title="Settings"
        lede="Everything stays on this PC unless you switch the AI model to Claude."
      />
      <dl className="divide-y divide-line border-y border-line">
        {SECTIONS.map((s) => (
          <div key={s.title} className="grid grid-cols-[220px_1fr_auto] items-center gap-6 py-5">
            <dt className="text-sm font-medium text-paper-100">{s.title}</dt>
            <dd className="text-sm text-paper-500">{s.body}</dd>
            <dd>
              <PhaseTag phase={s.phase} />
            </dd>
          </div>
        ))}
      </dl>
    </>
  )
}
