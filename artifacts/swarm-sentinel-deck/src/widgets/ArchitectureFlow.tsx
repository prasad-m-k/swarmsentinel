type Props = { mode: 'pipeline' | 'feedback'; accent: string; foreground: string; surface: string; muted: string; border: string; fontFamily: string };
export default function ArchitectureFlow(p: Props) {
 const pipeline = p.mode === 'pipeline';
 const arrowId = 'swarm-flow-arrow-' + p.mode;
 const nodes = pipeline ? [{x:0,w:290,t:'Simulator'},{x:347,w:290,t:'ASP gateway'},{x:694,w:290,t:'Sentinel'},{x:1041,w:290,t:'Reporter'},{x:1388,w:290,t:'React + Tailwind'}] : [{x:0,w:380,t:'Sentinel'},{x:610,w:460,t:'Revocation policy'},{x:1280,w:400,t:'ASP gateway'}];
 const arrow = (from:number,to:number,key:string) => <path key={key} d={'M '+from+' 65 H '+to} fill="none" stroke={p.accent} strokeWidth="4" markerEnd={'url(#'+arrowId+')'}/>;
 return <svg width="100%" height="100%" viewBox="0 0 1680 230" role="img" aria-label={pipeline?'SwarmSentinel processing pipeline with out-of-band observations bypassing the gateway':'Sentinel policy feedback to ASP gateway'} style={{fontFamily:p.fontFamily}}>
  <defs><marker id={arrowId} markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M 0 0 L 8 4 L 0 8 Z" fill={p.accent}/></marker></defs>
  {nodes.map((n,i)=><g key={n.t}><rect x={n.x+2} y="18" width={n.w-4} height="94" rx="10" fill={p.surface} stroke={i===1?p.accent:p.border} strokeWidth="2"/><text x={n.x+n.w/2} y="78" textAnchor="middle" fill={p.foreground} fontSize="38" fontWeight="600">{n.t}</text></g>)}
  {nodes.slice(0,-1).map((n,i)=>arrow(n.x+n.w+3,nodes[i+1].x-12,'a'+i))}
  {pipeline?<><path d="M 145 116 V 178 H 839 V 116" stroke={p.accent} strokeWidth="3" strokeDasharray="9 9" fill="none" markerEnd={'url(#'+arrowId+')'}/><text x="492" y="222" textAnchor="middle" fill={p.muted} fontSize="30">shared-state observations</text></>:<><path d="M 1480 116 V 177 H 190 V 116" stroke={p.accent} strokeWidth="3" strokeDasharray="9 9" fill="none" markerEnd={'url(#'+arrowId+')'}/><text x="835" y="222" textAnchor="middle" fill={p.muted} fontSize="30">structured telemetry</text></>}
 </svg>;
}
