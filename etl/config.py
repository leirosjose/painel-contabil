# ---------------------------------------------------------------------------
# config.py — Constantes de conexão e parâmetros globais do ETL
# ---------------------------------------------------------------------------
import os
from dotenv import load_dotenv

load_dotenv()

# ── PostgreSQL (destino) ───────────────────────────────────────────────────
PG_HOST   = os.getenv("PG_HOST", "localhost")
PG_PORT   = int(os.getenv("PG_PORT", "5432"))
PG_DB     = os.getenv("PG_DB",   "painel_bi")
PG_USER   = os.getenv("PG_USER", "postgres")
# Sem default: None = ausente no .env (connect_postgres faz fail-fast
# com mensagem clara); "" explicito (PG_PASS=) = trust auth local (dev).
PG_PASS   = os.getenv("PG_PASS")

# ── Saldo inicial ──────────────────────────────────────────────────────────
# O corte de referencia do projeto: o si CANONICO e "soma de lancamentos <
# 2023-01-01", que e onde a historia do raw comeca. Vive aqui para que todos
# os consumidores (ex.: o expurgo de geracoes do load) leiam o mesmo literal.
CORTE_CANONICO = "2023-01-01"

# ── Schemas no PostgreSQL ──────────────────────────────────────────────────
PG_SCHEMA_RAW  = "raw"
PG_SCHEMA_MART = "mart"
# As linhas fisicas do raw moram em raw_hist, carimbadas por geracao;
# raw.<tabela> e uma VIEW do estado vigente.
PG_SCHEMA_RAW_HIST = "raw_hist"
VERSAO_SCHEMA_GERACAO = "2026-09-10-geracao-raw"
# ops.alteracoes_retroativas: aditivo (CREATE IF NOT EXISTS na guarda de boot);
# codigo velho ignora a tabela, entao NAO exige janela de manutencao.
VERSAO_ALTERACOES_RETROATIVAS = "2026-09-24-alteracoes-retroativas"
