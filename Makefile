.PHONY: up down db test lint diagrams secrets migrate push

up:        ## start the stack
	docker compose up -d --build
down:
	docker compose down
db:        ## only postgres (for local dev / tests)
	docker compose up -d postgres
migrate:
	cd backend && uv run alembic upgrade head
test:
	cd backend && uv run pytest -q
lint:
	cd backend && uv run ruff check . && uv run ruff format --check .
diagrams:  ## render docs/diagrams/*.mmd -> svg + png
	cd docs/diagrams && for f in *.mmd; do \
	  npx -y @mermaid-js/mermaid-cli -p ../../infra/puppeteer.json -i $$f -o $${f%.mmd}.svg -b white && \
	  npx -y @mermaid-js/mermaid-cli -p ../../infra/puppeteer.json -i $$f -o $${f%.mmd}.png -b white -s 2; done
secrets:   ## print fresh random secrets for .env
	@python3 -c "import os,base64,secrets;print('PII_ENCRYPTION_KEY='+base64.b64encode(os.urandom(32)).decode());print('PII_HASH_PEPPER='+secrets.token_urlsafe(32));print('LANGGRAPH_AES_KEY='+secrets.token_hex(16))"
push:      ## push using PAT_GITHUB from .env (token never stored in git config)
	@PAT=$$(grep ^PAT_GITHUB= .env | cut -d= -f2-); \
	git -c http.extraHeader="Authorization: Basic $$(printf 'x-access-token:%s' "$$PAT" | base64 -w0)" push -q origin HEAD
