# Runbook: correlação de jornada entre plataforma distribuída e sistema transacional

**Componente:** `<COMPONENTE-A>` (fronteira entre API de pagamento e sistema
transacional legado)
**Escopo:** uma transação específica, reportada pelo cliente/estabelecimento com
queixa de "pago e não liquidou" ou "apareceu duas vezes".
**Tempo-alvo de primeiro veredito:** 10 min `[medir: mediana das últimas 20
consultas de correlação, cronometradas do abre-ticket ao primeiro "está no elo X"]`
**Último teste:** GameDay-2026-03 (ver `playbook-expurgo.yaml` para o formato de
validação de runbook)
**Autora:** implementação de referência própria, reconstruída de prática. Nenhum
sistema, hostname, tabela ou campo real aparece aqui.

> **Por que este runbook está num repositório de agentes de IA:** ele é o corpus
> que o agente indexa (ADR-001) e a peça que o agente executa em modo
> somente-leitura. Um runbook que o agente não pode executar é documentação; um
> runbook que o agente executa sem aprovação é incidente.

---

## 0. Antes de tocar em qualquer coisa

| checagem | por que |
|---|---|
| Existe incidente S1/S2 ativo no componente? | correlação individual durante incidente maior é distração; primeiro o agregado |
| A queixa tem ao menos **uma** chave: data+hora com fuso, valor, PAN-truncado(4+4), ID do app do cliente, `correlation_id`, `tx_id`? | sem chave de entrada, a busca vira varredura de log com custo de horas |
| O fuso informado é o do cliente, do estabelecimento ou do sistema? | é a causa nº 1 de "não achei": a janela de busca erra 3 horas |
| A transação está dentro da política de retenção de log? | se não está, o runbook termina aqui, com a resposta honesta: "não há como provar pelo rastro, só pelo extrato bancário" |

**Nunca** use valor de transação, PAN ou documento como chave de busca em
ferramenta compartilhada: isso espalha dado sensível em query history. Use a
chave de correlação, ou faça a conversão chave→`correlation_id` no sistema dono
da transação.

## 1. Mapa das chaves (o que existe de um lado e do outro)

| chave | nasce em | formato | sobrevive à fronteira? |
|---|---|---|---|
| `trace_id` | SDK na API | 32 hex | **não** — o runtime transacional não propaga contexto |
| `span_id` | SDK na API | 16 hex | não |
| `correlation_id` | borda (ou canal, se já veio) | UUID; do outro lado chega em maiúsculas, sem hífens | **sim** — é a chave de contrato (ADR-001) |
| `tx_id` | sistema transacional | variável por ambiente | nasce do outro lado; precisa de *join* com `correlation_id` para voltar |
| `nsu` / autorização | adquirente | variável | só no elo de autorização, não na liquidação |
| chave do canal (app do cliente) | canal de entrada | livre, 32-64 chars | depende do elo; **não é** chave de correlação, é pista |

Regra de ouro: **duas chaves, dois papéis.** `trace_id` é o caminho de
investigação técnica; `correlation_id` é o caminho entre mundos. Quando o trace
acaba, a correlação continua.

## 2. Passo a passo

### 2.1 Encontrar o lado distribuído (custo: segundos)

```
# trace/span pela chave de negócio
GET <PLATAFORMA-TRACES>/api/v2/search?tag:correlation.id=<CORRELATION_ID>&lookback=7d
```

O que olhar, nesta ordem (não em paralelo, porque cada resposta estreita a
próxima busca):

1. o span raiz existe? se não, a requisição nem chegou à API que você acha;
2. qual o último span filho **com sucesso** antes da fronteira? o elo seguinte é
   o suspeito;
3. o span da fronteira tem atributo `boundary.known=true`? se sim, o tempo do
   outro lado **não está no trace** e você vai precisar do passo 2.2 — não
   infira duração de silêncio.

### 2.2 Atravessar a fronteira pelo log (custo: minutos)

```loki
# logs do adaptador, dos dois lados da fronteira, pela mesma chave
{service_name="<ADAPTADOR-LEGADO>"} | json | correlation_id = "<CORRELATION_ID>"
```

O log do sistema transacional tem formato de relatório: sem ano, sem
milissegundo, sem fuso, chave em maiúsculas sem pontuação. O parser do lado de cá
precisa ser defensivo (ver `otel-correlation-design`, mesmo autor):

```python
def normalizar(valor: str) -> str:
    return valor.strip().lower().replace("-", "")   # idempotente, definido nas duas pontas
```

Se a busca com a forma normalizada não retorna nada, teste as três variantes de
campo antes de concluir que "não processou": original, maiúsculo-sem-hífen,
truncado em 32 caracteres. Truncamento é como ID morre em campo de largura fixa.

### 2.3 Do transacional de volta para cá (custo: minutos)

```sql
-- tabela de controle do sistema transacional (nomes fictícios)
SELECT tx_id, status, hora_processamento, codigo_retorno
  FROM <ESQUEMA>.CONTROLE_TRANSACAO
 WHERE CORRID = '<CORRELATION_ID_EM_MAISCULAS_SEM_HIFEN>'
   AND data_processamento = '<AAAA-MM-DD>';
```

`data_processamento` é a coluna que te derruba: em sistema transacional com
ciclo de fechamento, a data de processamento pode ser a **seguinte** à data do
evento. Consulte a janela de ±1 dia, sempre.

### 2.4 Veredito (o produto do runbook não é a query, é esta linha)

Escolha **uma**:

| veredito | evidência mínima | ação |
|---|---|---|
| `nao-chegou` | sem span raiz **e** sem `CORRID` no log transacional | devolver ao canal com a evidência de onde morreu |
| `chegou-nao-processou` | `CORRID` no adaptador, ausente no controle transacional | abrir no time do elo legado com as duas consultas |
| `processou-rc-erro` | linha no controle com `codigo_retorno != 00` | resposta é de negócio: recusa, não incidente |
| `processou-perdeu-rastro` | processado no transacional, sem `CORRID` normalizável | **isto é defeito de instrumentação, não de pagamento**: abrir item para o elo que perdeu a chave |
| `processou-duplicado` | duas linhas com o mesmo `CORRID` | prioridade alta: idempotência do elo falhou; acionar fluxo de estorno/reconciliação |

As duas últimas linhas da tabela são as que interessam para o agente: são casos
em que o sistema **funcionou** e a observabilidade falhou. `perdeu-rastro` é o
único veredito deste runbook que gera métrica de qualidade de telemetria
(`cobertura_de_correlacao_pct`), e é ela que alimenta o gap que aparece no
`README.md` como `[medir]`.

## 3. Comunicação (a parte que ninguém quer escrever, e é a que resolve)

Modelo de resposta ao cliente, com a truthfulness que se paga depois:

> Registramos a transação de `<hora>` (fuso `<UTC-3>`) no elo de
> `<autorizacao|liquidacao>`. O sistema transacional retornou
> `<codigo_retorno>` e o valor está `<estornado|a liquidar no próximo ciclo>`.
> A rastreabilidade desta transação entre nossos sistemas está
> `<completa|parcial>`; no segundo caso abrimos item `<ID>` porque a evidência
> disponível não permite afirmar mais do que isto.

Nunca afirmar "está tudo bem" quando o que existe é "não há evidência de
processamento". Ausência de evidência não é prova de ausência.

## 4. O que o agente faz sozinho aqui e o que exige humano

| etapa | modo do agente | motivo |
|---|---|---|
| 0.1-0.4 checagens | somente-leitura, automático | pergunta barata, resposta que evita trabalho perdido |
| 2.1-2.3 consultas | somente-leitura, automático, com limite de taxa | o agente é bom nisso e o risco é custo de query, não produção |
| normalizar e cruzar chaves | automático | regra determinística, já em `otel-correlation-design` |
| propor "refundir/estornar" | **proposta + aprovação humana sempre** | efeito financeiro irreversível |
| reprocessar a transação | **fora de escopo do agente** | escrever no sistema transacional não é ação de observabilidade |
| abrir ticket com veredito | automático (escrita em ticketing é baixa reversibilidade, mas nulo risco de produção) | e é o que mede `tempo_ate_primeiro_diagnostico` |

## 5. Falhas deste runbook (para ele não virar liturgia)

- depende de o `correlation_id` ter sido propagado **por todos** os elos. Em
  jornadas com fila assíncrona no meio, a cobertura é parcial, e este runbook não
  detecta o caso "fila entregou sem header" — que é exatamente o que o painel de
  cobertura do outro repositório existe para mostrar;
- não cobre reconciliação contábil (o que o estabelecimento vê no extrato é
  outro sistema, com outra chave);
- `data_processamento` no ciclo de fechamento é a maior fonte de falso negativo
  em consultas de véspera de feriado. Se o prazo da resposta não couber em
  ±1 dia, peça ao time dono;
- se o componente mudou de nome, de schema ou de formato de log, este runbook
  está **errado** e continua parecendo certo. Daí o campo `Último teste`: sem
  ele, o agente propõe com base em superstição (ver `nao_recomendado` em
  `schemas/proposta-agente.schema.json`).
