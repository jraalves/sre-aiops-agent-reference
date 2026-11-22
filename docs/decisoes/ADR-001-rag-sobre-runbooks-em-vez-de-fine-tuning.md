# ADR-001 — RAG sobre runbooks e telemetria, em vez de fine-tuning de modelo

- **Status:** Aceito, com ressalva (revisitar quando houver volume anotado suficiente para avaliar de verdade)
- **Data:** 2026-03-02
- **Decisão de:** SRE / observabilidade. Implementação de referência própria, reconstruída de prática — não é artefato de nenhum empregador ou cliente.

## Contexto

O agente precisa responder, dentro de um incidente, coisas como: "esta assinatura
de métrica já apareceu?", "qual o procedimento deste componente quando a fila
acumula?", "o que mudou nas últimas 24h que explica isso?". Isso exige conhecimento
de um ambiente específico, com vocabulário próprio (nomes de serviço, etapas de
jornada, códigos de retorno) e procedures que mudam toda semana.

Duas famílias de solução:

1. **Fine-tuning**: adaptar um modelo com o corpus interno (runbooks, post-mortems,
   tickets resolvidos, queries) e servir o resultado.
2. **RAG (retrieval-augmented generation)**: manter o conhecimento no lugar onde
   ele já vive — repositório Git de runbooks, base de post-mortems, catálogo de
   serviços — indexar, recuperar no momento da pergunta e citar a fonte.

O que pesou não foi custo de treino (segundário). Foi o ciclo de vida do
conhecimento operacional.

## Decisão

**RAG como fonte de verdade; nenhum fine-tuning de conhecimento de ambiente.**
Fine-tuning fica reservado, se um dia necessário, para **forma** (formato de saída,
registro de linguagem, disciplina de citar), nunca para **fundo** (o que fazer no
componente X).

Concretamente, o contrato de saída torna isso observável:
`schemas/proposta-agente.schema.json` → `modelo.recuperacao.documentos[]` exige
`id`, `titulo`, `score` e **`ultimo_teste_em`** de cada documento usado. Uma proposta
de escrita cujo documento-base não tem teste registrado é rejeitada pela política de
guardrail antes de chegar ao aprovador.

## Consequências

**Por que RAG ganhou:**

1. **Latência de atualização.** Runbook novo no `main` fica disponível para o
   agente no próximo índice (minutos), sem treino, sem deploy de modelo, sem
   janela de "o modelo ainda não sabe do procedure que saiu ontem". Em meio de
   pagamento, procedure muda com frequência de release, não de trimestre.
2. **Procedência auditável.** Toda afirmação carrega qual documento a sustentou.
   Isso é requisito, não enfeite: num incidente com efeito financeiro, "o modelo
   achou" não é resposta para auditoria nem para o time que vai executar a ação.
3. **Correção pontual.** Runbook errado se conserta no runbook — com revisão de
   código, dono e histórico. Errado num modelo, conserta-se com dado, treino e
   reavaliação, e ninguém sabe o que mais mudou junto.
4. **Custo marginal de dado sensível.** Não preciso tirar PAN, CPF e nome de
   cliente de um corpus para treinar um modelo que fica num endpoint: preciso só
   que o índice não contenha o que não deveria. A superficie de exposição do RAG
   é o índice; a do fine-tuning é o próprio peso do modelo, que não tem delete.
   **Esse ponto é o mais subestimado em times que tentam fine-tuning com tickets
   reais.**
5. **Retirada possível.** Documento sai do índice, some. Peso de modelo não
   esquece por demanda.

**O que paguei em troca (honesto):**

1. **A qualidade vira qualidade de retrieval.** Se o índice não acha o runbook
   certo, o agente fica mudo ou alucina por falta de base — e o erro parece do
   modelo, sendo do corpus. Mitigação: medir `[medir: recall@5 do índice num
   conjunto de 30 perguntas de incidentes reais (anonimizadas) escritas à mão]`,
   não só "o agente respondeu bonito".
2. **Janela de contexto é orçamento.** Contexto bem montado custa caro e fica
   lento. Por isso o `contexto-servico.schema.json` tem `qualidade.tokens_aproximados`
   e um bloco de lacunas explícitas: melhor 6k tokens com dono e data do que
   40k de "tudo que se falou deste serviço".
3. **Preciso de corpus vivo, com dono e validade.** Runbook vencido no índice é
   pior que runbook ausente: sustenta uma recomendação falsa com ar de
   autoridade. Daí o campo `ultimo_teste_em` obrigatório e o `runbooks` com
   `uri` versionada por commit.
4. **Duplicação e contradição entre documentos.** Dois runbooks que dizem coisas
   diferentes sobre o mesmo sintoma viram duas hipóteses rivais na saída do
   agente. Isso é custo de curadoria de conteúdo — trabalho de documentação, não
   de ML.

**Por que não fine-tuning, em uma frase:** porque meu gargalo não é "o modelo
não sabe o que é SRE"; é "o modelo não sabe o que **este** ambiente faz", e esse
conhecimento tem dono, versão e validade — três atributos que um checkpoint de
modelo não tem.

## Quando eu revisitaria

- se aparecer tarefa de **forma** com volume anotado (por exemplo: classificar
  causa de incidente no vocabulário interno de taxonomia, com 5k+ exemplos
  rotulados e concordância inter-avaliador medida), aí fine-tuning ou modelo
  pequeno dedicado faz sentido — e a decisão seria **outra ADR**, não esta;
- se o índice crescer a ponto do custo de tokens por pergunta superar o custo
  humano do toil economizado (medido, não sentido: `custo_estimado_brl` por
  rodada na saída do agente, contra `[medir: minutos de toil economizados por
  incidente]`), a resposta provavelmente é piorar o Retrieval com hierarquia de
  contexto, e não treinar.
