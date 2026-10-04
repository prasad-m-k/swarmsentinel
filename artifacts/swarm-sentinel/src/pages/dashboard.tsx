import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { createSwarmSession, deleteSwarmSession, getSwarmSession, getSwarmDemoSession, launchInjectionDemo, listSwarmSessions, listSwarmDemoSessions, useGetSwarmSources, useSimulateSwarm } from '@workspace/api-client-react';
import { useClerk, useUser } from '@clerk/react';
import { Link } from 'wouter';
import RealAgentGuide from '@/components/real-agent-guide';
import { canReconnectLive, countToolEvidence, matchesToolEvidence, mergeLiveEvents } from '@/lib/live-events';
import type { OutcomeFilter, ProvenanceFilter } from '@/lib/live-events';
import type { Episode, RunInput, SessionStreamMessage, SessionSummary, SwarmRun, Trace } from '@workspace/api-client-react';
import {
  Play, Pause, StepForward, RotateCcw, Download, ShieldAlert, ExternalLink,
  Plus, Minus, Maximize2, FileJson, FlaskConical, Radar,
} from 'lucide-react';

type Scenario = 'normal' | 'attack' | 'injection' | 'runaway' | 'ai-village' | 'live';
type LiveStatus = 'idle' | 'connecting' | 'streaming' | 'closed' | 'error';
const LIVE = '#b48cff';
type PolicyDecl = { delegation: { 'max-depth': number }; swarm: { 'write-cap': number; 'repeated-intent': { limit: number } } };

const SCENARIOS: [Scenario, string][] = [['normal', 'Normal Run'], ['attack', 'Swarm Attack'], ['injection', 'Prompt Injection'], ['runaway', 'Runaway Loop'], ['ai-village', 'AI Village'], ['live', 'Live']];
const SPEEDS = [1, 10, 50];
const TICK_BINS = 320;
const FEED_LIMIT = 400;
const SEVERITY: Record<string, number> = { allow: 0, observed: 1, throttle: 2, drop: 3 };
const LABELED_EDGES = 20;

type EdgeKind = 'lineage' | 'comm' | 'blocked';
interface GEdge { from: string; to: string; kind: EdgeKind; n: number }
interface EdgeTally { events: Trace[]; cursor: number; edges: Map<string, GEdge>; nodeIds: Set<string> }

// Repeated interactions between the same pair fold into one edge whose weight (n) increments.
function tallyEvent(t: EdgeTally, e: Trace, layout: Map<string, GNode>) {
  const add = (from: string, to: string, kind: EdgeKind) => {
    if (!from || !to || from === to || !layout.has(from) || !layout.has(to)) return;
    t.nodeIds.add(from); t.nodeIds.add(to);
    const k = `${from}|${to}|${kind}`;
    const edge = t.edges.get(k);
    if (edge) edge.n++; else t.edges.set(k, { from, to, kind, n: 1 });
  };
  if (e.parentId) add(e.parentId, e.agentId, 'lineage');
  t.nodeIds.add(e.agentId);
  add(e.agentId, e.target, !e.executed ? 'blocked' : 'comm');
}
function edgeWidth(e: GEdge) {
  return e.kind === 'lineage' ? 1 : Math.min(5, 1.2 + Math.log2(e.n) * 0.7);
}

function policyDefaults(p?: PolicyDecl) {
  return p ? { maxDepth: p.delegation['max-depth'], semanticLimit: p.swarm['repeated-intent'].limit, writeLimit: p.swarm['write-cap'] }
    : { maxDepth: 4, semanticLimit: 5, writeLimit: 6 };
}
function decLabel(d: string, reportOnly: boolean) {
  return reportOnly && (d === 'drop' || d === 'throttle') ? `would ${d}` : d;
}
function episodeLabel(e: Episode) {
  const kinds = Object.entries(e.alertsByKind).map(([k, n]) => `${k} ${n}`).join(', ');
  return `${e.day} ${tm(e.start)} UTC · ${e.alerts} alerts (${kinds}) · ${e.events} events`;
}
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
    // Agents mentioned in chat are agents even when they do not act in this window.
    e.mentions?.forEach((m) => touch(m, e.depth));
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
  const { user } = useUser();
  const { signOut } = useClerk();
  const [liveScope, setLiveScope] = useState<'demo' | 'private'>('demo');
  const [liveError, setLiveError] = useState('');
  const [completionGrace, setCompletionGrace] = useState(60);
  const sim = useSimulateSwarm();
  const sources = useGetSwarmSources();
  const village = sources.data?.aiVillage;
  const policyDecls = sources.data?.policies as Record<string, PolicyDecl> | undefined;
  const mutateRef = useRef(sim.mutate);
  mutateRef.current = sim.mutate;

  const [run, setRun] = useState<SwarmRun | null>(null);
  const [cursor, setCursor] = useState(0);
  const [playing, setPlaying] = useState(false);
  const [scenario, setScenario] = useState<Scenario>(() =>
    new URLSearchParams(window.location.search).get('scenario') === 'live' ? 'live' : 'attack');
  const [episodeId, setEpisodeId] = useState<string>('');
  const [speed, setSpeed] = useState(1);
  const [feedback, setFeedback] = useState(true);
  const [maxDepth, setMaxDepth] = useState(4);
  const [semanticLimit, setSemanticLimit] = useState(5);
  const [writeLimit, setWriteLimit] = useState(6);

  const chooseScenario = (sc: Scenario) => {
    setScenario(sc);
    if (sc !== 'live') { stopWatch(); setLiveStatus('idle'); }
    const d = policyDefaults(policyDecls?.[sc === 'ai-village' ? 'ai-village' : 'mock']);
    setMaxDepth(d.maxDepth); setSemanticLimit(d.semanticLimit); setWriteLimit(d.writeLimit);
    if (sc === 'ai-village') setSpeed(10);
  };
  useEffect(() => {
    if (!episodeId && village?.episodes.length) setEpisodeId(village.episodes[0].id);
  }, [village, episodeId]);
  const [tab, setTab] = useState<Tab>('feed');
  const [fDec, setFDec] = useState('all');
  const [fAct, setFAct] = useState('all');
  const [fCh, setFCh] = useState('all');
  const [fProvenance, setFProvenance] = useState<ProvenanceFilter>('all');
  const [fOutcome, setFOutcome] = useState<OutcomeFilter>('all');
  const [fAgent, setFAgent] = useState<string | null>(null);
  const [selEvent, setSelEvent] = useState<string | null>(null);
  const [hover, setHover] = useState<string | null>(null);
  const [view, setView] = useState({ x: 0, y: 0, k: 1 });
  const drag = useRef<{ x: number; y: number; vx: number; vy: number } | null>(null);
  const started = useRef(false);

  const input = useCallback((sc: Exclude<Scenario, 'live'>): RunInput => ({
    scenario: sc, feedbackEnabled: feedback, maxDepth, semanticLimit, writeLimit,
    ...(sc === 'ai-village' ? { episodeId } : {}),
  }), [feedback, maxDepth, semanticLimit, writeLimit, episodeId]);

  // first mount: normal sample, fully displayed
  useEffect(() => {
    if (started.current) return;
    started.current = true;
    if (new URLSearchParams(window.location.search).get('scenario') === 'live') return;
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
        return Math.min(total, c + speed);
      });
    }, 450);
    return () => clearInterval(t);
  }, [playing, total, speed]);

  // Live sessions: snapshot, then server-sent events from that point on.
  const [liveSessions, setLiveSessions] = useState<SessionSummary[]>([]);
  const [liveId, setLiveId] = useState('');
  const [liveStatus, setLiveStatus] = useState<LiveStatus>('idle');
  const resetFilters = () => {
    setFDec('all'); setFAct('all'); setFCh('all'); setFAgent(null);
    setFProvenance('all'); setFOutcome('all'); setSelEvent(null);
  };
  // A reconnect to the same session retains filters; a different context does not.
  useEffect(() => { resetFilters(); }, [liveId, run?.id, user?.id, liveScope, scenario]);
  const stream = useRef<EventSource | null>(null);
  const generation = useRef(0);
  const reconnect = useRef<ReturnType<typeof setTimeout> | null>(null);
  const shown = useRef(0);
  const stopWatch = () => {
    generation.current++;
    stream.current?.close();
    if (reconnect.current) clearTimeout(reconnect.current);
  };
  const snapshot = (id: string) => liveScope === 'demo' ? getSwarmDemoSession(id) : getSwarmSession(id);
  const previousOwner = useRef(user?.id);
  useEffect(() => {
    if (previousOwner.current === user?.id) return;
    previousOwner.current = user?.id;
    stopWatch(); setPlaying(false); setRun(null); setCursor(0);
    setLiveId(''); setLiveSessions([]); setLiveError(''); setLiveStatus('idle');
    linkedSession.current = '';
  }, [user?.id]);
  const showError = (error: unknown) => {
    const status = (error as { status?: number })?.status;
    if (status === 401 || status === 404) {
      stopWatch(); setRun(null); setCursor(0); setLiveId('');
    }
    setLiveError(status === 401 ? 'Sign in again to access your private sessions.'
      : status === 404 ? 'Session not found, expired, or not owned by your account.'
      : status === 429 ? 'Session limit reached. Delete an owned session first.'
      : 'Live request failed. Check your connection or authentication configuration.');
    setLiveStatus('error');
  };

  useEffect(() => {
    if (scenario !== 'live') return;
    let alive = true;
    const load = () => (liveScope === 'demo' ? listSwarmDemoSessions() : listSwarmSessions()).then((rows) => {
      if (!alive) return;
      setLiveSessions(rows);
      setLiveId((id) => id || rows[0]?.sessionId || '');
    }).catch((error) => { if (alive) { setLiveSessions([]); showError(error); } });
    load();
    const t = setInterval(load, 3000);
    return () => { alive = false; clearInterval(t); };
  }, [scenario, liveScope, user?.id]);

  useEffect(() => () => stopWatch(), []);

  const watch = async (id: string, reconnecting = false) => {
    stopWatch();
    const current = generation.current;
    setLiveError('');
    setPlaying(false);
    if (!reconnecting) { setSelEvent(null); setFAgent(null); }
    setLiveStatus('connecting');
    try {
      const snap = await snapshot(id);
      if (generation.current !== current) return;
      setRun(snap); setCursor(snap.events.length); shown.current = snap.events.length;
      setView({ x: 0, y: 0, k: 1 });
      const prefix = import.meta.env.BASE_URL.replace(/\/$/, '');
      const es = new EventSource(`${prefix}/api/swarm/${liveScope === 'demo' ? 'demo/' : ''}sessions/${encodeURIComponent(id)}/stream?after=${snap.events.length}`);
      es.onopen = () => setLiveStatus('streaming');
      es.onmessage = (m) => {
        if (generation.current !== current) return;
        const msg = JSON.parse(m.data) as SessionStreamMessage;
        setRun((r) => {
          if (!r || r.id !== id) return r;
          return { ...r, events: mergeLiveEvents(r.events, msg.events, msg.updates),
            completionWarnings: msg.completionWarnings ?? r.completionWarnings,
            alerts: msg.alerts, policies: msg.policies };
        });
        // Follow the live edge unless the viewer has scrubbed back to inspect something.
        const atEdge = shown.current;
        setCursor((c) => (c >= atEdge ? msg.total : c));
        shown.current = msg.total;
      };
      es.addEventListener('closed', () => { setLiveStatus('closed'); es.close(); });
      // Rehydrate before reconnecting so a refreshed Clerk cookie and cursor are used.
      const retry = () => {
        es.close();
        if (generation.current !== current) return;
        setLiveStatus('connecting');
        reconnect.current = setTimeout(() => { if (generation.current === current) void watch(id, true); }, 2000);
      };
      es.addEventListener('auth-expired', retry);
      es.onerror = retry;
      stream.current = es;
    } catch (error) {
      if (generation.current !== current) return;
      showError(error);
      // A drop can outlast the first snapshot refresh. Keep rehydrating rather
      // than abandoning the watch while offline; never retry terminal access
      // errors, and stopWatch cancels this timer on scope/account/session change.
      if (reconnecting && canReconnectLive(error)) {
        setLiveStatus('connecting');
        reconnect.current = setTimeout(() => { if (generation.current === current) void watch(id, true); }, 2000);
      }
    }
  };

  const launchDemo = async () => {
    const current = generation.current;
    try {
      const { sessionId } = await launchInjectionDemo({ feedbackEnabled: feedback, pause: 1.2 });
      if (current !== generation.current) return;
      setLiveId(sessionId);
      await watch(sessionId);
    } catch (error) {
      showError(error);
    }
  };

  // Restore terminal-launcher deep links only after an owner has signed in.
  const linkedSession = useRef('');
  useEffect(() => {
    const query = new URLSearchParams(window.location.search);
    const id = query.get('session');
    if (query.get('scenario') !== 'live' || !id || !user) return;
    if (liveScope !== 'private') { setLiveScope('private'); return; }
    if (linkedSession.current === id) return;
    linkedSession.current = id;
    setLiveId(id);
    void watch(id);
  }, [user?.id, liveScope]);

  const runDemo = () => {
    stopWatch(); setLiveStatus('idle');
    setPlaying(false);
    setSelEvent(null); setFAgent(null);
    if (scenario === 'live') return;
    sim.mutate({ data: input(scenario) }, {
      onSuccess: (r) => { setRun(r); setCursor(0); setPlaying(true); setView({ x: 0, y: 0, k: 1 }); },
    });
  };

  const events = run?.events ?? [];
  const reportOnly = run?.mode === 'report-only';
  const isVillage = run?.source === 'ai-village';
  const isLive = run?.source === 'live';
  const executionEvidence = run?.executionEvidence === true;
  const completionWarnings = executionEvidence ? run?.completionWarnings ?? [] : [];
  const toolOutcome = (e: Trace) => e.executionStatus
    ? `${e.executionStatus} · ${e.executionProvenance ?? 'engine-observed'}`
    : 'not observed';
  const ticks = useMemo(() => {
    const size = Math.max(1, Math.ceil(events.length / TICK_BINS));
    const bins: { first: number; last: number; decision: string }[] = [];
    for (let i = 0; i < events.length; i += size) {
      const worst = events.slice(i, i + size).reduce((w, e) => (SEVERITY[e.decision] > SEVERITY[w] ? e.decision : w), 'allow');
      bins.push({ first: i, last: Math.min(events.length, i + size), decision: worst });
    }
    return bins;
  }, [events]);
  const visible = useMemo(() => events.slice(0, cursor), [events, cursor]);
  const visIds = useMemo(() => new Set(visible.map((e) => e.id)), [visible]);
  const layout = useMemo(() => buildLayout(events), [events]);
  // Spawn targets the gateway refused never existed; draw them as greyed placeholders.
  const neverAdmitted = useMemo(() => {
    const refused = new Set(events.filter((e) => e.action === 'spawn' && !e.executed).map((e) => e.target));
    events.forEach((e) => { if (e.action === 'spawn' && e.executed) refused.delete(e.target); refused.delete(e.agentId); });
    return refused;
  }, [events]);
  const lastT = visible.length ? new Date(visible[visible.length - 1].timestamp).getTime() : -Infinity;
  const alerts = useMemo(() => (run?.alerts ?? []).filter((a) => visIds.has(a.eventId)), [run, visIds]);
  const policies = useMemo(() => (run?.policies ?? []).filter((p) => new Date(p.timestamp).getTime() <= lastT), [run, lastT]);
  const detected = useMemo(() => {
    const s = new Set<string>();
    alerts.forEach((a) => a.agents.forEach((x) => s.add(x)));
    policies.forEach((p) => p.blockedAgents.forEach((x) => s.add(x)));
    return s;
  }, [alerts, policies]);

  // Incremental tally: advancing the cursor folds in only the new events; a new run or a
  // backwards scrub starts over. Replaying thousands of AI Village events stays O(new events) per tick.
  const tally = useRef<EdgeTally | null>(null);
  const graph = useMemo(() => {
    // Auth/scope changes can clear the run before React flushes its cursor reset.
    // Never walk past the current event array, including during that transition.
    const end = Math.min(cursor, events.length);
    let t = tally.current;
    if (!t || t.events !== events || end < t.cursor) {
      t = { events, cursor: 0, edges: new Map(), nodeIds: new Set() };
    }
    for (let i = t.cursor; i < end; i++) tallyEvent(t, events[i], layout);
    t.cursor = end;
    tally.current = t;
    const edges = [...t.edges.values()];
    const heaviest = new Set(edges.filter((e) => e.kind !== 'lineage' && e.n > 1)
      .sort((a, b) => b.n - a.n).slice(0, LABELED_EDGES));
    return { nodeIds: new Set(t.nodeIds), edges, heaviest };
  }, [events, cursor, layout]);

  const counts = useMemo(() => {
    const c = { allow: 0, throttle: 0, drop: 0, observed: 0, executed: 0 };
    visible.forEach((e) => { c[e.decision]++; if (e.executed) c.executed++; });
    return c;
  }, [visible]);

  const matchingEvents = useMemo(() => visible.filter((e) =>
    (fDec === 'all' || e.decision === fDec) && (fAct === 'all' || e.action === fAct) &&
    (fCh === 'all' || e.channel === fCh) && (!fAgent || e.agentId === fAgent || e.target === fAgent) &&
    (!executionEvidence || matchesToolEvidence(e, fProvenance, fOutcome)),
  ), [visible, fDec, fAct, fCh, fAgent, executionEvidence, fProvenance, fOutcome]);
  const feed = useMemo(() => matchingEvents.slice(-FEED_LIMIT).reverse(), [matchingEvents]);
  const toolEvidenceCounts = useMemo(() => countToolEvidence(matchingEvents), [matchingEvents]);
  const toolCounts = toolEvidenceCounts.outcomes;

  const selected = matchingEvents.find((e) => e.id === selEvent) ?? null;
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
        {isVillage
          ? <><Badge color={C.info}>AI Village dataset replay</Badge><Badge color={C.warn}>report-only · counterfactual</Badge></>
          : isLive
            ? <><Badge color={LIVE}>{liveScope === 'demo' ? 'PUBLIC SYNTHETIC demo' : 'PRIVATE owner-only'} · enforce</Badge>
                <span className="inline-flex items-center gap-1.5 text-[10px] uppercase tracking-wider" style={{ ...mono, color: liveStatus === 'streaming' ? C.ok : liveStatus === 'error' ? C.bad : C.dim }} data-testid="status-live">
                  <span className={`h-2 w-2 rounded-full ${liveStatus === 'streaming' ? 'animate-pulse' : ''}`} style={{ background: 'currentColor' }} />{liveStatus}
                </span></>
            : <Badge color={C.warn}>Synthetic sample data</Badge>}
        {run && <span className="text-xs text-muted-foreground hidden md:inline" style={mono} data-testid="text-run-id">{run.id} / {run.provenance}</span>}
        <div className="flex-1" />
        <div className="flex items-center border border-border" role="tablist">
          {SCENARIOS.map(([s, label]) => {
            const col = s === 'attack' || s === 'runaway' ? C.bad : s === 'injection' ? C.warn : s === 'ai-village' ? C.info : s === 'live' ? LIVE : C.ok;
            const off = s === 'ai-village' && !village?.available;
            return (
              <button key={s} data-testid={`button-scenario-${s}`} onClick={() => chooseScenario(s)} disabled={off}
                title={off ? 'Private AI Village access requires explicit operator approval.' : undefined}
                className="px-3 h-8 text-xs transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                style={{ background: scenario === s ? col + '26' : 'transparent', color: scenario === s ? col : undefined }}>
                {label}
              </button>
            );
          })}
        </div>
        {scenario === 'ai-village' && village?.available && (
          <select className="h-8 max-w-[22rem] bg-secondary border border-border text-xs px-1.5 text-foreground" value={episodeId}
            onChange={(e) => setEpisodeId(e.target.value)} data-testid="select-episode" aria-label="AI Village episode">
            {village.episodes.map((e) => <option key={e.id} value={e.id}>{episodeLabel(e)}</option>)}
          </select>
        )}
        {scenario === 'live' ? (
          <>
            <select value={liveScope} aria-label="Live access" data-testid="select-live-access"
              className="h-8 bg-secondary border border-border text-xs px-1.5"
              onChange={(e) => {
                stopWatch(); setRun(null); setCursor(0); setLiveId(''); setLiveSessions([]);
                setLiveError(''); setLiveStatus('idle'); setLiveScope(e.target.value as 'demo' | 'private');
              }}>
              <option value="demo">Public synthetic demos</option>
              <option value="private" disabled={!user}>My private sessions{!user ? ' — sign in' : ''}</option>
            </select>
            {user ? <Btn testid="button-sign-out" onClick={() => { stopWatch(); setPlaying(false); setCursor(0); setRun(null); void signOut({ redirectUrl: import.meta.env.BASE_URL }); }}>Sign out</Btn>
              : <Link href="/sign-in" className="text-xs underline" data-testid="link-sign-in">Sign in for private sessions</Link>}
            <select className="h-8 max-w-[18rem] bg-secondary border border-border text-xs px-1.5 text-foreground" value={liveId}
              onChange={(e) => setLiveId(e.target.value)} data-testid="select-session" aria-label="Live session">
              {liveSessions.length === 0 && <option value="">no live sessions</option>}
              {liveSessions.map((r) => (
                <option key={r.sessionId} value={r.sessionId}>
                  {(r.label || r.sessionId.slice(0, 8))} · {tm(r.created)} · {r.events} events · {r.alerts} alerts
                </option>
              ))}
            </select>
            <Btn testid="button-watch" onClick={() => liveId && watch(liveId)} disabled={!liveId}>Watch</Btn>
            {liveScope === 'demo'
              ? <Btn primary testid="button-launch-demo" onClick={launchDemo}><FlaskConical size={14} /> Launch synthetic demo</Btn>
              : <>
                <label className="text-xs flex items-center gap-1">
                  Receipt grace (s)
                  <input type="number" min={1} max={86400} value={completionGrace}
                    className="h-8 w-20 bg-secondary border border-border px-1.5"
                    data-testid="input-completion-grace"
                    onChange={(e) => setCompletionGrace(Number(e.target.value))} />
                </label>
                <Btn testid="button-create-session" disabled={!Number.isInteger(completionGrace) || completionGrace < 1 || completionGrace > 86400} onClick={async () => {
                  const current = generation.current;
                  try {
                    const result = await createSwarmSession({ policy: 'mock', feedbackEnabled: feedback, root: 'orchestrator', completionGraceSeconds: completionGrace });
                    if (current !== generation.current) return;
                    setLiveId(result.sessionId); await watch(result.sessionId);
                  } catch (error) { showError(error); }
                }}>New private session</Btn>
                <Btn testid="button-delete-session" disabled={!liveId} onClick={async () => {
                  if (!window.confirm('Delete this private session and all of its in-memory traces?')) return;
                  try {
                    await deleteSwarmSession(liveId); stopWatch(); setRun(null); setCursor(0);
                    setLiveId(''); setLiveSessions((rows) => rows.filter((r) => r.sessionId !== liveId)); setLiveStatus('idle');
                  } catch (error) { showError(error); }
                }}>Delete session</Btn>
              </>}
            {liveError && <span role="alert" className="text-xs text-red-400">{liveError}</span>}
          </>
        ) : (
          <Btn primary testid="button-run-demo" onClick={runDemo} disabled={sim.isPending}>
            <FlaskConical size={14} /> {sim.isPending ? (scenario === 'ai-village' ? 'Replaying…' : 'Simulating…') : scenario === 'ai-village' ? 'Replay' : 'Run Demo'}
          </Btn>
        )}
      </header>

      {sim.isError && (
        <div className="px-4 py-2 text-xs flex items-center gap-3" style={{ background: C.bad + '1f', color: C.bad }} data-testid="status-error">
          Simulation request failed. {(sim.error as { data?: { detail?: string } } | null)?.data?.detail ?? 'The backend did not return a run.'}
          <button className="underline" data-testid="button-retry" onClick={() => (run ? runDemo() : mutateRef.current({ data: input('normal') }, { onSuccess: (r) => { setRun(r); setCursor(r.events.length); } }))}>Retry</button>
        </div>
      )}

      {scenario === 'live' && liveScope === 'private' && user && (
        <RealAgentGuide sessionCount={liveSessions.length}
          labeledCount={liveSessions.filter((r) => r.label.startsWith('Real LLM agents')).length}
          canWatch={!!liveId} onWatch={() => liveId && watch(liveId)} />
      )}

      {/* MAIN */}
      {executionEvidence && (
        <section className="border-b border-border px-4 py-2 text-xs" data-testid="completion-warnings">
          <b>Missing completion: {completionWarnings.length}</b>
          <span className="text-muted-foreground"> · Receipt grace: {run?.completionGraceSeconds ?? 60}s after server admission. Visibility only, not a detection.</span>
          <p className="text-muted-foreground">An overdue receipt does not establish body entry, failure, rollback, or retry safety. The tool may still be running; a late receipt clears the warning.</p>
          {completionWarnings.length > 0 && (
            <ul className="max-h-32 overflow-y-auto mt-1" data-testid="list-completion-warnings">
              {completionWarnings.map((warning) => (
                <li key={warning.eventId}>
                  <button className="underline" onClick={() => { setSelEvent(warning.eventId); setTab('feed'); }}>
                    Missing completion · {warning.eventId} · {warning.agentId} / {warning.target}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </section>
      )}
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
            <span style={{ color: C.ok }}>12 = edge weight ({executionEvidence ? 'authorizations, not successes' : 'repeats'})</span>
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
                const labeled = e.kind !== 'lineage' && e.n > 1 && (focus ? !faded : graph.heaviest.has(e));
                // Point on the curve at t = 0.5, where the weight label sits.
                const lx = 0.25 * x1 + 0.5 * mx + 0.25 * x2, ly = 0.25 * y1 + 0.5 * my + 0.25 * y2;
                return (
                  <g key={`${e.from}${e.to}${e.kind}`} data-testid={`edge-${e.kind}-${e.from}-${e.to}`} data-weight={e.n}>
                    <title>{`${e.from} → ${e.to}: ${e.n} ${e.kind === 'blocked' ? 'blocked attempt' : e.kind === 'lineage' ? 'event under this parent' : 'interaction'}${e.n === 1 ? '' : 's'}`}</title>
                    <path d={`M${x1} ${y1}Q${mx} ${my} ${x2} ${y2}`} fill="none" stroke={col}
                      strokeWidth={edgeWidth(e)}
                      strokeDasharray={e.kind === 'blocked' ? '5 4' : e.kind === 'lineage' ? '1 4' : undefined}
                      opacity={faded ? 0.12 : e.kind === 'lineage' ? 0.8 : 0.85} markerEnd={`url(#arr-${mk})`} />
                    {labeled && (
                      <g transform={`translate(${lx} ${ly})`} opacity={faded ? 0.3 : 1}>
                        <rect x={-(5 + String(e.n).length * 3.6)} y={-7.5} width={10 + String(e.n).length * 7.2} height={15} rx={7.5}
                          fill="#0e1622" stroke={col} strokeWidth={1} />
                        <text y={3.8} textAnchor="middle" fontSize={11} fontWeight={700} fill={col} style={mono}
                          data-testid={`weight-${e.kind}-${e.from}-${e.to}`}>{e.n}</text>
                      </g>
                    )}
                  </g>
                );
              })}
              {[...graph.nodeIds].map((id) => {
                const n = layout.get(id)!;
                const bad = detected.has(id);
                const col = n.kind === 'tool' ? C.tool : neverAdmitted.has(id) ? C.dim : bad ? C.bad : C.ok;
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
              {focus}: {neverAdmitted.has(focus) ? 'spawn refused, never admitted' : `${detected.has(focus) ? 'detected' : 'no detection'} / ${visible.filter((e) => e.agentId === focus).length} actions`}
            </div>
          )}
        </section>

        {/* RIGHT PANEL */}
        <section className="flex flex-col min-h-[460px] lg:max-h-[calc(100dvh-210px)]">
          <div className="grid grid-cols-5 border-b border-border text-center">
            {[['Allow', counts.allow, C.ok], [reportOnly ? 'Would throttle' : 'Throttle', counts.throttle, C.warn], [reportOnly ? 'Would drop' : 'Drop', counts.drop, C.bad], ['Observed', counts.observed, C.info], [executionEvidence ? 'Authorized' : 'Executed', counts.executed, '#dfe7f1']].map(([l, v, c]) => (
              <div key={l as string} className="py-2 border-r border-border last:border-r-0" data-testid={`counter-${(l as string).toLowerCase().replace(' ', '-')}`}>
                <div className="text-lg font-bold" style={{ ...mono, color: c as string }}>{v}</div>
                <div className="text-[9px] uppercase tracking-widest text-muted-foreground">{l}</div>
              </div>
            ))}
          </div>
          {executionEvidence && (
            <div className="border-b border-border px-3 py-2 text-[10px]" style={mono} data-testid="tool-outcomes">
              <div className="text-muted-foreground mb-1">Matching tool outcomes · current replay window, before display cap · authorization does not mean success</div>
              <div className="flex flex-wrap gap-x-3 gap-y-1">
                <span>Not started: {toolCounts['not-started']}</span>
                <span style={{ color: C.ok }}>Succeeded: {toolCounts.succeeded}</span>
                <span style={{ color: C.bad }}>Failed: {toolCounts.failed}</span>
                <span className="text-muted-foreground">Not observed: {toolCounts.unobserved}</span>
              </div>
              <div className="text-muted-foreground mt-1">
                Engine-observed: {toolEvidenceCounts.provenance['engine-observed']}
                {' · '}Caller-reported: {toolEvidenceCounts.provenance['caller-reported']}
                {' · '}Unobserved provenance: {toolEvidenceCounts.provenance.unobserved}
                {' · '}Caller reports are not engine verification
              </div>
            </div>
          )}
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
                {executionEvidence && <>
                  <select className={sel} value={fProvenance} onChange={(e) => setFProvenance(e.target.value as ProvenanceFilter)}
                    data-testid="select-filter-provenance" aria-label="Evidence provenance filter">
                    <option value="all">any provenance</option>
                    <option value="engine-observed">engine-observed</option>
                    <option value="caller-reported">caller-reported</option>
                    <option value="unobserved">unobserved provenance</option>
                  </select>
                  <select className={sel} value={fOutcome} onChange={(e) => setFOutcome(e.target.value as OutcomeFilter)}
                    data-testid="select-filter-outcome" aria-label="Tool outcome filter">
                    <option value="all">any tool outcome</option>
                    <option value="not-started">not started</option>
                    <option value="succeeded">succeeded</option>
                    <option value="failed">failed</option>
                    <option value="unobserved">not observed</option>
                  </select>
                </>}
                <button className={sel} onClick={resetFilters} data-testid="button-reset-filters">Reset filters</button>
                {fAgent && <button className="text-xs px-2 h-7 border" style={{ borderColor: C.info, color: C.info }} data-testid="button-clear-agent" onClick={() => setFAgent(null)}>{fAgent} x</button>}
              </div>
              <div className="px-3 py-1 text-[10px] text-muted-foreground border-b border-border" style={mono}
                data-testid="text-feed-count" aria-live="polite">
                Showing {feed.length} of {matchingEvents.length} matching events / {visible.length} in replay window
                {matchingEvents.length > FEED_LIMIT && ` · newest ${FEED_LIMIT} shown`}
              </div>
              {/* Newest rows are inserted at the top; scroll anchoring would push them out of view. */}
              <div className="flex-1 overflow-y-auto min-h-[180px]" style={{ overflowAnchor: 'none' }} data-testid="list-feed">
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
                      <Badge color={decColor(e.decision)}>{decLabel(e.decision, reportOnly)}</Badge>
                      {executionEvidence && e.action === 'tool' && (
                        <span className="text-[9px] uppercase" style={{ ...mono, color: e.executionStatus === 'failed' ? C.bad : e.executionStatus === 'succeeded' ? C.ok : C.dim }}
                          data-testid={`outcome-${e.id}`}>{toolOutcome(e)}</span>
                      )}
                      {!e.executed && !executionEvidence && <span className="text-[9px] uppercase" style={{ ...mono, color: C.bad }}>not executed</span>}
                      {e.executed && e.mode === 'report-only' && (e.decision === 'drop' || e.decision === 'throttle') && <span className="text-[9px] uppercase" style={{ ...mono, color: C.warn }}>happened</span>}
                      {e.taintOrigin && !isVillage && <span className="text-[9px] uppercase" style={{ ...mono, color: C.warn }} title={`Contaminated by untrusted content from ${e.taintOrigin}`}>tainted</span>}
                    </span>
                  </button>
                ))}
              </div>
              {selected && (
                <div className="border-t border-border bg-card p-3 text-xs space-y-1.5 max-h-[45%] overflow-y-auto" data-testid="panel-event-detail">
                  <div className="flex items-center justify-between">
                    <span className="uppercase tracking-widest text-[10px] text-muted-foreground">Event {selected.id}</span>
                    <Badge color={decColor(selected.decision)}>{decLabel(selected.decision, reportOnly)}</Badge>
                  </div>
                  {([
                    ['agent', selected.agentId], ['parent agent', selected.parentId || 'none (root)'],
                    ['span', selected.spanId], ['parent span', selected.parentSpanId || 'none'],
                    ['action', `${selected.action} / ${selected.channel}`], ['target', selected.target],
                    ['depth', String(selected.depth)], ['rule', selected.rule], ['policy version', `v${selected.policyVersion}`],
                    [executionEvidence ? 'authorized' : 'executed', executionEvidence ? (selected.executed ? 'yes (permission only)' : 'no, blocked before execution') : selected.mode === 'report-only' ? 'yes, historical record (policy evaluated in report-only mode)' : selected.executed ? 'yes' : 'no, blocked before execution'],
                    ...(executionEvidence && selected.action === 'tool' ? [
                      ['tool outcome', toolOutcome(selected)],
                      ['body entered', selected.toolBodyExecuted == null ? 'not observed' : selected.toolBodyExecuted ? 'yes (not proof of success)' : 'no'],
                      ...(selected.executed && !selected.executionStatus ? [
                        ['completion receipt', completionWarnings.some((w) => w.eventId === selected.id)
                          ? 'Missing completion (overdue; outcome not observed)'
                          : 'Pending (outcome not observed)'],
                      ] : []),
                      ...(selected.executionError ? [['tool error', selected.executionError]] : []),
                    ] : []),
                    ...(selected.sourceRef ? [['source', selected.sourceRef]] : []),
                    ...(selected.network ? [['network', selected.network]] : []),
                    ...(selected.reads ? [['reads', `${selected.reads} content`]] : []),
                    ...(selected.write ? [[executionEvidence ? 'write target' : 'writes', selected.resource || 'local / private state']] : []),
                    ...(selected.contaminated ? [['context', selected.taintOrigin ? `contaminated by untrusted content from ${selected.taintOrigin}` : 'contaminated: this span read untrusted content earlier']] : []),
                    ...(selected.scope?.length ? [['delegated scope', selected.scope.join(', ')]] : []),
                    ...(selected.mentions?.length ? [['mentions', selected.mentions.join(', ')]] : []),
                  ] as [string, string][]).map(([k, v]) => (
                    <div key={k} className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">{k}</span><span className="break-all" style={mono}>{v}</span></div>
                  ))}
                  <div className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">reason</span><span>{selected.reason}</span></div>
                  <div className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">{isVillage ? 'narration' : 'intent'}</span><span>{selected.intent}{isVillage && <span className="text-muted-foreground"> (agent's own claim)</span>}</span></div>
                  {selected.detail && <div className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">detail</span><pre className="whitespace-pre-wrap break-all text-[11px] max-h-40 overflow-y-auto" style={mono}>{selected.detail}</pre></div>}
                  {!!selected.violations?.length && <div className="flex gap-2"><span className="w-24 shrink-0 text-muted-foreground">violations</span>
                    <span className="space-y-0.5">{selected.violations.map((v, i) => (
                      <span key={i} className="block" style={mono}>{v.directive}: {v.detail} {v.enforced ? '(enforced)' : '(reported)'}</span>
                    ))}</span></div>}
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
                  <div className="flex justify-between"><Badge color={C.warn}>{reportOnly ? 'would revoke ' : ''}policy v{p.version}</Badge><span style={mono} className="text-muted-foreground">{tm(p.timestamp)}</span></div>
                  <div className="mt-1">{p.reason}</div>
                  <div className="mt-1" style={{ ...mono, color: C.bad }}>{reportOnly ? 'would block' : 'blocked'}: {p.blockedAgents.join(', ') || 'none'}</div>
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
                ['writeLimit', 'Write limit', writeLimit, setWriteLimit, 1, 60],
              ] as [string, string, number, (n: number) => void, number, number][]).map(([k, l, v, set, mn, mx]) => (
                <div key={k}>
                  <div className="flex justify-between text-xs mb-1"><label htmlFor={k}>{l}</label><span style={mono}>{v}</span></div>
                  <input id={k} type="range" min={mn} max={mx} value={v} onChange={(e) => set(Number(e.target.value))} className="w-full accent-[#3ddc97]" data-testid={`input-${k}`} />
                </div>
              ))}
              <p className="text-xs text-muted-foreground">Settings apply to the next run. Currently selected scenario: <b>{SCENARIOS.find(([s]) => s === scenario)?.[1]}</b>.
                {scenario === 'ai-village' && ' AI Village replays always run in report-only mode: the actions already happened, so the policy can only record what it would have done.'}</p>
              <Btn primary testid="button-apply-run" onClick={runDemo} disabled={sim.isPending}><FlaskConical size={14} /> Run with these settings</Btn>
            </div>
          )}

          {tab === 'info' && (
            <div className="flex-1 overflow-y-auto p-4 space-y-4 text-xs leading-relaxed">
              <div>
                <h3 className="uppercase tracking-widest text-[10px] text-muted-foreground mb-1.5">Honest limits</h3>
                <ul className="space-y-1.5 list-disc pl-4" data-testid="list-limits">
                  {isVillage ? <>
                    <li>Historical AI Village records replayed in report-only mode. The gateway did not exist when these actions ran; drops and revocations are counterfactual.</li>
                    <li>Agent narration is the agent's own claim, not verified ground truth. Check screenshots in the dataset before relying on it.</li>
                    <li>GUI micro-actions (clicks, scrolls, screenshots) are aggregated, not replayed. Shared-state writes are inferred from shell commands only.</li>
                    <li>Human messages are anonymized into one actor with text withheld. Behaviour before and after 2026-03-24 reflects different scaffolding regimes.</li>
                  </> : isLive ? <>
                    <li>Live session: every decision was made before the agent ran the action. Blocked calls never reached the tool.</li>
                    {executionEvidence && <li>Authorized means permission only. Registered tools have engine-observed outcomes. External completion receipts are caller-reported, not engine verification; without a receipt the outcome is not observed. Failed bodies may have partial side effects; failure does not prove rollback.</li>}
                    <li>The real-agent demo's four sandbox tools are evaluated and executed by the server gateway. Other guard-wrapped tools remain cooperative. This is not operating-system isolation for code with host filesystem access.</li>
                  </> : <li>All data here is synthetic and generated for demonstration. Nothing depicts a real incident.</li>}
                  <li>Intent fingerprints are hashes, not semantic embeddings. Throttling matches identical normalized intents; the gateway is not an isolated tamper-proof process.</li>
                  <li>Out-of-band monitoring observes and alerts. It cannot undo edits that already happened.</li>
                  <li>{isVillage ? 'Would-block decisions are recorded alongside the action that actually ran.' : 'Blocked attempts are recorded but not executed.'}</li>
                  <li>This is a research aid, not a production security product. No security guarantees are claimed.</li>
                </ul>
              </div>
              <div className="border border-border p-3" data-testid="panel-dataset">
                <h3 className="uppercase tracking-widest text-[10px] text-muted-foreground mb-1.5">Dataset access</h3>
                <p>The AI Village dataset is access-controlled and reviewed manually, for research use only. Do not train models on it without the owners' permission, and do not attempt re-identification.
                  {village?.available
                    ? ` This engine has a local, normalized store of the ${village.exportedAt?.slice(0, 10)} export (${village.range?.map((r) => r.slice(0, 10)).join(' to ')}). Raw records are never committed or served beyond replay windows.`
                    : ' This deployment has no local store, so only the synthetic scenarios are available.'}</p>
                {village?.citation && <p className="mt-2 text-muted-foreground">Cite: {village.citation}</p>}
                <a href="https://huggingface.co/datasets/aidigestorg/ai-village" target="_blank" rel="noreferrer" data-testid="link-dataset"
                  className="inline-flex items-center gap-1.5 mt-2 underline" style={{ color: C.info }}>
                  huggingface.co/datasets/aidigestorg/ai-village <ExternalLink size={12} />
                </a>
              </div>
              {run?.summary && isVillage && (
                <div className="border border-border p-3 space-y-2" data-testid="panel-summary">
                  <h3 className="uppercase tracking-widest text-[10px] text-muted-foreground">This replay (counterfactual)</h3>
                  {run.window && <p style={mono}>{run.window.start.slice(0, 16)} to {run.window.end.slice(11, 16)} UTC · {run.window.regime}{run.window.truncated ? ' · truncated' : ''}</p>}
                  {([
                    ['Would block by rule', run.summary.blockedByRule], ['Reported, not blocked', run.summary.advisories],
                    ['Network destinations', run.summary.networkHosts], ['Shared resources written', run.summary.sharedResources],
                  ] as [string, Record<string, number>][]).map(([title, rows]) => (
                    <div key={title}>
                      <div className="text-muted-foreground">{title}</div>
                      {Object.keys(rows).length === 0 ? <div style={mono}>none</div>
                        : Object.entries(rows).map(([k, n]) => <div key={k} className="flex justify-between gap-2" style={mono}><span className="truncate">{k}</span><span>{n}</span></div>)}
                    </div>
                  ))}
                  <p>{executionEvidence ? 'Proposed writes' : 'Writes'} from contexts that had read untrusted content: <b style={mono}>{run.summary.contaminatedWrites}</b> of {run.summary.writes}</p>
                </div>
              )}
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
          <select className={sel} value={speed} onChange={(e) => setSpeed(Number(e.target.value))} data-testid="select-speed" aria-label="Replay speed">
            {SPEEDS.map((n) => <option key={n} value={n}>{n}x</option>)}
          </select>
          <span className="text-xs text-muted-foreground ml-1" style={mono} data-testid="text-cursor">{cursor}/{total} events{visible.length ? ` / ${tm(visible[visible.length - 1].timestamp)}` : ''}</span>
          <div className="flex-1" />
          <Btn testid="button-download-json" disabled={!complete} title={complete ? '' : 'Available when the run is fully replayed'}
            onClick={async () => { const current = generation.current; try { const r = run && (isLive ? await snapshot(run.id) : run); if (r && current === generation.current) download(`${r.id}-traces.json`, JSON.stringify(r, null, 2), 'application/json'); } catch (error) { showError(error); } }}>
            <FileJson size={14} /> Traces (.json)
          </Btn>
          <Btn primary testid="button-download-report" disabled={!complete} title={complete ? '' : 'Available when the run is fully replayed'}
            onClick={async () => { const current = generation.current; try { const r = run && (isLive ? await snapshot(run.id) : run); if (r && current === generation.current) download(`${r.id}-report.md`, r.report, 'text/markdown'); } catch (error) { showError(error); } }}>
            <Download size={14} /> Download Report (.md)
          </Btn>
        </div>
        {!complete && run && <div className="text-[10px] text-muted-foreground" style={mono}>Export unlocks once the whole run is on the timeline, so the report always matches the replay.</div>}
        <div className="relative pt-4">
          <div className="absolute top-0 left-0 right-0 h-3 flex" aria-hidden>
            {ticks.map((b) => (
              <button key={b.first} tabIndex={-1} data-testid={`tick-${events[b.first].id}`}
                onClick={() => { setPlaying(false); setCursor(b.last); setSelEvent(events[b.last - 1].id); setTab('feed'); }}
                className="flex-1 min-w-px h-full" title={b.last - b.first > 1 ? `events ${b.first + 1}-${b.last}: ${b.decision}` : `${events[b.first].id} ${b.decision}`}>
                <span className="block mx-auto w-px h-full" style={{ background: decColor(b.decision), opacity: b.first < cursor ? 1 : 0.25 }} />
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
