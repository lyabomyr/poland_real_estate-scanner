.PHONY: help install run dry config clean reset-db

help:
	@awk 'BEGIN{FS=":.*##"; printf "Usage: make <target>\n\nTargets:\n"} /^[a-zA-Z_-]+:.*##/{printf "  \033[36m%-12s\033[0m %s\n",$$1,$$2}' $(MAKEFILE_LIST)

install:  ## install python deps into poetry venv
	poetry install --no-root

run: config  ## run the scanner (prints matches + sends to Telegram if configured)
	poetry run python main.py

dry: config  ## dry run — print matches, don't touch DB, don't send Telegram
	poetry run python main.py --dry-run

config:
	@if [ ! -f config.yml ]; then \
		cp config.example.yml config.yml; \
		echo "created config.yml from example — edit it to add Telegram credentials"; \
	fi

reset-db:  ## delete the SQLite dedup store
	rm -f data/seen.db

chats: config  ## list chats the bot has recently seen (for picking chat_id)
	poetry run python main.py --print-chats

prune: config  ## archive + delete rejected rows older than storage.prune_rejected_days
	poetry run python main.py --prune

lint:  ## static-check for unused imports / undefined names
	poetry run pyflakes scanner/ main.py

greet: config  ## announce chat_id in every chat the bot has newly joined
	poetry run python main.py --greet-chats

clean:  ## remove caches and virtualenv
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	poetry env remove --all 2>/dev/null || true
