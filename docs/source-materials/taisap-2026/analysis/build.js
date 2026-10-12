// Builds the ACM-format submission .docx (single column, ACM named styles), then injects OMML.
// usage: node build.js out.docx
const fs = require("fs");
const path = require("path");
const JSZip = require("jszip");
const {
  Document, Packer, Paragraph, TextRun, ImageRun, Table, TableRow, TableCell, WidthType, BorderStyle,
  AlignmentType, LineRuleType, LevelFormat, TabStopType, VerticalAlign, TableLayoutType,
} = require("docx");
const C = require("./content.js");
const M = require("./math.js");

const OUT = process.argv[2];
const FIG = path.join(__dirname, "fig");
const SANS = "Linux Biolinum O", SERIF = "Linux Libertine O", MONO_ALG = "InconsolataN", MONO = "Courier New";
const TEXT_W = 12240 - 1440 - 2040; // 8760 twips = 6.083 in
const pt = (v) => Math.round(v * 20);
const ln = (m) => Math.round(240 * m);

function style(id, name, font, size, o = {}) {
  return {
    id, name, basedOn: o.basedOn || "Normal", next: o.next || "Para", quickFormat: true,
    run: { font, size, bold: o.bold, italics: o.italic, allCaps: o.caps },
    paragraph: {
      alignment: o.align, keepNext: o.keepNext,
      spacing: { before: o.before != null ? pt(o.before) : undefined, after: o.after != null ? pt(o.after) : 0, line: ln(o.line || 1.0) },
      indent: o.indent,
    },
  };
}
const J = AlignmentType.JUSTIFIED, CTR = AlignmentType.CENTER;
const STYLES = [
  style("Title_document", "Title_document", SANS, 24, { bold: true, after: 0, line: 1.8, next: "Authors" }),
  style("ShortTitle", "Short Title", SERIF, 20, { basedOn: "Title_document" }),
  style("Authors", "Authors", SANS, 18, { caps: true, before: 3, after: 0, line: 1.6 }),
  style("Affiliation", "Affiliation", SERIF, 18, { before: 3, after: 0, line: 1.48 }),
  style("Abstract", "Abstract", SERIF, 16, { align: J, before: 10, after: 0, line: 1.2 }),
  style("KeyWords", "KeyWords", SERIF, 18, { align: J, before: 7, after: 0, line: 1.35 }),
  style("CCSDescription", "CCSDescription", SERIF, 18, { align: J, before: 7, after: 0, line: 1.35, basedOn: "KeyWords" }),
  style("ACMRefHead", "ACMRefHead", SERIF, 16, { align: J, before: 8, line: 0.96 }),
  style("ACMRef", "ACMRef", SERIF, 16, { align: J, before: 1, line: 1.2 }),
  style("Head1", "Head1", SANS, 18, { bold: true, caps: true, before: 16, after: 3, line: 1.125, keepNext: true, next: "PostHeadPara" }),
  style("Head2", "Head2", SANS, 18, { bold: true, before: 12, after: 3, line: 1.125, keepNext: true, next: "PostHeadPara" }),
  style("Head3", "Head3", SERIF, 18, { italic: true, align: J, before: 12, after: 3, line: 1.125, keepNext: true, next: "PostHeadPara" }),
  style("Para", "Para", SERIF, 18, { align: J, after: 0, line: 1.35, indent: { firstLine: 240 } }),
  style("PostHeadPara", "PostHeadPara", SERIF, 18, { align: J, after: 0, line: 1.35, basedOn: "Para" }),
  style("ParaContinue", "ParaContinue", SERIF, 18, { align: J, after: 0, line: 1.35 }),
  style("TableCaption", "TableCaption", SANS, 16, { align: CTR, before: 9, after: 6, line: 1.0, keepNext: true }),
  style("TableFootnote", "TableFootnote", SERIF, 14, { after: 10, line: 1.0 }),
  style("TableCell", "TableCell", SERIF, 15, { after: 0, line: 1.05 }),
  style("FigureCaption", "FigureCaption", SANS, 16, { align: CTR, before: 3, after: 9, line: 1.0 }),
  style("Image", "Image", SERIF, 18, { align: CTR, after: 10, line: 1.125, keepNext: true }),
  style("Extract", "Extract", SERIF, 18, { align: J, before: 6, after: 9, line: 1.35, indent: { left: 259 } }),
  style("DisplayFormula", "DisplayFormula", SERIF, 18, { align: CTR, before: 6, after: 9, line: 1.35 }),
  style("DisplayFormulaUnnum", "DisplayFormulaUnnum", SERIF, 18, { align: CTR, before: 6, after: 9, line: 1.35 }),
  style("Statements", "Statements", SERIF, 18, { italic: true, align: J, before: 6, after: 6, line: 1.125, indent: { firstLine: 240 } }),
  style("AlgorithmCaption", "AlgorithmCaption", SANS, 16, { align: CTR, before: 15, after: 6, line: 1.0, keepNext: true }),
  style("Algorithm", "Algorithm", MONO_ALG, 15, { after: 3, line: 1.08 }),
  style("ComputerCode", "ComputerCode", MONO, 14, { after: 0, line: 1.15 }),
  style("AckHead", "AckHead", SANS, 18, { bold: true, before: 12, after: 3, line: 1.125, keepNext: true, next: "AckPara" }),
  style("AckPara", "AckPara", SERIF, 18, { align: J, after: 0, line: 1.35 }),
  style("ReferenceHead", "ReferenceHead", SANS, 18, { bold: true, before: 15, after: 3, line: 1.125, keepNext: true, next: "Bib_entry" }),
  style("Bib_entry", "Bib_entry", SERIF, 14, { align: J, after: 3, line: 1.0, indent: { left: 240, hanging: 240 } }),
  style("AppendixH1", "AppendixH1", SANS, 18, { bold: true, caps: true, before: 16, after: 3, line: 1.125, keepNext: true, next: "PostHeadPara" }),
  style("AppendixH2", "AppendixH2", SANS, 18, { bold: true, before: 12, after: 3, line: 1.125, keepNext: true, next: "PostHeadPara" }),
  style("AppendixH3", "AppendixH3", SANS, 18, { italic: true, before: 12, after: 0, line: 1.125, keepNext: true }),
  style("ListParagraph", "List Paragraph", SERIF, 18, { align: J, before: 3, after: 3, line: 1.35 }),
];

// Inline markup: `code`, **bold**, *italic*, {{m:key}} (inline OMML placeholder)
function runs(text, base = {}) {
  const out = [];
  const parts = text.split(/(`[^`]+`|\*\*[^*]+\*\*|\*[^*]+\*|\{\{m:\w+\}\})/);
  for (const part of parts) {
    if (!part) continue;
    if (part.startsWith("`")) out.push(new TextRun({ ...base, text: part.slice(1, -1), font: MONO }));
    else if (part.startsWith("**")) out.push(...runs(part.slice(2, -2), { ...base, bold: true }));
    else if (part.startsWith("{{m:")) out.push(new TextRun({ text: `@@M:${part.slice(4, -2)}@@` }));
    else if (part.startsWith("*") && part.length > 2) out.push(...runs(part.slice(1, -1), { ...base, italics: !base.italics }));
    else out.push(new TextRun({ ...base, text: part }));
  }
  return out;
}
const para = (style, text, extra = {}) => new Paragraph({ style, children: typeof text === "string" ? runs(text) : text, ...extra });

const children = [];
let figN = 0, tabN = 0, eqN = 0, h1N = 0, h2N = 0, h3N = 0, lastWasHead = false;
const EQNUM = {};

function emitTable(b) {
  tabN += 1;
  children.push(para("TableCaption", `Table ${tabN}. ${b.caption}`));
  const widthsIn = b.colW || b.header.map(() => 6.0 / b.header.length);
  const scale = TEXT_W / (widthsIn.reduce((a, c) => a + c, 0) * 1440);
  const widths = widthsIn.map((w) => Math.round(w * 1440 * Math.min(1, scale)));
  const rule = { style: BorderStyle.SINGLE, size: 6, color: "000000" };
  const none = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
  const cell = (text, i, header, last) => new TableCell({
    width: { size: widths[i], type: WidthType.DXA },
    margins: { top: 20, bottom: 20, left: 80, right: 80 },
    verticalAlign: VerticalAlign.TOP,
    borders: { top: header ? rule : none, bottom: header || last ? rule : none, left: none, right: none },
    children: [new Paragraph({ style: "TableCell", children: runs(String(text), header ? { bold: true } : {}) })],
  });
  const rows = [new TableRow({ tableHeader: true, cantSplit: true, children: b.header.map((h, i) => cell(h, i, true, false)) }),
    ...b.rows.map((row, ri) => new TableRow({ cantSplit: true, children: row.map((v, i) => cell(v, i, false, ri === b.rows.length - 1)) }))];
  children.push(new Table({ width: { size: widths.reduce((a, c) => a + c, 0), type: WidthType.DXA }, columnWidths: widths, rows,
    borders: { top: rule, bottom: rule, left: none, right: none, insideHorizontal: none, insideVertical: none } }));
  children.push(new Paragraph({ style: "TableFootnote", children: [] }));
}

// Code listings in the IEEE skill's box format: thick rules above and below, a thin rule under the
// title, grey right-aligned line numbers, Courier New 8 pt, leading spaces kept as a left indent.
let pendingCode = null;
function emitCodeBox(title, lines) {
  const thick = { style: BorderStyle.SINGLE, size: 6, color: "000000" };
  const thin = { style: BorderStyle.SINGLE, size: 1, color: "000000" };
  const none = { style: BorderStyle.NONE, size: 0, color: "FFFFFF" };
  const NUM = 520, CONTENT = TEXT_W - NUM;
  const full = (children, borders, margins, header = false) => new TableRow({ cantSplit: true, tableHeader: header, children: [new TableCell({
    columnSpan: 2, borders, margins: margins || { top: 0, bottom: 0, left: 0, right: 0 }, children })] });
  const rows = [
    full([new Paragraph({ alignment: AlignmentType.CENTER, children: [new TextRun({ text: title, font: SANS, size: 16, bold: true })] })],
      { top: thick, bottom: thin, left: none, right: none }, { top: 40, bottom: 40, left: 80, right: 80 }, true),
    ...lines.map((line, i) => {
      const lead = line.length - line.trimStart().length;
      return new TableRow({ cantSplit: true, children: [
        new TableCell({ width: { size: NUM, type: WidthType.DXA }, borders: { top: none, bottom: none, left: none, right: none },
          margins: { top: 0, bottom: 0, left: 40, right: 40 },
          children: [new Paragraph({ alignment: AlignmentType.RIGHT, spacing: { before: 0, after: 0, line: 240 },
            children: [new TextRun({ text: `${i + 1}:`, font: MONO, size: 16, color: "888888" })] })] }),
        new TableCell({ width: { size: CONTENT, type: WidthType.DXA }, borders: { top: none, bottom: none, left: none, right: none },
          margins: { top: 0, bottom: 0, left: 40, right: 60 },
          children: [new Paragraph({ style: "ComputerCode", alignment: AlignmentType.LEFT, indent: { left: lead * 90 },
            spacing: { before: 0, after: 0, line: 240 },
            children: [new TextRun({ text: line.trimStart(), font: MONO, size: 16 })] })] }),
      ] });
    }),
    full([new Paragraph({ children: [] })], { top: thick, bottom: none, left: none, right: none }),
  ];
  children.push(new Table({ width: { size: TEXT_W, type: WidthType.DXA }, columnWidths: [NUM, CONTENT], rows,
    layout: TableLayoutType.FIXED,
    borders: { top: none, bottom: none, left: none, right: none, insideHorizontal: none, insideVertical: none } }));
}

function emitFigure(b) {
  figN += 1;
  const file = path.join(FIG, b.file);
  const buf = fs.readFileSync(file);
  // JPEG size from SOF marker
  let w = 0, h = 0;
  for (let i = 2; i < buf.length;) {
    if (buf[i] !== 0xff) { i++; continue; }
    const marker = buf[i + 1], len = buf.readUInt16BE(i + 2);
    if (marker >= 0xc0 && marker <= 0xc3) { h = buf.readUInt16BE(i + 5); w = buf.readUInt16BE(i + 7); break; }
    i += 2 + len;
  }
  const widthIn = b.width || 6.0;
  const px = Math.round(widthIn * 96), py = Math.round(px * h / w);
  children.push(new Paragraph({ style: "Image", children: [new ImageRun({ type: "jpg", data: buf, transformation: { width: px, height: py },
    altText: { title: `Figure ${figN}`, description: b.alt, name: b.file } })] }));
  children.push(para("FigureCaption", `Fig. ${figN}. ${b.caption}`));
}

function heading(level, text, appendix = false) {
  if (appendix) {
    if (level === 1) { h1N += 1; h2N = 0; return para("AppendixH1", `${String.fromCharCode(64 + h1N)} ${text}`); }
    h2N += 1; return para("AppendixH2", `${String.fromCharCode(64 + h1N)}.${h2N} ${text}`);
  }
  if (level === 1) { h1N += 1; h2N = 0; h3N = 0; return para("Head1", `${h1N} ${text}`); }
  if (level === 2) { h2N += 1; h3N = 0; return para("Head2", `${h1N}.${h2N} ${text}`); }
  h3N += 1; return para("Head3", `${h1N}.${h2N}.${h3N} ${text}`);
}

function emit(b, appendix = false) {
  const head = ["h1", "h2", "h3", "ah1", "ah2"].includes(b.type);
  switch (b.type) {
    case "h1": case "h2": case "h3": children.push(heading(Number(b.type[1]), b.text)); break;
    case "ah1": case "ah2": children.push(heading(Number(b.type[2]), b.text, true)); break;
    case "p": children.push(para(lastWasHead ? "PostHeadPara" : "Para", b.text)); break;
    case "pcont": children.push(para("ParaContinue", b.text)); break;
    case "list":
      for (const item of b.items) children.push(new Paragraph({ style: "ListParagraph", numbering: { reference: b.numbered ? "numbers" : "bullets", level: 0 }, children: runs(item) }));
      break;
    case "fig": emitFigure(b); break;
    case "table": emitTable(b); break;
    case "algo":
      children.push(para("AlgorithmCaption", b.caption));
      for (const line of b.lines) children.push(new Paragraph({ style: "Algorithm", children: [new TextRun({ text: line })] }));
      break;
    case "code": pendingCode = b.lines; break;
    case "codecap": emitCodeBox(b.text.replace(/\.$/, ""), pendingCode || []); pendingCode = null; break;
    case "eq":
      eqN += 1; EQNUM[b.key] = eqN;
      children.push(new Paragraph({ style: "DisplayFormula", alignment: AlignmentType.LEFT,
        tabStops: [{ type: TabStopType.CENTER, position: Math.round(TEXT_W / 2) }, { type: TabStopType.RIGHT, position: TEXT_W }],
        children: [new TextRun({ text: "\t" }), new TextRun({ text: `@@EQ:${b.key}@@` }), new TextRun({ text: `\t(${eqN})` })] }));
      break;
    case "stmt":
      children.push(new Paragraph({ style: "Statements", children: [new TextRun({ text: b.label + " ", italics: false, smallCaps: true }), ...runs(b.text)] }));
      break;
    case "ext": children.push(para("Extract", b.text)); break;
    default: throw new Error("unknown block " + b.type);
  }
  lastWasHead = head;
}

// Front matter
children.push(para("Title_document", C.TITLE));
children.push(para("Authors", "Anonymous Author(s)"));
children.push(para("Affiliation", "Submission under double-anonymous review"));
children.push(para("Abstract", C.ABSTRACT));
const ccs = C.CCS.reduce((acc, path) => {
  const top = path[0], leaf = path[path.length - 1];
  (acc[top] = acc[top] || []).push(leaf);
  return acc;
}, {});
children.push(new Paragraph({ style: "CCSDescription", children: [
  new TextRun({ text: "CCS Concepts: ", bold: true }),
  new TextRun({ text: Object.entries(ccs).map(([top, leaves]) => `\u2022 ${top} \u2192 ${leaves.join("; ")}`).join("; ") + "." }),
] }));
children.push(new Paragraph({ style: "KeyWords", children: [new TextRun({ text: "Additional Key Words and Phrases: ", bold: true }), new TextRun({ text: C.KEYWORDS })] }));
// Review submission: no ACM Reference Format block (DOI, volume, article number are assigned at publication).

for (const b of C.BODY) emit(b);
for (const b of C.BACK) {
  children.push(para("AckHead", b.head.toUpperCase()));
  children.push(para("AckPara", b.text));
  lastWasHead = false;
}
children.push(para("ReferenceHead", "REFERENCES"));
const ORGS = ["AI Digest", "Anthropic", "OASIS", "OWASP GenAI Security Project", "National Institute of Standards and Technology"];
const sortKey = (ref) => {
  const org = ORGS.find((o) => ref.startsWith(o));
  const year = (ref.match(/\. (\d{4})\./) || [])[1] || "";
  if (org) return `${org} ${year}`;
  const authors = ref.split(/\. \d{4}\./)[0].replace(/ et al\.?$/, "");
  const first = authors.split(/, | and /)[0].trim().split(/\s+/);
  const surname = first[first.length - 2] === "Van" ? `Van ${first[first.length - 1]}` : first[first.length - 1];
  return `${surname} ${ref}`;
};
const strip = (s) => s.normalize("NFD").replace(/[\u0300-\u036f]/g, "");
for (const ref of [...C.REFS].sort((a, b) => strip(sortKey(a)).localeCompare(strip(sortKey(b)), "en", { sensitivity: "base" }))) children.push(para("Bib_entry", ref));
// Review submission: no history dates; ACM adds them at publication.
h1N = 0; lastWasHead = false;
for (const b of C.APP) emit(b, true);

const doc = new Document({
  title: C.TITLE, creator: "", lastModifiedBy: "", description: "",
  styles: { default: { document: { run: { font: SERIF, size: 18 } } }, paragraphStyles: STYLES },
  numbering: { config: [
    { reference: "bullets", levels: [{ level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 360, hanging: 200 } } } }] },
    { reference: "numbers", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "(%1)", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 360, hanging: 300 } } } }] },
  ] },
  sections: [{ properties: { titlePage: true, page: { size: { width: 12240, height: 15840 },
    margin: { top: 1760, right: 2040, bottom: 2840, left: 1440, header: 706, footer: 706, gutter: 0 } } }, children }],
});

(async () => {
  const buf = await Packer.toBuffer(doc);
  const zip = await JSZip.loadAsync(buf);
  let xml = await zip.file("word/document.xml").async("string");
  if (!/xmlns:m=/.test(xml)) xml = xml.replace("<w:document ", '<w:document xmlns:m="http://schemas.openxmlformats.org/officeDocument/2006/math" ');
  let inl = 0, disp = 0;
  xml = xml.replace(/<w:r>(?:(?!<\/w:r>).)*?@@M:(\w+)@@(?:(?!<\/w:r>).)*?<\/w:r>/gs, (_, key) => { inl += 1; return M.inline(key); });
  xml = xml.replace(/<w:r>(?:(?!<\/w:r>).)*?@@EQ:(\w+)@@(?:(?!<\/w:r>).)*?<\/w:r>/gs, (_, key) => { disp += 1; return M.display(key); });
  if (/@@(M|EQ):/.test(xml)) throw new Error("unreplaced math placeholder");
  zip.file("word/document.xml", xml);
  // Document language and anonymous metadata
  let st = await zip.file("word/styles.xml").async("string");
  st = st.replace(/<w:rPrDefault><w:rPr>/, '<w:rPrDefault><w:rPr><w:lang w:val="en-US" w:eastAsia="en-US" w:bidi="ar-SA"/>');
  zip.file("word/styles.xml", st);
  let core = await zip.file("docProps/core.xml").async("string");
  core = core.replace(/<dc:creator>[^<]*<\/dc:creator>/, "<dc:creator></dc:creator>")
    .replace(/<cp:lastModifiedBy>[^<]*<\/cp:lastModifiedBy>/, "<cp:lastModifiedBy></cp:lastModifiedBy>");
  if (!/<dc:language>/.test(core)) core = core.replace("</cp:coreProperties>", "<dc:language>en-US</dc:language></cp:coreProperties>");
  zip.file("docProps/core.xml", core);
  fs.writeFileSync(OUT, await zip.generateAsync({ type: "nodebuffer", compression: "DEFLATE" }));
  console.log(`wrote ${OUT}: ${figN} figures, ${tabN} tables, ${eqN} equations, ${inl} inline math, ${disp} display math`);
})().catch((e) => { console.error(e); process.exit(1); });
