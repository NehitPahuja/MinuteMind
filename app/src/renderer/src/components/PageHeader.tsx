interface PageHeaderProps {
  eyebrow: string
  title: React.ReactNode
  lede?: React.ReactNode
  aside?: React.ReactNode
}

export function PageHeader({ eyebrow, title, lede, aside }: PageHeaderProps): React.JSX.Element {
  return (
    <header className="mb-10 flex flex-wrap items-end justify-between gap-4 border-b border-line pb-6">
      <div className="max-w-2xl">
        <p className="eyebrow mb-3">{eyebrow}</p>
        <h1 className="display text-4xl leading-tight text-paper-50">{title}</h1>
        {lede && <p className="mt-3 text-[15px] leading-relaxed text-paper-300">{lede}</p>}
      </div>
      {aside}
    </header>
  )
}

/** Small mono tag marking which build phase delivers a feature. */
export function PhaseTag({ phase }: { phase: number }): React.JSX.Element {
  return (
    <span className="rounded-sm border border-line px-1.5 py-0.5 font-mono text-[10px] uppercase tracking-wider text-paper-500">
      Phase {phase}
    </span>
  )
}
