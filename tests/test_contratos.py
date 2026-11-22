"""Testes dos contratos de dados e do validador leve.

Rode:  python3 -m pytest -q          (offline, stdlib + pytest; nada de rede)

Cobrem três riscos reais:
  1. um exemplo do repositório deixar de bater com o schema (o contrato muda e
     o README fica contando história antiga);
  2. o validador leve aprovar coisa errada - o que é pior do que não ter teste,
     porque dá confiança falsa;
  3. vocabulário proibido (padrões de dado sensível) entrar num arquivo do repo.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys

import pytest

TESTES = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(TESTES)
SCRIPT = os.path.join(REPO, "scripts", "valida_contratos.py")
SCHEMAS = os.path.join(REPO, "schemas")
EXEMPLOS = os.path.join(REPO, "schemas", "exemplos")


def _carregar_modulo():
    especificacao = importlib.util.spec_from_file_location("valida_contratos", SCRIPT)
    modulo = importlib.util.module_from_spec(especificacao)
    especificacao.loader.exec_module(modulo)
    return modulo


PARES = [
    ("alerta-normalizado", ["fingerprint", "servico", "sinal", "correlacao"]),
    ("contexto-servico", ["servico", "mudancas_recentes", "qualidade"]),
    ("proposta-agente", ["acoes", "nao_recomendado", "guardrails", "execucao", "auditoria"]),
]


@pytest.mark.parametrize("nome,campos", PARES)
def test_schema_e_json_validos(nome, campos):
    schema = json.load(open(os.path.join(SCHEMAS, f"{nome}.schema.json"), encoding="utf-8"))
    instancia = json.load(open(os.path.join(EXEMPLOS, f"{nome}.exemplo.json"), encoding="utf-8"))
    assert schema["$schema"].endswith("2020-12/schema")
    for campo in campos:
        assert campo in schema["properties"], f"{nome}: schema perdeu o campo {campo}"
        assert campo in instancia, f"{nome}: exemplo perdeu o campo {campo}"


def test_exemplos_conforme_o_validador_leve():
    modulo = _carregar_modulo()
    erros = []
    for nome, _ in PARES:
        schema = json.load(open(os.path.join(SCHEMAS, f"{nome}.schema.json"), encoding="utf-8"))
        instancia = json.load(open(os.path.join(EXEMPLOS, f"{nome}.exemplo.json"), encoding="utf-8"))
        modulo.validar(schema, instancia, schema, "$", erros)
    assert erros == [], "\n".join(erros)


def test_processo_integro_retorna_zero():
    resultado = subprocess.run(
        [sys.executable, SCRIPT, "--sem-varredura"], capture_output=True, text=True, cwd=REPO)
    assert resultado.returncode == 0, resultado.stdout + resultado.stderr
    assert "0 violação(ões) de contrato" in resultado.stdout


def test_validador_pega_campo_obrigatorio_auscente(tmp_path):
    modulo = _carregar_modulo()
    schema = json.load(open(os.path.join(SCHEMAS, "proposta-agente.schema.json"), encoding="utf-8"))
    instancia = json.load(open(os.path.join(EXEMPLOS, "proposta-agente.exemplo.json"), encoding="utf-8"))
    del instancia["guardrails"]
    del instancia["diagnostico"]["hipoteses"][0]["como_refutar"]
    erros = []
    modulo.validar(schema, instancia, schema, "$", erros)
    assert any("guardrails" in e and "obrigatório" in e for e in erros), erros
    assert any("como_refutar" in e for e in erros), erros


def test_validador_pega_valor_fora_de_enum(tmp_path):
    modulo = _carregar_modulo()
    schema = json.load(open(os.path.join(SCHEMAS, "proposta-agente.schema.json"), encoding="utf-8"))
    instancia = json.load(open(os.path.join(EXEMPLOS, "proposta-agente.exemplo.json"), encoding="utf-8"))
    # a regra estrutural mais importante do projeto: não existe modo de execução
    # sem aprovação humana no vocabulário
    instancia["acoes"][1]["modo"] = "auto-execucao-sem-aprovacao"
    erros = []
    modulo.validar(schema, instancia, schema, "$", erros)
    assert any("enum" in e for e in erros), erros


def test_validador_pega_campo_fora_do_contrato():
    modulo = _carregar_modulo()
    schema = json.load(open(os.path.join(SCHEMAS, "alerta-normalizado.schema.json"), encoding="utf-8"))
    instancia = json.load(open(os.path.join(EXEMPLOS, "alerta-normalizado.exemplo.json"), encoding="utf-8"))
    instancia["servico"]["numero_cartao"] = "5" * 16          # construído, não escrito
    instancia["tokens_para_llm"] = ["não é parte do contrato"]
    erros = []
    modulo.validar(schema, instancia, schema, "$", erros)
    assert any("numero_cartao" in e for e in erros), erros
    assert any("tokens_para_llm" in e for e in erros), erros


def test_validador_pega_data_hora_invalida():
    modulo = _carregar_modulo()
    schema = json.load(open(os.path.join(SCHEMAS, "alerta-normalizado.schema.json"), encoding="utf-8"))
    instancia = json.load(open(os.path.join(EXEMPLOS, "alerta-normalizado.exemplo.json"), encoding="utf-8"))
    instancia["recebido_em"] = "14/05/2026 17:32"
    erros = []
    modulo.validar(schema, instancia, schema, "$", erros)
    assert any("ISO-8601" in e for e in erros), erros


def test_varredura_pega_padrao_proibido_fora_do_repo(tmp_path):
    """A varredura tem que pegar segredo/cartão/IP se um dia entrarem num arquivo.

    Os ganchos são montados por composição (não escritos literalmente neste
    arquivo) exatamente para que este teste não seja, ele mesmo, um achado.
    """
    modulo = _carregar_modulo()
    cartão = "4" * 15 + "1"
    cnpj = ".".join(["12", "345", "678"]) + "/" + "0001" + "-95"
    ip = ".".join(["203", "0", "113", "99"])
    chave = "AK" + "IA" + "A" * 16
    (tmp_path / "suspeito.md").write_text(
        f"nota {cartão}\ncontato {ip}\naccess {chave}\ncnpj {cnpj}\n"
        "senha em linha: " + '"' + "x" * 12 + '"' + "\n", encoding="utf-8")
    achados = modulo.varrer(str(tmp_path))
    rotulos = " ".join(achados)
    assert "cartao-16-digitos" in rotulos, achados
    assert "ipv4-roteavel" in rotulos, achados
    assert "chave-de-acesso-nuvem" in rotulos, achados
    assert "documento-com-mascara" in rotulos, achados


def test_varredura_do_repositorio_esta_limpa():
    modulo = _carregar_modulo()
    achados = modulo.varrer(REPO)
    assert achados == [], "vocabulário proibido entrou no repositório:\n" + "\n".join(achados)


def test_nenhum_exemplo_contem_chave_de_correlacao_completa_fora_do_contrato():
    """`correlation_id` é dado operacional: o schema permite, o exemplo trunca."""
    instancia = json.load(open(os.path.join(EXEMPLOS, "alerta-normalizado.exemplo.json"), encoding="utf-8"))
    assert "trunc:correlation_id" in instancia["redacao_aplicada"]
    assert len(instancia["correlacao"]["correlation_id"]) == 32


def test_readme_cita_campos_que_existem_no_contrato():
    readme = os.path.join(REPO, "README.md")
    if not os.path.exists(readme):
        pytest.skip("README ainda não escrito")
    texto = open(readme, encoding="utf-8").read()
    for campo in ("fingerprint", "correlation_id", "burn_rate", "nao_recomendado", "kill_switch_ativo"):
        assert campo in texto, f"README não menciona `{campo}`, que existe no contrato"
