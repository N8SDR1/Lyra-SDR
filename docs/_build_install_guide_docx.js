// Build Lyra-SDR Install Guide DOCX
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, Table, TableRow, TableCell,
  Header, Footer, AlignmentType, PageOrientation, LevelFormat,
  ExternalHyperlink, HeadingLevel, BorderStyle, WidthType,
  ShadingType, PageNumber,
} = require(path.join(
  process.env.APPDATA, "npm", "node_modules", "docx"
));

// ----- helpers -----------------------------------------------------------
const ARIAL = "Arial";
const MONO = "Consolas";

function P(text, opts = {}) {
  return new Paragraph({
    spacing: { after: 120 },
    ...opts,
    children: [
      new TextRun({ text, font: ARIAL, size: 22, ...(opts.run || {}) }),
    ],
  });
}

function PMixed(runs, opts = {}) {
  return new Paragraph({
    spacing: { after: 120 },
    ...opts,
    children: runs,
  });
}

function H1(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_1,
    spacing: { before: 240, after: 160 },
    children: [new TextRun({ text, font: ARIAL, size: 36, bold: true })],
  });
}

function H2(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_2,
    spacing: { before: 220, after: 140 },
    children: [new TextRun({ text, font: ARIAL, size: 30, bold: true })],
  });
}

function H3(text) {
  return new Paragraph({
    heading: HeadingLevel.HEADING_3,
    spacing: { before: 180, after: 100 },
    children: [new TextRun({ text, font: ARIAL, size: 26, bold: true })],
  });
}

function Code(line) {
  // Monospace, light grey background, full content width
  return new Paragraph({
    spacing: { before: 60, after: 60 },
    shading: { type: ShadingType.CLEAR, fill: "F2F2F2", color: "auto" },
    indent: { left: 360 },
    children: [
      new TextRun({ text: line, font: MONO, size: 20 }),
    ],
  });
}

function CodeBlock(lines) {
  return lines.map((l) => Code(l));
}

function Bullet(text) {
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { after: 80 },
    children: [new TextRun({ text, font: ARIAL, size: 22 })],
  });
}

function BulletMixed(runs) {
  return new Paragraph({
    numbering: { reference: "bullets", level: 0 },
    spacing: { after: 80 },
    children: runs,
  });
}

function Numbered(text) {
  return new Paragraph({
    numbering: { reference: "numbers", level: 0 },
    spacing: { after: 80 },
    children: [new TextRun({ text, font: ARIAL, size: 22 })],
  });
}

function NumberedMixed(runs) {
  return new Paragraph({
    numbering: { reference: "numbers", level: 0 },
    spacing: { after: 80 },
    children: runs,
  });
}

function Link(text, url) {
  return new ExternalHyperlink({
    children: [new TextRun({ text, style: "Hyperlink", font: ARIAL, size: 22 })],
    link: url,
  });
}

function HR() {
  return new Paragraph({
    spacing: { before: 120, after: 120 },
    border: {
      bottom: {
        color: "BFBFBF", style: BorderStyle.SINGLE, size: 6, space: 1,
      },
    },
    children: [new TextRun({ text: "" })],
  });
}

// ----- gotchas table -----------------------------------------------------
const GOT_BORDER = {
  style: BorderStyle.SINGLE, size: 4, color: "B0B0B0",
};
const GOT_BORDERS = {
  top: GOT_BORDER, bottom: GOT_BORDER, left: GOT_BORDER, right: GOT_BORDER,
};

function gotchaCell(text, width, isHeader = false) {
  return new TableCell({
    borders: GOT_BORDERS,
    width: { size: width, type: WidthType.DXA },
    shading: isHeader
      ? { fill: "D9E2F3", type: ShadingType.CLEAR, color: "auto" }
      : undefined,
    margins: { top: 100, bottom: 100, left: 140, right: 140 },
    children: [
      new Paragraph({
        children: [
          new TextRun({
            text, font: ARIAL, size: 22, bold: isHeader,
          }),
        ],
      }),
    ],
  });
}

function gotchaRow(symptom, fix, isHeader = false) {
  return new TableRow({
    tableHeader: isHeader,
    children: [
      gotchaCell(symptom, 3360, isHeader),
      gotchaCell(fix, 6000, isHeader),
    ],
  });
}

const gotchas = [
  ["'python' is not recognized",
    "Python wasn’t added to PATH. Reinstall and check that box."],
  ["'pip' is not recognized",
    "Same — reinstall Python with PATH option."],
  ["ModuleNotFoundError: No module named 'PySide6'",
    "You skipped the pip install step. Run it now."],
  ["ftd2xx fails to install",
    "Skip it — install the others without ftd2xx."],
  ["Windows firewall popup on first launch",
    "Allow it. Lyra needs UDP to talk to the HL2."],
  ["“No radio found”",
    "Check HL2 power, network cable, and that no other client (Thetis, SparkSDR) is connected at the same time."],
  ["Audio works in other apps but not Lyra",
    "Switch the “Out” combo on the DSP+Audio panel between HL2 audio jack and PC Soundcard. Most operators use PC Soundcard."],
];

const gotchaTable = new Table({
  width: { size: 9360, type: WidthType.DXA },
  columnWidths: [3360, 6000],
  rows: [
    gotchaRow("Symptom", "Fix", true),
    ...gotchas.map(([s, f]) => gotchaRow(s, f)),
  ],
});

// ----- document body -----------------------------------------------------
const children = [];

// Title
children.push(new Paragraph({
  alignment: AlignmentType.LEFT,
  spacing: { after: 200 },
  children: [new TextRun({
    text: "Lyra-SDR — Install Guide for Windows",
    font: ARIAL, size: 44, bold: true,
  })],
}));

children.push(P("A Qt6/PySide6 desktop SDR transceiver for the Hermes Lite 2 / 2+."));

children.push(PMixed([
  new TextRun({ text: "Repo: ", font: ARIAL, size: 22 }),
  Link("https://github.com/N8SDR1/Lyra-SDR", "https://github.com/N8SDR1/Lyra-SDR"),
]));

children.push(P("License: GPL v3 or later (since v0.0.6)"));
children.push(P("Authors: Rick Langford (N8SDR), Brent Crier (N9BC), Timmy Davis (KC8TYK)"));

children.push(P(
  "This guide is written for “I have Windows and I sort of know what a terminal is” — not for Python developers. If you can copy and paste, you can install Lyra."
));

children.push(HR());

// Prerequisites
children.push(H2("Prerequisites (one-time setup)"));

children.push(H3("1. Install Python 3.11 or newer"));
children.push(BulletMixed([
  new TextRun({ text: "Download from ", font: ARIAL, size: 22 }),
  Link("https://www.python.org/downloads/", "https://www.python.org/downloads/"),
]));
children.push(Bullet("Run the installer."));
children.push(BulletMixed([
  new TextRun({
    text: "CRITICAL: Check the box “Add python.exe to PATH” before clicking Install. ",
    font: ARIAL, size: 22, bold: true,
  }),
  new TextRun({
    text: "If you miss this, every command below fails. You can re-run the installer if needed.",
    font: ARIAL, size: 22,
  }),
]));
children.push(Bullet(
  "Verify: open Command Prompt (press Windows key, type “cmd”, Enter), and type:"
));
children.push(...CodeBlock(["python --version"]));
children.push(P("You should see something like “Python 3.11.x” or higher."));

children.push(H3("2. Install Git for Windows"));
children.push(BulletMixed([
  new TextRun({ text: "Download from ", font: ARIAL, size: 22 }),
  Link("https://git-scm.com/download/win", "https://git-scm.com/download/win"),
]));
children.push(Bullet("Run the installer with all defaults."));
children.push(Bullet("Verify: in Command Prompt:"));
children.push(...CodeBlock(["git --version"]));
children.push(P("Should print a version."));
children.push(P(
  "You can skip Git if you’d rather download a zip file — see Option B in the next section."
));

children.push(HR());

// Get the code
children.push(H2("Get the code (one-time)"));

children.push(H3("Option A — with Git (recommended, easier to update later)"));
children.push(P("In Command Prompt:"));
children.push(...CodeBlock([
  "cd %USERPROFILE%\\Documents",
  "git clone https://github.com/N8SDR1/Lyra-SDR.git",
  "cd Lyra-SDR",
]));
children.push(P("This drops the project at C:\\Users\\<you>\\Documents\\Lyra-SDR\\."));

children.push(H3("Option B — without Git (zip download)"));
children.push(NumberedMixed([
  new TextRun({ text: "Visit ", font: ARIAL, size: 22 }),
  Link("https://github.com/N8SDR1/Lyra-SDR", "https://github.com/N8SDR1/Lyra-SDR"),
]));
children.push(Numbered("Click the green “<> Code” button, then “Download ZIP”."));
children.push(Numbered("Unzip to C:\\Users\\<you>\\Documents\\Lyra-SDR\\."));
children.push(Numbered("Open Command Prompt and cd into that folder."));

children.push(HR());

// Install Python deps
children.push(H2("Install Python dependencies (one-time)"));
children.push(P("In the Lyra-SDR folder, the easy way:"));
children.push(...CodeBlock([
  "pip install -r requirements.txt",
]));
children.push(P("Pip downloads about 150 MB of libraries. Takes a minute or two."));
children.push(P("If you hit “permission denied” errors:"));
children.push(...CodeBlock([
  "pip install --user -r requirements.txt",
]));
children.push(P(
  "If ftd2xx specifically fails (no FTDI driver on your machine), it’s optional — only needed for USB-BCD external linear-amp control. To install everything else and skip it:"
));
children.push(...CodeBlock([
  "pip install PySide6 numpy scipy sounddevice websockets",
]));

children.push(HR());

// Run Lyra
children.push(H2("Run Lyra"));
children.push(...CodeBlock(["python -m lyra.ui.app"]));
children.push(P("The Lyra window opens. From there:"));
children.push(Numbered("Make sure your HL2 or HL2+ is powered on and on the same network as your PC."));
children.push(Numbered("Click the “▶ Start” button on the toolbar — discovery should find the radio automatically."));
children.push(Numbered("If discovery fails, use File → Network/TCI to set the radio IP manually."));
children.push(P(
  "That’s it. Press F1 inside Lyra for the in-app User Guide covering operating, AGC, notch filters, the spectrum/waterfall display, TCI integration, and more."
));

children.push(HR());

// Updating later
children.push(H2("Updating later (Git users only)"));
children.push(P("When new commits land in the repo:"));
children.push(...CodeBlock([
  "cd %USERPROFILE%\\Documents\\Lyra-SDR",
  "git pull",
  "pip install -r requirements.txt",
]));
children.push(P(
  "The second pip install only matters when dependencies change — it’s a no-op otherwise, so it’s safe to always run after a pull."
));

children.push(HR());

// Gotchas
children.push(H2("Common gotchas"));
children.push(gotchaTable);

children.push(HR());

// Tester feedback
children.push(H2("Tester feedback"));
children.push(P(
  "When you run into something — a bug, a confusing UI, a missing feature — please open an Issue on the repo:"
));
children.push(PMixed([
  Link("https://github.com/N8SDR1/Lyra-SDR/issues",
       "https://github.com/N8SDR1/Lyra-SDR/issues"),
]));
children.push(P(
  "Include what you tried, what happened, what you expected, and the contents of the Command Prompt window if there was a Python error. Screenshots help."
));
children.push(new Paragraph({
  spacing: { before: 240, after: 120 },
  children: [new TextRun({
    text: "73 from N8SDR.", font: ARIAL, size: 22, italics: true,
  })],
}));

// ----- document --------------------------------------------------------
const doc = new Document({
  creator: "Rick Langford (N8SDR)",
  title: "Lyra-SDR — Install Guide for Windows",
  styles: {
    default: {
      document: { run: { font: ARIAL, size: 22 } },
    },
    paragraphStyles: [
      {
        id: "Heading1", name: "Heading 1", basedOn: "Normal",
        next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, font: ARIAL, color: "1F3864" },
        paragraph: { spacing: { before: 240, after: 160 }, outlineLevel: 0 },
      },
      {
        id: "Heading2", name: "Heading 2", basedOn: "Normal",
        next: "Normal", quickFormat: true,
        run: { size: 30, bold: true, font: ARIAL, color: "1F3864" },
        paragraph: { spacing: { before: 220, after: 140 }, outlineLevel: 1 },
      },
      {
        id: "Heading3", name: "Heading 3", basedOn: "Normal",
        next: "Normal", quickFormat: true,
        run: { size: 26, bold: true, font: ARIAL, color: "2F5496" },
        paragraph: { spacing: { before: 180, after: 100 }, outlineLevel: 2 },
      },
    ],
  },
  numbering: {
    config: [
      {
        reference: "bullets",
        levels: [{
          level: 0, format: LevelFormat.BULLET, text: "•",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
      {
        reference: "numbers",
        levels: [{
          level: 0, format: LevelFormat.DECIMAL, text: "%1.",
          alignment: AlignmentType.LEFT,
          style: { paragraph: { indent: { left: 720, hanging: 360 } } },
        }],
      },
    ],
  },
  sections: [{
    properties: {
      page: {
        size: { width: 12240, height: 15840 },
        margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 },
      },
    },
    headers: {
      default: new Header({
        children: [new Paragraph({
          alignment: AlignmentType.RIGHT,
          children: [new TextRun({
            text: "Lyra-SDR — Install Guide for Windows",
            font: ARIAL, size: 18, color: "808080",
          })],
        })],
      }),
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [
            new TextRun({ text: "Page ", font: ARIAL, size: 18, color: "808080" }),
            new TextRun({ children: [PageNumber.CURRENT], font: ARIAL, size: 18, color: "808080" }),
            new TextRun({ text: " of ", font: ARIAL, size: 18, color: "808080" }),
            new TextRun({ children: [PageNumber.TOTAL_PAGES], font: ARIAL, size: 18, color: "808080" }),
          ],
        })],
      }),
    },
    children,
  }],
});

const outPath = "Y:\\Claude local\\SDRProject\\docs\\Lyra-SDR-Install-Guide.docx";
Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(outPath, buf);
  console.log("Wrote " + outPath + " (" + buf.length + " bytes)");
});
