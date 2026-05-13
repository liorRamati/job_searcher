# Job Search Agent

An automated job search tool that scrapes job boards across dozens of companies, filters listings against your personal profile, scores them for relevance, and writes the results to a Google Sheet — so every weekday morning you wake up to a curated list of new openings.

## How it works

Jobs flow through a four-stage pipeline:

```
Scrape → Disqualify → Score → (optional) LLM Filter → Google Sheets
```

1. **Scrape** — Each enabled company is queried via its ATS API or a Playwright browser session. Raw job postings are normalised into a common format.
2. **Disqualify** — Hard rules drop jobs that are clearly wrong: wrong location, fully remote, excluded title patterns, forbidden technologies, or experience requirements you can't meet. Runs entirely in code with no API calls.
3. **Score** — Each surviving job receives a 0–100 score across five dimensions: title match (25 pts), tech-stack overlap (30 pts), location tier (20 pts), work arrangement (10 pts), and experience fit (15 pts).
4. **LLM Filter** *(optional)* — A local or cloud LLM re-classifies jobs using your profile and five-step reasoning: location, hard disqualifiers, role-category alignment, experience fit, and domain fit. More accurate than the rule-based scorer alone, especially for borderline or generically-titled roles.
5. **Google Sheets** — Qualified jobs are appended as rows with score, tech stack found, extracted requirements, and optionally a cover letter.

### Scoring modes

| Mode | How to invoke | What it does |
|---|---|---|
| Strict (default) | `python main.py` | Requires title keywords + score ≥ threshold. Fast, no LLM calls. |
| LLM filter | `python main.py --llm-filter` | Loosens scorer thresholds, then uses LLM for final classification. Slower but more accurate. |

---

## Setup

### 1. Install dependencies

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

Install Playwright for JavaScript-rendered job pages (Check Point, Comeet, Meta, etc.):

```bash
playwright install chromium
playwright install-deps chromium  # Linux only
```

### 2. Set up Google Sheets

1. Create a Google Cloud project and enable the **Google Sheets API**.
2. Create a **Service Account**, download its JSON key, and save it to `config/google_credentials.json`.
3. Create a Google Sheet and share it with the service account's email address (editor access).
4. Copy the spreadsheet ID from the URL (`https://docs.google.com/spreadsheets/d/<ID>/edit`) into `config/settings.yaml` under `google_sheets.spreadsheet_id`, or set the `GOOGLE_SPREADSHEET_ID` environment variable.

### 3. Configure the LLM (for LLM filter and cover letters)

**Option A — Local model via LM Studio:**

1. Download [LM Studio](https://lmstudio.ai/) and load a model (recommended: Gemma 4 27B or similar).
2. Start the local server on port 1234.
3. In `config/settings.yaml`, set `llm.provider: lm_studio` and update `llm.lm_studio.model` to your model name.

**Option B — Anthropic Claude:**

1. Get an API key from [console.anthropic.com](https://console.anthropic.com).
2. Add `ANTHROPIC_API_KEY=your_key` to your `.env` file.
3. In `config/settings.yaml`, set `llm.provider: claude`.

### 4. Environment variables

Create a `.env` file in the project root:

```env
ANTHROPIC_API_KEY=sk-ant-...          # only needed for Claude provider
GOOGLE_SPREADSHEET_ID=1BxiM...        # overrides the value in settings.yaml
```

---

## Configuration

### `config/settings.yaml`

Main settings file. Key sections:

```yaml
agent:
  score_threshold: 60        # jobs below this score are not written to Sheets
  profile_path: config/profile.json

search:
  max_job_age_days: 7        # ignore jobs older than this
  job_titles:                # keywords sent to scrapers that support search APIs
    - "algorithm developer"  # (workday, google, amazon, meta, eightfold, phenom)
    - "ML engineer"          # joined as "term1 OR term2 OR ..." in each request
    - "data scientist"

scraping:
  request_delay_seconds: 1.0  # delay between requests per company
  timeout_seconds: 30

llm:
  provider: lm_studio        # claude | lm_studio
  lm_studio:
    base_url: http://localhost:1234/v1
    model: gemma-4-26b-a4b-it

llm_filter:
  batch_size: 5              # jobs per LLM call
  min_score_before_llm: 30  # pre-filter threshold before sending to LLM

disqualifier:
  disqualify_remote: true    # set to false to allow fully-remote jobs through

scoring:
  # Keywords that must appear in a title for it to score any title-match points.
  # Only applied in strict mode (without --llm-filter). Tailor to your domain.
  title_required_keywords:
    - "data"
    - "ml"
    - "machine learning"
    - "algorithm"
  title_min_threshold: 50    # minimum fuzzy-match score (0-100) vs your target titles
  tech_full_match_count: 6   # skill matches needed for maximum tech-stack score

schedule:
  enabled: false
  cron: "0 8 * * 1-5"       # run at 08:00 Monday–Friday
```

**`search.job_titles`** — Scrapers that expose a keyword search API (Workday, Google, Amazon, Meta, Eightfold, PhenomPeople) use this list to pre-filter results server-side, reducing the number of irrelevant jobs that reach the pipeline. Scrapers that have no search API ignore this field and return all open positions.

**`disqualifier.disqualify_remote`** — When `true` (the default), jobs whose location field is blank or explicitly marked remote are dropped before scoring. Set to `false` if you are open to remote work. Note: in LLM-filter mode, geographic reasoning is handled by the LLM using `locations.yaml`, so this toggle mainly affects the fast rule-based pass.

**`scoring`** — Controls the rule-based scorer thresholds. Structural weights (how many points each dimension is worth) are defined in `pipeline/scorer.py`. These settings let you tune sensitivity without touching code.

---

### `config/profile.json`

Your personal profile. Drives both the hard disqualifier and the scorer. You can generate this file from a resume:

```bash
python main.py --parse-resume /path/to/resume.pdf
```

Or maintain it by hand. Key fields:

```json
{
  "name": "Your Name",
  "seniority": "mid",              // junior | mid | senior
  "years_of_experience": 3,
  "previous_titles": ["Software Engineer", "Algorithm Engineer"],
  "target_titles": ["Machine Learning Engineer", "Data Scientist"],
  "skills": {
    "languages": ["Python", "SQL"],
    "frameworks": ["PyTorch", "Pandas"],
    "tools": ["Git", "Docker"],
    "domains": ["machine learning", "computer vision", "optimization"]
  },
  "hard_disqualifiers": {
    "excluded_titles": ["Manager", "VP", "Director"],
    "excluded_title_keywords": ["frontend", "full-stack", "devops"],
    "excluded_keywords": ["FPGA", "PHP"],
    "dominant_tech_stacks": [
      {
        "name": "Frontend/JS",
        "title_keywords": ["react developer", "angular engineer"],
        "body_keywords": ["react", "angular", "typescript", "javascript", "html", "css"],
        "body_threshold": 4
      }
    ],
    "min_years_required_max": 7
  }
}
```

**Hard disqualifier rules:**

| Field | What triggers it | Match location |
|---|---|---|
| `excluded_titles` | Seniority/role level (Manager, VP, Lead) | Title only, word-boundary regex |
| `excluded_title_keywords` | Tech-role direction (frontend, devops) | Title only, substring |
| `excluded_keywords` | Absolute deal-breakers (FPGA) | Title + description |
| `dominant_tech_stacks` | Tech is the primary focus of the job | Title (any keyword) OR description (N+ mentions) |
| `min_years_required_max` | Job requires more years than this | Description |

> **Note:** The `hard_disqualifiers` block in `profile.json` is normally generated by `--parse-resume` and should not be edited by hand — it will be overwritten on the next resume parse. Put your rules in `config/disqualifiers.yaml` instead; they are merged into the profile automatically after each parse run.

---

### `config/disqualifiers.yaml`

Hard-disqualifier rules stored separately from the profile so they survive resume re-parsing. When you run `--parse-resume`, the LLM re-generates your profile from your CV — but the rules in this file are merged back in at the end, so your carefully tuned filters are never overwritten.

The fields here are identical to the `hard_disqualifiers` block in `profile.json`:

```yaml
excluded_titles:             # exact role-level words — matched word-boundary in title
  - "Manager"
  - "Director"
  - "VP"
  - "Head of"
  - "Team Lead"

excluded_title_keywords:     # substring match in title — drops clear role-direction mismatches
  - "frontend"
  - "full-stack"
  - "devops"

excluded_keywords:           # matched anywhere in title + description — absolute deal-breakers
  - "FPGA"
  - "Embedded"
  - "PHP"

dominant_tech_stacks:        # drops jobs where a tech stack you don't use is the primary focus
  - name: "Frontend/JS"
    title_keywords:          # matched in title → immediate disqualify (no body check)
      - "react developer"
      - "angular engineer"
    body_keywords:           # matched in description
      - "react"
      - "angular"
      - "typescript"
      - "javascript"
      - "html"
      - "css"
    body_threshold: 4        # disqualify if this many body_keywords appear

  - name: "C/C++"
    title_keywords:
      - "c++ developer"
      - "c/c++ engineer"
    body_keywords:
      - "c++"
      - "c/c++"
    body_threshold: 4

min_years_required_max: 7    # drop jobs that explicitly require more years than this
```

> **Tip:** `dominant_tech_stacks` uses a two-signal approach: a title match alone is enough to disqualify (the job is clearly about that tech), but a description match requires at least `body_threshold` occurrences (the tech could just be mentioned in passing otherwise).

---

### `config/companies.yaml`

List of companies to scrape. Each entry has:

```yaml
- name: Company Name
  ats: greenhouse          # ATS type (see supported platforms below)
  slug: company-slug       # used by API-based scrapers
  career_url: https://...  # used by URL-based scrapers
  enabled: true
```

To add a company, find its ATS platform and add an entry with the right `ats` type and either a `slug` or `career_url`. Disable a company without deleting it by setting `enabled: false`.

**Supported ATS platforms:**

| `ats` value | Platform | Config needed |
|---|---|---|
| `greenhouse` | Greenhouse (API) | `slug` |
| `lever` | Lever (API) | `slug` |
| `smartrecruiters` | SmartRecruiters (API) | `slug`; optionally `career_url` for country filter |
| `workday` | Workday (REST API) | `career_url` (full Workday jobs URL) |
| `comeet` | Comeet (API) | `slug` in `"uid:token"` format |
| `eightfold` | Eightfold.ai | `career_url` in `"base_url\|domain\|location"` format |
| `gem` | Gem.com | `slug` |
| `phenom` | PhenomPeople | `career_url` in `"base_url\|page_id\|company_id"` format |
| `mobileye` | Mobileye (custom) | `career_url` |
| `varonis` | Varonis (custom) | `career_url` |
| `allot` | Allot (WordPress) | `career_url` |
| `radware` | Radware (Taleo) | `career_url` |
| `checkpoint` | Check Point | `career_url` |
| `google` | Google Careers | `career_url` |
| `amazon` | Amazon Jobs | `career_url` |
| `apple` | Apple Jobs | `career_url` |
| `ibm` | IBM Jobs | `career_url` |
| `meta` | Meta Careers | `career_url` |
| `elbit` | Elbit Systems | `career_url` |
| `akamai` | Akamai | `career_url` |
| `towersemi` | Tower Semiconductor | `career_url` |
| `custom` | Generic Playwright scraper | `career_url` |

---

### `config/locations.yaml`

Defines your target geographic areas. Used by the disqualifier (in non-LLM mode) and by the scorer for location-based points.

```yaml
main_areas:
  - City a area from city/neighborhood b to city/neighborhood c   # human-readable, used by LLM filter

areas:
  area_name:
    tier: 1               # 1 = best location score, 2 = good, 3 = acceptable
    cities:
      - City Name
      - Another City

train_cities:
  - City Name             # cities with direct train access get a +3 score bonus
```

The `main_areas` list is passed to the LLM filter as a geographic description. The `areas` dict with explicit city lists is used by the rule-based disqualifier and scorer.

---

### `config/tech_keywords.yaml`

Maps skill names to search terms so the scorer can find them even when the job description uses different wording.

```yaml
pytorch:
  - pytorch
  - torch          # common import alias

scikit-learn:
  - scikit-learn
  - sklearn
```

Each key should match a skill name in your `profile.json`. If a skill has no entry, the skill name itself is used as the only search term.

---

## Usage

```bash
# Full run — scrape all companies and write new jobs to Google Sheets
python main.py

# Dry run — print results without writing to Sheets
python main.py --dry-run

# Verbose dry run — show every job and why it was kept or filtered
python main.py --dry-run --verbose

# Limit to specific companies
python main.py --companies taboola nvidia --dry-run

# Use LLM for more accurate filtering (requires LLM configured)
python main.py --llm-filter --dry-run

# Generate cover letters for top-scoring jobs
python main.py --llm-filter --generate-cover-letters

# Parse resume to generate/update config/profile.json
python main.py --parse-resume /path/to/resume.pdf

# Validate config without making any API calls
python main.py --validate
```

### Logging

All runs write structured logs to `logs/job_searcher.log` (rotating, max 10 MB × 5 backups) and a separate `logs/errors.log` for warnings and above. Console output is clean one-line messages by default; pass `--verbose` to also print debug-level details (per-job scoring, LLM batch decisions, deduplication counts) to the console — the same information is always written to the log file regardless.

```bash
# Show per-job detail on screen and in log
python main.py --dry-run --verbose
```

---

### Scheduling

To run automatically on a schedule:

```bash
# Run once immediately
python schedule_agent.py --run-once

# Start the scheduler (uses cron expression from settings.yaml)
python schedule_agent.py --enable
```

Or set `schedule.enabled: true` in `config/settings.yaml` and run `python schedule_agent.py`.

---

## Project structure

```
├── main.py                   entry point — orchestrates the full pipeline
├── logging_config.py         sets up console + rotating-file log handlers
├── schedule_agent.py         APScheduler wrapper for automated runs
├── config/
│   ├── settings.yaml         main configuration (LLM, scoring, scheduling, disqualifier toggles)
│   ├── profile.json          user profile (skills, targets, disqualifiers — auto-generated)
│   ├── disqualifiers.yaml    hard-disqualifier rules (edit here, not in profile.json)
│   ├── companies.yaml        companies to scrape with ATS type and URL/slug
│   ├── locations.yaml        target cities and area tiers
│   ├── tech_keywords.yaml    skill name → search term aliases
│   └── google_credentials.json   Google Sheets service account key
├── scrapers/                 one file per ATS platform
├── pipeline/
│   ├── disqualifier.py       hard rules — drops clearly irrelevant jobs
│   ├── scorer.py             soft scoring — ranks surviving jobs 0–100
│   ├── llm_filter.py         LLM-based relevance classification
│   ├── translator.py         Hebrew → English translation (cached)
│   └── cover_letter.py       LLM cover letter generation
├── models/
│   ├── job.py                RawJob and ScoredJob data models
│   └── profile.py            UserProfile data model
├── llm/
│   ├── claude_client.py      Anthropic Claude client
│   └── lm_studio_client.py   OpenAI-compatible client (LM Studio, Ollama)
├── output/
│   └── google_sheets.py      writes results to Google Sheets
├── resume/
│   └── parser.py             PDF/Word → profile.json via LLM
└── tests/                    pytest test suite
```
