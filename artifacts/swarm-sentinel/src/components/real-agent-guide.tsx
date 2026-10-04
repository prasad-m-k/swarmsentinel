import { useRef, useState } from 'react';
import { Terminal, Copy, Check } from 'lucide-react';

type Mode = 'normal' | 'adversarial';
const mono = { fontFamily: 'var(--app-font-mono)' } as const;
const LIVE = '#b48cff';

interface Props {
  sessionCount: number;
  labeledCount: number;
  canWatch: boolean;
  onWatch: () => void;
}

export default function RealAgentGuide({ sessionCount, labeledCount, canWatch, onWatch }: Props) {
  const [mode, setMode] = useState<Mode>('normal');
  const [fb, setFb] = useState<{ ok: boolean; msg: string } | null>(null);
  const timer = useRef<number | undefined>(undefined);
  const cmd = `python artifacts/swarm-sentinel/python/examples/real_agents_demo.py --mode ${mode} --token-file /path/to/private/clerk-token.jwt`;

  const say = (ok: boolean, msg: string) => {
    setFb({ ok, msg });
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => setFb(null), 3500);
  };
  const copy = async () => {
    try {
      if (!navigator.clipboard) throw new Error('unavailable');
      await navigator.clipboard.writeText(cmd);
      say(true, 'Command copied. Paste it in a terminal at the repo root.');
    } catch {
      say(false, 'Copy failed. Select the command text and copy it manually.');
    }
  };

  const modes: [Mode, string][] = [['normal', 'Normal'], ['adversarial', 'Adversarial']];

  return (
    <details className="border-b border-border bg-card/70 text-xs" data-testid="panel-real-agents">
      <summary className="px-4 py-2 cursor-pointer flex flex-wrap items-center gap-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-[-2px]" style={{ outlineColor: LIVE }}>
        <Terminal size={14} color={LIVE} aria-hidden="true" />
        <span className="font-medium">Real agents showcase</span>
        <span className="text-muted-foreground">three LLM-backed agents, run from a terminal</span>
        <span className="ml-auto text-[10px] uppercase tracking-wider text-muted-foreground" style={mono}>
          {sessionCount} sessions / {labeledCount} labeled
        </span>
      </summary>
      <div className="px-4 pb-3 grid gap-3 lg:grid-cols-[1.2fr_1fr]">
        <div className="grid gap-2">
          <ol className="grid gap-1 list-decimal pl-4 text-muted-foreground">
            <li>Pick a mode and copy the command. Run <b className="text-foreground">normal</b> first, then <b className="text-foreground">adversarial</b>.</li>
            <li>Run it in a terminal from the repo root. Agents use real sandbox file tools; nothing starts from this page.</li>
            <li>Replace the token-file path with a private file containing a current Clerk session JWT for your signed-in account. Keep it outside the repository, restrict it to your user (chmod 600), and refresh it before expiry. Never paste credentials into the dashboard or command arguments.</li>
            <li>Choose the labeled live session in the selector above, then press <b className="text-foreground">Watch</b>.</li>
          </ol>
          <div role="radiogroup" aria-label="Real agent mode" className="inline-flex border border-border w-fit">
            {modes.map(([m, l]) => (
              <button key={m} type="button" role="radio" aria-checked={mode === m} data-testid={`radio-real-mode-${m}`}
                onClick={() => setMode(m)} className="px-3 h-7 transition-colors"
                style={{ background: mode === m ? LIVE + '26' : 'transparent', color: mode === m ? LIVE : undefined }}>
                {l}
              </button>
            ))}
          </div>
          <div className="flex items-stretch gap-1.5">
            <code className="flex-1 min-w-0 overflow-x-auto whitespace-nowrap bg-secondary border border-border px-2 py-1.5" style={mono} data-testid="text-real-command" tabIndex={0}>{cmd}</code>
            <button type="button" onClick={copy} data-testid="button-copy-command"
              className="inline-flex items-center gap-1.5 px-3 text-xs border border-border bg-secondary hover:bg-accent">
              {fb?.ok ? <Check size={13} /> : <Copy size={13} />} Copy
            </button>
          </div>
          <div role="status" aria-live="polite" className="min-h-4" data-testid="status-copy"
            style={{ color: fb ? (fb.ok ? '#3ddc97' : '#ff5d5d') : undefined }}>{fb?.msg}</div>
          <div>
            <button type="button" onClick={onWatch} disabled={!canWatch} data-testid="button-watch-real"
              className="inline-flex items-center px-3 h-7 border border-border bg-secondary hover:bg-accent disabled:opacity-40 disabled:cursor-not-allowed">
              Watch selected session
            </button>
            {labeledCount === 0 && <span className="ml-2 text-muted-foreground">No labeled session yet. It appears here within a few seconds of starting the command.</span>}
          </div>
        </div>
        <ul className="grid gap-1.5 text-muted-foreground content-start">
          <li><b className="text-foreground">Default model:</b> <span style={mono}>gpt-5.4-mini</span>; add <span style={mono}>--model NAME</span> to change it (optional).</li>
          <li><b className="text-foreground">Credits:</b> the Replit OpenAI integration uses your credits.</li>
          <li><b className="text-foreground">Sandbox tools:</b> the server evaluates and executes all four tools on disposable fictional fixtures. The runner gets results, not fixture paths. No real money or external systems; this is not OS isolation.</li>
          <li><b className="text-foreground">Adversarial mode:</b> bad behavior is explicitly instructed to stress-test scope and taint refusals. It is not proof a model fell for injection on its own.</li>
          <li><b className="text-foreground">Public synthetic demos</b> use scripted fixtures, not an LLM-driven run, and are separate from these owner-private sessions.</li>
        </ul>
      </div>
    </details>
  );
}
