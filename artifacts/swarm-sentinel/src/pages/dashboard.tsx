import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useSimulateSwarm } from '@workspace/api-client-react';
import type { RunInput, SwarmRun, Trace } from '@workspace/api-client-react';
import {
  Play, Pause, StepForward, RotateCcw, Download, ShieldAlert, ExternalLink,
  Plus, Minus, Maximize2, FileJson, FlaskConical, Radar,
} from 'lucide-react';

type Scenario = 'normal' | 'attack';
type Tab = 'feed' | 'policy' | 'settings' | 'info';

const C = {
  ok: '#3ddc97', bad: '#ff5d5d', warn: '#f5b83d', info: '#5cc8ff', dim: '#4b5a6e', tool: '#8d9bb0',
};
const mono = { fontFamily: 'var(--app-font-mono)' } as const;

function decColor(d: string) {
  return d === 'drop' ? C.bad : d === 'throttle' ? C.warn : d === 'observed' ? C.info : C.ok;
}
function tm(ts: string) {
  const m = /T(\d\d:\d\d:\d\d)/.exec(ts);
  return m ? m[1] : ts;
}

function Badge({ color, children }: { color: string; children: React.ReactNode }) {
  return (
    <span className="inline-flex items-center px-1.5 py-0.5 text-[10px] uppercase tracking-wider border"
      style={{ ...mono, color, borderColor: color + '66', background: color + '14' }}>
      {children}
    </span>
  );
}

function Btn({ children, onClick, disabled, primary, testid, title }: {
  children: React.ReactNode; onClick?: () => void; disabled?: boolean; primary?: boolean; testid: string; title?: string;
}) {
  return (
    <button data-testid={testid} title={title} onClick={onClick} disabled={disabled}
      className={`inline-flex items-center gap-1.5 px-3 h-8 text-xs font-medium border transition-colors disabled:opacity-40 disabled:cursor-not-allowed ${
        primary ? 'bg-primary text-primary-foreground border-primary hover:brightness-110'
          : 'bg-secondary text-foreground border-border hover:bg-accent'}`}>
      {children}
    </button>
  );
}

function download(name: string, text: string, type: string) {
  const url = URL.createObjectURL(new Blob([text], { type }));
  const a = document.createElement('a');
  a.href = url; a.download = name; document.body.appendChild(a); a.click(); a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 500);
}

interface GNode { id: string; kind: 'agent' | 'tool'; x: number; y: number }

function buildLayout(events: Trace[]) {
  const agents = new Map<string, number>();
  const touch = (id: string, d: number) => {
    if (!id) return;
    agents.set(id, Math.max(agents.get(id) ?? 0, d));
  };
  events.forEach((e) => {
    touch(e.agentId, e.depth);
    if (e.parentId) touch(e.parentId, Math.max(0, e.depth - 1));
    if (e.action === 'spawn' && e.target) touch(e.target, e.depth + 1);
  });
  const tools = new Set<string>();
  events.forEach((e) => { if (e.target && !agents.has(e.target)) tools.add(e.target); });
  const maxD = Math.max(1, ...agents.values());
  const step = Math.min(85, 170 / maxD);
  const byD = new Map<number, string[]>();
  agents.forEach((d, id) => byD.set(d, [...(byD.get(d) ?? []), id]));
  const nodes = new Map<string, GNode>();
  byD.forEach((ids, d) => {
    ids.forEach((id, i) => {
      if (d === 0 && ids.length === 1) { nodes.set(id, { id, kind: 'agent', x: 400, y: 300 }); return; }
      const r = 60 + d * step;
      const a = (i / ids.length) * Math.PI * 2 + d * 0.7 - Math.PI / 2;
      nodes.set(id, { id, kind: 'agent', x: 400 + Math.cos(a) * r, y: 300 + Math.sin(a) * r });
    });
  });
  const tl = [...tools];
  const R = 60 + (maxD + 1) * step + 20;
  tl.forEach((id, i) => {
    const a = (i / Math.max(1, tl.length)) * Math.PI * 2 + 0.3;
    nodes.set(id, { id, kind: 'tool', x: 400 + Math.cos(a) * Math.min(R, 270), y: 300 + Math.sin(a) * Math.min(R, 250) });
  });
  return nodes;
}

export default function Dashboard() {
  const sim = useSimulateSwarm();
  const mutateRef = useRef(sim.mutate);
  mutateRef.current = sim.mutate;

  const [run, setRun] = useState<SwarmRun | null>(null);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [scenario, setScenario] = useState<Scenario>('attack');
  const [feedback, setFeedback] = useState(true);
  const [maxDepth, setMaxDepth] = useState(4);
  const [semanticLimit, setSemanticLimit] = useState(5);
  const [writeLimit, setWriteLimit] = useState(6);
  const [tab, setTab] = useState<Tab>('feed');
  const [fDec, setFDec] = useState('all');
  const [fAct, setFAct] = useState('all');
  const [fCh, setFCh] = useState('all');
  const [fAgent, setFAgent] = useState<string | null>(null);
  const [selEvent, setSelEvent] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const started = useRef(false);

  const input = useCallback((sc: Scenario): RunInput => ({
    scenario: sc, feedbackEnabled: feedback, maxDepth, semanticLimit, writeLimit,
  }), [feedback, maxDepth, semanticLimit, writeLimit]);

  // first mount: normal sample, fully displayed
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    mutateRef.current({ data: { scenario: 'normal', feedbackEnabled: true, maxDepth: 4, semanticLimit: 5, writeLimit: 6 } }, {
      onSuccess: (r) => { setRun(r); setCursor(r.events.length); setScenario('normal'); },
    });
  }, []);

  const total = run?.events.length ?? 0;

  useEffect(() => {
    if (!playing) return;
    const t = setInterval(() => {
      setCursor((c) => {
        if (c >= total) { setPlaying(false); return c; }
        return c + 1;
      });
    }, 450);
    return () => clearInterval(t);
  }, [playing, total]);

  const runDemo = () => {
    setPlaying(false);
    setSelEvent(null); setFAgent(null);
    sim.mutate({ data: input(scenario) }, {
      onSuccess: (r) => { setRun(r); setCursor(0); setPlaying(true); setView({ x: 0, y: 0, k: 1 }); },
    });
  };

  const events = run?.events ?? [];
  const visible = useMemo(() => events.slice(0, cursor), [events, cursor]);
  const visIds = useMemo(() => new Set(visible.map((e) => e.id)), [visible]);
  const layout = useMemo(() => buildLayout(events), [events]);
  const lastT = visible.length ? new Date(visible[visible.length - 1].timestamp).getTime() : -Infinity;
  const alerts = useMemo(() => (run?.alerts ?? []).filter((a) => visIds.has(a.eventId)), [run, visIds]);
  const policies = useMemo(() => (run?.policies ?? []).filter((p) => new Date(p.timestamp).getTime() <= lastT), [run, lastT]);
  const detected = useMemo(() => {
    const s = new Set<string>();
    alerts.forEach((a) => a.agents.forEach((x) => s.add(x)));
    policies.forEach((p) => p.blockedAgents.forEach((x) => s.add(x)));
    return s;
  }, [alerts, policies]);

  const graph = useMemo(() => {
    const nodeIds = new Set<string>();
    const edges = new Map<string, { from: string; to: string; kind: 'lineage' | 'comm' | 'blocked'; n: number }>();
    const add = (from: string, to: string, kind: 'lineage' | 'comm' | 'blocked') => {
      if (!from || !to || from === to || !layout.has(from) || !layout.has(to)) return;
      nodeIds.add(from); nodeIds.add(to);
      const k = `${from}|${to}|${kind}`;
      const e = edges.get(k);
      if (e) e.n++; else edges.set(k, { from, to, kind, n: 1 });
    };
    visible.forEach((e) => {
      if (e.parentId) add(e.parentId, e.agentId, 'lineage');
      nodeIds.add(e.agentId);
      add(e.agentId, e.target, !e.executed ? 'blocked' : 'comm');
    });
    return { nodeIds, edges: [...edges.values()] };
  }, [visible, layout]);

  const counts = useMemo(() => {
    const c = { allow: 0, throttle: 0, drop: 0, observed: 0, executed: 0 };
    visible.forEach((e) => { c[e.decision]++; if (e.executed) c.executed++; });
    return c;
  }, [visible]);

  const feed = useMemo(() => visible.filter((e) =>
    (fDec === 'all' || e.decision === fDec) && (fAct === 'all' || e.action === fAct) &&
    (fCh === 'all' || e.channel === fCh) && (!fAgent || e.agentId === fAgent || e.target === fAgent),
  ).slice().reverse(), [visible, fDec, fAct, fCh, fAgent]);

  const selected = events.find((e) => e.id === selEvent) ?? null;
  const complete = !!run && cursor >= total && !playing && total > 0;
  const highlightNodes = new Set<string>();
  const focus = hover ?? fAgent;
  if (selected) { highlightNodes.add(selected.agentId); highlightNodes.add(selected.target); }

  const step = () => { setPlaying(false); setCursor((c) => Math.min(total, c + 1)); };
  const reset = () => { setPlaying(false); setCursor(0); setSelEvent(null); };
  const togglePlay = () => {
    if (cursor >= total) setCursor(0);
    setPlaying((p) => !p);
  };

  const onWheel = (e: React.WheelEvent) => {
    const f = e.deltaY < 0 ? 1.1 : 0.9;
    setView((v) => ({ ...v, k: Math.min(3, Math.max(0.5, v.k * f)) }));
  };

  const sel = 'h-7 bg-secondary border border-border text-xs px-1.5 text-foreground';

  return (
    <div className="min-h-[100dvh] flex flex-col text-sm">
      {/* TOP BAR */}
      <header className="border-b border-border bg-card/90 backdrop-blur px-4 py-2.5 flex flex-wrap items-center gap-3">
        <div className="flex items-center gap-2 mr-2">
          <ShieldAlert size={20} color={C.ok} />
          <div className="leading-tight">
            <div className="font-semibold tracking-wide" data-testid="text-title">SwarmSentinel</div>
            <div className="text-[10px] uppercase tracking-[0.2em] text-muted-foreground" style={mono}>flight recorder</div>
          </div>
        </div>
        <Badge color={C.warn}>Synthetic sample data</Badge>
        {run && <span className="text-xs text-muted-foreground hidden md:inline" style={mono} data-testid="text-run-id">{run.id} / {run.provenance}</span>}
        <div className="flex-1" />
        <div className="flex items-center border border-border" role="tablist">
          {(['normal', 'attack'] as Scenario[]).map((s) => (
            <button key={s} data-testid={`button-scenario-${s}`} onClick={() => setScenario(s)}
              className="px-3 h-8 text-xs transition-colors"
              style={{ background: scenario === s ? (s === 'attack' ? C.bad + '33' : C.ok + '26') : 'transparent',
                color: scenario === s ? (s === 'attack' ? C.bad : C.ok) : undefined }}>
              {s === 'normal' ? 'Normal Run' : 'Swarm Attack'}
            </button>
          ))}
        </div>
        <Btn primary testid="button-run-demo" onClick={runDemo} disabled={sim.isPending}>
          <FlaskConical size={14} /> {sim.isPending ? 'Simulating…' : 'Run Demo'}
        </Btn>
      </header>

      {sim.isError && (
        <div className="px-4 py-2 text-xs flex items-center gap-3" style={{ background: C.bad + '1f', color: C.bad }} data-testid="status-error">
          Simulation request failed. The backend did not return a run.
          <button className="underline" data-testid="button-retry" onClick={() => (run ? runDemo() : mutateRef.current({ data: input('normal') }, { onSuccess: (r) => { setRun(r); setCursor(r.events.length); } }))}>Retry</button>
        </div>
      )}

      {/* MAIN */}
      <main className="flex-1 grid grid-cols-1 lg:grid-cols-[60fr_40fr] min-h-0">
        {/* RADAR */}
        <section className="relative border-b lg:border-b-0 lg:border-r border-border min-h-[420px] lg:min-h-[560px] overflow-hidden">
          <div className="absolute top-3 left-4 z-10 flex items-center gap-2 text-xs uppercase tracking-widest text-muted-foreground" style={mono}>
            <Radar size={14} /> Agent / tool radar
          </div>
          <div className="absolute top-3 right-3 z-10 flex gap-1">
            <button aria-label="Zoom in" data-testid="button-zoom-in" className="h-7 w-7 bg-secondary border border-border grid place-items-center" onClick={() => setView((v) => ({ ...v, k: Math.min(3, v.k * 1.2) }))}><Plus size={13} /></button>
            <button aria-label="Zoom out" data-testid="button-zoom-out" className="h-7 w-7 bg-secondary border border-border grid place-items-center" onClick={() => setView((v) => ({ ...v, k: Math.max(0.5, v.k / 1.2) }))}><Minus size={13} /></button>
            <button aria-label="Reset view" data-testid="button-zoom-reset" className="h-7 w-7 bg-secondary border border-border grid place-items-center" onClick={() => setView({ x: 0, y: 0, k: 1 })}><Maximize2 size={13} /></button>
          </div>

          <div className="absolute bottom-3 left-4 z-10 flex flex-wrap gap-x-4 gap-y-1 text-[10px] text-muted-foreground" style={mono}>
            <span style={{ color: C.ok }}>● normal agent</span>
            <span style={{ color: C.bad }}>● detected cluster</span>
            <span style={{ color: C.tool }}>■ tool / page</span>
            <span style={{ color: C.bad }}>- - blocked attempt (not executed)</span>
            <span style={{ color: C.dim }}>··· lineage</span>
          </div>

          {!run && (
            <div className="absolute inset-0 grid place-items-center text-muted-foreground text-xs" style={mono} data-testid="status-loading">
              <div className="animate-pulse">Loading normal sample run…</div>
            </div>
          )}

          <svg viewBox="0 0 800 600" className="w-full h-full min-h-[420px] lg:min-h-[560px] touch-none cursor-grab active:cursor-grabbing"
            data-testid="svg-radar" onWheel={onWheel}
            onPointerDown={(e) => { drag.current = { x: e.clientX, y: e.clientY, vx: view.x, vy: view.y }; (e.currentTarget as Element).setPointerCapture(e.pointerId); }}
            onPointerMove={(e) => { const d = drag.current; if (d) setView((v) => ({ ...v, x: d.vx + (e.clientX - d.x) * 1.3, y: d.vy + (e.clientY - d.y) * 1.3 })); }}
            onPointerUp={() => { drag.current = null; }}>
            <defs>
              {([['ok', C.ok], ['bad', C.bad], ['dim', C.dim]] as const).map(([n, col]) => (
                <marker key={n} id={`arr-${n}`} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto-start-reverse">
                  <path d="M0 0L10 5L0 10z" fill={col} />
                </marker>
              ))}
            </defs>
            <g transform={`translate(${400 + view.x} ${300 + view.y}) scale(${view.k}) translate(-400 -300)`}>
              {[80, 165, 250].map((r) => <circle key={r} cx={400} cy={300} r={r} fill="none" stroke="#223044" strokeDasharray="2 6" />)}
              {graph.edges.map((e) => {
                const a = layout.get(e.from)!, b = layout.get(e.to)!;
                const dx = b.x - a.x, dy = b.y - a.y, L = Math.hypot(dx, dy) || 1;
                const x1 = a.x + (dx / L) * 16, y1 = a.y + (dy / L) * 16, x2 = b.x - (dx / L) * 18, y2 = b.y - (dy / L) * 18;
                const col = e.kind === 'blocked' ? C.bad : e.kind === 'lineage' ? C.dim : C.ok;
                const mk = e.kind === 'blocked' ? 'bad' : e.kind === 'lineage' ? 'dim' : 'ok';
                const faded = focus && e.from !== focus && e.to !== focus;
                // slight curve so opposing edges don't overlap
                const mx = (x1 + x2) / 2 - (dy / L) * (e.kind === 'comm' ? 14 : e.kind === 'blocked' ? -14 : 0);
                const my = (y1 + y2) / 2 + (dx / L) * (e.kind === 'comm' ? 14 : e.kind === 'blocked' ? -14 : 0);
                return (
                  <path key={`${e.from}${e.to}${e.kind}`} d={`M${x1} ${y1}Q${mx} ${my} ${x2} ${y2}`} fill="none" stroke={col}
                    strokeWidth={e.kind === 'lineage' ? 1 : Math.min(3.5, 1.2 + e.n * 0.3)}
                    strokeDasharray={e.kind === 'blocked' ? '5 4' : e.kind === 'lineage' ? '1 4' : undefined}
                    opacity={faded ? 0.12 : e.kind === 'lineage' ? 0.8 : 0.85} markerEnd={`url(#arr-${mk})`}
                    data-testid={`edge-${e.kind}-${e.from}-${e.to}`} />
                );
              })}
              {[...graph.nodeIds].map((id) => {
                const n = layout.get(id)!;
                const bad = detected.has(id);
                const col = n.kind === 'tool' ? C.tool : bad ? C.bad : C.ok;
                const isSel = fAgent === id || highlightNodes.has(id);
                return (
                  <g key={id} transform={`translate(${n.x} ${n.y})`} className="cursor-pointer" data-testid={`node-${id}`}
                    onPointerDown={(e) => e.stopPropagation()}
                    onMouseEnter={() => setHover(id)} onMouseLeave={() => setHover(null)}
                    onClick={() => { setFAgent(fAgent === id ? null : id); setTab('feed'); }}>
                    {bad && <circle r={22} fill="none" stroke={C.bad} strokeWidth={1.5} className="pulse" />}
                    {n.kind === 'tool'
                      ? <rect x={-9} y={-9} width={18} height={18} fill="#0e1622" stroke={col} strokeWidth={isSel ? 3 : 1.5} />
                      : <circle r={13} fill={col + '26'} stroke={col} strokeWidth={isSel ? 3.5 : 1.8} />}
                    {n.kind === 'agent' && run && id === run.rootAgent && <circle r={4} fill={col} />}
                    <text y={n.kind === 'tool' ? 24 : 28} textAnchor="middle" fontSize={9.5} fill={focus === id ? '#fff' : '#9fb0c4'} style={mono}>
                      {id.length > 16 ? id.slice(0, 15) + '…' : id}
                    </text>
                  </g>
                );
              })}
            </g>
          </svg>
          {focus && (
            <div className="absolute bottom-10 right-3 z-10 bg-card border border-border px-2.5 py-1.5 text-xs" style={mono} data-testid="text-node-info">
              {focus}: {detected.has(focus) ? 'detected' : 'no detection'} / {visible.filter((e) => e.agentId === focus).length} actions
            </div>
          )}
        </section>

        {/* RIGHT PANEL */}
        <section className="flex flex-col min-h-[460px] lg:max-h-[calc(100dvh-210px)]">
          <div className="grid grid-cols-5 border-b border-border text-center">
            {[['Allow', counts.allow, C.ok], ['Throttle', counts.throttle, C.warn], ['Drop', counts.drop, C.bad], ['Observed', counts.observed, C.info], ['Executed', counts.executed, '#dfe7f1']].map(([l, v, c]) => (
              <div key={l as string} className="py-2 border-r border-border last:border-r-0" data-testid={`counter-${(l as string).toLowerCase()}`}>
                <div className="text-lg font-bold" style={{ ...mono, color: c as string }}>{v}</div>
                <div className="text-[9px] uppercase tracking-widest text-muted-foreground">{l}</div>
              </div>
            ))}
          </div>
          <div className="flex border-b border-border">
            {([['feed', 'ASP feed'], ['policy', 'Tripwires'], ['settings', 'Policy'], ['info', 'Limits & data']] as [Tab, string][]).map(([t, l]) => (
              <button key={t} data-testid={`tab-${t}`} onClick={() => setTab(t)}
                className={`flex-1 h-9 text-xs border-b-2 transition-colors ${tab === t ? 'border-primary text-foreground' : 'border-transparent text-muted-foreground hover:text-foreground'}`}>
                {l}{t === 'policy' && policies.length ? ` (${policies.length})` : ''}
              </button>
            ))}
          </div>

          {tab === 'feed' && (
            <div className="flex-1 flex flex-col min-h-0">
              <div className="flex flex-wrap items-center gap-1.5 p-2 border-b border-border">
                <select className={sel} value={fDec} onChange={(e) => setFDec(e.target.value)} data-testid="select-filter-decision" aria-label="Decision filter">
                  {['all', 'allow', 'throttle', 'drop', 'observed'].map((o) => <option key={o} value={o}>{o === 'all' ? 'any decision' : o}</option>)}
                </select>
                <select className={sel} value={fAct} onChange={(e) => setFAct(e.target.value)} data-testid="select-filter-action" aria-label="Action filter">
                  {['all', 'spawn', 'tool', 'message', 'wiki_edit'].map((o) => <option key={o} value={o}>{o === 'all' ? 'any action' : o}</option>)}
                </select>
                <select className={sel} value={fCh} onChange={(e) => setFCh(e.target.value)} data-testid="select-filter-channel" aria-label="Channel filter">
                  {['all', 'in_band', 'out_of_band'].map((o) => <option key={o} value={o}>{o === 'all' ? 'any channel' : o}</option>)}
                </select>
                {fAgent && <button className="text-xs px-2 h-7 border" style={{ borderColor: C.info, color: C.info }} data-testid="button-clear-agent" onClick={() => setFAgent(null)}>{fAgent} x</button>}
              </div>
              <div className="flex-1 overflow-y-auto min-h-[180px]" data-testid="list-feed">
                {feed.length === 0 && (
                  <div className="p-8 text-center text-xs text-muted-foreground" data-testid="status-feed-empty">
                    {total === 0 ? 'No events recorded yet.' : cursor === 0 ? 'Recorder armed. Press play to replay events.' : 'No events match these filters.'}
                  </div>
                )}
                {feed.map((e) => (
                  <button key={e.id} data-testid={`row-event-${e.id}`} onClick={() => setSelEvent(e.id === selEvent ? null : e.id)}
                    className={`slide-in w-full text-left px-3 py-1.5 border-b border-border/60 hover:bg-accent/50 flex gap-2 items-start ${selEvent === e.id ? 'bg-accent' : ''}`}
                    style={{ borderLeft: `3px solid ${decColor(e.decision)}` }}>
                    <span className="text-[10px] text-muted-foreground pt-0.5" style={mono}>{tm(e.timestamp)}</span>
                    <span className="flex-1 min-w-0">
                      <span className="block text-xs truncate" style={mono}>
                        {e.agentId} <span className="text-muted-foreground">{e.action}</span> {e.target}
                      </span>
                      <span className="block text-[11px] text-muted-foreground truncate">{e.rule}: {e.reason}</span>
                    </span>
                    <span className="flex flex-col items-end gap-1">
                      <Badge color={decColor(e.decision)}>{e.decision}</Badge>
                      {!e.executed && <span className="text-[9px] uppercase" style={{ ...mono, color: C.bad }}>not executed</span>}
                    </span>
                  </button>
                ))}
              </div>
              {selected && (
                <div className="border-t border-border bg-card p-3 text-xs space-y-1.5 max-h-[45%] overflow-y-auto" data-testid="panel-event-detail">
                  <div className="flex items-center justify-between">
                    <span className="uppercase tracking-widest text-[10px] text-muted-foreground">Event {selected.id}</span>
                    <Badge color={decColor(selected.decision)}>{selected.decision}</Badge>
                  </div>
                  {([
                    ['agent', selected.agentId], ['parent agent', selected.parentId || 'none (root)'],
                    ['span', selected.spanId], ['parent span', selected.parentSpanId || 'none'],
                    ['action', `${selected.action} / ${selected.channel}`], ['target', selected.target],
                    ['depth', String(selected.depth)], ['rule', selected.rule], ['policy version', `v${selected.policyVersion}`],
                    ['executed', selected.executed ? 'yes' : 'no, blocked before execution'],
                  ] as [string, string][]).map(([k, v]) => (
                    <div key={k} className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">{k}</span><span className="break-all" style={mono}>{v}</span></div>
                  ))}
                  <div className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">reason</span><span>{selected.reason}</span></div>
                  <div className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">intent</span><span>{selected.intent}</span></div>
                  <div className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">intent vector</span>
                    <span className="break-all" style={mono} data-testid="text-intent-vector">[{selected.intentVector.map((n) => n.toFixed(2)).join(', ')}]</span></div>
                </div>
              )}
            </div>
          )}

          {tab === 'policy' && (
            <div className="flex-1 overflow-y-auto p-3 space-y-2" data-testid="list-policies">
              <p className="text-xs text-muted-foreground">Dynamic tripwire history. Entries appear as the replay reaches them.</p>
              {policies.length === 0 && <div className="p-6 text-center text-xs text-muted-foreground border border-dashed border-border" data-testid="status-policy-empty">No tripwire has fired in the visible window.</div>}
              {alerts.map((a) => (
                <div key={a.id} className="border p-2 text-xs" style={{ borderColor: C.bad + '55', background: C.bad + '0f' }} data-testid={`alert-${a.id}`}>
                  <div className="flex justify-between"><Badge color={C.bad}>{a.kind}</Badge><span style={mono} className="text-muted-foreground">{tm(a.timestamp)}</span></div>
                  <div className="mt-1">{a.reason}</div>
                  <div className="mt-1 text-muted-foreground" style={mono}>{a.agents.join(', ')} / event {a.eventId}</div>
                </div>
              ))}
              {policies.map((p) => (
                <div key={p.version} className="border border-border p-2 text-xs" data-testid={`policy-v${p.version}`}>
                  <div className="flex justify-between"><Badge color={C.warn}>policy v{p.version}</Badge><span style={mono} className="text-muted-foreground">{tm(p.timestamp)}</span></div>
                  <div className="mt-1">{p.reason}</div>
                  <div className="mt-1" style={{ ...mono, color: C.bad }}>blocked: {p.blockedAgents.join(', ') || 'none'}</div>
                </div>
              ))}
            </div>
          )}

          {tab === 'settings' && (
            <div className="flex-1 overflow-y-auto p-4 space-y-4">
              <label className="flex items-center justify-between gap-3 border border-border p-3 cursor-pointer">
                <span>
                  <span className="block font-medium">Feedback containment</span>
                  <span className="block text-xs text-muted-foreground">Off: the same run replays with detection only, for comparison.</span>
                </span>
                <input type="checkbox" className="h-5 w-5 accent-[#3ddc97]" checked={feedback} onChange={(e) => setFeedback(e.target.checked)} data-testid="toggle-feedback" />
              </label>
              {([
                ['maxDepth', 'Max spawn depth', maxDepth, setMaxDepth, 1, 10],
                ['semanticLimit', 'Semantic limit', semanticLimit, setSemanticLimit, 2, 20],
                ['writeLimit', 'Write limit', writeLimit, setWriteLimit, 1, 30],
              ] as [string, string, number, (n: number) => void, number, number][]).map(([k, l, v, set, mn, mx]) => (
                <div key={k}>
                  <div className="flex justify-between text-xs mb-1"><label htmlFor={k}>{l}</label><span style={mono}>{v}</span></div>
                  <input id={k} type="range" min={mn} max={mx} value={v} onChange={(e) => set(Number(e.target.value))} className="w-full accent-[#3ddc97]" data-testid={`input-${k}`} />
                </div>
              ))}
              <p className="text-xs text-muted-foreground">Settings apply to the next Run Demo. Currently selected scenario: <b>{scenario === 'attack' ? 'Swarm Attack' : 'Normal Run'}</b>.</p>
              <Btn primary testid="button-apply-run" onClick={runDemo} disabled={sim.isPending}><FlaskConical size={14} /> Run with these settings</Btn>
            </div>
          )}

          {tab === 'info' && (
            <div className="flex-1 overflow-y-auto p-4 space-y-4 text-xs leading-relaxed">
              <div>
                <h3 className="uppercase tracking-widest text-[10px] text-muted-foreground mb-1.5">Honest limits</h3>
                <ul className="space-y-1.5 list-disc pl-4" data-testid="list-limits">
                  <li>All data here is synthetic and generated for demonstration. Nothing depicts a real incident.</li>
                  <li>Intent fingerprints are hashes, not semantic embeddings. Throttling matches identical normalized intents; the gateway is not an isolated tamper-proof process.</li>
                  <li>Out-of-band monitoring observes and alerts. It cannot undo edits that already happened.</li>
                  <li>Blocked attempts are recorded but not executed.</li>
                  <li>This is a research aid, not a production security product. No security guarantees are claimed.</li>
                </ul>
              </div>
              <div className="border border-border p-3" data-testid="panel-dataset">
                <h3 className="uppercase tracking-widest text-[10px] text-muted-foreground mb-1.5">Dataset access</h3>
                <p>Researchers can request manual access to the AI Village dataset through its Hugging Face page. Access is manual and for research. Do not train models on it without the owners' permission. This app does not connect to or load that dataset.</p>
                <a href="https://huggingface.co/datasets/aidigestorg/ai-village" target="_blank" rel="noreferrer" data-testid="link-dataset"
                  className="inline-flex items-center gap-1.5 mt-2 underline" style={{ color: C.info }}>
                  huggingface.co/datasets/aidigestorg/ai-village <ExternalLink size={12} />
                </a>
              </div>
            </div>
          )}
        </section>
      </main>

      {/* TIMELINE */}
      <footer className="border-t border-border bg-card/95 px-4 py-3 space-y-2">
        <div className="flex flex-wrap items-center gap-2">
          <Btn testid="button-play" onClick={togglePlay} disabled={!run || total === 0}>{playing ? <Pause size={14} /> : <Play size={14} />}{playing ? 'Pause' : 'Play'}</Btn>
          <Btn testid="button-step" onClick={step} disabled={!run || cursor >= total}><StepForward size={14} /> Step</Btn>
          <Btn testid="button-reset" onClick={reset} disabled={!run}><RotateCcw size={14} /> Reset</Btn>
          <span className="text-xs text-muted-foreground ml-1" style={mono} data-testid="text-cursor">{cursor}/{total} events{visible.length ? ` / ${tm(visible[visible.length - 1].timestamp)}` : ''}</span>
          <div className="flex-1" />
          <Btn testid="button-download-json" disabled={!complete} title={complete ? '' : 'Available when the run is fully replayed'}
            onClick={() => run && download(`${run.id}-traces.json`, JSON.stringify(run, null, 2), 'application/json')}>
            <FileJson size={14} /> Traces (.json)
          </Btn>
          <Btn primary testid="button-download-report" disabled={!complete} title={complete ? '' : 'Available when the run is fully replayed'}
            onClick={() => run && download(`${run.id}-report.md`, run.report, 'text/markdown')}>
            <Download size={14} /> Download Report (.md)
          </Btn>
        </div>
        {!complete && run && <div className="text-[10px] text-muted-foreground" style={mono}>Export unlocks once the whole run is on the timeline, so the report always matches the replay.</div>}
        <div className="relative pt-4">
          <div className="absolute top-0 left-0 right-0 h-3 flex" aria-hidden>
            {events.map((e, i) => (
              <button key={e.id} tabIndex={-1} data-testid={`tick-${e.id}`} onClick={() => { setPlaying(false); setCursor(i + 1); setSelEvent(e.id); setTab('feed'); }}
                className="flex-1 min-w-px h-full" title={`${e.id} ${e.decision}`}>
                <span className="block mx-auto w-px h-full" style={{ background: decColor(e.decision), opacity: i < cursor ? 1 : 0.25 }} />
              </button>
            ))}
          </div>
          <input type="range" min={0} max={Math.max(total, 1)} value={cursor} disabled={!run}
            onChange={(e) => { setPlaying(false); setCursor(Number(e.target.value)); }}
            className="w-full accent-[#3ddc97]" aria-label="Forensic timeline scrubber" data-testid="input-scrubber" />
        </div>
      </footer>
    </div>
  );
}
