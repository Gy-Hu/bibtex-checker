# bibtex-checker

Utility script that verifies BibTeX entries against the Semantic Scholar proxy API. API can be accessed at https://lifuai.com/.

## Requirements

- Python 3.9 or newer
- Semantic Scholar proxy API key (set `LIFUAI_API_KEY` or pass `--api-key`)

Install the single runtime dependency with:

```bash
pip install -r requirements.txt
```

## Environment Setup

Create an isolated virtual environment and install dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

The commands below assume the virtual environment is active. You can also prefix paths with `.venv/bin/` instead of activating the environment.

## Usage

1. Provide your API key via `export LIFUAI_API_KEY=...` or `--api-key`.
2. Run the verifier against your BibTeX file:

   ```bash
   python verify_refs.py --bib-path refs.bib --delay 1 --stop-on-failure
   ```

   Drop `--stop-on-failure` to review every citation; adjust `--delay`, `--max-retries`, and `--backoff` if you encounter rate limits.

## Current Verification Status

- The command above verified the first 15 entries and halted on `Russo2017` due to the known year mismatch (API reports 2017 vs. BibTeX 2018).

## Next Steps

1. Re-run without `--stop-on-failure` to review remaining mismatches (e.g., vendor manuals and the Thompson Sampling survey) and decide which fields need updates or skips.
