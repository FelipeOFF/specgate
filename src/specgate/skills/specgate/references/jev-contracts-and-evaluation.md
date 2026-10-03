# Jev: contratos e avaliação

Conferido em 18/09/2026. Consulte a documentação oficial novamente ao mudar
modelo, SDK ou provider. Cliente e servidor deste projeto são Python.

## Provedores

| Caminho | Request | Retorno / limite observado |
| --- | --- | --- |
| OpenRouter | `POST https://openrouter.ai/api/alpha/decisions`, Bearer auth; `model`, `state`, `questions` | JSON com `answers`, `model`, `usage`; `provider` pode aparecer |
| TypeSafe direto | `POST https://api.typesafe.ai/v1/systemone`; SDK `TypeSafeClient` / `AsyncTypeSafeClient`, método `system_one` | Contrato e IDs de modelo próprios; não intercambiar payloads sem adapter |

Use `typesafe/jev-1.13` no OpenRouter. Nas seis chamadas do projeto, o alias resolveu
para `typesafe/jev-1.13-20260917`; registre solicitado e resolvido separadamente.
O model card OpenRouter informa contexto de 32k. Os limites da API direta podem
ser diferentes e incluem estado e perguntas; não reutilize um limite entre providers.
[Decisions API](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-questions-and-answers-request),
[Model card](https://openrouter.ai/typesafe/jev-1.13),
[TypeSafe API](https://docs.typesafe.ai/api),
[Python SDK](https://docs.typesafe.ai/sdk/python),
[Models](https://docs.typesafe.ai/models)

O schema Decisions consultado não oferece `stream`. SSE pertence ao transporte
MCP entre harness e servidor; não invente streaming de tokens para Jev.
[OpenAPI](https://openrouter.ai/openapi.json)

## Respostas tipadas

| Tipo | Campos e interpretação |
| --- | --- |
| `noul` | `noul` em `[0,1]`: probabilidade de verdadeiro; sem `confidence` separado |
| `choice` | `choice`, `probabilities`, `confidence`: uma opção e distribuição entre opções |
| `score` | `score`, `legend`, `probabilities`, `confidence`: posição ponderada numa escala de níveis |

Valide correspondência das perguntas, tipos, opções e valores finitos nas faixas
esperadas. OpenRouter marca confiança/distribuição como opcionais no schema, mas
os gates deste plugin precisam desses campos para Choice/Score: ausência é falha
de contrato, não zero, sucesso ou baixa confiança inventada.
[Noul](https://docs.typesafe.ai/primitives/noul),
[Choice](https://docs.typesafe.ai/primitives/choice),
[Score](https://docs.typesafe.ai/primitives/score),
[OpenAPI](https://openrouter.ai/openapi.json)

## Confiança e avaliação

Choice tem probabilidades por opção e uma confiança global. `confidence` descreve
concentração da distribuição; não equivale à acurácia medida nem à suficiência do
contexto. Veredito, evidência, confiança e autorização são verificações diferentes.
Thresholds devem refletir o custo dos erros e casos representativos do projeto.
[Confidence](https://docs.typesafe.ai/confidence),
[Confidence routing](https://docs.typesafe.ai/patterns/confidence-routing)

Sem manifesto de calibração, o gate recomenda pela política de confiança
(`gate.basis=confidence_policy`, `calibrated=false`): cada corte de 0,80 compara
estritamente acima, então 0,79 e 0,80 ficam em revisão e só 0,81 avança. O score é
um filtro de incerteza e não mede acurácia. Um manifesto validado, quando existe,
passa a valer (`gate.basis=validated_manifest`, `calibrated=true`) e nunca cai de
volta para a política se ficar inválido ou revogado. Resposta sem `gate.basis` vem de
um host anterior a este contrato: o cliente a deixa em revisão, nunca em sucesso
inferido. Thresholds de exemplos oficiais não definem os valores de produção.
O Codex será o primeiro harness usado para validar o fluxo completo.

O piloto de 60 casos usa exclusivamente mock: 30 casos para ajuste offline e 30
reservados para validação. Reutilize as respostas reais já gravadas; não consulte
APIs pagas em testes rotineiros. Resultado do mock não libera o gate real.

As seis chamadas sintéticas do projeto retornaram 13 respostas tipadas. Servem
como exemplos gravados para testes offline, não como estimativa de acurácia.
Um mock para textos arbitrários continua sendo heurístico: identifique esse modo
explicitamente e não apresente seus scores como medidas do modelo real.

Ao depurar, confira estado, perguntas, candidatos, resposta original, composição
em Python e resultado esperado. Separe evidência ausente, erro semântico, política
incorreta e falha técnica. Mantenha prompts e thresholds versionados; reavalie após
alterações e reserve casos independentes para medir erros de automação e revisões.
[Skill oficial](https://github.com/typesafe-ai/skills/blob/main/skills/typesafe-ai/SKILL.md),
[Agent skill](https://docs.typesafe.ai/agent-skill)

Timeout, HTTP inválido ou resposta malformada são falhas técnicas. Não os converta
em decisão semântica. Retries e fallback precisam de orçamento; não repita uma
decisão válida apenas para obter um veredito favorável. Evite logs de corpos com
contexto privado ou chaves.
[Exceptions](https://docs.typesafe.ai/sdk/python/api/exceptions),
[Retries](https://docs.typesafe.ai/sdk/python/api/retries),
[SDK logging](https://docs.typesafe.ai/sdk/python/usage)

## Proveniência

Estas referências são sínteses próprias com links para as fontes. A skill oficial
está sob [MIT](https://github.com/typesafe-ai/skills/blob/main/LICENSE), copyright
2026 TypeSafe AI. Não foi copiada integralmente; sua licença não foi presumida para
as demais páginas do site. Para ampliar a pesquisa, use o
[índice oficial](https://docs.typesafe.ai/llms.txt) ou a navegação da documentação.
