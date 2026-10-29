# Website tests: synthetic users

Under **More › Website tests**, AI personas use your website or web app the way people do: they see
only screenshots, click and type at screen coordinates, misclick, lose patience and quit. They then
answer behavioural questions. Every reported issue must survive a judge, an automatic replay and
repetition across runs before it reaches the report.

> **Synthetic data.** Use the results to decide what to test next. Confirm major decisions with
> real users. ProofLoop never predicts sales or demand.

It runs **free and offline by default**: the built-in persona engine reads the screen with local
OCR and acts by rules, with no API key needed. You can switch any role to Claude (Anthropic) or to
a local Ollama model when you want richer behaviour.

- [Quick start](#quick-start)
- [How it counters the weaknesses of AI testers](#how-it-counters-the-weaknesses-of-ai-testers)
- [Engines and cost](#engines-and-cost)
- [Configuration](#configuration)
- [Running a test](#running-a-test)
- [Reading the report](#reading-the-report)
- [Calibrating against real users](#calibrating-against-real-users)
- [Safety sandbox](#safety-sandbox)
- [Limitations](#limitations)

## Quick start

In the window, open *More › Website tests*, paste the page to start on, describe the task and the
text that shows it worked, and press *Start the test*. A site is added to the allowlist only when
you tick "This is my own site". Or in one line:

```bash
proofloop web test https://staging.example.com/ --task "Find the returns policy" --success "30 days" --find --allow --open
```

`--allow` adds the site to the allowlist as a real site, where payments, emails and deletes stay
blocked. The test is saved under `~/.proofloop/studies/` so you can edit it and run it again.
`proofloop web init` asks a few questions and writes a study file instead.

The whole pipeline on the bundled demo shop:

```bash
proofloop web settings                    # validates settings, shows engines (all free by default)
proofloop web demo &                      # demo shop with planted UX flaws on http://127.0.0.1:8765
proofloop web personas generate --study examples/studies/demo.yaml
proofloop web run examples/studies/demo.yaml --dry-run   # plan + estimated cost ($0.00 with the default engine)
proofloop web run examples/studies/demo.yaml             # 6 personas x 6 tasks x 3 repeats, then judge + replay
proofloop web runs                        # find the run id
proofloop web report RUN_ID               # writes ~/.proofloop/reports/RUN_ID/index.html
proofloop web dashboard                   # results on http://127.0.0.1:8800
```

Chromium downloads by itself the first time (about 150 MB). Runs use about 2 parallel browser
sessions per CPU core (`run.concurrency: auto`, up to 8). OCR reuses text lines it has already
read, and the persona doesn't wait out its full patience when the page is plainly doing nothing.
Its simulated time still counts the full wait. The 108-session demo run, with verification, takes
about 3 minutes on a 4-core machine. If Chromium is somewhere unusual, set
`browser.executable_path` in your `settings.yaml`, or `PROOFLOOP_CHROMIUM`.

## How it counters the weaknesses of AI testers

| Weakness | What it does |
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

Switch roles in your `settings.yaml` (`~/.proofloop/config/settings.yaml`; only what you change):

```yaml
engine:
  roles: {judge: anthropic, reporter: anthropic}   # strong model only where it matters
```

* **heuristic**: no model and no cost. OCR (RapidOCR, ONNX) gives the persona text, contrast and
  input boxes. Its traits shape what it notices (contrast thresholds by eyesight), how it fills
  forms (date habits, typos), how long it searches and when it gives up. It also handles ordinary
  site furniture: it closes cookie banners and pop-ups, ticks required boxes and picks from
  dropdowns. It uses the menu, the three-line ☰ menu on phones and the site's search box to find
  information, and goes back out of dead ends.
* **ollama**: free local models (`models.yaml -> ollama`). Runs a local model server with a vision
  model.
* **anthropic**: Claude via the official SDK (`pip install -e ".[anthropic]"`). Needs
  `ANTHROPIC_API_KEY` in the environment or `~/.proofloop/.env`. Model names and prices live in
  `models.yaml`: cheap `claude-haiku-5-5` for `actor`/`persona`, strong `claude-opus-5-5` for
  `judge`/`grouper`/`reporter`. Swap them freely.

**Hybrid persona: model quality where it matters, at a fraction of the cost.** Set
`engine.roles: {actor: hybrid}`. The free rules handle routine steps: pop-ups, labelled form fields,
obvious buttons. The model sees the screenshot only when a screen is hard, which means the rules are
searching for a way forward, about to give up, facing an error they can't explain or an unclear
label, or just made a click that did nothing. `engine.hybrid.max_calls_per_session` (default 6)
caps the cost. `engine.hybrid.engine` picks Claude or a free local Ollama model. On the demo study
the estimate drops from about 900 model calls to about 270.

**Free by default, smart when you want it.** Add `--smart` to `proofloop web run` or
`proofloop web test` to use this cheap paid mode for that one run (hybrid actor, Claude for judging;
`run` also batches the judging at half price). Without `ANTHROPIC_API_KEY` it says so and runs free.

Other ways the model engines are kept cheap:

* Screenshots are scaled down to `engine.model_image_width` (default 1024 px) before the model sees
  them. The model's click coordinates are mapped back to the real screen.
* The persona description and rules are identical on every step of a session, so they are sent as
  a cached system prompt. Cached input is billed at about a tenth of the normal input price.
* Each role has its own effort level in `models.yaml` (`effort:`). Per-step decisions run at `low`
  and the judge at `medium`.
* `engine.batch: true` sends the verification calls (judge reviews, questionnaires, duplicate
  grouping) through Anthropic's Batches API at half price. They are sent all at once, so
  verification waits for one batch (usually minutes) instead of many. The per-step persona never
  batches.
* Verification runs its model calls concurrently, and text read by OCR is remembered between runs
  (`~/.proofloop/cache/`), so testing the same site again is faster.

You can spread personas over engines for extra variety (study file:
`personas.engines: [heuristic, "anthropic:claude-haiku-5-5"]`). Model calls are cached in SQLite.
The cache key includes a per-run seed, so the N repeats stay independent. `proofloop web run
--dry-run` prints the estimated cost. A paid run asks for confirmation (`--yes` skips the question)
and is refused above `run.max_cost_usd`.

## Configuration

The built-in files are in `src/proofloop/data`. To change one, put a file of the same name in
`~/.proofloop/config`: it replaces the built-in one, except `settings.yaml`, which is merged over
the built-in settings so it only needs what you change.

| File | Contents |
|---|---|
| `settings.yaml` | engines, **allowlist**, safety patterns, repeats/promotion/concurrency, noise rates, devices, network presets, calibration profile |
| `models.yaml` | model ids per role and prices, per engine |
| `scales.yaml` | anchored rating scales (every point described) |
| `calibration/*.yaml` | simulator parameters: patience, frustration weights and thresholds, perception contrast, judge, novelty |
| `~/.proofloop/.env` | secrets only (API key, test-account passwords); see `.env.example` |
| study files | one target, its tasks, personas and conditions (`examples/studies/`) |

Values like `${DEMO_PASSWORD}` or `${DEMO_PASSWORD:-fallback}` are read from the environment.
Secrets are never stored in YAML.

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
  sources: [survey.csv, reviews.txt]    # CSV, JSON, text, analytics exports, next to the studies folder
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

1. **Personas.** `proofloop web personas generate --study STUDY` (or `-n 6 -s file.csv -s notes.md`).
   Names, emails, phone numbers, addresses, card numbers and handles are removed before anything
   else sees the data. Personas with disability, age 65+ or second-language traits are flagged
   *low confidence; verify with real users*.
2. **Run.** `proofloop web run STUDY`. For every persona × task × repeat (× visit), a fresh browser
   context opens the task. Each step goes screenshot → decision (think-aloud: sees / expects / does
   / feels) → noise → safety guard → action → measured signals → frustration. Sessions run in
   parallel up to `run.concurrency`.
3. **Verify** (automatic, unless you pass `--no-verify`):
   * a questionnaire per session;
   * the judge reviews every issue, with send-back rounds;
   * accepted issues are replayed (a few representative sessions per issue group);
   * novelty labelling.
4. **Report.** `proofloop web report RUN_ID` groups and ranks the issues and writes the HTML report.

Useful options: `--task signup`, `--persona per_xxx`, `--repeats 5`, `--profile default-cal2`.

## Reading the report

* **Header**: the synthetic-data disclaimer, engines, calibration profile, repeats and cost.
* **Top 10 issues**: severity, confidence (high/medium/low), screen, sessions affected, personas,
  replay status and quits. Each issue includes a zoomed screenshot with the area outlined, the step
  sequence, the persona's think-aloud at that moment and the judge's notes.
* **Replays**: each top issue has a short animated replay of the persona's last steps. The clicks
  are marked, and the replay ends on the screen where the problem shows.
* **Task completion by persona type**, **where and why personas quit** (with their words), and
  **frustration timelines** (dashed line = that persona's quit threshold). Every chart has a table
  view.
* **Accessibility (measured)**: text contrast against WCAG 2.2 AA, very small text, and tap targets
  under 24 × 24 px on touch screens, measured on the full-quality screenshots of every screen the
  personas saw (screens covered by a pop-up are skipped). These are measurements, kept apart from
  persona findings.
* **Where price became an objection**: reasons only, with no purchase rates.
* **Unfamiliar, not necessarily hard**, **How findings were filtered** (rejections, replay results,
  what was not promoted and why), **Agent limitations**, **Personas**, and **Limitations**.
* **Session viewer** (`proofloop web dashboard` → a run → sessions): every session step by step,
  showing the screenshot the persona saw, what it did, its think-aloud, the measured signals, its
  frustration, and any simulated slip.

## Calibrating against real users

The most important loop: it is how you learn how far to trust the personas.

```bash
proofloop web calibrate import --json examples/real_users/demo_study.json --name "Oct moderated test"
#   or: --tasks tasks.csv --issues issues.csv   (templates in examples/real_users/)
proofloop web calibrate compare RUN_ID --real real_xxx
proofloop web calibrate apply cal_xxx --only 1,3   # creates profile default-cal1 (DB + config/calibration/default-cal1.yaml)
proofloop web run examples/studies/demo.yaml --profile default-cal1
proofloop web calibrate history                    # agreement over versions (also on the dashboard)
```

Or let it search for the settings automatically:

```bash
proofloop web calibrate search examples/studies/demo.yaml --real real_xxx --apply
```

It re-runs the shared tasks with scaled patience, frustration and perception settings, one at a
time, and keeps whichever version brings completion rates and quit points closest to the real
users. Every trial uses the same random seeds, so the differences come from the settings alone.
With the free engine it costs nothing; a paid engine asks first. On the bundled example it raised
agreement from 0.50 to 0.63.

`compare` reports three lists: issues **both** found, issues **only real users** found, and issues
**only the personas** found. It also reports issue precision, recall and F1; severity rank
correlation; completion-rate MAE and bias; quit-point agreement; and one combined agreement score.

Suggestions are specific and come with reasons, for example "lower
`frustration.threshold_by_patience.3` from 65 to 52: personas finish more often than real users
(+20%)", or "raise `perception.glance_contrast.normal`: real users missed controls the personas
found". Nothing changes until you `apply`. Every run stores the profile version it used, so results
stay comparable over time.

## Safety sandbox

* No test runs unless the target, and every navigation, is on `safety.allowlist` in your settings.
  A study file cannot widen the list. Userinfo, suffix and port tricks are rejected.
* Requests to payment and email providers (configurable patterns) are blocked unless the target is
  marked `sandbox: true`. Writes to hosts off the allowlist are always blocked.
* Before every click or Enter, code checks the element under the pointer. Pay, send, delete and
  similar actions are blocked on non-sandbox targets and recorded as `BLOCKED_BY_SAFETY`.
* Test credentials come from the environment, are typed by code and are masked in logs.

## Limitations

* Personas are simulations. They have no real emotions, context or stakes. Use them to find
  candidates, then confirm with real users.
* The free heuristic engine is good at structural problems (visibility, contrast, forms, errors,
  speed, dead ends). It is poor at visual appeal, content quality, trust, tone and anything that
  needs understanding of meaning. Model engines help, and calibration tells you how much.
* OCR can misread text. Unusual widgets (canvas, drag and drop, custom pickers, CAPTCHAs) may be
  unusable and are reported as agent limitations, not UX issues.
* Disability, age and cultural personas are coarse approximations. Their findings are always
  flagged low confidence.
* A clean report means no persona hit a problem. It does not mean there are none.
* No sales, demand or conversion predictions, by design.
