.PHONY: help install server mock tui web test difficulty guild e2e clean reset

PY ?= python
HOST ?= 127.0.0.1
PORT ?= 8787

help:
	@echo "AI WoW Simulator — make targets:"
	@echo "  install   - pip install -r requirements.txt"
	@echo "  server    - start FastAPI server (foreground)"
	@echo "  mock      - start server + run 5 mock AI agents"
	@echo "  tui LANG  - start server + run rich TUI observer (LANG=zh|en, default zh)"
	@echo "  web       - start server + open browser to observer"
	@echo "  test      - run pytest smoke tests"
	@echo "  difficulty - run difficulty balance check"
	@echo "  guild     - run guild CLI smoke"
	@echo "  e2e       - run end-to-end test"
	@echo "  reset     - delete data/world.db and restart fresh"
	@echo "  clean     - clean pyc + logs"

install:
	$(PY) -m pip install -r requirements.txt

server:
	$(PY) -m server.main

mock:
	@$(PY) -m server.main & echo $$! > .wow.pid; sleep 2; \
	  $(PY) -m mock_agents.run_demo --n 5; \
	  kill `cat .wow.pid` 2>/dev/null || true; rm -f .wow.pid

tui:
	@LANG=$(or $(LANG),zh); $(PY) -m server.main & echo $$! > .wow.pid; sleep 2; \
	  $(PY) -m terminal.observer_tui --lang $$LANG; \
	  kill `cat .wow.pid` 2>/dev/null || true; rm -f .wow.pid

web:
	@$(PY) -m server.main & echo $$! > .wow.pid; sleep 2; \
	  sleep 999999 & waiter=$$!; \
	  $(PY) -c "import webbrowser,time; time.sleep(1); webbrowser.open('http://$(HOST):$(PORT)/')"; \
	  wait $$waiter; kill `cat .wow.pid` 2>/dev/null || true; rm -f .wow.pid

test:
	$(PY) -m pytest tests/ -v

difficulty:
	$(PY) scripts/difficulty_check.py

guild:
	$(PY) scripts/guild_cli.py smoke

e2e:
	$(PY) scripts/e2e_test.py

reset:
	rm -f data/world.db data/world.db-* .wow.pid

clean:
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -f logs/*.log
