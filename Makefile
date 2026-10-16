.PHONY: up down test test-db lint dados pdf

up:            ## gera os dados, carrega e sobe o painel (http://localhost:8503)
	docker compose up --build

down:          ## derruba os containers e apaga o banco
	docker compose down -v

test:          ## suite sem banco e sem Chrome
	python -m pytest -m "not db and not chrome" -q

test-db:       ## testes que exigem PostgreSQL (PG_HOST/PG_USER/PG_PASS do ambiente)
	python -m pytest -m "db and not chrome" -q

lint:
	ruff check .

dados:         ## gera os CSV sinteticos em ./dados
	python -m gerador --saida dados

pdf:           ## PDF de uma empresa no container do app: make pdf EMPRESA=103
	docker compose exec app python scripts/gen_pdf.py $(EMPRESA) 2025-01 2025-12
