# Driving Exam Tickets

A browser-based app for practising road traffic exam questions extracted from PDF tickets.

## Prerequisites

- Python 3.11+

## Quick Start

```bash
# 1. Create a virtual environment and activate it
python3 -m venv .venv
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Start the web app
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Pre-extracted images and `questions.json` are already included in the repo, so you can start the app right away. Re-running extraction is only needed when you add new PDFs (see below).

Open [http://localhost:8000](http://localhost:8000) in your browser.

## Adding PDFs

PDFs are stored in the `pdfs/` directory, organized by language code:

```
pdfs/
├── ru/          # Russian
│   ├── ticket1.pdf
│   └── ticket2.pdf
├── hy/          # Armenian
│   ├── ticket1.pdf
│   └── ...
└── en/          # English (or any other language code)
    └── ...
```

To add questions in a new language (e.g. Armenian):

1. Create the directory: `mkdir -p pdfs/hy`
2. Place the PDF files inside `pdfs/hy/`
3. Re-run extraction: `python extract_questions.py`
4. Restart the server

The language selector in the app header lets you switch between available languages.

## Explain Feature (optional)

The app can call an external LLM API to explain answers. To enable it:

1. Copy the example env file: `cp .env.example .env`
2. Fill in your API key and specify model provider URL in `.env`

Without the key the app works normally — the "Explain" button will just show an error.

## Project Structure

```
├── pdfs/                  # Source PDF tickets (by language)
├── media/                 # Extracted question images (tracked via Git LFS)
├── questions.json         # Extracted questions
├── extract_questions.py   # PDF → questions.json + images
├── app/
│   └── main.py            # FastAPI backend
├── static/
│   ├── index.html         # Frontend HTML
│   ├── app.js             # Frontend logic
│   └── styles.css         # Styles
├── requirements.txt
└── .env.example
```
