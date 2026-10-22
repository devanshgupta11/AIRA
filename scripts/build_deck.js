/**
 * Builds the AIRA mid-semester deck.
 *   cd D:\AIRA && node scripts/build_deck.js
 *
 * Palette is deliberately indigo/ember: both reference decks in this folder use teal-slate
 * (1F8A8A, 5E7C86, 0E1B24), and this one should not look like either. Violet carries the agent
 * layer, crimson marks an identified cause, amber marks a deliberate skip - the same semantics the
 * live console uses, so the slides and the demo read as one system.
 */
const pptxgen = require("pptxgenjs");
const path = require("path");
const { applyTheme } = require(path.join(
  process.env.USERPROFILE || process.env.HOME,
  ".claude/skills/synced/a4dc0f31-c293-4d10-b7d7-45d94c4727f5_80c2ad60-0411-4b45-b51b-cfc4658f2040/pptx/scripts/apply_theme.js"
));

const THEME = {
  name: "AIRA Indigo",
  headFontFace: "Cambria",
  bodyFontFace: "Calibri",
  colors: {
    dk1: "1B1630", lt1: "FFFFFF",
    dk2: "3A2E5C", lt2: "F4F2FA",
    accent1: "4C2E83",  // deep violet - the agent layer
    accent2: "7B5EA7",  // mid violet
    accent3: "D1495B",  // crimson - an identified cause / a failure
    accent4: "E09F3E",  // amber - a deliberate skip / a caveat
    accent5: "4F8A5B",  // green - verified / done
    accent6: "6E6880",  // muted - secondary text
    hlink: "4C2E83", folHlink: "7B5EA7",
  },
};

const INK = "1B1630", VIOLET = "4C2E83", VIOLET2 = "7B5EA7",
      CRIMSON = "D1495B", AMBER = "E09F3E", GREEN = "4F8A5B",
      MUTED = "6E6880", LILAC = "F4F2FA", WHITE = "FFFFFF", LINE = "DAD5E8";

const pres = new pptxgen();
pres.layout = "LAYOUT_WIDE";            // 13.3 x 7.5
pres.theme = { headFontFace: THEME.headFontFace, bodyFontFace: THEME.bodyFontFace };
pres.author = "Group 18 - Devansh Gupta, Deepanshu Goyal, Dev Garg";
pres.title = "AIRA - Autonomous Intelligent Response Agent";

/* ---------------------------------------------------------------- layouts */
pres.defineSlideMaster({
  title: "TITLE_DARK", background: { color: INK },
  objects: [
    { text: { text: "", options: { placeholder: "title", x: 0.8, y: 1.6, w: 11.7, h: 1.5,
        fontSize: 44, bold: true, color: WHITE, fontFace: "Cambria" } } },
    { text: { text: "", options: { placeholder: "sub", x: 0.8, y: 3.05, w: 10.6, h: 0.9,
        fontSize: 17, color: "C9C2DE" } } },
  ],
});

pres.defineSlideMaster({
  title: "CONTENT", background: { color: WHITE },
  objects: [
    { text: { text: "", options: { placeholder: "title", x: 0.6, y: 0.42, w: 12.1, h: 0.75,
        fontSize: 32, bold: true, color: INK, fontFace: "Cambria" } } },
    { text: { text: "", options: { placeholder: "kicker", x: 0.62, y: 1.14, w: 12.1, h: 0.42,
        fontSize: 14.5, color: MUTED } } },
    { text: { text: "Group 18  ·  AIRA  ·  Mid-semester review", options: {
        x: 0.6, y: 7.02, w: 7, h: 0.3, fontSize: 9.5, color: "A9A2BC" } } },
    { placeholder: { options: { name: "slideNum", type: "sldNum", x: 12.5, y: 7.02, w: 0.5, h: 0.3,
        fontSize: 9.5, color: "A9A2BC", align: "right" } } },
  ],
});

pres.defineSlideMaster({
  title: "CLOSING", background: { color: INK },
  objects: [
    { text: { text: "", options: { placeholder: "title", x: 0.8, y: 0.7, w: 11.7, h: 0.9,
        fontSize: 34, bold: true, color: WHITE, fontFace: "Cambria" } } },
  ],
});

/* ---------------------------------------------------------------- helpers */
const S = (s) => s;                                   // readability for addSection titles

// A content card: tinted panel, bold heading, wrapped body. No edge stripes.
function card(slide, { x, y, w, h, head, body, accent = VIOLET, fill = LILAC, headSize = 14.5, bodySize = 12.5 }) {
  slide.addShape(pres.ShapeType.roundRect, {
    x, y, w, h, rectRadius: 0.06, fill: { color: fill }, line: { color: LINE, width: 0.75 },
    objectName: `card-${head}`.slice(0, 40),
  });
  slide.addText(head, { x: x + 0.22, y: y + 0.16, w: w - 0.44, h: 0.3, margin: 0,
    fontSize: headSize, bold: true, color: accent, isTextBox: true });
  if (body) slide.addText(body, { x: x + 0.22, y: y + 0.52, w: w - 0.44, h: h - 0.68, margin: 0,
    fontSize: bodySize, color: INK, isTextBox: true, valign: "top", lineSpacingMultiple: 1.08 });
}

// A numbered step chip + heading + description, for icon-row style lists.
function stepRow(slide, { x, y, w, n, head, body, accent = VIOLET }) {
  slide.addShape(pres.ShapeType.ellipse, { x, y, w: 0.46, h: 0.46, fill: { color: accent },
    objectName: `step-${n}` });
  slide.addText(String(n), { x, y, w: 0.46, h: 0.46, margin: 0, align: "center", valign: "middle",
    fontSize: 14, bold: true, color: WHITE, isTextBox: true });
  slide.addText(head, { x: x + 0.66, y: y - 0.02, w: w - 0.66, h: 0.3, margin: 0,
    fontSize: 15, bold: true, color: INK, isTextBox: true });
  slide.addText(body, { x: x + 0.66, y: y + 0.3, w: w - 0.66, h: 0.42, margin: 0,
    fontSize: 12.5, color: MUTED, isTextBox: true, lineSpacingMultiple: 1.06 });
}

// Large number + caption.
function stat(slide, { x, y, w, value, label, color = VIOLET, size = 40 }) {
  slide.addText(value, { x, y, w, h: 0.62, margin: 0, fontSize: size, bold: true, color,
    isTextBox: true, fontFace: "Cambria" });
  slide.addText(label, { x, y: y + 0.6, w, h: 0.5, margin: 0, fontSize: 11.5, color: MUTED,
    isTextBox: true, lineSpacingMultiple: 1.05 });
}

// A flow box with an optional arrow to the next one.
function flowBox(slide, { x, y, w, h, label, sub, fill = LILAC, border = LINE, labelColor = INK, subColor = MUTED }) {
  slide.addShape(pres.ShapeType.roundRect, { x, y, w, h, rectRadius: 0.06,
    fill: { color: fill }, line: { color: border, width: 1 }, objectName: `flow-${label}`.slice(0, 40) });
  slide.addText(label, { x: x + 0.1, y: y + 0.12, w: w - 0.2, h: 0.28, margin: 0, align: "center",
    fontSize: 12.5, bold: true, color: labelColor, isTextBox: true });
  if (sub) slide.addText(sub, { x: x + 0.08, y: y + 0.42, w: w - 0.16, h: h - 0.5, margin: 0,
    align: "center", fontSize: 9.5, color: subColor, isTextBox: true, lineSpacingMultiple: 1.0 });
}

function arrowR(slide, x, y, len = 0.32, color = MUTED) {
  slide.addShape(pres.ShapeType.rightArrow, { x, y, w: len, h: 0.17, fill: { color },
    line: { color }, objectName: "arrow" });
}
function arrowD(slide, x, y, len = 0.3, color = MUTED) {
  slide.addShape(pres.ShapeType.downArrow, { x, y, w: 0.17, h: len, fill: { color },
    line: { color }, objectName: "arrowd" });
}

const tblBase = {
  border: { type: "solid", color: LINE, pt: 0.75 },
  fontFace: "Calibri", fontSize: 12, color: INK, valign: "middle",
};
const th = { fill: { color: INK }, color: WHITE, bold: true, fontSize: 11.5 };

/* ================================================================ 1 TITLE */
pres.addSection({ title: S("Opening") });
let s = pres.addSlide({ masterName: "TITLE_DARK", sectionTitle: S("Opening") });
s.addText("AIRA", { placeholder: "title" });
s.addText("A multi-agent AI system that investigates production incidents on its own", { placeholder: "sub" });
s.addShape(pres.ShapeType.rect, { x: 0.82, y: 1.42, w: 1.5, h: 0.055, fill: { color: AMBER }, objectName: "rule" });
s.addText("CSE3101 Agentic AI  ·  Mid-semester review  ·  BML Munjal University", {
  x: 0.8, y: 0.72, w: 11, h: 0.35, margin: 0, fontSize: 13, color: AMBER, isTextBox: true, charSpacing: 1.2 });

[["Devansh Gupta", "230514", "Orchestration & backend"],
 ["Deepanshu Goyal", "230500", "Monitoring & DevOps"],
 ["Dev Garg", "230487", "RCA, frontend & testing"]].forEach(([n, r, role], i) => {
  const x = 0.8 + i * 4.0;
  s.addText(n, { x, y: 4.25, w: 3.7, h: 0.3, margin: 0, fontSize: 15, bold: true, color: WHITE, isTextBox: true });
  s.addText(`${r}  ·  ${role}`, { x, y: 4.58, w: 3.7, h: 0.3, margin: 0, fontSize: 11.5, color: "9A92B8", isTextBox: true });
});

[["5", "agents built", VIOLET2], ["20-27s", "full investigation", AMBER],
 ["0", "cloud cost", GREEN], ["15/15", "regression checks", VIOLET2]].forEach(([v, l, c], i) => {
  const x = 0.8 + i * 3.0;
  s.addText(v, { x, y: 5.45, w: 2.8, h: 0.6, margin: 0, fontSize: 32, bold: true, color: c,
    isTextBox: true, fontFace: "Cambria" });
  s.addText(l, { x, y: 6.05, w: 2.8, h: 0.3, margin: 0, fontSize: 11, color: "9A92B8", isTextBox: true });
});
s.addNotes("One line: AIRA detects a production incident, decides what to investigate, and explains what broke - entirely on this laptop. Introduce the team, then move on quickly.");

/* ================================================================ 2 PROBLEM */
pres.addSection({ title: S("Problem") });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("Problem") });
s.addText("A symptom is not a cause", { placeholder: "title" });
s.addText("An alert tells you something broke. It does not tell you why, or where.", { placeholder: "kicker" });

s.addShape(pres.ShapeType.roundRect, { x: 0.6, y: 1.85, w: 5.5, h: 1.0, rectRadius: 0.06,
  fill: { color: "FBEEF0" }, line: { color: CRIMSON, width: 1 }, objectName: "alert" });
s.addText("ALERT", { x: 0.82, y: 2.02, w: 1.2, h: 0.26, margin: 0, fontSize: 10.5, bold: true,
  color: CRIMSON, isTextBox: true, charSpacing: 1 });
s.addText("\"checkout: 100% of requests failed\"", { x: 0.82, y: 2.3, w: 5.1, h: 0.4, margin: 0,
  fontSize: 16, bold: true, color: INK, isTextBox: true });

s.addText("Any of these could be the real cause", { x: 6.6, y: 1.9, w: 6.1, h: 0.3, margin: 0,
  fontSize: 13, bold: true, color: MUTED, isTextBox: true });
[["A config change", "a flag or setting someone switched on"],
 ["A dependency", "a service it calls is the one that broke"],
 ["Resource pressure", "CPU or memory saturation upstream"],
 ["A bad deployment", "a version rolled out minutes earlier"]].forEach(([h, b], i) => {
  const y = 2.3 + i * 0.62;
  s.addShape(pres.ShapeType.ellipse, { x: 6.6, y: y + 0.04, w: 0.17, h: 0.17, fill: { color: VIOLET2 },
    line: { color: VIOLET2 }, objectName: `dot${i}` });
  s.addText(h, { x: 6.9, y, w: 2.25, h: 0.3, margin: 0, fontSize: 13, bold: true, color: INK, isTextBox: true });
  s.addText(b, { x: 9.15, y: y + 0.02, w: 3.55, h: 0.34, margin: 0, fontSize: 11.5, color: MUTED, isTextBox: true });
});

card(s, { x: 0.6, y: 3.15, w: 5.5, h: 1.5, head: "What an engineer does today",
  body: "Opens Grafana, then the logs, then the deployment history, then the flag console - and correlates four systems by hand, under pressure, at 3am.",
  accent: INK, fill: "F6F5FA" });

s.addShape(pres.ShapeType.roundRect, { x: 0.6, y: 5.0, w: 12.1, h: 1.35, rectRadius: 0.06,
  fill: { color: INK }, line: { color: INK }, objectName: "thesis" });
s.addText("Monitoring tells you that something is wrong. AIRA works out what.", {
  x: 0.95, y: 5.2, w: 11.4, h: 0.45, margin: 0, fontSize: 20, bold: true, color: WHITE,
  isTextBox: true, fontFace: "Cambria" });
s.addText("The contribution is not the dashboards - those already exist. It is the layer that decides what to investigate, gathers the evidence and explains the failure.", {
  x: 0.95, y: 5.72, w: 11.4, h: 0.45, margin: 0, fontSize: 13, color: "C9C2DE", isTextBox: true });
s.addNotes("Keep this short. The key line: an alert names a symptom; a human still has to correlate four systems to find the cause. That correlation is what we automate.");

/* ================================================================ 3 ARCHITECTURE */
pres.addSection({ title: S("What we built") });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("What we built") });
s.addText("How a real fault becomes an investigated incident", { placeholder: "title" });
s.addText("Standard monitoring on top; our agent layer below. Everything runs locally.", { placeholder: "kicker" });

s.addText("OFF-THE-SHELF MONITORING", { x: 0.6, y: 1.72, w: 5, h: 0.26, margin: 0, fontSize: 10,
  bold: true, color: MUTED, isTextBox: true, charSpacing: 1.1 });
const R1 = 2.05, BH = 0.86;
flowBox(s, { x: 0.6, y: R1, w: 2.25, h: BH, label: "OTel Demo", sub: "28 containers, live traffic" });
arrowR(s, 2.96, R1 + 0.34);
flowBox(s, { x: 3.42, y: R1, w: 2.1, h: BH, label: "Collector", sub: "metrics every 15s" });
arrowR(s, 5.63, R1 + 0.34);
flowBox(s, { x: 6.09, y: R1, w: 2.1, h: BH, label: "Prometheus", sub: "4 AIRA alert rules" });
arrowR(s, 8.3, R1 + 0.34);
flowBox(s, { x: 8.76, y: R1, w: 2.1, h: BH, label: "Alertmanager", sub: "groups + routes" });
arrowR(s, 10.97, R1 + 0.34, 0.32, VIOLET);
flowBox(s, { x: 11.43, y: R1, w: 1.27, h: BH, label: "webhook", sub: ":8000", fill: "EDE7F6", border: VIOLET, labelColor: VIOLET });

s.addText("OUR AGENT LAYER", { x: 0.6, y: 3.22, w: 5, h: 0.26, margin: 0, fontSize: 10, bold: true,
  color: VIOLET, isTextBox: true, charSpacing: 1.1 });
const R2 = 3.56;
flowBox(s, { x: 0.6, y: R2, w: 2.25, h: BH, label: "Triage", sub: "summary + category",
  fill: "EDE7F6", border: VIOLET2, labelColor: VIOLET });
arrowR(s, 2.96, R2 + 0.34, 0.32, VIOLET);
flowBox(s, { x: 3.42, y: R2, w: 2.5, h: BH, label: "Orchestrator", sub: "picks WHICH agents run",
  fill: VIOLET, border: VIOLET, labelColor: WHITE, subColor: "D8CEEB" });
arrowR(s, 6.03, R2 + 0.34, 0.32, VIOLET);
flowBox(s, { x: 6.49, y: R2, w: 1.95, h: BH, label: "Metrics", sub: "Prometheus",
  fill: "EDE7F6", border: VIOLET2, labelColor: VIOLET });
flowBox(s, { x: 8.56, y: R2, w: 1.95, h: BH, label: "Log", sub: "OpenSearch + Docker",
  fill: "EDE7F6", border: VIOLET2, labelColor: VIOLET });
flowBox(s, { x: 10.63, y: R2, w: 2.07, h: BH, label: "Change", sub: "flags, restarts",
  fill: "EDE7F6", border: VIOLET2, labelColor: VIOLET });

arrowD(s, 1.6, R2 + BH + 0.06, 0.26, VIOLET);
flowBox(s, { x: 0.6, y: 4.88, w: 12.1, h: 0.66, label: "Shared incident context  -  one stored row per agent: plan, findings, evidence used, runtime",
  fill: "F4F2FA", border: VIOLET2, labelColor: INK });

card(s, { x: 0.6, y: 5.72, w: 3.9, h: 1.15, head: "Three different sources",
  body: "The Change agent never touches Prometheus.", accent: VIOLET, bodySize: 12 });
card(s, { x: 4.7, y: 5.72, w: 3.9, h: 1.15, head: "Parallel where it is free",
  body: "Evidence gathered concurrently in 0.6-1.9 s.", accent: VIOLET, bodySize: 12 });
card(s, { x: 8.8, y: 5.72, w: 3.9, h: 1.15, head: "One local model",
  body: "qwen2.5:7b on the GPU. Nothing leaves the laptop.", accent: VIOLET, bodySize: 12 });
s.addNotes("Top row is off-the-shelf and deliberately so - we reuse Prometheus rather than reinvent it. Bottom row is ours. Emphasise that the three investigators read three different systems; that pre-empts 'isn't this just Prometheus?'.");

/* ================================================================ 4 THE PATIENT */
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("What we built") });
s.addText("The system we monitor, and how we break it", { placeholder: "title" });
s.addText("A real microservices application we did not write - so the faults are not ours to stage.", { placeholder: "kicker" });

card(s, { x: 0.6, y: 1.8, w: 6.0, h: 2.45, head: "OpenTelemetry Demo 3.1.0  (Astronomy Shop)",
  body: "28 containers in Docker: payment, checkout, cart, recommendation, ad, product-catalog and more, in Go, Java, .NET, Node and Python.\n\nA Locust load generator produces continuous real customer traffic. Prometheus, Grafana, Jaeger and OpenSearch ship with it.",
  accent: VIOLET });

s.addText("How a fault is injected", { x: 6.9, y: 1.85, w: 5.8, h: 0.3, margin: 0, fontSize: 14,
  bold: true, color: INK, isTextBox: true });
[["Feature flags", "flagd switches a real failure on inside a service"],
 ["Stopping a container", "docker stop, for a service-down scenario"],
 ["Verified at the source", "flagd itself confirms the change took effect"]].forEach(([h, b], i) =>
  stepRow(s, { x: 6.9, y: 2.3 + i * 0.72, w: 5.8, n: i + 1, head: h, body: b }));

s.addShape(pres.ShapeType.roundRect, { x: 0.6, y: 4.55, w: 12.1, h: 1.9, rectRadius: 0.06,
  fill: "F6F5FA", line: { color: LINE, width: 0.75 }, objectName: "proof" });
s.addText("Nothing is simulated - and that is demonstrable", { x: 0.9, y: 4.72, w: 11.5, h: 0.32,
  margin: 0, fontSize: 15, bold: true, color: INK, isTextBox: true });
s.addText("With the payment fault active, a real purchase through the shop fails in the browser: POST /api/checkout returns HTTP 422, \"Your payment could not be processed.\"  The same error appears 66 times in the payment container's own log, and Prometheus reports the error ratio at 100%.",
  { x: 0.9, y: 5.08, w: 11.5, h: 0.75, margin: 0, fontSize: 13, color: INK, isTextBox: true, lineSpacingMultiple: 1.1 });
s.addText("Three independent sources, one fault. Any of them can be checked live during the demo.",
  { x: 0.9, y: 5.92, w: 11.5, h: 0.3, margin: 0, fontSize: 12, italic: true, color: VIOLET, isTextBox: true });
s.addNotes("If asked 'how do I know this is real?' - break checkout in the browser with DevTools open, before showing any dashboard. The 422 lands before they can doubt it.");

/* ================================================================ 4b TECH STACK */
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("What we built") });
s.addText("Technology stack", { placeholder: "title" });
s.addText("Seven layers, all on one laptop. Shaded blocks are ours; the rest is off-the-shelf and deliberately reused.", { placeholder: "kicker" });

// legend
s.addShape(pres.ShapeType.roundRect, { x: 9.5, y: 1.24, w: 0.24, h: 0.24, rectRadius: 0.04,
  fill: LILAC, line: { color: LINE, width: 1 }, objectName: "lg1" });
s.addText("off-the-shelf", { x: 9.82, y: 1.2, w: 1.3, h: 0.3, margin: 0, fontSize: 11, color: MUTED, isTextBox: true });
s.addShape(pres.ShapeType.roundRect, { x: 11.2, y: 1.24, w: 0.24, h: 0.24, rectRadius: 0.04,
  fill: { color: VIOLET }, line: { color: VIOLET }, objectName: "lg2" });
s.addText("built by us", { x: 11.52, y: 1.2, w: 1.2, h: 0.3, margin: 0, fontSize: 11, color: MUTED, isTextBox: true });

// layers, top (what the user sees) down to the application being watched
const LAYERS = [
  ["Interface", [["Incident console", "HTML · CSS · JS", true]],
   "Live view of the plan, each agent's findings and the evidence behind them"],
  ["Reasoning", [["Ollama", "0.9.6", false], ["qwen2.5:7b-instruct", "4.7 GB", false]],
   "One local model on the laptop GPU, shared by all five agents"],
  ["AIRA backend · Python 3.12", [["FastAPI", "", true], ["SQLite", "", true], ["CrewAI", "1.15", true], ["Pydantic", "2.12", true]],
   "Webhook, storage, the agent framework, and validation of every answer"],
  ["Detection and routing", [["4 AIRA alert rules", "PromQL", true], ["Alertmanager", "0.34", false]],
   "Rules evaluated every 15s; a firing alert becomes a webhook"],
  ["Telemetry storage", [["Prometheus", "3.13", false], ["Jaeger", "2.19", false], ["OpenSearch", "", false], ["Grafana", "13.1", false]],
   "Metrics, traces, logs, dashboards"],
  ["Collection", [["OpenTelemetry Collector", "0.159", false]],
   "One pipeline for every service, whatever language it is written in"],
  ["Monitored application", [["OpenTelemetry Demo", "3.1.0", false], ["flagd", "", false], ["Locust", "", false]],
   "28 containers across 10 languages, with continuous real traffic"],
];
LAYERS.forEach(([label, boxes, note], i) => {
  const y = 1.62 + i * 0.74;
  s.addText(label.toUpperCase(), { x: 0.6, y: y + 0.02, w: 2.25, h: 0.28, margin: 0, fontSize: 9.5,
    bold: true, color: MUTED, isTextBox: true, charSpacing: 0.8 });
  let x = 0.6;
  boxes.forEach(([nm, ver, ours]) => {
    const w = Math.max(1.55, 0.14 * nm.length + 0.5);
    s.addShape(pres.ShapeType.roundRect, { x, y: y + 0.28, w, h: 0.34, rectRadius: 0.05,
      fill: ours ? { color: VIOLET } : LILAC, line: { color: ours ? VIOLET : LINE, width: 1 },
      objectName: `st-${nm}`.slice(0, 40) });
    s.addText(ver ? [{ text: nm, options: { bold: true } }, { text: "  " + ver, options: { fontSize: 9.5 } }] : nm, {
      x: x + 0.12, y: y + 0.28, w: w - 0.24, h: 0.34, margin: 0, valign: "middle",
      fontSize: 11.5, bold: true, color: ours ? WHITE : INK, isTextBox: true });
    x += w + 0.14;
  });
  s.addText(note, { x: Math.max(x + 0.1, 6.6), y: y + 0.3, w: 12.7 - Math.max(x + 0.1, 6.6), h: 0.32,
    margin: 0, valign: "middle", fontSize: 11, color: MUTED, isTextBox: true });
});

s.addText("Platform underneath: Docker and Docker Compose on Windows with WSL2 — 29 containers from one command. PowerShell scripts start, stop, check and inject faults.",
  { x: 0.6, y: 6.96, w: 12.1, h: 0.3, margin: 0, fontSize: 11.5, color: MUTED, isTextBox: true });
s.addNotes("Point out the shading: almost everything below our backend is standard tooling we reuse rather than reinvent. Our contribution is the alert rules, the backend with its five agents, and the console.");

/* ================================================================ 5 DETECTION */
pres.addSection({ title: S("Detection") });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("Detection") });
s.addText("Detection: four rules, written from the live system", { placeholder: "title" });
s.addText("We listed the 440 metric names Prometheus actually had, rather than guessing them.", { placeholder: "kicker" });

s.addTable([
  [{ text: "Scenario injected", options: th }, { text: "Rule that fires", options: th },
   { text: "Detected in", options: th }, { text: "Resolves after fix", options: th }],
  ["High CPU (ad service)", "AiraHighCpu", "70-80 s", "~74 s"],
  ["Service stopped", "AiraServiceDown", "84-89 s", "~31 s"],
  ["Payment failure", "AiraOperationFailing", "119-153 s", "~2-4 min"],
  ["Product-catalog failure", "AiraHighErrorRate", "~117 s", "~90 s"],
], { x: 0.6, y: 1.8, w: 7.3, colW: [2.5, 2.0, 1.4, 1.4], rowH: 0.42, ...tblBase });

card(s, { x: 8.25, y: 1.8, w: 4.45, h: 1.3, head: "No `up` metric exists",
  body: "Services push telemetry, so nothing is scraped. We detect a stopped service by its container metrics going silent.",
  accent: AMBER, fill: "FDF6EA", bodySize: 12 });
card(s, { x: 8.25, y: 3.25, w: 4.45, h: 1.3, head: "Docker's CPU gauge was wrong",
  body: "It read 15% while the real value was 400%. We use the CPU-time counter instead.",
  accent: AMBER, fill: "FDF6EA", bodySize: 12 });

s.addText("Why detection takes 70-150 seconds, by design", { x: 0.6, y: 4.1, w: 7.3, h: 0.3,
  margin: 0, fontSize: 14, bold: true, color: INK, isTextBox: true });
s.addText("The rule averages over a window instead of reacting to one failed request. Payment receives about one request every 16 seconds, so a shorter window would produce false alarms. The delay buys reliability: across every test run there were no spurious alerts.",
  { x: 0.6, y: 4.45, w: 7.3, h: 0.95, margin: 0, fontSize: 12.5, color: MUTED, isTextBox: true, lineSpacingMultiple: 1.1 });

s.addShape(pres.ShapeType.roundRect, { x: 0.6, y: 5.5, w: 12.1, h: 0.95, rectRadius: 0.06,
  fill: "F6F5FA", line: { color: LINE, width: 0.75 }, objectName: "fp" });
s.addText("Two false positives found and fixed", { x: 0.9, y: 5.63, w: 3.6, h: 0.3, margin: 0,
  fontSize: 13, bold: true, color: CRIMSON, isTextBox: true });
s.addText("Streaming connections ended in error whenever a client reconnected, and the error-rate rule flapped on low-traffic services - one fault produced a duplicate incident every two minutes. Both are fixed and verified.",
  { x: 4.6, y: 5.63, w: 7.8, h: 0.68, margin: 0, fontSize: 12, color: INK, isTextBox: true, lineSpacingMultiple: 1.08 });
s.addNotes("The table is measured, not estimated - from docs/measurements. If asked why not faster: a one-request trigger on a 0.06 req/s service is a false-alarm generator.");

/* ================================================================ 6 THE AGENTS */
pres.addSection({ title: S("The agents") });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("The agents") });
s.addText("Five agents, and what each one is for", { placeholder: "title" });
s.addText("The numbers are never produced by a model - deterministic tools fetch them, the agent interprets.", { placeholder: "kicker" });

const agents = [
  ["1", "Triage", "the alert + metrics evidence", "Is this real, and what kind of failure?", "category + confidence", "7-14 s", VIOLET],
  ["2", "Orchestrator", "alert signal + triage category", "Which investigators does THIS incident need?", "a plan, with skip reasons", "rules", VIOLET],
  ["3", "Metrics", "13 measured values + baselines", "Which numbers differ from this service's own normal?", "abnormal findings", "6-10 s", VIOLET2],
  ["4", "Log", "OpenSearch + container stdout", "Do any log lines explain this alert - or are there none?", "quoted lines, verbatim", "4-7 s", VIOLET2],
  ["5", "Change", "flags, restarts, dependency graph", "Did something change that could cause this, here or upstream?", "likely cause", "5-9 s", CRIMSON],
];
s.addTable([
  [{ text: "", options: { ...th, fill: { color: INK } } }, { text: "Agent", options: th },
   { text: "Receives", options: th }, { text: "Decides", options: th },
   { text: "Produces", options: th }, { text: "Time", options: th }],
  ...agents.map(([n, name, inp, dec, out, t, c]) => [
    { text: n, options: { bold: true, color: c, align: "center" } },
    { text: name, options: { bold: true, color: INK } },
    { text: inp, options: { fontSize: 11.5, color: MUTED } },
    { text: dec, options: { fontSize: 11.5 } },
    { text: out, options: { fontSize: 11.5, bold: name === "Change", color: name === "Change" ? CRIMSON : INK } },
    { text: t, options: { fontSize: 11.5, color: MUTED, align: "center" } },
  ]),
], { x: 0.6, y: 1.78, w: 12.1, colW: [0.42, 1.45, 2.85, 3.7, 2.38, 1.3], rowH: 0.52, ...tblBase });

s.addShape(pres.ShapeType.roundRect, { x: 0.6, y: 5.3, w: 5.9, h: 1.15, rectRadius: 0.06,
  fill: "FBEEF0", line: { color: CRIMSON, width: 1 }, objectName: "cause" });
s.addText("Only the Change agent can name a cause", { x: 0.88, y: 5.44, w: 5.4, h: 0.3, margin: 0,
  fontSize: 13.5, bold: true, color: CRIMSON, isTextBox: true });
s.addText("The others describe symptoms. Change is the one that correlates a configuration change with the failing service.",
  { x: 0.88, y: 5.76, w: 5.4, h: 0.55, margin: 0, fontSize: 12, color: INK, isTextBox: true, lineSpacingMultiple: 1.08 });

s.addShape(pres.ShapeType.roundRect, { x: 6.8, y: 5.3, w: 5.9, h: 1.15, rectRadius: 0.06,
  fill: "FDF6EA", line: { color: AMBER, width: 1 }, objectName: "gap" });
s.addText("What they do not yet do", { x: 7.08, y: 5.44, w: 5.4, h: 0.3, margin: 0,
  fontSize: 13.5, bold: true, color: "A8751F", isTextBox: true });
s.addText("Nothing reads all four outputs together and ranks a root cause. A human still joins those dots - that is the RCA and Critic phase.",
  { x: 7.08, y: 5.76, w: 5.4, h: 0.55, margin: 0, fontSize: 12, color: INK, isTextBox: true, lineSpacingMultiple: 1.08 });
s.addNotes("Be straight about the amber box before they ask. Saying it first reads as judgement; being caught reads as overclaiming.");

/* ================================================================ 7 ORCHESTRATOR */
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("The agents") });
s.addText("The Orchestrator plans - it does not run everything", { placeholder: "title" });
s.addText("Which investigators run depends on the incident, and every skip records its reason.", { placeholder: "kicker" });

s.addTable([
  [{ text: "Fault injected", options: th }, { text: "Alert", options: th },
   { text: "Agents run", options: th }, { text: "Skipped, and why", options: th },
   { text: "Total", options: th }],
  ["High CPU", "AiraHighCpu", "Metrics, Change",
   { text: "Log - CPU saturation is a resource problem; logs rarely explain it", options: { color: "A8751F", fontSize: 11.5 } }, "21.1 s"],
  ["Payment failure", "AiraOperationFailing", "Metrics, Log, Change",
   { text: "none - an error alert needs every source", options: { color: MUTED, fontSize: 11.5 } }, "27.0 s"],
  ["Service stopped", "AiraServiceDown", "Metrics, Change",
   { text: "Log - the service stopped logging before the incident began", options: { color: "A8751F", fontSize: 11.5 } }, "20.1 s"],
], { x: 0.6, y: 1.8, w: 12.1, colW: [1.9, 2.2, 2.4, 4.3, 1.3], rowH: 0.6, ...tblBase });

card(s, { x: 0.6, y: 4.1, w: 3.9, h: 1.55, head: "Visible on screen",
  body: "A skipped agent appears greyed and dashed on the console, with its reason on hover. The plan is shown, not implied.",
  accent: VIOLET });
card(s, { x: 4.7, y: 4.1, w: 3.9, h: 1.55, head: "Rules, not another model call",
  body: "The choice must be explainable and repeatable, and an extra call would cost ~7 s of a ~25 s budget to reproduce four rules.",
  accent: VIOLET });
card(s, { x: 8.8, y: 4.1, w: 3.9, h: 1.55, head: "Parallel evidence",
  body: "All sources are fetched concurrently in 0.6-1.9 s. Only the reasoning is serial, because one local model serves every agent.",
  accent: VIOLET });

s.addText("Three fault types, three different plans - verified end to end, 3/3.", {
  x: 0.6, y: 5.9, w: 12.1, h: 0.35, margin: 0, fontSize: 14, bold: true, color: VIOLET,
  isTextBox: true, fontFace: "Cambria" });
s.addNotes("This is the slide that answers 'what is agentic about it?'. It selects per incident rather than running a fixed pipeline - and records the reasoning.");

/* ================================================================ 8 WORKED EXAMPLE */
pres.addSection({ title: S("Worked example") });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("Worked example") });
s.addText("One flag, two incidents, a cause found across a boundary", { placeholder: "title" });
s.addText("The alert is on checkout. The fault is on payment. The Change agent crosses that gap.", { placeholder: "kicker" });

const FY = 1.78;
flowBox(s, { x: 0.6, y: FY, w: 2.3, h: 0.72, label: "flagd", sub: "paymentFailure = 100%" });
arrowR(s, 3.0, FY + 0.27);
flowBox(s, { x: 3.4, y: FY, w: 2.3, h: 0.72, label: "payment", sub: "Charge throws \"Invalid token\"",
  fill: "FBEEF0", border: CRIMSON, labelColor: CRIMSON });
arrowR(s, 5.8, FY + 0.27, 0.32, CRIMSON);
flowBox(s, { x: 6.2, y: FY, w: 2.3, h: 0.72, label: "checkout", sub: "PlaceOrder fails too",
  fill: "FBEEF0", border: CRIMSON, labelColor: CRIMSON });
arrowR(s, 8.6, FY + 0.27, 0.32, CRIMSON);
flowBox(s, { x: 9.0, y: FY, w: 2.3, h: 0.72, label: "shop", sub: "HTTP 422 at the browser" });
s.addText("checkout calls payment - read from the container's own PAYMENT_ADDR variable, not assumed",
  { x: 0.6, y: 2.56, w: 12.1, h: 0.28, margin: 0, fontSize: 11.5, italic: true, color: MUTED, isTextBox: true });

// two incident cards
s.addShape(pres.ShapeType.roundRect, { x: 0.6, y: 2.98, w: 5.9, h: 3.4, rectRadius: 0.06,
  fill: WHITE, line: { color: LINE, width: 1 }, objectName: "incA" });
s.addShape(pres.ShapeType.rect, { x: 0.6, y: 2.98, w: 5.9, h: 0.45, fill: "F4F2FA", line: { color: LINE, width: 1 }, objectName: "incAh" });
s.addText("Incident A  ·  payment  -  the service that broke", { x: 0.85, y: 3.05, w: 5.4, h: 0.3,
  margin: 0, fontSize: 13, bold: true, color: INK, isTextBox: true });
[["Metrics", "error ratio 100.0% (baseline 0.0%) · p95 40 ms (baseline 5.6 ms)"],
 ["Log", "\"Payment request failed. Invalid token.\" seen 66 times"],
].forEach(([h, b], i) => {
  const y = 3.56 + i * 0.75;
  s.addText(h, { x: 0.85, y, w: 1.1, h: 0.26, margin: 0, fontSize: 12.5, bold: true, color: VIOLET, isTextBox: true });
  s.addText(b, { x: 0.85, y: y + 0.26, w: 5.4, h: 0.44, margin: 0, fontSize: 11.5, color: INK, isTextBox: true, lineSpacingMultiple: 1.05 });
});
s.addShape(pres.ShapeType.roundRect, { x: 0.85, y: 5.12, w: 5.4, h: 0.82, rectRadius: 0.05,
  fill: "FBEEF0", line: { color: CRIMSON, width: 1 }, objectName: "causeA" });
s.addText("LIKELY CAUSE", { x: 1.05, y: 5.22, w: 1.5, h: 0.24, margin: 0, fontSize: 10, bold: true, color: CRIMSON, isTextBox: true });
s.addText("paymentFailure flag set to 100%", { x: 1.05, y: 5.46, w: 5.0, h: 0.26, margin: 0, fontSize: 12.5, bold: true, color: INK, isTextBox: true });
s.addText("affects 'payment', which is this service", { x: 1.05, y: 5.7, w: 5.0, h: 0.22, margin: 0, fontSize: 10.5, color: MUTED, isTextBox: true });
s.addText("All four agents ran  ·  28.1 s", { x: 0.85, y: 6.02, w: 5.4, h: 0.26, margin: 0, fontSize: 11, color: MUTED, isTextBox: true });

s.addShape(pres.ShapeType.roundRect, { x: 6.8, y: 2.98, w: 5.9, h: 3.4, rectRadius: 0.06,
  fill: WHITE, line: { color: CRIMSON, width: 1.25 }, objectName: "incB" });
s.addShape(pres.ShapeType.rect, { x: 6.8, y: 2.98, w: 5.9, h: 0.45, fill: "FBEEF0", line: { color: CRIMSON, width: 1.25 }, objectName: "incBh" });
s.addText("Incident B  ·  checkout  -  collateral damage", { x: 7.05, y: 3.05, w: 5.4, h: 0.3,
  margin: 0, fontSize: 13, bold: true, color: INK, isTextBox: true });
[["Metrics", "error ratio 100.0% · failing op CheckoutService/PlaceOrder"],
 ["Log", "\"No logs available for this service.\" - stated, not guessed"],
].forEach(([h, b], i) => {
  const y = 3.56 + i * 0.75;
  s.addText(h, { x: 7.05, y, w: 1.1, h: 0.26, margin: 0, fontSize: 12.5, bold: true, color: VIOLET, isTextBox: true });
  s.addText(b, { x: 7.05, y: y + 0.26, w: 5.4, h: 0.44, margin: 0, fontSize: 11.5, color: INK, isTextBox: true, lineSpacingMultiple: 1.05 });
});
s.addShape(pres.ShapeType.roundRect, { x: 7.05, y: 5.12, w: 5.4, h: 0.82, rectRadius: 0.05,
  fill: "FBEEF0", line: { color: CRIMSON, width: 1 }, objectName: "causeB" });
s.addText("LIKELY CAUSE", { x: 7.25, y: 5.22, w: 1.5, h: 0.24, margin: 0, fontSize: 10, bold: true, color: CRIMSON, isTextBox: true });
s.addText("paymentFailure flag set to 100%", { x: 7.25, y: 5.46, w: 5.0, h: 0.26, margin: 0, fontSize: 12.5, bold: true, color: INK, isTextBox: true });
s.addText("affects 'payment', a service this one CALLS", { x: 7.25, y: 5.7, w: 5.0, h: 0.22, margin: 0, fontSize: 10.5, bold: true, color: CRIMSON, isTextBox: true });
s.addText("Nothing is wrong inside checkout  ·  27.0 s", { x: 7.05, y: 6.02, w: 5.4, h: 0.26, margin: 0, fontSize: 11, color: MUTED, isTextBox: true });
s.addNotes("This is the strongest moment in the whole demo. Let it land: the alert fires on checkout, nothing inside checkout is broken, and the agent still names the upstream flag.");

/* ================================================================ 9 RIGOUR */
pres.addSection({ title: S("Rigour") });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("Rigour") });
s.addText("Bugs we found in our own system, and fixed", { placeholder: "title" });
s.addText("Every one was caught by testing against the live system, and every one now has a regression check.", { placeholder: "kicker" });

s.addTable([
  [{ text: "What went wrong", options: th }, { text: "Why", options: th }, { text: "The fix", options: th }],
  ["The agent invented a percentage",
   "Our own prompt contained a realistic example number - the model copied the example, not the data",
   "All example values are placeholders now"],
  ["\"No changes detected\" while a flag was set to 100%",
   "Stronger prompt wording was not enough on a 7B model",
   "A code check rejects that answer and forces a retry"],
  ["A payment flag blamed for a cart alert",
   "The model was guessing which services call which",
   "The dependency graph is read from container environment variables"],
  ["Incidents stuck \"firing\" after a restart",
   "Resolved webhooks were lost while the backend was down",
   "The backend reconciles against Alertmanager on startup"],
], { x: 0.6, y: 1.8, w: 12.1, colW: [3.5, 4.8, 3.8], rowH: 0.72, ...tblBase, fontSize: 11.5 });

s.addShape(pres.ShapeType.roundRect, { x: 0.6, y: 5.0, w: 12.1, h: 1.45, rectRadius: 0.06,
  fill: INK, line: { color: INK }, objectName: "lesson" });
s.addText("The lesson: prompting is not a guarantee - validation is", { x: 0.95, y: 5.15, w: 11.4,
  h: 0.35, margin: 0, fontSize: 17, bold: true, color: WHITE, isTextBox: true, fontFace: "Cambria" });
s.addText("Where better wording failed, we added checks in code that reject an answer contradicting its own evidence. A separate tool re-runs every agent and verifies that each number and each quoted log line appears in the input it was given - and that tool is itself tested against a deliberately fabricated quote, so it cannot pass by doing nothing.",
  { x: 0.95, y: 5.54, w: 11.4, h: 0.75, margin: 0, fontSize: 12.5, color: "C9C2DE", isTextBox: true, lineSpacingMultiple: 1.1 });
s.addNotes("Spend time here. Feature count is common; finding and fixing your own model's failures is not. The 4.3% story lands well - it was our prompt's fault, not the model's.");

/* ================================================================ 10 RESULTS */
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("Rigour") });
s.addText("What we can measure", { placeholder: "title" });
s.addText("Every figure below came from a real injected fault on this laptop, not an estimate.", { placeholder: "kicker" });

[["5", "agents built\nof the 8 planned", VIOLET],
 ["20-27s", "full investigation\nagainst a 45 s budget", VIOLET2],
 ["3/3", "fault scenarios passed\nend to end", GREEN],
 ["15/15", "regression checks\none per bug found", GREEN],
].forEach(([v, l, c], i) => {
  const x = 0.6 + i * 3.1;
  s.addShape(pres.ShapeType.roundRect, { x, y: 1.8, w: 2.85, h: 1.55, rectRadius: 0.06,
    fill: LILAC, line: { color: LINE, width: 0.75 }, objectName: `stat${i}` });
  s.addText(v, { x: x + 0.2, y: 1.95, w: 2.45, h: 0.68, margin: 0, fontSize: 34, bold: true,
    color: c, isTextBox: true, fontFace: "Cambria" });
  s.addText(l, { x: x + 0.2, y: 2.62, w: 2.45, h: 0.6, margin: 0, fontSize: 11.5, color: MUTED,
    isTextBox: true, lineSpacingMultiple: 1.05 });
});

s.addTable([
  [{ text: "Measure", options: th }, { text: "Result", options: th }, { text: "Note", options: th }],
  ["Agent output valid on first attempt", "~25/25 runs", "schema-validated; one retry available, rarely needed"],
  ["Invented numbers or quotes", "0 across 16 checked runs", "verified by a tool tested against a fabricated quote"],
  ["Evidence gathered in parallel", "0.6-1.9 s for three sources", "Prometheus, OpenSearch and Docker concurrently"],
  ["Cold start, whole stack", "116 s, one command", "29 containers, model warmed, health verified"],
  ["Cost to run", "zero", "qwen2.5:7b on the laptop GPU; nothing leaves the machine"],
], { x: 0.6, y: 3.62, w: 12.1, colW: [4.0, 3.0, 5.1], rowH: 0.46, ...tblBase });

s.addText("An incident we did not plan: Grafana spiked its own CPU above a full core overnight, and AIRA investigated it unattended.",
  { x: 0.6, y: 6.2, w: 12.1, h: 0.45, margin: 0, fontSize: 12.5, italic: true, color: VIOLET, isTextBox: true });
s.addNotes("The unplanned Grafana incident is worth more than any staged scenario - it ran with nobody watching. Mention it last so it sticks.");

/* ================================================================ 11 LIMITS */
pres.addSection({ title: S("Honest status") });
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("Honest status") });
s.addText("What is not built yet", { placeholder: "title" });
s.addText("Stated before being asked - each gap is the next phase, not an oversight.", { placeholder: "kicker" });

[["The agents do not reason over each other's findings",
  "Each investigates independently and writes to a shared context. Nothing yet combines four findings into one ranked root cause. A human joins the dots.", CRIMSON],
 ["AIRA takes no actions",
  "It detects, investigates and diagnoses. We clear the fault; AIRA then verifies recovery from the metrics. No remediation exists yet.", CRIMSON],
 ["Two services produce no logs at all",
  "checkout and product-catalog write nothing to either source, so the Log agent can only report their absence - honestly, but with nothing to add.", AMBER],
 ["Reasoning is serial, not parallel",
  "One local model serves every agent, and 1.4 GB of spare GPU memory cannot hold a second. The evidence gathering is genuinely parallel; the thinking is not.", AMBER],
].forEach(([h, b, c], i) => {
  const y = 1.85 + i * 1.12;
  s.addShape(pres.ShapeType.roundRect, { x: 0.6, y, w: 12.1, h: 0.98, rectRadius: 0.06,
    fill: c === CRIMSON ? "FBEEF0" : "FDF6EA", line: { color: c, width: 1 }, objectName: `lim${i}` });
  s.addText(h, { x: 0.9, y: y + 0.12, w: 11.5, h: 0.3, margin: 0, fontSize: 14, bold: true,
    color: c === CRIMSON ? CRIMSON : "A8751F", isTextBox: true });
  s.addText(b, { x: 0.9, y: y + 0.44, w: 11.5, h: 0.45, margin: 0, fontSize: 12, color: INK,
    isTextBox: true, lineSpacingMultiple: 1.06 });
});

s.addText("Saying this first is the point: every one of these is scoped work with a plan behind it.",
  { x: 0.6, y: 6.5, w: 12.1, h: 0.3, margin: 0, fontSize: 12.5, italic: true, color: MUTED, isTextBox: true });
s.addNotes("Do not rush this slide. Volunteering limitations reads as judgement; being caught by them reads as overclaiming.");

/* ================================================================ 12 ROADMAP */
s = pres.addSlide({ masterName: "CONTENT", sectionTitle: S("Honest status") });
s.addText("What remains, and in what order", { placeholder: "title" });
s.addText("Roughly 55-60% of the full project. The remaining phases build on what exists rather than replacing it.", { placeholder: "kicker" });

const phases = [
  ["DONE", "Monitoring, detection, alerting", "28-container app, 4 rules, Alertmanager, webhook pipeline", GREEN, "F0F6F1"],
  ["DONE", "Triage + Orchestrator + 3 investigators", "Dynamic planning, shared incident context, live console", GREEN, "F0F6F1"],
  ["NEXT", "RCA + Critic + replanning", "Combine the findings into ranked hypotheses; the Critic challenges them and can send the incident back. Our synopsis calls this the central contribution.", VIOLET, "EDE7F6"],
  ["THEN", "Remediation + verification", "An allowlist of safe, reversible actions with human approval, then confirming recovery from live metrics.", VIOLET2, "F4F2FA"],
  ["THEN", "Qdrant memory + evaluation + React dashboard", "Retrieve similar past incidents; measure AIRA against a rule baseline, a single LLM, and AIRA without the Critic.", MUTED, "F6F5FA"],
];
phases.forEach(([tag, head, body, c, fill], i) => {
  const y = 1.85 + i * 0.95;
  s.addShape(pres.ShapeType.roundRect, { x: 0.6, y, w: 12.1, h: 0.82, rectRadius: 0.06,
    fill, line: { color: LINE, width: 0.75 }, objectName: `ph${i}` });
  s.addShape(pres.ShapeType.roundRect, { x: 0.8, y: y + 0.19, w: 0.82, h: 0.33, rectRadius: 0.05,
    fill: { color: c }, line: { color: c }, objectName: `tag${i}` });
  s.addText(tag, { x: 0.8, y: y + 0.19, w: 0.82, h: 0.33, margin: 0, align: "center", valign: "middle",
    fontSize: 9.5, bold: true, color: WHITE, isTextBox: true });
  s.addText(head, { x: 1.78, y: y + 0.12, w: 4.2, h: 0.3, margin: 0, fontSize: 13.5, bold: true,
    color: INK, isTextBox: true });
  s.addText(body, { x: 6.1, y: y + 0.1, w: 6.4, h: 0.62, margin: 0, fontSize: 11.5, color: MUTED,
    isTextBox: true, lineSpacingMultiple: 1.04 });
});
s.addText("The React dashboard is the one deliverable we would cut first: the current console works and is labelled honestly as a preview.",
  { x: 0.6, y: 6.75, w: 12.1, h: 0.3, margin: 0, fontSize: 11.5, italic: true, color: MUTED, isTextBox: true });
s.addNotes("If asked what you would drop under time pressure: React. The agentic layer matters more than the frontend, and that is a defensible engineering answer.");

/* ================================================================ 13 CLOSING */
s = pres.addSlide({ masterName: "CLOSING", sectionTitle: S("Honest status") });
s.addText("What we can claim at mid-semester", { placeholder: "title" });

[["A real fault becomes an investigated incident, automatically",
  "Detection, evidence gathering from three different systems, and a diagnosis - with no human in the loop until the fix."],
 ["The system plans its own investigation",
  "A CPU alert skips the Log agent and records why. Three fault types produced three different plans."],
 ["It traced a cause across a service boundary",
  "The alert fired on checkout; the fault was a flag on payment. The agent named it, using a dependency graph read from the running containers."],
 ["Everything is local, and everything is checkable",
  "One 7B model on the laptop GPU, zero cost, and a tool that verifies no number or quoted line was ever invented."],
].forEach(([h, b], i) => {
  const y = 1.75 + i * 1.16;
  s.addShape(pres.ShapeType.ellipse, { x: 0.85, y: y + 0.06, w: 0.34, h: 0.34,
    fill: { color: VIOLET2 }, line: { color: VIOLET2 }, objectName: `chk${i}` });
  s.addText(String(i + 1), { x: 0.85, y: y + 0.06, w: 0.34, h: 0.34, margin: 0, align: "center",
    valign: "middle", fontSize: 12, bold: true, color: WHITE, isTextBox: true });
  s.addText(h, { x: 1.4, y, w: 11.2, h: 0.34, margin: 0, fontSize: 16, bold: true, color: WHITE,
    isTextBox: true, fontFace: "Cambria" });
  s.addText(b, { x: 1.4, y: y + 0.38, w: 11.2, h: 0.6, margin: 0, fontSize: 12.5, color: "C9C2DE",
    isTextBox: true, lineSpacingMultiple: 1.08 });
});

s.addShape(pres.ShapeType.rect, { x: 0.85, y: 6.5, w: 1.4, h: 0.05, fill: { color: AMBER }, objectName: "endrule" });
s.addText("Next: the RCA and Critic agents - combining these findings into one ranked root cause, and challenging it.",
  { x: 0.85, y: 6.68, w: 11.7, h: 0.35, margin: 0, fontSize: 13, color: AMBER, isTextBox: true });
s.addNotes("Close on the cascade claim - it is the one thing that is hard to dismiss as a wrapper around a dashboard. Then hand over to the live demo.");

/* ---------------------------------------------------------------- write */
(async () => {
  const out = "AIRA-MidSem-Presentation.pptx";
  await pres.writeFile({ fileName: out });
  await applyTheme(out, THEME);
  console.log("wrote", out);
})();
