# synthux: synthetic user testing

AI personas use your website or web app the way people do: they see only
screenshots, click and type at screen coordinates, misclick, lose patience and
quit. They then answer behavioural questions. Every reported issue must
survive a judge, an automatic replay and repetition across runs before it
reaches the report. A secondary module gives **predictive** feedback on
physical products.

> **Synthetic data.** Use the results to decide what to test next. Confirm
> major decisions with real users. The tool never predicts sales or demand.

It runs **free and offline by default**: the built-in persona engine reads the
screen with local OCR and acts by rules, with no API key needed. You can switch any
role to Claude (Anthropic) or to a local Ollama model when you want richer
behaviour.

---

## Contents

1. [Quick start](#quick-start)
2. [How it counters the weaknesses of AI testers](#how-it-counters-the-weaknesses-of-ai-testers)
3. [Engines and cost](#engines-and-cost)
4. [Configuration](#configuration)
5. [Running a test](#running-a-test)
6. [Reading the report](#reading-the-report)
7. [Calibrating against real users](#calibrating-against-real-users)
8. [CAD designs: check before you print](#cad-designs-check-before-you-print)
9. [Physical products (prediction, not a test)](#physical-products-prediction-not-a-test)
10. [Safety sandbox](#safety-sandbox)
11. [Tests](#tests)
12. [Limitations](#limitations)
13. [Project layout](#project-layout)

---

## Quick start

Requirements: Python 3.11+, and Chromium for Playwright.

**Windows:** double-click `setup.bat` (or run
`powershell -ExecutionPolicy Bypass -File scripts\setup_windows.ps1`). It finds
Python 3.11+ (and tells you the `winget` command if it is missing), creates
`.venv`, installs everything, downloads Chromium and runs `synthux check`.
Then open a new terminal in the folder and run `.venv\Scripts\activate`.
**macOS / Linux:** `sh scripts/setup_unix.sh` does the same.

**Fastest start:** `synthux init` asks a few questions, then writes a study for a website or a design
file for a CAD part. Or test a site in one line, with no file at all:

```bash
synthux test https://staging.example.com/ --task "Find the returns policy" --success "30 days" --find --allow --open
```

`--allow` adds the site to the allowlist as a real site, where payments, emails and deletes stay blocked.
The test is saved under `data/studies/` so you can edit it and run it again.

By hand:

```bash
pip install -e ".[dev]"              # add ,anthropic for the optional Claude engine
python -m playwright install chromium  # skip if a Playwright Chromium is already installed

synthux check                        # validates config, shows engines (all free by default)
synthux demo &                       # demo shop with planted UX flaws on http://127.0.0.1:8765
synthux personas generate --study studies/demo.yaml
synthux run studies/demo.yaml --dry-run   # plan + estimated cost ($0.00 with the default engine)
synthux run studies/demo.yaml        # 6 personas x 6 tasks x 3 repeats, then judge + replay
synthux runs                         # find the run id
synthux report RUN_ID                # writes data/reports/RUN_ID/index.html
synthux dashboard                    # http://127.0.0.1:8800
```

Runs use about 2 parallel browser sessions per CPU core (`run.concurrency: auto`, up to 8). OCR reuses
text lines it has already read, and the persona doesn't wait out its full patience when the page is
plainly doing nothing. Its simulated time still counts the full wait. The 108-session demo run, with verification,
takes about 3 minutes on a 4-core machine.

If Chromium is installed somewhere unusual, set `browser.executable_path` in
`config/settings.yaml`, or set the `SYNTHUX_CHROMIUM` environment variable.

## How it counters the weaknesses of AI testers

| Weakness | What the tool does |
|---|---|
| Testers are too similar | Personas come from your (anonymized) data and are spread deliberately across traits: data-grounded anchors, then farthest-point sampling over the trait space, with coverage rules for skill/patience extremes, mobile and accessibility. A similarity check on descriptions and traits regenerates near-duplicates, and the report flags personas whose feedback is near-identical. |
| Too persistent | A patience budget (steps, retries, simulated time) and a frustration score measured from signals: errors, dead clicks, slow loads, repeats, lost data, searching. When the score crosses the persona's threshold, the persona quits. Quitting is a recorded outcome. |
| Agent failures | The agent sees pixels only; the DOM is read only by the safety guard. CAPTCHAs, technical errors, unsupported interactions and safety blocks are labelled `AGENT_LIMITATION` / `BLOCKED_BY_SAFETY` and kept out of UX findings. |
| Too agreeable | No open-ended questions. Questions are forced-choice at fixed prices, ask which step would make you quit, have you rank screens and use anchored scales. At least one located complaint per screen. The maker is not named, and competitor framing is optional. Behaviour outweighs opinion in every score, and stated-only complaints are never promoted. |
| Made-up problems | A judge rejects vague, unsupported or flattering issues and sends the persona back for specifics. Every issue carries a screenshot, the step sequence and the think-aloud. An automatic replay re-runs the intended steps without noise and marks issues `CONFIRMED`, `NOT_REPRODUCED` or `REPLAY_FAILED`. |
| Inconsistency | Each persona-task pair runs N times (default 3). Only issues that recur (default 2 of 3) and reproduce are promoted. |
| New ≠ hard | Issues that mainly affect unfamiliar personas, which they then get past and which break no convention, are labelled "unfamiliar" and ranked lower. |
| Too much feedback | Duplicates are grouped by text similarity, never across screens. Issues are ranked by severity × frequency × personas affected, weighted by behaviour. The report shows a top 10 and confidence levels. |

## Engines and cost

Each step of the pipeline has a **role**, and each role runs on an **engine**:

| Role | Does | Default |
|---|---|---|
| `actor` | the step-by-step browser behaviour | `heuristic` (free) |
| `persona` | persona text, questionnaires | `heuristic` |
| `judge` | reviews issues | `heuristic` (rules) |
| `grouper` | confirms borderline duplicate merges | `heuristic` |
| `reporter` | report summary | `heuristic` (template) |

Switch roles in `config/settings.yaml`:

```yaml
engine:
  default: heuristic
  roles: {judge: anthropic, reporter: anthropic}   # strong model only where it matters
```

* **heuristic**: no model and no cost. OCR (RapidOCR, ONNX) gives the persona
  text, contrast and input boxes. Its traits shape what it notices (contrast
  thresholds by eyesight), how it fills forms (date habits, typos), how long it
  searches and when it gives up. It also handles ordinary site furniture: it closes
  cookie banners and pop-ups, ticks required boxes and picks from dropdowns. It uses
  the menu, the three-line ☰ menu on phones and the site's search box to find
  information, and goes back out of dead ends.
* **ollama**: free local models (`config/models.yaml -> ollama`). Runs a local
  model server with a vision model.
* **anthropic**: Claude via the official SDK. Needs `ANTHROPIC_API_KEY` in the
  environment or `.env`. Model names and prices live in `config/models.yaml`:
  cheap `claude-haiku-5-5` for `actor`/`persona`, strong `claude-opus-5-5` for
  `judge`/`grouper`/`reporter`. Swap them freely.

**Hybrid persona: model quality where it matters, at a fraction of the cost.**
Set `engine.roles: {actor: hybrid}`. The free rules handle routine steps: pop-ups, labelled form fields,
obvious buttons. The model sees the screenshot only when a screen is hard, which means the rules are
searching for a way forward, about to give up, facing an error they can't explain or an unclear label,
or just made a click that did nothing. `engine.hybrid.max_calls_per_session` (default 6) caps the cost.
`engine.hybrid.engine` picks Claude or a free local Ollama model.
On the demo study the estimate drops from about 900 model calls to about 270.

Other ways the model engines are kept cheap:

* Screenshots are scaled down to `engine.model_image_width` (default 1024 px) before the model sees
  them. The model's click coordinates are mapped back to the real screen.
* The persona description and rules are identical on every step of a session, so they are sent as
  a cached system prompt. Cached input is billed at about a tenth of the normal input price.
* Each role has its own effort level in `config/models.yaml` (`effort:`). Per-step decisions run at
  `low` and the judge at `medium`.
* `engine.batch: true` sends the verification calls (judge reviews, questionnaires, duplicate grouping)
  through Anthropic's Batches API at half price. They are sent all at once, so verification waits for one
  batch (usually minutes) instead of many. The per-step persona never batches.
* Verification runs its model calls concurrently, and text read by OCR is remembered between runs
  (`data/cache/`), so testing the same site again is faster.

You can spread personas over engines for extra variety (study file:
`personas.engines: [heuristic, "anthropic:claude-haiku-5-5"]`). Model calls are
cached in SQLite. The cache key includes a per-run seed, so the N repeats stay
independent. `synthux run --dry-run` prints the estimated cost. A paid run asks
for confirmation (`--yes` skips the question) and is refused above
`run.max_cost_usd`.

## Configuration

| File | Contents |
|---|---|
| `config/settings.yaml` (copy of `settings.example.yaml`) | data folder, engines, **allowlist**, safety patterns, repeats/promotion/concurrency, noise rates, devices, network presets, calibration profile |
| `config/models.yaml` | model ids per role and prices, per engine |
| `config/scales.yaml` | anchored rating scales (every point described) |
| `config/calibration/*.yaml` | simulator parameters: patience, frustration weights and thresholds, perception contrast, judge, novelty |
| `.env` | secrets only (API key, test-account passwords); see `.env.example` |
| `studies/*.yaml` | one target, its tasks, personas and conditions |

Values like `${DEMO_PASSWORD}` or `${DEMO_PASSWORD:-fallback}` are read from
the environment. Secrets are never stored in YAML.

### A study file

```yaml
name: demo-shop
target:
  url: http://127.0.0.1:8765            # must be on the allowlist in settings.yaml
  credentials: {password: "${DEMO_PASSWORD:-demo-pass}"}
  brand_mask: [".brand-logo"]           # blurred in screenshots: maker not shown
  framing: neutral                      # or competitor (+ competitor_label)
tasks:
  - id: signup
    goal: Create an account
    start: /signup
    success_text: ["Account created"]   # visible text that means "done" (or success_url)
    keywords: ["create account", "sign up"]
    inputs:                             # what the persona can type; field kinds are matched to labels
      full_name: Sam Rivera
      birthdate: "1985-03-14"           # each persona types it in its own habit (MDY/DMY/YMD) first
      password: "{{cred:password}}"     # typed by code; never shown to a model or logged
    price_points: [113]                 # optional forced-choice price question
personas:
  count: 6
  sources: [examples/survey.csv, examples/reviews.txt]   # CSV, JSON, text, analytics exports
  manual: [{tech_skill: 1, patience: 2, device: mobile, label: "Rushed novice"}]
conditions:
  device: null                          # null = each persona's own device; or desktop|tablet|mobile
  network: fast                         # fast | 4g | 3g (Chromium throttling)
  interruption_step: null               # e.g. 4: persona leaves, forgets, page reloads
  time_pressure: null                   # null = persona trait
visits: 1                               # >1 = return visits with memory (learnability)
repeats: 3
```

## Running a test

1. **Personas.** `synthux personas generate --study STUDY` (or `-n 6 -s file.csv
   -s notes.md`). Names, emails, phone numbers, addresses, card numbers and
   handles are removed before anything else sees the data. Personas with
   disability, age 65+ or second-language traits are flagged *low confidence;
   verify with real users*.
2. **Run.** `synthux run STUDY`. For every persona × task × repeat (× visit), a
   fresh browser context opens the task. Each step goes screenshot →
   decision (think-aloud: sees / expects / does / feels) → noise → safety
   guard → action → measured signals → frustration. Sessions run in parallel up
   to `run.concurrency`.
3. **Verify** (automatic, unless you pass `--no-verify`):
   * a questionnaire per session;
   * the judge reviews every issue, with send-back rounds;
   * accepted issues are replayed (a few representative sessions per issue
     group);
   * novelty labelling.
4. **Report.** `synthux report RUN_ID` groups and ranks the issues and writes
   the HTML report.

Useful options: `--task signup`, `--persona per_xxx`, `--repeats 5`,
`--profile default-cal2`.

## Reading the report

* **Session viewer** (dashboard, `synthux dashboard` → a run → sessions): every session step by step,
  showing the screenshot the persona saw, what it did, its think-aloud, the measured signals, its frustration,
  and any simulated slip.
* **Replays**: each top issue has a short animated replay of the persona's last steps. The clicks are marked,
  and the replay ends on the screen where the problem shows.
* **Accessibility (measured)**: text contrast against WCAG 2.2 AA, very small text, and tap targets under
  24 × 24 px on touch screens. Each is measured on the full-quality screenshots of every screen the personas
  saw, and screens covered by a pop-up are skipped. These are measurements, kept apart from persona findings.

* **Header**: the synthetic-data disclaimer, engines, calibration profile,
  repeats and cost.
* **Top 10 issues**: severity, confidence (high/medium/low), screen, sessions
  affected, personas, replay status and quits. Each issue includes a zoomed
  screenshot with the area outlined, the step sequence, the persona's
  think-aloud at that moment and the judge's notes.
* **Task completion by persona type**, **where and why personas quit** (with
  their words), and **frustration timelines** (dashed line = that persona's
  quit threshold). Every chart has a table view.
* **Where price became an objection**: reasons only, with no purchase rates.
* **Unfamiliar, not necessarily hard**, **How findings were filtered**
  (rejections, replay results, what was not promoted and why), **Agent
  limitations**, **Personas**, and **Limitations**.

## Calibrating against real users

The most important loop: it is how you learn how far to trust the personas.

```bash
synthux calibrate import --json examples/real_users/demo_study.json --name "Oct moderated test"
#   or: --tasks tasks.csv --issues issues.csv   (templates in examples/real_users/)
synthux calibrate compare RUN_ID --real real_xxx
synthux calibrate apply cal_xxx --only 1,3      # creates profile default-cal1 (DB + config/calibration/default-cal1.yaml)
synthux run studies/demo.yaml --profile default-cal1
synthux calibrate history                       # agreement over versions (also on the dashboard)
```

Or let it search for the settings automatically:

```bash
synthux calibrate search studies/demo.yaml --real real_xxx --apply
```

It re-runs the shared tasks with scaled patience, frustration and perception settings, one at a time, and
keeps whichever version brings completion rates and quit points closest to the real users. Every trial uses
the same random seeds, so the differences come from the settings alone. With the free engine it costs
nothing; a paid engine asks first. On the bundled example it raised agreement from 0.50 to 0.63.

`compare` reports three lists: issues **both** found, issues **only real users**
found, and issues **only the personas** found. It also reports these metrics:

* issue precision, recall and F1;
* severity rank correlation;
* completion-rate MAE and bias;
* quit-point agreement;
* one combined agreement score.

Suggestions are specific and come with reasons. Examples:

* "lower `frustration.threshold_by_patience.3` from 65 to 52: personas finish
  more often than real users (+20%)";
* "raise `perception.glance_contrast.normal`: real users missed controls the
  personas found";
* prompt notes for model-backed personas.

Nothing changes until you `apply`. Every run stores the profile version it
used, so results stay comparable over time.

## CAD designs: check before you print

A typical part checks in about 6 seconds, and a 670,000-triangle export in about 13.
Point it at a CAD export and it checks the geometry before you spend filament,
then learns from your real prints so the next check is about *your* printer.

```bash
synthux physical check part.stl                         # STL, 3MF, OBJ, PLY, GLB (STEP: pip install ".[step]")
synthux physical check part.stl --printer bambu_x1 --material PETG --handheld
synthux physical check part_v2.stl --design examples/cad/phone_stand.yaml   # same design name = next version
synthux physical printed "Phone stand" --measure measured.csv --defect stringing --notes "clip too tight"
synthux physical history "Phone stand"                  # versions and what changed
```

Each check writes `data/reports/designs/<name>_v<N>/index.html` (also listed on
the dashboard) with renders from several angles, every finding marked on the
model, and a "fixed / still there / new" comparison against the previous
version.

**Printability:** a broken or non-watertight mesh, wrong units (a 0.06 mm part
exported in metres), fit in the build volume, overhangs and bridges (only on
outside faces), the best print orientation, bed contact, thin walls and thin
pins below what the nozzle can lay down, slender towers, sharp edges you will
touch, clearances between parts that will fuse, parts joined only along an
edge, and tipping over (centre of mass, plus a declared load such as a phone
leaning on it). It also estimates mass, filament length, time and cost. Support
material is measured from each overhang down to the bed or the part below; it counts
towards filament, time and cost but not towards the weight of the finished part.

**Human fit**, from `data/anthropometrics.yaml` and the design YAML:

* grip size and weight in the hand (`--handheld`);
* wearable inner sizes against wrist, finger, head or other population ranges;
* connector openings too small for the plug's moulded body (USB-C, USB-A,
  micro-USB, Lightning, 5.5 mm barrel, HDMI);
* buttons smaller than a fingertip.

Personas differ in hand size, grip strength, body size, motor control and
eyesight, and react only to measured facts that apply to them (a persona with
a weak grip, for example, reacts to the measured grip diameter and weight).
A product-knowledge checklist adds category-specific questions for everyday
goods, electronics, wearables and 3D-printed parts.

**After printing**, record what happened. Use a CSV of
`feature, designed_mm, measured_mm, kind (outer|hole|gap), axis` (see
`examples/cad/v1_print_measurements.csv`), plus defects. The tool learns
offsets per printer and material: holes printing 0.2 mm small, gaps closing up,
the overhang angle your printer really manages. The next `physical check`
applies them and tells you which ones it used.

`examples/cad/` has a phone stand with planted flaws (v1) and its fixed v2, a
tool handle with a too-small USB-C opening, and a plain block as a control
(`python examples/cad/make_examples.py` regenerates them). Add your own printer to
`config/printers.yaml`.

## Physical products (prediction, not a test)

```bash
synthux physical review examples/physical/trailflask.yaml
```

Input (`examples/physical/trailflask.yaml`):

* spec values;
* photos;
* CAD exports (STL bounding box) or measured dimensions (CSV);
* **real reviews of similar products** (CSV/text);
* specs of those similar products;
* optional prototype sensor CSVs.

The module turns the reviews into a complaint taxonomy (size, weight,
durability, ergonomics, leaks, cleaning, value, performance, instructions)
with quotes. Personas weigh each pattern against spec comparisons with the
similar products, adjusted by their traits, and give a forced-choice "most
likely return reason". The same judge rules apply: every claim must cite a
spec value, a review pattern or a photo.

Every output is labelled **"Prediction, not a test"**. The tool never claims to
assess feel, weight in the hand, texture, taste or smell. It compares a spec
value (e.g. 540 g against a 495 g median); it does not say "feels heavy".
Sensor data is analysed for spikes, stuck sensors and usage sessions.

## Safety sandbox

* The tool refuses to run unless the target, and every navigation, is on
  `safety.allowlist` in your settings. A study file cannot widen the list.
  Userinfo, suffix and port tricks are rejected.
* Requests to payment and email providers (configurable patterns) are blocked
  unless the target is marked `sandbox: true`. Writes to hosts off the
  allowlist are always blocked.
* Before every click or Enter, code checks the element under the pointer.
  Pay, send, delete and similar actions are blocked on non-sandbox targets and
  recorded as `BLOCKED_BY_SAFETY`.
* Test credentials come from the environment, are typed by code and are
  masked in logs.

## Tests

```bash
pytest tests/unit                  # ~130 tests, under a minute, no browser
pytest tests/integration           # real Chromium against the demo app (~3 min)
pytest tests/e2e/test_demo_app_offline.py   # full pipeline, 108 sessions (~6 min)
pytest tests/e2e/test_pinecone_site.py      # second site: pop-ups, menus, buried pages (~5 min)
pytest tests/e2e/test_cad_designs.py        # CAD checks + print-iteration loop on the examples
SYNTHUX_LIVE=1 ANTHROPIC_API_KEY=... pytest tests/e2e/test_demo_app_live.py   # same bar on Claude (costs money)
```

The end-to-end test runs the whole pipeline on the demo shop, whose planted
flaws are listed in `demo_app/planted_flaws.yaml`:

* a hidden button;
* a confusing form;
* a slow page;
* an error with no explanation;
* two clean control flows.

It requires every planted flaw to be found, and **no promoted finding that
isn't planted**.

A second site, **Pinecone Home** (`python demo_app/pinecone.py`,
`studies/pinecone.yaml`, ground truth in `demo_app/pinecone_flaws.yaml`),
tests ordinary real-world patterns. Its planted flaws are a newsletter pop-up
that comes back on every page, a return policy buried three pages deep and a
"Stores" menu link that does nothing. Its clean parts are a cookie banner that
stays closed and a sign-up form with a dropdown and a terms checkbox. Personas
dismiss pop-ups and cookie banners, tick required boxes, pick from dropdowns,
use the menu to find information and go back from dead ends. Tasks marked
`type: find` also report information that takes too many pages to reach.

## Limitations

* Personas are simulations. They have no real emotions, context or stakes. Use
  them to find candidates, then confirm with real users.
* The free heuristic engine is good at structural problems (visibility,
  contrast, forms, errors, speed, dead ends). It is poor at visual appeal,
  content quality, trust, tone and anything that needs understanding of
  meaning. Model engines help, and calibration tells you how much.
* OCR can misread text. Unusual widgets (canvas, drag and drop, custom
  pickers, CAPTCHAs) may be unusable and are reported as agent limitations,
  not UX issues.
* Disability, age and cultural personas are coarse approximations. Their
  findings are always flagged low confidence.
* A clean report means no persona hit a problem. It does not mean there are
  none.
* No sales, demand or conversion predictions, by design.
* Physical predictions are only as good as the reviews and specs you provide,
  and they are never a substitute for handling the product.

## Project layout

```
config/            settings, models (ids + prices), anchored scales, calibration profiles
studies/           study files (demo.yaml, pinecone.yaml)
demo_app/          two demo sites with planted flaws + their ground truth
examples/          persona sources, real-user results, physical product example, CAD examples
scripts/           one-step setup for Windows (setup.bat) and macOS/Linux
src/synthux/
  core/            config, SQLite + migrations, model gateway (cache, cost), similarity, study
  safety/          allowlist, network policy, action guard, credential vault
  personas/        ingest, anonymize, traits, deliberate design, generation, diversity
  runner/          browser (pixels only), perception (OCR), behaviour (patience, frustration,
                   noise), observe (signals, issues), memory, session loop, orchestrator
  engines/         heuristic persona (free), model persona (Claude/Ollama)
  elicitation/     behavioural questionnaires
  judge/           review, replay, repetition, novelty, verification pipeline
  aggregate/       grouping, ranking, demand-forecast guardrail
  report/          HTML report + inline SVG charts
  calibration/     profiles, import, matching, metrics, tuning, compare/history
  physical/        inputs, review taxonomy, evaluation, sensors, guardrails
    cad/           mesh loading, voxel/print checks, renders, ergonomics, personas, print iteration
  data/            anthropometrics, product-knowledge checklists, name lists
  dashboard/       local read-only web dashboard
tests/             unit, integration (browser), e2e (offline and live)
```
