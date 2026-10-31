.PHONY: install doctor seed run backtest ui webhook test guard clean all

PY ?= python3

install:            ## install dependencies
	$(PY) -m pip install -r requirements.txt

doctor:             ## auth + field-group entitlement probe — RUN THIS FIRST
	$(PY) -m prahari.cli doctor

seed:               ## load the ledger + replay fixtures (no network needed)
	$(PY) -m prahari.cli seed --fresh

baseline:           ## identity resolution + live first snapshots (needs a key)
	$(PY) -m prahari.cli baseline

run:                ## score every vendor, print the priority queue
	$(PY) -m prahari.cli run

backtest:           ## time-travel replay + validation table
	$(PY) -m prahari.cli backtest

guard:              ## prove the citation guard refuses an uncited claim
	$(PY) -m prahari.cli guard-demo

ui:                 ## one-page operator console
	streamlit run app/streamlit_app.py

webhook:            ## signed Crustdata watcher receiver
	uvicorn prahari.webhook:app --port 8000 --reload

test:               ## run the test suite
	$(PY) -m pytest tests/ -q

fixtures:           ## regenerate the synthetic replay fixtures
	$(PY) scripts/make_fixtures.py

demo: seed backtest guard run   ## the whole offline demo path, in order

clean:
	rm -f prahari.db data/backtest_report.json data/entitlements.json
	find . -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null || true

all: install seed test backtest
