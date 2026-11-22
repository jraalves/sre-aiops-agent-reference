#!/usr/bin/env python3
"""valida_contratos.py — valida os exemplos de contrato contra os JSON Schemas.

Por que um validador próprio se existe `jsonschema`? Porque este repositório
precisa verificar os próprios contratos **sem rede e sem pip**: o `make check`
tem que rodar num runner enxuto e no laptop de quem revisa. Este validador cobre
o subconjunto de JSON Schema que os contratos daqui usam:

    type (inclusive lista com "null") · required · properties ·
    additionalProperties:false · items · enum · const · pattern ·
    minLength/maxLength · minimum/maximum/exclusiveMinimum ·
    minItems/maxItems · propertyNames · $ref local (#/$defs/...) · format: date-time

**Ele não é um validador JSON Schema completo.** Não implementa `oneOf`, `anyOf`,
`if/then/else`, `dependentSchemas`, `patternProperties` nem word-count de `format`
além de data-time. Para validação formal use uma implementação certificada:

    python3 -m jsonschema -i schemas/exemplos/alerta-normalizado.exemplo.json \\
        schemas/alerta-normalizado.schema.json

e a checagem de vocabulário de dados (o que nunca pode aparecer num payload):
mesma filosofia do `scripts/scan_anonimia.sh` de outros repositórios — nomes de
empresa/cliente ficam numa lista **fora** do repositório público; aqui só há
padrões estruturais (número de cartão, documento com máscara, chave de nuvem,
token JWT, bloco de chave privada, IP roteável).

Uso:
    python3 scripts/valida_contratos.py                # exemplos + varredura
    python3 scripts/valida_contratos.py --sem-varredura
    python3 scripts/valida_contratos.py --schema schemas/x.json --instancia y.json
    python3 scripts/valida_contratos.py --json         # saída mecanicamente legível

Códigos de saída: 0 ok · 1 violação de contrato · 2 uso incorreto
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from typing import Any

RAIZ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PARES = [
    ("schemas/alerta-normalizado.schema.json", "schemas/exemplos/alerta-normalizado.exemplo.json"),
    ("schemas/contexto-servico.schema.json", "schemas/exemplos/contexto-servico.exemplo.json"),
    ("schemas/proposta-agente.schema.json", "schemas/exemplos/proposta-agente.exemplo.json"),
]

# Padrões estruturais que jamais deveriam aparecer em repositório público de
# portfólio. Nenhum nome de empresa ou cliente entra nesta lista, de propósito.
VARREDURA = {
    "ipv4-roteavel": re.compile(r"\b(?!127\.|10\.|192\.168\.|0\.0\.0\.0)(?:\d{1,3}\.){3}\d{1,3}\b"),
    "chave-de-acesso-nuvem": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    "bloco-de-chave-privada": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "cartao-16-digitos": re.compile(r"\b(?:\d[ -]?){15}\d\b"),
    "documento-com-mascara": re.compile(r"\b\d{2,3}\.\d{3}\.\d{3}/\d{4}-\d{2}\b|\b\d{3}\.\d{3}\.\d{3}-\d{2}\b"),
    "senha-em-linha": re.compile(r"(?i)\b(?:password|passwd|secret|token|api[_-]?key)\s*[:=]\s*['\"][^'\"]{6,}"),
    "string-de-conexao": re.compile(r"(?i)\b(?:postgres|mysql|mongodb(?:\+srv)?|amqp|redis)://[^\s'\"]+:[^\s'\"]+@"),
    "url-de-hostname-interno": re.compile(r"https?://[a-z0-9\-\.]*\.(?:internal|intra|corp|local)(?::\d+)?/"),
}
EXTENSOES_VARRIDAS = (".md", ".json", ".yaml", ".yml", ".py", ".txt", ".sh")
IGNORAR_PASTAS = {".git", ".venv", "__pycache__", "node_modules", "out"}


class Violacao(Exception):
    pass


# --------------------------------------------------------------------------- #
# Mini validador (subconjunto declarado no docstring)
# --------------------------------------------------------------------------- #

def _resolver_ref(ref: str, raiz: dict) -> dict:
    if not ref.startswith("#/"):
        raise Violacao(f"$ref externo não suportado por este validador: {ref}")
    no = raiz
    for parte in ref[2:].split("/"):
        parte = parte.replace("~1", "/").replace("~0", "~")
        if not isinstance(no, dict) or parte not in no:
            raise Violacao(f"$ref quebrado: {ref}")
        no = no[parte]
    return no


def _tipos(schema: dict) -> list[str]:
    tipo = schema.get("type")
    if tipo is None:
        return []
    return tipo if isinstance(tipo, list) else [tipo]


def _compativel(valor: Any, tipo: str) -> bool:
    if tipo == "object":
        return isinstance(valor, dict)
    if tipo == "array":
        return isinstance(valor, list)
    if tipo == "string":
        return isinstance(valor, str)
    if tipo == "integer":
        return isinstance(valor, int) and not isinstance(valor, bool)
    if tipo == "number":
        return isinstance(valor, (int, float)) and not isinstance(valor, bool)
    if tipo == "boolean":
        return isinstance(valor, bool)
    if tipo == "null":
        return valor is None
    return True


def validar(schema: dict, instancia: Any, raiz: dict, caminho: str, erros: list[str]) -> None:
    if "$ref" in schema:
        schema = _resolver_ref(schema["$ref"], raiz)

    tipos = _tipos(schema)
    if tipos and not any(_compativel(instancia, t) for t in tipos):
        erros.append(f"{caminho}: tipo esperado {'|'.join(tipos)}, veio {type(instancia).__name__}")
        return
    if instancia is None and ("null" in tipos or not tipos):
        return

    if "const" in schema and instancia != schema["const"]:
        erros.append(f"{caminho}: valor deve ser exatamente {schema['const']!r}")
    if "enum" in schema and instancia not in schema["enum"]:
        erros.append(f"{caminho}: {instancia!r} fora de enum {schema['enum']}")

    if isinstance(instancia, str):
        if "pattern" in schema and not re.search(schema["pattern"], instancia):
            erros.append(f"{caminho}: {instancia!r} não casa com o pattern do contrato")
        if "minLength" in schema and len(instancia) < schema["minLength"]:
            erros.append(f"{caminho}: mais curto que minLength={schema['minLength']}")
        if "maxLength" in schema and len(instancia) > schema["maxLength"]:
            erros.append(f"{caminho}: mais longo que maxLength={schema['maxLength']}")
        if schema.get("format") == "date-time":
            try:
                dt.datetime.fromisoformat(instancia.replace("Z", "+00:00"))
            except ValueError:
                erros.append(f"{caminho}: {instancia!r} não é data-hora ISO-8601")

    if isinstance(instancia, (int, float)) and not isinstance(instancia, bool):
        if "minimum" in schema and instancia < schema["minimum"]:
            erros.append(f"{caminho}: {instancia} abaixo de minimum={schema['minimum']}")
        if "maximum" in schema and instancia > schema["maximum"]:
            erros.append(f"{caminho}: {instancia} acima de maximum={schema['maximum']}")
        if "exclusiveMinimum" in schema and instancia <= schema["exclusiveMinimum"]:
            erros.append(f"{caminho}: {instancia} não excede exclusiveMinimum={schema['exclusiveMinimum']}")

    if isinstance(instancia, list):
        if "minItems" in schema and len(instancia) < schema["minItems"]:
            erros.append(f"{caminho}: {len(instancia)} itens, mínimo exigido {schema['minItems']}")
        if "maxItems" in schema and len(instancia) > schema["maxItems"]:
            erros.append(f"{caminho}: {len(instancia)} itens, máximo permitido {schema['maxItems']}")
        itens_schema = schema.get("items")
        if isinstance(itens_schema, dict):
            for idx, item in enumerate(instancia):
                validar(itens_schema, item, raiz, f"{caminho}[{idx}]", erros)

    if isinstance(instancia, dict):
        for obrigatorio in schema.get("required", []):
            if obrigatorio not in instancia:
                erros.append(f"{caminho}.{obrigatorio}: campo obrigatório ausente")
        propriedades = schema.get("properties", {})
        if schema.get("additionalProperties") is False:
            extras = sorted(set(instancia) - set(propriedades))
            if extras:
                erros.append(f"{caminho}: campo fora do contrato: {', '.join(extras)}")
        nomes_schema = schema.get("propertyNames")
        if isinstance(nomes_schema, dict):
            for chave in instancia:
                validar(nomes_schema, chave, raiz, f"{caminho}<{chave}>", erros)
        limite = schema.get("maxProperties")
        if isinstance(limite, int) and len(instancia) > limite:
            erros.append(f"{caminho}: {len(instancia)} chaves, máximo permitido {limite}")
        for chave, valor in instancia.items():
            sub = propriedades.get(chave)
            if isinstance(sub, bool):
                continue
            if isinstance(sub, dict):
                validar(sub, valor, raiz, f"{caminho}.{chave}", erros)


# --------------------------------------------------------------------------- #
# Varredura de vocabulário proibido
# --------------------------------------------------------------------------- #

def varrer(repositorio: str) -> list[str]:
    achados: list[str] = []
    for diretorio, subpastas, arquivos in os.walk(repositorio):
        subpastas[:] = [p for p in subpastas if p not in IGNORAR_PASTAS and not p.startswith(".")]
        for nome in arquivos:
            if not nome.endswith(EXTENSOES_VARRIDAS):
                continue
            caminho = os.path.join(diretorio, nome)
            try:
                texto = open(caminho, encoding="utf-8").read()
            except (OSError, UnicodeDecodeError):
                continue
            for numero, linha in enumerate(texto.splitlines(), start=1):
                if "exemplo.invalid" in linha or "example.invalid" in linha:
                    continue  # domínio reservido para documentação
                for rotulo, padrao in VARREDURA.items():
                    if padrao.search(linha):
                        relativo = os.path.relpath(caminho, repositorio)
                        achados.append(f"{relativo}:{numero}: {rotulo}: {linha.strip()[:110]}")
    return achados


def carregar(caminho: str) -> Any:
    try:
        with open(caminho, encoding="utf-8") as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise Violacao(f"arquivo não encontrado: {caminho}")
    except json.JSONDecodeError as exc:
        raise Violacao(f"{caminho}: JSON inválido na linha {exc.lineno}: {exc.msg}")


def principal(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="valida_contratos.py",
        description="Valida os exemplos de contrato contra os JSON Schemas do repositório "
                    "e varre padrões de dado proibido. Stdlib apenas.",
    )
    parser.add_argument("--schema", metavar="ARQUIVO", help="valida uma instância contra um schema específico")
    parser.add_argument("--instancia", metavar="ARQUIVO", help="instância a validar (com --schema)")
    parser.add_argument("--sem-varredura", action="store_true", help="não roda a varredura de vocabulário proibido")
    parser.add_argument("--json", action="store_true", dest="saida_json", help="saída em JSON")
    parser.add_argument("--repositorio", default=RAIZ, help="raiz a varrer (padrão: raiz deste repositório)")
    args = parser.parse_args(argv)

    resultados: list[dict] = []
    total_erros = 0

    try:
        if args.schema or args.instancia:
            if not (args.schema and args.instancia):
                print("ERRO: --schema e --instancia são um par", file=sys.stderr)
                return 2
            schema = carregar(args.schema)
            instancia = carregar(args.instancia)
            erros: list[str] = []
            validar(schema, instancia, schema, "$", erros)
            resultados.append({"par": f"{args.instancia} ⟶ {args.schema}", "erros": erros})
            total_erros += len(erros)
        else:
            for schema_rel, exemplo_rel in PARES:
                schema = carregar(os.path.join(args.repositorio, schema_rel))
                instancia = carregar(os.path.join(args.repositorio, exemplo_rel))
                erros = []
                validar(schema, instancia, schema, "$", erros)
                resultados.append({"par": f"{exemplo_rel} ⟶ {schema_rel}", "erros": erros})
                total_erros += len(erros)

        achados = [] if args.sem_varredura else varrer(args.repositorio)
    except Violacao as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    if args.saida_json:
        print(json.dumps({"violacoes_de_contrato": total_erros, "resultados": resultados,
                          "varredura": achados}, ensure_ascii=False, indent=2))
    else:
        for resultado in resultados:
            marca = "ok  " if not resultado["erros"] else "FAIL"
            print(f"[{marca}] {resultado['par']}")
            for erro in resultado["erros"]:
                print(f"        - {erro}")
        if args.sem_varredura:
            print("[skip] varredura de vocabulário proibido desabilitada")
        else:
            print(f"[{'FAIL' if achados else 'ok  '}] varredura de padrão proibido "
                  f"({len(VARREDURA)} classes em {len(EXTENSOES_VARRIDAS)} tipos de arquivo)")
            for achado in achados[:20]:
                print(f"        - {achado}")
            if len(achados) > 20:
                print(f"        - ... e mais {len(achados) - 20}")
        print(f"\n{total_erros} violação(ões) de contrato, {len(achados)} achado(s) de varredura")

    return 1 if (total_erros or achados) else 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(principal())
