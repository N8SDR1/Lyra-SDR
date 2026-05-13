"""Build Lyra-SDR Install Guide PDF using reportlab."""

from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT, TA_CENTER, TA_RIGHT
from reportlab.lib.pagesizes import LETTER
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, KeepTogether, PageBreak,
)


OUT = r"Y:\Claude local\SDRProject\docs\Lyra-SDR-Install-Guide.pdf"

# ----- styles -----------------------------------------------------------
NAVY = colors.HexColor("#1F3864")
BLUE = colors.HexColor("#2F5496")
GREY_HDR = colors.HexColor("#D9E2F3")
GREY_RULE = colors.HexColor("#BFBFBF")
CODE_BG = colors.HexColor("#F2F2F2")
CODE_BORDER = colors.HexColor("#E0E0E0")
GREY_TXT = colors.HexColor("#808080")

styles = getSampleStyleSheet()

title_style = ParagraphStyle(
    "Title",
    parent=styles["Title"],
    fontName="Helvetica-Bold",
    fontSize=22,
    textColor=NAVY,
    alignment=TA_LEFT,
    spaceAfter=12,
)

h2_style = ParagraphStyle(
    "H2",
    parent=styles["Heading2"],
    fontName="Helvetica-Bold",
    fontSize=15,
    textColor=NAVY,
    spaceBefore=14,
    spaceAfter=8,
)

h3_style = ParagraphStyle(
    "H3",
    parent=styles["Heading3"],
    fontName="Helvetica-Bold",
    fontSize=12.5,
    textColor=BLUE,
    spaceBefore=10,
    spaceAfter=6,
)

body_style = ParagraphStyle(
    "Body",
    parent=styles["BodyText"],
    fontName="Helvetica",
    fontSize=11,
    leading=15,
    spaceAfter=6,
    alignment=TA_LEFT,
)

bullet_style = ParagraphStyle(
    "Bullet",
    parent=body_style,
    leftIndent=22,
    bulletIndent=8,
    spaceAfter=4,
)

numbered_style = ParagraphStyle(
    "Numbered",
    parent=body_style,
    leftIndent=22,
    bulletIndent=8,
    spaceAfter=4,
)

code_style = ParagraphStyle(
    "Code",
    parent=body_style,
    fontName="Courier",
    fontSize=10,
    leading=13,
    textColor=colors.black,
    backColor=CODE_BG,
    borderColor=CODE_BORDER,
    borderWidth=0.5,
    borderPadding=6,
    leftIndent=18,
    rightIndent=18,
    spaceBefore=4,
    spaceAfter=8,
)

footer_style = ParagraphStyle(
    "Footer",
    fontName="Helvetica",
    fontSize=8,
    textColor=GREY_TXT,
    alignment=TA_CENTER,
)

header_style = ParagraphStyle(
    "Header",
    fontName="Helvetica",
    fontSize=8,
    textColor=GREY_TXT,
    alignment=TA_RIGHT,
)

italic_style = ParagraphStyle(
    "Italic",
    parent=body_style,
    fontName="Helvetica-Oblique",
    spaceBefore=10,
)


# ----- helpers ----------------------------------------------------------
def code_block(lines):
    """Render one or more terminal lines in a single shaded code box."""
    text = "<br/>".join(line.replace(" ", "&nbsp;") for line in lines)
    return Paragraph(text, code_style)


def bullet(text):
    return Paragraph(text, bullet_style, bulletText="•")


def numbered(idx, text):
    return Paragraph(text, numbered_style, bulletText=f"{idx}.")


def hr():
    return HRFlowable(
        width="100%", thickness=0.6, color=GREY_RULE,
        spaceBefore=10, spaceAfter=10,
    )


def link(text, url):
    return f'<link href="{url}" color="#1F6FEB"><u>{text}</u></link>'


# ----- gotchas table ----------------------------------------------------
gotchas_rows = [
    ["Symptom", "Fix"],
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
     "Check HL2 power, network cable, and that no other client "
     "(Thetis, SparkSDR) is connected at the same time."],
    ["Audio works in other apps but not Lyra",
     "Switch the “Out” combo on the DSP+Audio panel between HL2 audio jack "
     "and PC Soundcard. Most operators use PC Soundcard."],
]

cell_style = ParagraphStyle(
    "Cell", parent=body_style, fontSize=10.5, leading=14, spaceAfter=0,
)
cell_hdr_style = ParagraphStyle(
    "CellHdr", parent=cell_style, fontName="Helvetica-Bold",
)

table_data = [
    [Paragraph(gotchas_rows[0][0], cell_hdr_style),
     Paragraph(gotchas_rows[0][1], cell_hdr_style)],
]
for sym, fix in gotchas_rows[1:]:
    table_data.append([
        Paragraph(sym, cell_style),
        Paragraph(fix, cell_style),
    ])

# Content width = 8.5" - 0.75"*2 = 7.0" => 504pt
gotcha_table = Table(
    table_data,
    colWidths=[2.4 * inch, 4.6 * inch],
    repeatRows=1,
    hAlign="LEFT",
)
gotcha_table.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), GREY_HDR),
    ("BOX", (0, 0), (-1, -1), 0.5, colors.HexColor("#B0B0B0")),
    ("INNERGRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B0B0B0")),
    ("VALIGN", (0, 0), (-1, -1), "TOP"),
    ("TOPPADDING", (0, 0), (-1, -1), 6),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ("LEFTPADDING", (0, 0), (-1, -1), 8),
    ("RIGHTPADDING", (0, 0), (-1, -1), 8),
]))


# ----- page decorations -------------------------------------------------
def on_page(canvas, doc):
    canvas.saveState()
    # Header (right-aligned grey)
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GREY_TXT)
    canvas.drawRightString(
        LETTER[0] - 0.75 * inch, LETTER[1] - 0.45 * inch,
        "Lyra-SDR — Install Guide for Windows",
    )
    # Footer (centered "Page X of Y")
    canvas.drawCentredString(
        LETTER[0] / 2.0, 0.45 * inch,
        f"Page {doc.page}",
    )
    canvas.restoreState()


# ----- build flowables --------------------------------------------------
story = []

story.append(Paragraph("Lyra-SDR — Install Guide for Windows", title_style))
story.append(Paragraph(
    "A Qt6/PySide6 desktop SDR transceiver for the Hermes Lite 2 / 2+.",
    body_style,
))
story.append(Paragraph(
    "Repo: " + link("https://github.com/N8SDR1/Lyra-SDR",
                    "https://github.com/N8SDR1/Lyra-SDR"),
    body_style,
))
story.append(Paragraph("License: GPL v3 or later (since v0.0.6)", body_style))
story.append(Paragraph(
    "Authors: Rick Langford (N8SDR), Brent Crier (N9BC), "
    "Timmy Davis (KC8TYK)", body_style))
story.append(Paragraph(
    "This guide is written for “I have Windows and I sort of know what a "
    "terminal is” — not for Python developers. If you can copy and paste, "
    "you can install Lyra.",
    body_style,
))
story.append(hr())

# Prerequisites
story.append(Paragraph("Prerequisites (one-time setup)", h2_style))

story.append(Paragraph("1. Install Python 3.11 or newer", h3_style))
story.append(bullet("Download from " + link(
    "https://www.python.org/downloads/", "https://www.python.org/downloads/"
)))
story.append(bullet("Run the installer."))
story.append(bullet(
    "<b>CRITICAL:</b> Check the box <b>“Add python.exe to PATH”</b> before "
    "clicking Install. If you miss this, every command below fails. "
    "You can re-run the installer if needed."
))
story.append(bullet(
    "Verify: open Command Prompt (press Windows key, type “cmd”, Enter), "
    "and type:"
))
story.append(code_block(["python --version"]))
story.append(Paragraph(
    "You should see something like “Python 3.11.x” or higher.",
    body_style,
))

story.append(Paragraph("2. Install Git for Windows", h3_style))
story.append(bullet("Download from " + link(
    "https://git-scm.com/download/win", "https://git-scm.com/download/win"
)))
story.append(bullet("Run the installer with all defaults."))
story.append(bullet("Verify: in Command Prompt:"))
story.append(code_block(["git --version"]))
story.append(Paragraph("Should print a version.", body_style))
story.append(Paragraph(
    "You can skip Git if you’d rather download a zip file — see Option B "
    "in the next section.",
    body_style,
))
story.append(hr())

# Get the code
story.append(Paragraph("Get the code (one-time)", h2_style))

story.append(Paragraph(
    "Option A — with Git (recommended, easier to update later)", h3_style,
))
story.append(Paragraph("In Command Prompt:", body_style))
story.append(code_block([
    "cd %USERPROFILE%\\Documents",
    "git clone https://github.com/N8SDR1/Lyra-SDR.git",
    "cd Lyra-SDR",
]))
story.append(Paragraph(
    "This drops the project at <font face='Courier'>"
    "C:\\Users\\&lt;you&gt;\\Documents\\Lyra-SDR\\</font>.",
    body_style,
))

story.append(Paragraph("Option B — without Git (zip download)", h3_style))
story.append(numbered(1, "Visit " + link(
    "https://github.com/N8SDR1/Lyra-SDR", "https://github.com/N8SDR1/Lyra-SDR"
)))
story.append(numbered(2, "Click the green “&lt;&gt; Code” button, then "
                          "“Download ZIP”."))
story.append(numbered(3, "Unzip to "
                          "<font face='Courier'>C:\\Users\\&lt;you&gt;\\"
                          "Documents\\Lyra-SDR\\</font>."))
story.append(numbered(4, "Open Command Prompt and "
                          "<font face='Courier'>cd</font> into that folder."))
story.append(hr())

# Install Python deps
story.append(Paragraph("Install Python dependencies (one-time)", h2_style))
story.append(Paragraph("In the Lyra-SDR folder:", body_style))
story.append(code_block([
    "pip install -r requirements.txt",
]))
story.append(Paragraph(
    "This downloads about 150 MB of libraries. Takes a minute or two.",
    body_style,
))
story.append(Paragraph(
    "If you hit “permission denied” errors, try:", body_style,
))
story.append(code_block([
    "pip install --user -r requirements.txt",
]))
story.append(Paragraph(
    "If <font face='Courier'>ftd2xx</font> specifically fails, you can skip "
    "it — that package is only needed for USB-BCD external linear amplifier "
    "control. Most users don’t need it. Run instead:",
    body_style,
))
story.append(code_block([
    "pip install PySide6 numpy scipy sounddevice websockets",
]))
story.append(hr())

# Run Lyra
story.append(Paragraph("Run Lyra", h2_style))
story.append(code_block(["python -m lyra.ui.app"]))
story.append(Paragraph("The Lyra window opens. From there:", body_style))
story.append(numbered(
    1, "Make sure your HL2 or HL2+ is powered on and on the same network "
       "as your PC."
))
story.append(numbered(
    2, "Click the “▶ Start” button on the toolbar — discovery should find "
       "the radio automatically."
))
story.append(numbered(
    3, "If discovery fails, use File → Network/TCI to set the radio IP "
       "manually."
))
story.append(Paragraph(
    "That’s it. Press <b>F1</b> inside Lyra for the in-app User Guide "
    "covering operating, AGC, notch filters, the spectrum/waterfall "
    "display, TCI integration, and more.",
    body_style,
))
story.append(hr())

# Updating later
story.append(Paragraph("Updating later (Git users only)", h2_style))
story.append(Paragraph("When new commits land in the repo:", body_style))
story.append(code_block([
    "cd %USERPROFILE%\\Documents\\Lyra-SDR",
    "git pull",
]))
story.append(Paragraph(
    "The second pip install only matters when dependencies change — "
    "it’s a no-op otherwise, so it’s safe to always run after a pull.",
    body_style,
))
story.append(hr())

# Gotchas
story.append(Paragraph("Common gotchas", h2_style))
story.append(gotcha_table)
story.append(hr())

# Tester feedback
story.append(Paragraph("Tester feedback", h2_style))
story.append(Paragraph(
    "When you run into something — a bug, a confusing UI, a missing "
    "feature — please open an Issue on the repo:",
    body_style,
))
story.append(Paragraph(
    link("https://github.com/N8SDR1/Lyra-SDR/issues",
         "https://github.com/N8SDR1/Lyra-SDR/issues"),
    body_style,
))
story.append(Paragraph(
    "Include what you tried, what happened, what you expected, and the "
    "contents of the Command Prompt window if there was a Python error. "
    "Screenshots help.",
    body_style,
))
story.append(Paragraph("73 from N8SDR.", italic_style))


# ----- build ------------------------------------------------------------
doc = SimpleDocTemplate(
    OUT,
    pagesize=LETTER,
    leftMargin=0.75 * inch,
    rightMargin=0.75 * inch,
    topMargin=0.85 * inch,
    bottomMargin=0.75 * inch,
    title="Lyra-SDR — Install Guide for Windows",
    author="Rick Langford (N8SDR)",
)

doc.build(story, onFirstPage=on_page, onLaterPages=on_page)

import os
print(f"Wrote {OUT} ({os.path.getsize(OUT)} bytes)")
