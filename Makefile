# Everything runs out of a local .venv built from requirements.txt — the same
# file Streamlit Cloud installs from. One dependency list, no Poetry layer,
# and no "more than one requirements file" ambiguity on deploy.
VENV := .venv
PY   := $(VENV)/bin/python
PIP  := $(VENV)/bin/pip
SELECT_PY := ./scripts/select_python.sh

.PHONY: help install run dry config clean reset-db test lint chats prune greet \
        check-dashboard-deps boot-check

help:
	@awk 'BEGIN{FS=":.*##"; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z_-]+:.*##/{printf "  \033[36m%-22s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

$(PY):
	PYTHON_BIN="$$($(SELECT_PY))"; \
	"$$PYTHON_BIN" -m venv $(VENV)
	$(PIP) install --quiet --upgrade pip

install: $(PY)  ## create .venv and install runtime + dev dependencies
	$(PIP) install --quiet -r requirements-dev.txt
	@echo "installed into $(VENV)"

config:
	@if [ ! -f config.yml ]; then \
		cp config.example.yml config.yml; \
		echo "created config.yml from example — edit it to add Telegram credentials"; \
	fi

run: config  ## run the scanner (prints matches + sends to Telegram if configured)
	$(PY) main.py

dry: config  ## dry run — print matches, don't touch DB, don't send Telegram
	$(PY) main.py --dry-run

chats: config  ## list chats the bot has recently seen (for picking chat_id)
	$(PY) main.py --print-chats

greet: config  ## announce chat_id in every chat the bot has newly joined
	$(PY) main.py --greet-chats

pin-dashboard: config  ## (re)post + pin the dashboard link in every chat
	$(PY) main.py --pin-dashboard

prune: config  ## archive + delete rejected rows older than storage.prune_rejected_days
	$(PY) main.py --prune

dashboard: config  ## run the Streamlit dashboard locally on :8501
	$(VENV)/bin/streamlit run dashboard/app.py

reset-db:  ## delete the local SQLite dedup store
	rm -f data/seen.db

lint:  ## static-check for unused imports / undefined names
	$(PY) -m pyflakes scanner/ main.py tests dashboard

test:  ## run unit tests
	$(PY) -m unittest discover -s tests -v

check-dashboard-deps:  ## verify requirements.txt covers every dashboard import
	./scripts/check_dashboard_deps.sh

boot-check:  ## boot the dashboard in a clean venv and hit every page
	./scripts/boot_check.sh

clean:  ## remove caches and the virtualenv
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	rm -rf $(VENV)
