// Minimal OMML builders for the manuscript's equations (inline and display).
const esc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const r = (t, sty) => `<m:r>${sty ? `<m:rPr><m:sty m:val="${sty}"/></m:rPr>` : ""}<m:t xml:space="preserve">${esc(t)}</m:t></m:r>`;
const rp = (t) => r(t, "p"); // upright (function names, operators, words)
const e = (...xs) => `<m:e>${xs.join("")}</m:e>`;
const sub = (base, s) => `<m:sSub>${e(base)}<m:sub>${s}</m:sub></m:sSub>`;
const delim = (inner, beg = "(", end = ")") =>
  `<m:d><m:dPr><m:begChr m:val="${esc(beg)}"/><m:endChr m:val="${esc(end)}"/></m:dPr>${e(inner)}</m:d>`;
const frac = (n, d) => `<m:f><m:num>${n}</m:num><m:den>${d}</m:den></m:f>`;
const nary = (chr, lower, body) =>
  `<m:nary><m:naryPr><m:chr m:val="${chr}"/><m:limLoc m:val="undOvr"/><m:supHide m:val="1"/></m:naryPr><m:sub>${lower}</m:sub><m:sup/>${e(body)}</m:nary>`;
const matrix = (rows) => {
  const cols = rows[0].length;
  return `<m:m><m:mPr><m:mcs><m:mc><m:mcPr><m:count m:val="${cols}"/><m:mcJc m:val="left"/></m:mcPr></m:mc></m:mcs></m:mPr>${rows
    .map((row) => `<m:mr>${row.map((c) => e(c)).join("")}</m:mr>`).join("")}</m:m>`;
};
const fn = (name, arg) => rp(name) + delim(arg);
const S = (x) => fn("S", r(x));
const anc = (x) => fn("anc", x);
const ae = sub(r("a"), r("e"));

const INLINE = {
  e: r("e"),
  ea: ae,
  a: r("a"),
  b: r("b"),
  c: r("c"),
  A: r("A"),
  D: r("D"),
  anc: anc(r("a")),
  Sb: S("b"),
  Sc: S("c"),
  sig: r("σ"),
  tau: r("τ"),
  clean: fn("origin", r("e")) + rp(" = ") + r("⊥"),
  SubSet: S("c") + rp(" ⊆ ") + S("p"),
  kc: sub(r("k"), r("c")),
  ks: sub(r("k"), r("s")),
  ke: sub(r("k"), r("e")),
  kt: sub(r("k"), r("t")),
  thd: sub(r("θ"), r("d")),
  EC: sub(r("E"), r("C")),
};

const DISPLAY = {
  // Eff(a) = (A \ D) ∩ ⋂_{b ∈ anc(a), S(b) defined} S(b)
  eff: fn("Eff", r("a")) + rp(" = ") + delim(r("A") + rp(" ∖ ") + r("D")) + rp(" ∩ ") +
    nary("⋂", r("b") + rp(" ∈ ") + anc(r("a")) + rp(", ") + S("b") + rp(" defined"), S("b")),
  // origin(e) = { σ(span(e)) if span(e) ∈ dom σ ; τ(b*) if b* first on anc(a_e) in dom τ ; ⊥ otherwise }
  taint: fn("origin", r("e")) + rp(" = ") + delim(matrix([
    [r("σ") + delim(fn("span", r("e"))), rp("if ") + fn("span", r("e")) + rp(" ∈ dom ") + r("σ")],
    [r("τ") + delim(r("b") + rp("*")), rp("if ") + r("b") + rp("* is the first ") + r("b") + rp(" ∈ ") + anc(ae) + rp(" with ") + r("b") + rp(" ∈ dom ") + r("τ")],
    [r("⊥"), rp("otherwise")],
  ]), "{", ""),
  // dens(C) = |E_C| / (|C|(|C|−1)) ≥ θ_d
  density: fn("dens", r("C")) + rp(" = ") + frac(delim(sub(r("E"), r("C")), "|", "|"),
    delim(r("C"), "|", "|") + delim(delim(r("C"), "|", "|") + rp(" − 1"))) + rp(" ≥ ") + sub(r("θ"), r("d")),
};

const inline = (key) => {
  if (!INLINE[key]) throw new Error("no inline math " + key);
  return `<m:oMath>${INLINE[key]}</m:oMath>`;
};
const display = (key) => {
  if (!DISPLAY[key]) throw new Error("no display math " + key);
  return `<m:oMath>${DISPLAY[key]}</m:oMath>`;
};
module.exports = { inline, display, INLINE, DISPLAY };
