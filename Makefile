# Installing puts `ncl` on PATH so it is callable from anywhere, which is what
# a harness reaching a Nextcloud instance needs — `bin/ncl` works, but only if
# the caller already knows where the checkout is.
#
# The install is editable. A snapshot install keeps working after the source
# changes, and a tool whose failures are mostly silent should not have a stale
# copy of itself as one of them.

.DEFAULT_GOAL := help
.PHONY: help install uninstall test lint build check

help:
	@echo 'make install    put ncl on PATH (editable: tracks this checkout)'
	@echo 'make uninstall  take it off PATH'
	@echo 'make check      every gate: test, lint, build'
	@echo 'make test       uv run pytest'
	@echo 'make lint       uv run ruff check .'
	@echo 'make build      uv build'

# Editable, so the installed command is this checkout rather than a copy of it.
# That also means the checkout has to stay where it is: installing from a
# throwaway worktree leaves an `ncl` on PATH pointing at a directory that is
# about to be deleted.
install:
	uv tool install --force --editable .
	@echo
	@command -v ncl >/dev/null 2>&1 \
		&& echo "installed: $$(command -v ncl)" \
		|| echo "installed, but not on PATH yet — run: uv tool update-shell"
	@echo 'run `ncl` with no arguments for the guide'

uninstall:
	uv tool uninstall ncl

test:
	uv run pytest

lint:
	uv run ruff check .

build:
	uv build

check: test lint build
