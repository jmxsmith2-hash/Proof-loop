# Synthetic User Testing — Build Plan

Status: **draft, awaiting review**. No code has been written yet.

## 1. Guiding principles

1. **The persona model sees only pixels.** It gets screenshots and returns
   actions at screen coordinates. The DOM is read only by *non-model* code
   (the safety guard, evidence capture, replay checks) and never reaches a
   prompt.
2. **Behavior beats opinion.** Completion, time, retries, quit points and
   frustration are measured by code, not reported by the model. Stated opinions
   carry less weight in every score.
3. **Every finding must hold up.** An issue reaches the report only if it has
   evidence, passed the judge, reproduced on replay (or is clearly marked as
   unconfirmed), and recurred across repeated runs.
4. **Honesty is part of the output.** Each report says the data is synthetic,
   each finding carries a confidence level, and findings from accessibility,
   age or cultural personas are flagged low confidence. The report never
   forecasts sales or demand.
5. **Calibration is the feedback loop.** Results from real users adjust the
   simulator's parameters, and every run records which calibration version
   produced it.

## 2. Architecture

```
            ┌────────────── CLI (typer) ──────────────┐
            │ personas generate │ run │ report │ calibrate │ physical review │ dashboard
            └───────┬──────────────────────────────────┘
                    │
   ┌────────────────┼──────────────────────────────────────────────────┐
   │ core: config (pydantic + YAML + env) · logging · db (sqlite) ·     │
   │       llm gateway (model roles, cache, cost meter, retries)        │
   └──┬──────────┬──────────┬────────────┬───────────┬─────────────┬───┘
      │          │          │            │           │             │
  personas    safety     runner      elicitation   judge       aggregate/report
  ingest      allowlist  browser     questionnaires verify      dedupe · rank ·
  anonymize   action     (pixels)    anchored       replay      HTML report
  trait grid  guard      frustration scales         repeat N
  diversity   net block  noise/conds blind framing  novelty
                         think-aloud
                         classifier
                         memory
      └──────────────── calibration (real-user import, metrics, tuning) ─┘
                        physical (reviews → complaint taxonomy, sensor CSV)
```

### Model roles (config-driven)

| Role       | Used for                                        | Default |
|------------|-------------------------------------------------|---------|
| `actor`    | per-step browser actions, think-aloud           | cheap (Haiku tier) |
| `persona`  | persona narrative generation, questionnaires   | cheap |
| `judge`    | issue review, replay check, novelty call        | strong (Opus/Sonnet tier) |
| `grouper`  | semantic de-duplication confirmation            | strong |
| `reporter` | report summaries, calibration suggestions       | strong |

Model IDs and per-token prices live in `config/models.yaml`. Each persona can
override `actor` to spread personas across models.

### The LLM gateway (`core/llm.py`)

* A `LLMClient` protocol with an Anthropic implementation and a scripted
  `FakeLLM` used by the tests.
* Structured output through tool use (JSON schema per call type), validated
  with pydantic. One automatic retry with the validation error is allowed,
  then the call fails and is logged.
* **Cache:** a SQLite table keyed by a hash of (model, prompt, images, params,
  seed). The seed is part of the key, so the N repeated runs **do not**
  collapse into one cached answer. Judge, grouping and report calls on
  identical inputs are cache hits.
* **Cost meter:** counts tokens per role and uses the price table. Screenshot
  tokens are estimated as about `w*h/750`. `run --dry-run` prints the estimate
  and asks for confirmation; `--yes` skips the prompt.

### The agent loop (`runner/`)

```
for step in budget:
    shot = browser.screenshot()                     # pixels only
    signals = observe(shot, prev_shot, timings)     # no model: visual diff, load time, repeats
    frustration.update(signals)
    if frustration > threshold or budget exhausted: quit(reason)  # recorded outcome
    decision = actor(persona, task, memory, shot)   # {sees, expects, action, feels, confusion}
    action = noise.apply(decision.action, persona)  # misclicks / typos
    safety.guard(action, page)                      # DOM lookup at (x, y); never sent to the model
    browser.execute(action)                         # mouse/keyboard at coordinates
    log(step, shot, decision, action, signals, frustration)
```

* **Frustration** is a deterministic function of measured signals: an error
  seen on screen (model-flagged, then confirmed by the judge), no visual change
  after an action (perceptual hash), load time above a threshold, the same
  action repeated, backtracking, and the confusion score the model reports.
  Weights and decay are in config so calibration can tune them.
* **Patience budget:** max steps, max retries per sub-goal and max simulated
  seconds. All three scale with the persona's patience trait and time
  pressure.
* **Conditions:** viewport/device presets, CDP network throttling (Chromium),
  interruptions at step *k* (the memory is cut down to a lossy summary and the
  page may be reloaded), and time pressure (a smaller budget plus a note in the
  prompt).
* **Failure classifier:** non-model rules first (CAPTCHA detected, a
  navigation or HTTP error, a browser crash, the safety guard blocking, an
  action the agent cannot perform such as drag-and-drop or a file picker) →
  `AGENT_LIMITATION`. Anything else goes to the judge, which may label it
  `UX_ISSUE`. Agent limitations appear in a separate report appendix.
* **Multi-session memory:** after each session the persona writes a short
  memory summary to the DB. A return visit loads it, and we measure the change
  in steps and time between sessions (learnability).

### Safety (`safety/`)

* The URL allowlist is checked before the browser launches and on **every**
  navigation. Navigation off the allowlist is aborted.
* Network route interception blocks requests to payment and email providers
  (patterns in config) unless the target has `sandbox: true`.
* Action guard: before each click or Enter, code looks up the element at
  (x, y) using `elementFromPoint` and checks its text and attributes against
  payment, send and delete patterns. A blocked action is recorded as
  `BLOCKED_BY_SAFETY`, a kind of agent limitation.
* Test credentials come from the environment, referenced in YAML as
  `${ENV_VAR}`. The actor sees them as "your login is in your password
  manager", and the values are typed by code, never placed in prompts or
  logs.

### Anti-agreeableness (`elicitation/`)

* Only behavioral question types: forced-choice purchase at fixed prices,
  "which step would make you quit", ranking screens worst to best, A/B
  preference with a reason tied to a specific screen.
* Anchored scales: each point is described in concrete words, kept in
  `config/scales.yaml`.
* Blind framing: the maker's name is never mentioned. An optional
  `brand_mask` (CSS selectors) blurs logos in screenshots, and a "competitor"
  framing mode is available.
* At least one specific, located complaint per screen: `{screen_id, region
  (x, y, w, h), what, why}`. If it is missing or vague, the questionnaire asks
  again.

### Judge and verification (`judge/`)

1. **Review:** the strong model checks each issue against a rubric (specific?
   located? supported by the screenshot and step log? flattering or
   generic?). Rejections go back to the persona with a reason, up to 2
   rounds, and are then dropped.
2. **Evidence bundle:** a cropped and full screenshot, the step sequence, and
   the think-aloud at that moment.
3. **Replay:** a fresh browser context re-runs the recorded *intended* actions
   (without noise) up to the issue step. Confirmation requires both (a) a
   perceptual-hash match with the original screenshot and (b) the judge seeing
   the described symptom in the replay screenshot. The result is
   `CONFIRMED`, `NOT_REPRODUCED` or `REPLAY_FAILED`.
4. **Repetition:** each persona × task runs N times (default 3) with different
   seeds. Issues are clustered across runs, and only those seen in at least
   `promote_min` runs (default 2 of 3) are promoted.
5. **Unfamiliar vs hard:** an issue is tagged `NOVELTY` when it affects mainly
   personas with low familiarity, *and* they recover within the session or on
   a return visit, *and* the judge finds no violation of established
   conventions. NOVELTY issues are ranked lower and listed separately.

### Aggregation and report (`aggregate/`, `report/`)

* De-duplication: similarity clustering, with the strong model confirming
  borderline merges.
* `score = severity_weight × frequency × personas_affected × behavior_factor`.
  The behavior factor boosts issues tied to quits, retries or slow
  completion, and shrinks issues that were only stated.
* Static HTML (Jinja2) with inline SVG charts: the top 10 issues, completion
  by persona type, a quit funnel and its reasons, frustration timelines,
  evidence pop-outs, confidence badges, and appendices (agent limitations,
  NOVELTY, rejected or unconfirmed issues).
* A fixed synthetic-data disclaimer header. A final output filter removes or
  flags demand and sales forecasts ("X% would buy", "expected revenue") in
  any model-written text.

### Calibration (`calibration/`)

* Import real-user results from CSV/JSON: tasks, completion, time, quit step
  and reason, and issues with severity. Templates are in `examples/`.
* Issue matching: similarity, then confirmation by the strong model. Output:
  issues both found, issues only real users found, issues only the AI found.
* Metrics: issue precision, recall and F1, completion-rate mean absolute
  error, agreement on quit points, Spearman correlation of severity ranks.
  Each comparison is stored with the calibration-profile version.
* Suggestions come from rules (synthetic completion ≫ real → lower patience
  or raise the frustration weights by a computed amount) plus prompt edits
  proposed by the model. `calibrate apply` writes a new **versioned
  calibration profile**, and new runs record which profile they used.

### Physical products (`physical/`) — always labeled *prediction, not a test*

* Inputs: photos, a spec sheet (YAML/JSON), dimensions or CAD-exported
  measurements, reviews of similar products (CSV/text), and optional sensor
  CSVs.
* Reviews → a complaint taxonomy (size, durability, ergonomics, value, …),
  with frequencies and supporting quotes.
* Personas evaluate the product against those patterns, under the same judge
  and evidence rules. Every claim must cite a spec value, a photo region or a
  review pattern.
* Sensor analysis (numpy): rolling z-score and IQR anomalies, usage-session
  segmentation, and duty-cycle statistics.
* A hard filter rejects any claim about feel, weight perception, texture,
  taste or smell.

## 3. File structure

```
pyproject.toml            # deps, ruff, pytest config, `synthux` entry point
README.md
.env.example
config/
  settings.example.yaml   # targets/allowlist, run params, concurrency, paths
  models.yaml             # model ids per role + prices
  scales.yaml             # anchored rating scales
  calibration/default.yaml# frustration weights, patience scaling (versioned)
src/synthux/
  cli.py
  core/{config.py, logging.py, db.py, schema.sql, llm.py, cache.py, cost.py, similarity.py}
  safety/{allowlist.py, guard.py, network.py, credentials.py}
  personas/{ingest.py, anonymize.py, traits.py, design.py, generate.py, diversity.py, models.py}
  runner/{browser.py, observe.py, actor.py, actions.py, noise.py, frustration.py,
          patience.py, conditions.py, classify.py, memory.py, session.py, orchestrator.py}
  elicitation/{questions.py, scales.py, framing.py, complaints.py}
  judge/{review.py, evidence.py, replay.py, repeat.py, novelty.py}
  aggregate/{dedupe.py, rank.py, guardrails.py}
  report/{build.py, charts.py, templates/report.html.j2}
  calibration/{importer.py, match.py, metrics.py, tune.py, profiles.py}
  physical/{inputs.py, reviews.py, evaluate.py, sensors.py, guardrails.py}
  dashboard/{server.py, templates/}
  prompts/*.md            # all prompts as versioned files, hashed into run metadata
demo_app/
  server.py               # stdlib HTTP server, deterministic
  pages/…                 # signup/checkout flow with planted flaws + clean control pages
  planted_flaws.yaml      # ground truth used by the e2e test
examples/                 # sample survey CSV, reviews, real-user results, sensor CSV
tests/
  unit/<module>/…
  e2e/test_demo_app_offline.py   # scripted FakeLLM: pipeline correctness
  e2e/test_demo_app_live.py      # real model, @pytest.mark.live: the quality bar
```

### Demo app with planted flaws

| Flaw | How it is planted | Expected finding |
|------|-------------------|------------------|
| Hidden button | "Continue" is light grey on white, below the fold on mobile | Can't find how to proceed |
| Confusing form | Ambiguous labels ("ID"), an unexplained date format, fields cleared on error | Form confusion, re-entry |
| Slow page | The shipping step delays 6 s with no spinner | Slow load, no feedback |
| Unexplained error | Submit shows "Error 0x3F" without a cause or fix | Error without explanation |
| **Control pages** | A clean login and product page | **No issues** (false-positive check) |

The live e2e test passes when it finds at least 3 of 4 planted flaws (exact
recall is reported) and confirms **zero** issues on the control pages.

## 4. Database (SQLite)

`personas`, `persona_sources`, `runs`, `sessions`, `steps` (screenshot path,
think-aloud JSON, action, signals, frustration), `issues`, `issue_evidence`,
`issue_clusters`, `judge_reviews`, `replays`, `questionnaires`, `llm_cache`,
`llm_usage`, `real_user_studies`, `real_user_issues`, `calibration_runs`,
`calibration_profiles`, `physical_products`, `review_complaints`,
`sensor_findings`. Migrations are numbered SQL files applied at startup.
Screenshots are stored as files under `data/runs/<run_id>/`, not as blobs.

## 5. Build phases (each ends with tests passing + a status note)

| # | Phase | Done when |
|---|-------|-----------|
| 1 | Config, LLM gateway skeleton + cache + cost, sandbox safety, DB | Unit tests: config validation, env secrets, allowlist incl. redirects, guard patterns, migrations, cache-key seeding |
| 2 | Persona ingest, anonymization, trait design, generation, diversity check | Unit tests with FakeLLM; PII redaction tests; diversity regenerates near-duplicates |
| 3 | Browser runner: pixels-only, noise, conditions, patience, frustration, think-aloud, classifier, memory; demo app | Runner tests against demo app with scripted actor; asserts no DOM text in any prompt |
| 4 | Elicitation + judge + evidence + replay + repetition + novelty | Judge rejection loop, replay confirm/not-reproduced, promotion threshold tests |
| 5 | Aggregation + HTML report + demand-forecast filter | Report snapshot test; offline e2e on demo app passes |
| 6 | Calibration import, matching, metrics, tuning, profiles | Metric tests on known fixtures; apply → new profile version |
| 7 | Physical module | Review taxonomy, sensor anomaly tests, forbidden-claim filter |
| 8 | Dashboard (local server over the DB) + README polish | Live e2e run (if API key available) + docs |

## 6. Known risks

* **Coordinate accuracy** of a screenshot-only agent is the main cause of
  false `UX_ISSUE`s. To counter it: the screenshot is scaled to the model's
  native resolution and coordinates are mapped back; the classifier flags
  "clicked on empty area 3+ times while a target was visible" for the judge;
  and replay without noise separates misclicks from real problems.
* **The judge shares the actor's biases** (same model family). To counter
  it: the judge uses a different prompt, a stronger model and only evidence;
  calibration against real users is the real check.
* **Persona realism for disability, age and cultural traits** is weak by
  nature. These results are always flagged low confidence.
