# Contributor Onboarding Discussion — 2026-05-03

**Forwarded to:** Brent Crier (N9BC) — <https://www.qrz.com/db/N9BC>
**From:** Rick Langford (N8SDR), project owner
**Re:** Joining the Lyra-SDR project as a contributor

---

## Context

This document is a distilled record of a planning conversation
between Rick (N8SDR) and Claude on 2026-05-03, covering:

1. License posture under GPL v3+ for new contributors
2. GitHub collaboration mechanics
3. Honest concerns and ground rules for a two-person project
4. The long-term hardware roadmap (HL2 now → ANAN family eventually)

The intent is for you to read this end-to-end, then come back to
Rick with one of:
- **"Yes, I'm onboard, let's go"** — Rick gets you set up per §2 below.
- **"Yes, with one tweak — can we discuss XXX?"** — write back with
  the discussion item, settle it, then start.
- **"This isn't quite right for me right now"** — no hard feelings,
  the door stays open.

---

## 1. License: GPL v3+ for contributors

Lyra-SDR is licensed **GPL v3 or later** (since v0.0.6). License
file: `LICENSE` at the project root. License history note: v0.0.5
and earlier were MIT — we relicensed specifically to enable
WDSP-derived code integration (WDSP is GPL v3+).

**What contributing under GPL v3+ means for you:**

- You retain copyright on the code you write. You're not signing
  anything over.
- By submitting a PR to the Lyra repo, you license *your
  contribution* under GPL v3+ on the same terms as the rest of
  the project. (This is automatic when you PR against a GPL'd
  repo. No separate paperwork.)
- We use the **Developer Certificate of Origin (DCO)** sign-off
  pattern. Each commit ends with a `Signed-off-by:` line, which
  is shorthand for "I wrote this (or have the right to submit it)
  and I'm licensing it under the project license." Linux kernel
  uses the same pattern. You add it with `git commit -s`.
- We do **not** use a CLA (Contributor License Agreement). CLAs
  are for projects that want to retain dual-license rights for
  commercial relicensing later. Lyra has no such plans.
- No paperwork, no email signatures, no PDFs.

**What you should know about the WDSP / Thetis line before
writing anything:**

This is critical and lives in `CLAUDE.md` §2 in the repo. Quick
version:

- **WDSP source code** (`wdsp/*.c` in the Thetis tree, by Warren
  Pratt NR0V) is GPL v3+. Lyra is GPL-compatible with WDSP, so
  we **may port WDSP source directly** into Lyra (Python or C
  extension) with attribution comments. Already-ported modules
  include `lyra/dsp/nr.py`, `nr2.py`, `lms.py`, `anf.py`. See
  `docs/architecture/wdsp_integration.md` for the attribution
  template.
- **Thetis C# code** (`Console/`) and ChannelMaster C glue
  (`ChannelMaster/*.c`) are also GPL but are protocol/UI scaffolding
  we should write **Lyra-native**. Study the pattern, don't copy
  character-for-character.
- The line: WDSP DSP algorithms = port directly with attribution.
  Everything else = study the pattern, then write Lyra-native.

If you bring in code from another project, the project's license
must be GPL-v3-compatible (MIT, BSD-2/3, Apache 2.0 with
attribution all OK; anything proprietary or with "additional
restrictions" needs a conversation first).

If you've previously contributed to other openHPSDR-family
projects (PowerSDR, Thetis forks, SparkSDR plugins, etc.) and
want to port over an idea, that's fine — same WDSP-vs-Thetis line
applies.

---

## 2. GitHub mechanics

**Repository:** <https://github.com/N8SDR1/Lyra-SDR>

**Setup model:** Collaborator on the main repo with branch
protection on `main` and `feature/threaded-dsp`. Translation:

- You'll be added as a collaborator (Rick invites your GitHub
  username; you accept the email).
- You can push directly to any `feature/<topic>` branch.
- You **can't** push directly to `main` or `feature/threaded-dsp`
  — those require a PR with at least one approval. Rick reviews
  and merges.
- This is the same protection that applies to Rick. Both of us
  go through PRs into the trunk.

**Branch model:**

```
main                       <- published release branch (tagged)
└── feature/threaded-dsp   <- active development trunk
    ├── feature/v0.1-rx2-phase-0
    ├── feature/v0.1-rx2-phase-1
    └── feature/<your-topic>
```

- All work happens on `feature/<topic>` branches off
  `feature/threaded-dsp`.
- PRs target `feature/threaded-dsp`, not `main`.
- Rick batches `feature/threaded-dsp` → `main` for releases
  (fast-forward only, preserves linear history).

**Daily flow (both of us):**

```bash
git checkout feature/threaded-dsp
git pull
git checkout -b feature/<your-topic>
# ... write code ...
git commit -s -m "RX2: implement DDC freq-source abstraction"
git push -u origin feature/<your-topic>
# Open PR on GitHub: feature/<your-topic> → feature/threaded-dsp
# The other person reviews, approves, merges
```

**Commit conventions** (from `CLAUDE.md` §11):

- Sign off with `-s`.
- Summary-line prefix tells you what subsystem: `RX2: ...`,
  `TX: ...`, `PS: ...`, `UI: ...`, `DSP: ...`, etc.
- If you're using Claude (the AI assistant) to draft code, keep
  the `Co-Authored-By: Claude` trailer that Claude adds. Rick is
  doing this on his commits already; please match.
- Imperative mood ("add", "fix", "refactor"), not past tense.

**Code review expectations:**

- Reviews aren't gatekeeping — they're a sanity check + spread
  context across both contributors.
- If a PR sits more than 48 hours without review, ping the other
  person.
- Disagreements: see §3 below.

---

## 3. Honest concerns + ground rules

Rick asked Claude for a frank read on the risks of bringing on a
co-contributor. Reproduced here so we're aligned up front.

**A. Scope discipline (HL2 only, for now)**

Lyra is HL2/HL2+ only by design through v0.3 (RX2, TX, PureSignal).
You have an ANAN G2 — that's strategically valuable (see §4 below)
but **don't add ANAN code paths during v0.1–v0.3 work**. The
project would balloon beyond what two people can ship. Use the G2
as a "is this assumption HL2-specific?" reviewer instead. We
formally tackle ANAN/P2 in v0.4 (see §4).

**B. Decision authority**

Rick is project owner; he has final call on direction, scope,
naming, and architecture. This isn't bureaucracy — it's how almost
all single-maintainer FOSS projects operate, and stating it once
up front avoids painful conversations later. In practice, most
decisions will be obvious or collaborative; "owner has final call"
matters only for the rare contested choice.

**C. Two streams of Claude work**

Both of us use Claude as a coding assistant. Two Claudes editing
the same file in parallel breeds merge conflicts. Suggested
mitigation: divide by area. One person owns protocol/TX/PS, the
other owns DSP/UI/audio (or whatever split makes sense based on
your interests). Cross-area changes get reviewed by the area
owner. This isn't strict — small touchups in someone else's area
are fine — it's a gravity well, not a wall.

**D. License posture acknowledgment**

Before your first PR: read `CLAUDE.md` §2 (the WDSP / Thetis line)
and acknowledge in writing (Slack message, email, doesn't matter).
This protects both of us.

**E. Pace mismatch is normal**

Rick has been heads-down on Lyra for several months. You're
joining mid-flight. Don't feel bad if you commit a PR a week and
Rick commits five — that's not a competition. Don't feel bad
about taking a quiet week, either.

**F. Burnout risk**

Two-person FOSS works best when nobody feels guilty about not
committing. If something is annoying, say so before it festers.
If you need a break, take it. The project will be there when you
get back.

---

## 4. Long-term roadmap (where this is heading)

Operator decision 2026-05-03: ANAN family + Protocol 2 support is
a real future milestone. **Not now.** v0.1 (RX2), v0.2 (TX), and
v0.3 (PureSignal) all stay HL2-only. ANAN/P2 gets the v0.4 slot.

**Roadmap snapshot:**

| Version | Scope | Hardware |
|---|---|---|
| **v0.0.9** | Memory & Stations *(shipped today)* | HL2/HL2+ |
| **v0.1** | RX2 dual receive | HL2/HL2+ |
| **v0.2** | TX (SSB → CW/AM/FM, leveler) | HL2/HL2+ |
| **v0.3** | PureSignal | HL2/HL2+ |
| **v0.4** | Multi-radio refactor + Protocol 2 + ANAN family | HL2 + ANAN |
| **v1.0** | Stable release | Full HPSDR P1/P2 support |

**Why v0.4 is interesting for you specifically:**

- You have the exact target hardware (ANAN G2 / G2-1K).
- Stock Thetis on G2 means you already know the operator-facing
  feel of P2 from the field.
- Your G2 becomes the primary test bench when v0.4 work begins.
  Saves Rick buying or borrowing a unit.

**What we're doing during v0.1–v0.3 to make v0.4 tractable:**

`CLAUDE.md` §6.7 (just landed) defines five hardware-abstraction
disciplines that apply to every PR:

1. `nddc` is a runtime value, not a magic constant.
2. `Radio` facade is hardware-agnostic; HL2 quirks live in
   `lyra/protocol/p1_hl2.py`.
3. Don't permanently kill the sounddevice audio path — ANAN needs it.
4. PureSignal posture is conditional on radio capabilities (HL2
   needs hardware mod; ANAN doesn't).
5. TX hardware quirks live in protocol module, not DSP.

If you've already noticed (during reading) places where Lyra
violates these in the current codebase — write them down. v0.1
Phase 0 is "multi-channel refactor with no behavior change," which
is the right time to fix abstraction debt.

---

## 5. What a typical first week looks like

Concrete onboarding sequence Rick and Claude sketched out:

1. **Rick:** decide on collaborator-with-branch-protection (done in
   conversation). Set up branch protection rules on GitHub.
2. **Rick:** create `CONTRIBUTORS.md` with his name + a
   placeholder for you. Send you the repo + this discussion doc.
3. **You:** read `README.md`, `CLAUDE.md` (especially §1, §2, §6.7,
   §11), and `docs/help/getting-started.md` (operator perspective).
   Try to actually run Lyra against your HL2.
4. **You:** accept the GitHub collab invite. Clone the repo. Set
   up a Python venv per the install guide. Confirm Lyra launches
   and connects to your HL2.
5. **You:** acknowledge `CLAUDE.md` §2 (WDSP/Thetis line) to Rick
   in writing.
6. **You:** first contribution — small. Suggest a typo fix in a
   doc, a help-topic clarification, or a tiny code cleanup. Don't
   make your first PR a 500-line refactor; the goal is exercising
   the PR flow end-to-end and confirming GitHub permissions work.
7. **Both:** once that lands cleanly, divide RX2 phases or pick
   complementary work areas based on your interests.
8. **Rick:** add you to `CONTRIBUTORS.md` with your actual area
   after PR #1 merges.

---

## 6. What to write back

Three branches to pick from:

**Branch A: "Yes, I'm onboard."**
Reply to Rick. He invites you on GitHub, you go.

**Branch B: "Yes, with discussion."**
Reply with what needs talking through. Common candidates:
- Specific area you want to own (or not own)
- Disagreement with one of the §3 ground rules
- Different communication preference (Slack? Discord? Email-only?)
- Time commitment expectations from your end
- License posture concern
- ANAN scope timing — if v0.4 feels too far off

**Branch C: "Not right now."**
Door stays open. We'll catch up later.

Whatever you decide — thanks for considering it, Brent.

73,
Rick (N8SDR)

---

## Appendix: Pointers into the repo

If you want to skim before deciding:

- **`README.md`** — top-level overview, current version, install.
- **`CHANGELOG.md`** — full version history including today's
  v0.0.9 ship.
- **`CLAUDE.md`** — architecture decisions, protocol facts, roadmap.
  This is the "why is the codebase shaped this way" doc.
- **`docs/architecture/implementation_playbook.md`** — RX2 / TX /
  PS detailed planning.
- **`docs/help/`** — operator-facing User Guide.
- **`lyra/__init__.py`** — version source of truth.
- **`lyra/protocol/stream.py`** — current HL2 protocol implementation
  (will get refactored in v0.4 per `CLAUDE.md` §6.7).
- **`lyra/dsp/`** — DSP modules, several already ported from WDSP
  with attribution.

This doc itself lives at `docs/onboarding/contributor-discussion-2026-05-03.md`
in the repo.
