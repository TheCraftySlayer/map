# Operator workflow for the Bernalillo County spatial-equity map.
#
# Conventions:
#   * Plaintext lives under _work/ (gitignored). Never check it in.
#   * Passwords come from the env: PUB and STAFF. Set them once
#     per shell, not per command.
#   * Most targets are idempotent — re-run safely after fixing a
#     hook complaint.
#
# Quick reference:
#   make help        — list targets
#   make test        — full unittest suite
#   make decrypt     — _work/index_body.html etc. from the deployed .enc
#   make patch       — apply patch_body.py to _work/index_body.html
#   make encrypt     — re-encrypt _work/index_body.html into the deploy bundle
#   make check       — verify the deploy bundle decrypts (no plaintext written)
#   make rebuild     — full body-edit cycle (decrypt → patch → encrypt → clean)
#   make rotate-staff — rotate staff salt only (preserves public ciphertext)
#   make hooks       — install .githooks/pre-commit (one-time per checkout)

WORK ?= _work
SRC ?= .
PUB ?=
STAFF ?=

.PHONY: help test decrypt patch encrypt check rebuild rotate-staff rotate-public hooks clean

help:
	@echo "Targets: test decrypt patch encrypt check rebuild rotate-{staff,public} hooks clean"
	@echo "Env: PUB=<public-pw> STAFF=<staff-pw> WORK=$(WORK) SRC=$(SRC)"

test:
	python -m unittest discover -s tests

decrypt:
	@if [ -z "$(STAFF)" ]; then echo "set STAFF=<staff-password>"; exit 2; fi
	mkdir -p $(WORK)
	python decrypt_data.py --staff-password "$(STAFF)" --src $(SRC) --out $(WORK)

patch:
	@if [ ! -f $(WORK)/index_body.html ]; then \
		echo "no $(WORK)/index_body.html — run 'make decrypt' first"; exit 2; fi
	python patch_body.py $(WORK)/index_body.html

encrypt:
	@if [ -z "$(PUB)" ] || [ -z "$(STAFF)" ]; then \
		echo "set PUB=<public-pw> STAFF=<staff-pw>"; exit 2; fi
	@if [ ! -f $(WORK)/index_body.html ]; then \
		echo "no $(WORK)/index_body.html — run 'make patch' first"; exit 2; fi
	python encrypt_data.py \
		--public-password "$(PUB)" --staff-password "$(STAFF)" \
		--body $(WORK)/index_body.html --out .

check:
	@if [ -z "$(STAFF)" ]; then echo "set STAFF=<staff-password>"; exit 2; fi
	python decrypt_data.py --staff-password "$(STAFF)" --src $(SRC) --check

# Full body-edit cycle. Bails out if any step fails.
rebuild: decrypt patch encrypt clean

rotate-staff:
	@if [ -z "$(STAFF)" ]; then echo "set STAFF=<new-staff-password>"; exit 2; fi
	python encrypt_data.py --rotate-tier staff \
		--staff-password "$(STAFF)" --src $(SRC) --out public_rotated
	@echo "Rotated bundle in public_rotated/. Review then upload to deploy branch."

rotate-public:
	@if [ -z "$(PUB)" ]; then echo "set PUB=<new-public-password>"; exit 2; fi
	python encrypt_data.py --rotate-tier public \
		--public-password "$(PUB)" --src $(SRC) --out public_rotated
	@echo "Rotated bundle in public_rotated/. Review then upload to deploy branch."

hooks:
	@if [ ! -d .git ]; then echo "not a git checkout"; exit 2; fi
	git config core.hooksPath .githooks
	@echo "git hooks path -> .githooks/"
	@ls -la .githooks/

clean:
	rm -rf $(WORK)
