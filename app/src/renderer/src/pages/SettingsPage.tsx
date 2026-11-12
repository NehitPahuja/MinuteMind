import { useEffect, useState } from 'react'
import type { HardwareReport, Settings } from '@shared/models'
import { PageHeader, PhaseTag } from '../components/PageHeader'
import { useBackend } from '../lib/contexts'

const LATER = [
  {
    title: 'Speaker labels',
    body: 'Hugging Face token for telling individual voices apart.',
    phase: 4
  },
  {
    title: 'AI model',
    body: 'Built-in local model, or Claude with your own API key.',
    phase: 7
  }
]

export function SettingsPage(): React.JSX.Element {
  const { client } = useBackend()
  const [settings, setSettings] = useState<Settings | null>(null)
  const [hardware, setHardware] = useState<HardwareReport | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Bumped after a change so the hardware readout shows the new profile.
  const [version, setVersion] = useState(0)

  useEffect(() => {
    if (!client) return
    let active = true
    Promise.all([
      client.get<Settings>('/settings'),
      client.get<HardwareReport>('/system/hardware')
    ]).then(
      ([s, h]) => {
        if (!active) return
        setSettings(s)
        setHardware(h)
      },
      (err: Error) => {
        if (active) setError(err.message)
      }
    )
    return () => {
      active = false
    }
  }, [client, version])

  const change = (patch: Partial<Settings>): void => {
    if (!client) return
    setSettings((s) => (s ? { ...s, ...patch } : s))
    client.patch<Settings>('/settings', patch).then(
      (s) => {
        setSettings(s)
        setVersion((v) => v + 1)
      },
      (err: Error) => setError(err.message)
    )
  }

  return (
    <>
      <PageHeader
        eyebrow="Preferences"
        title="Settings"
        lede="Everything stays on this PC unless you switch the AI model to Claude."
      />

      {error && (
        <p className="mb-6 text-sm text-signal" role="alert">
          {error}
        </p>
      )}

      <dl className="divide-y divide-line border-y border-line">
        <Row title="Audio devices">
          <p className="text-sm text-paper-500">
            Pick the microphone and PC sound output on the Record page before each recording.
          </p>
        </Row>

        <Row title="This computer">
          {hardware ? (
            <dl className="grid grid-cols-[110px_1fr] gap-x-4 gap-y-1.5 text-sm">
              <dt className="text-paper-500">Graphics</dt>
              <dd className="text-paper-100">
                {hardware.info.gpu_name ?? 'No NVIDIA GPU found'}
                {hardware.info.vram_total_mb
                  ? ` · ${(hardware.info.vram_total_mb / 1024).toFixed(0)} GB video memory`
                  : ''}
              </dd>
              <dt className="text-paper-500">Memory</dt>
              <dd className="text-paper-100">
                {(hardware.info.ram_total_mb / 1024).toFixed(0)} GB RAM ·{' '}
                {hardware.info.cpu_threads} processor threads
              </dd>
              <dt className="text-paper-500">Running on</dt>
              <dd className="text-paper-100">{hardware.profile.label}</dd>
            </dl>
          ) : (
            <p className="text-sm text-paper-500">Checking hardware…</p>
          )}
        </Row>

        <Row title="Processing">
          <div className="space-y-2">
            <select
              value={settings?.processing ?? 'auto'}
              disabled={!settings}
              onChange={(e) => change({ processing: e.target.value as Settings['processing'] })}
              aria-label="Processing"
              className="w-64 rounded-md border border-line-strong bg-ink-850 px-2.5 py-2 text-sm text-paper-100 focus:border-paper-500 focus:outline-none"
            >
              <option value="auto">Automatic (recommended)</option>
              <option value="gpu">Always use the GPU</option>
              <option value="cpu">Processor only</option>
            </select>
            <p className="text-xs leading-relaxed text-paper-500">
              The GPU is much faster. Processor only is slower but leaves the GPU free for games or
              other apps. If the GPU runs out of memory, MinuteMind switches to the processor on its
              own.
            </p>
          </div>
        </Row>

        <Row title="Transcription">
          <div className="space-y-3">
            <label className="flex cursor-pointer items-center gap-3 text-sm text-paper-100">
              <input
                type="checkbox"
                checked={settings?.auto_transcribe ?? true}
                disabled={!settings}
                onChange={(e) => change({ auto_transcribe: e.target.checked })}
                className="h-4 w-4 accent-paper-100"
              />
              Transcribe meetings automatically when a recording stops
            </label>
            <label className="flex cursor-pointer items-center gap-3 text-sm text-paper-100">
              <input
                type="checkbox"
                checked={settings?.live_transcript ?? true}
                disabled={!settings}
                onChange={(e) => change({ live_transcript: e.target.checked })}
                className="h-4 w-4 accent-paper-100"
              />
              Show a live transcript while recording
            </label>
            <p className="text-xs leading-relaxed text-paper-500">
              Speech model: {hardware?.model.name ?? 'large-v3-turbo'}
              {hardware
                ? hardware.model.downloaded
                  ? ', downloaded.'
                  : ', downloads once (about 1.5 GB) the first time you transcribe.'
                : '.'}
            </p>
          </div>
        </Row>

        {LATER.map((s) => (
          <Row key={s.title} title={s.title} aside={<PhaseTag phase={s.phase} />}>
            <p className="text-sm text-paper-500">{s.body}</p>
          </Row>
        ))}
      </dl>
    </>
  )
}

function Row({
  title,
  aside,
  children
}: {
  title: string
  aside?: React.ReactNode
  children: React.ReactNode
}): React.JSX.Element {
  return (
    <div className="grid grid-cols-[180px_1fr_auto] items-start gap-6 py-5">
      <dt className="pt-0.5 text-sm font-medium text-paper-100">{title}</dt>
      <dd>{children}</dd>
      <dd>{aside}</dd>
    </div>
  )
}
