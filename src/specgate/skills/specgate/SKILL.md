---
name: specgate
description: Prepare contexto e interprete decisões tipadas do Jev para verificar claims, avaliar textos externos e selecionar skills em projetos de software. Use ao integrar ou conduzir esses julgamentos com TypeSafe ou OpenRouter.
---

# Specgate

O Dev no harness reúne evidências e conduz o trabalho. Jev responde perguntas
delimitadas; o código aplica a política de decisão. O harness produz a prosa.

Para uma pergunta pontual, formule `question` e opções `{id, text}` e consulte
`jev_decide` com o contexto autorizado, sem exigir uma execução do fluxo Matt.
Declare `question_type`, `requires_authorization` e `missing_personal_fact`
conforme o pedido original; nunca remova uma restrição para obter uma escolha.
`selected_option` é uma candidata do avaliador, não uma resposta do usuário.
`origin=mock` identifica simulação; `origin=jev` identifica avaliação real;
`origin=policy` identifica uma saída definida pelas restrições, sem julgamento.
Preserve `collect`, `review`, `error`, motivos da política e referências à avaliação.
O cliente compartilha até três rodadas entre lacunas locais e semânticas e
interrompe a coleta sem informação nova. Um manifesto de calibração ausente não
desliga o Jev: sem manifesto, uma avaliação real avança pela política de confiança,
com `calibrated=false` e `gate.basis=confidence_policy`; com um manifesto real
compatível, `gate.basis=validated_manifest` e `calibrated=true`. `gate.basis` nomeia
quem recomendou, e o score é um filtro de incerteza, não acurácia medida. Os cortes
de 0,80 comparam estritamente acima: 0,79 e 0,80 ficam em revisão, só 0,81 avança.
`verified` em `jev_verify` é um veredito usável depois da confirmação de quem mantém.
`auto_advance` descreve só a recomendação: `execution_authorized` fica `false` e o
gate nunca autoriza um efeito.

O cliente confere `gate.policy_binding` (política, receita, tool, revisões do pedido
e do contexto, provider e modelo resolvido) contra o pedido que ele enviou; a palavra
do host não basta. Resposta sem `gate.basis` vem de um host anterior a este
contrato: leve o passo à revisão, nunca a um sucesso inferido.

O piloto local disponibiliza `jev_verify`, `jev_screen` e `jev_find` via MCP
autenticado, com mock heurístico. Confirme que as tools estão conectadas no harness.
Não apresente simulação como chamada real ao Jev. Os adapters de harness aplicam
somente escolhas autorizadas pelos mesmos gates deste fluxo.

No Grok Bot, esta é uma skill privada cooperativa: anexe também o Custom MCP
connector `specgate` e consulte `jev_decide` antes de encaminhar uma pergunta
pontual ao usuário. Aplique a opção somente quando o resultado trouxer
`action=auto`, `origin=automated`, `gate.basis=validated_manifest`
(`calibrated=true`) e nenhuma autorização pendente. O Bot não tem cliente para
conferir `gate.policy_binding`: sob `confidence_policy` (`calibrated=false`) preserve
as opções e pergunte ao usuário. Mock, falha técnica, contexto insuficiente,
pergunta aberta ou autorização mantêm a revisão humana. O produto não publica um
contrato para interceptar toda pergunta; não descreva esta skill como hook
obrigatório. Nunca envie conversas, sessões ou transcripts do Bot ao MCP.

No Cursor, hooks que bloqueiam
ou alteram argumentos não respondem `cursor/ask_question`; quando essa garantia
for necessária, inicie o turno pelo cliente Python ACP controlado.
No Grok Build, hooks também não substituem a resposta anterior à pergunta.
Quando essa garantia for necessária, inicie `grok agent stdio` pelo cliente
Python controlado e responda somente `x.ai/ask_user_question` pelo contrato ACP.

Leia apenas a referência necessária à tarefa:

- Para coletar contexto ou formular perguntas, leia
  [Contexto e perguntas](references/jev-context-and-questions.md).
- Para verificar claims, filtrar material ou selecionar skills, leia
  [Receitas de decisão](references/jev-recipes.md).
- Para integrar Python/OpenRouter, interpretar confiança ou validar resultados,
  leia [Contratos e avaliação](references/jev-contracts-and-evaluation.md).

Preserve IDs públicos e rejeite duplicatas antes de construir mapas. Evidências
ausentes ou contraditórias precisam permanecer visíveis. Confiança alta não
substitui contexto suficiente nem autorização para executar uma ação.

Sem manifesto, o avanço automático segue a política de confiança descrita acima, e
não uma calibração: nenhum corpus de domínio mede a acurácia, e a calibração com
casos representativos é opcional. Siga o limite de três rodadas de coleta descrito
na referência de contexto; encaminhe lacunas persistentes para revisão humana.
Os 60 casos do piloto rodam exclusivamente com mock; reutilize as respostas reais
gravadas sem fazer novas chamadas pagas em testes rotineiros.

Para conduzir pesquisa, spec, tickets e implementação até PRs draft, use o fluxo
composto do cliente público: `specgate flow request.json --host URL --adapter
modulo:fabrica --authorize-jev`. `--authorize-jev` registra a autorização que a
pessoa deu para enviar as fontes do pedido ao host e ao provider configurados; a flag
não cria consentimento, e sem ela o fluxo para em `jev_authorization_required`. Passe-a
só quando a pessoa já autorizou esse envio. O fluxo escolhe cada chamada pelo estado
que o host devolve, então uma retomada continua da etapa válida, e não pede manifesto
de calibração. A pesquisa roda sem manifesto, em até três ciclos por pergunta ou
claim, com fonte nova admitida pela política. Cada etapa avança só quando a
recomendação traz `gate.basis` e o predicado da sua tool passou com o binding que o
cliente conferiu; resposta sem `gate.basis` leva a etapa à revisão. A recomendação não
substitui a concessão humana: publicar spec, tickets e PRs draft exige a concessão
vigente de `specgate setup` (ou a aprovação humana da revisão exata da spec e dos
tickets), e nenhum agente executa `specgate setup` para se autorizar.
`--authorize-drafts` é só da pessoa: ela autoriza apenas os PRs draft (`draft_pr`),
nunca spec nem tickets, e com ela a verificação do ticket dispensa o predicado
automático, bastando claims `verified`. Por isso o agente nunca a passa por conta
própria. O fluxo nunca faz merge, deploy ou force-push. Cada síntese usa uma skill
cujo carregamento o harness confirmou (`loaded=true`). A síntese da spec carrega
`to-spec`, o arquivo com `<spec-template>`, e a dos tickets carrega `to-tickets`: as
duas vêm de outra fonte, não do conjunto do Specgate, e `to-spec-jev` não as
substitui. Sem manifesto elas não são skills públicas, então o roteamento as mantém em
revisão e uma pessoa escolhe aquela skill e revisão (`HumanReview`); sem isso o fluxo
para em `skill_load_unconfirmed`.

Use apenas dados autorizados da tarefa. Exclua segredos e identificadores,
caminhos ou transcripts de sessões dos harnesses do contexto transmitido.

Ao apresentar uma decisão, informe o veredito, as evidências que o sustentam e
as lacunas relevantes. Distinga resposta real, resultado gravado e simulação
heurística. Políticas e thresholds pertencem ao projeto; exemplos da documentação
não definem os valores de produção.

O cliente Python expõe `specgate.client.review_request`, que executa
pedido → fontes explícitas → contexto → MCP → revisão humana. O `ReviewRequest`
informa `objective`, `tool`, `arguments`, `sources` e `required`; `source_rounds`
fornece fontes adicionais para até duas rodadas seguintes. Sem evidência suficiente,
o cliente retorna as lacunas antes de consultar o servidor. O resultado traz
`gate_basis`, `calibrated` e `score`, os números que o predicado comparou.

Para roteamento, descubra o catálogo das roots autorizadas, consulte `jev_find` e
leia o `SKILL.md` completo do candidato antes de considerar seu uso. `absent` não
autoriza escolher o primeiro colocado. Resultados do mock exigem revisão.

O cliente Python expõe `specgate.routing.route_skills`: recebe um
`ReviewRequest` de `jev_find`, `authorized_roots` do harness e `disabled_ids`.
`request_context` pode fornecer novas fontes conforme as lacunas. O catálogo é
avaliado em lotes de até 50 candidatos, dentro do orçamento de bytes; cada lote
contribui seu `top` para uma shortlist. Se as instruções de uma candidata
reprovarem, o cliente tenta a próxima. Após confirmar adequação, examina somente
as empatadas na avaliação da descrição. `coverage` identifica o que foi examinado,
desativado, rejeitado ou ficou fora da shortlist; não infira incompatibilidade de
IDs não examinados. Nenhum conteúdo é truncado para caber em uma chamada.
`transport`, `ca_file`, `timeout_seconds` e `progress_callback` são encaminhados
ao mesmo cliente MCP usado pelas demais avaliações.

As instruções integrais da shortlist passam por `jev_screen` e por adequação
em `jev_find`, com o contexto e as regras do projeto. `evaluations` preserva os
resultados e a política de cada etapa. Ausência, empate ou pendência exige
revisão; uma indicação em `candidate` também permanece em revisão, salvo sob
manifesto validado ou, sem manifesto, quando a skill é pública
(`candidate.public`, `gate_basis=confidence_policy`, sem calibração). O catálogo
pessoal ou do projeto exige um escopo separado. O mock não prova compatibilidade
semântica. Skill desativada no harness, ausente, de revisão divergente ou de
instruções não examinadas retém a seleção: não a selecione nem a carregue.

O adapter deve carregar `candidate.instructions` e chamar
`confirm_skill_loaded(indication, skill_id, revision, authorized_roots=...,
disabled_ids=...)` com o ID e a revisão efetivamente carregados. A função
reconfere autorização, origem e revisão do arquivo. `loaded=true` registra apenas
essa confirmação explícita; não significa execução nem autorização automática.
Sem `gate_basis=validated_manifest`, `loaded` só é registrado com
`hook=NativeHook(...)`, montado pelo handler do hook nativo do harness quando ele
está instalado e executou naquele carregamento. Uma indicação em revisão
(`origin=review`, que não prova revisão) também carrega com
`review=HumanReview(skill_id, revision)`, montada só quando uma pessoa escolheu
aquela skill e revisão; ela não substitui o hook numa seleção automática. Sem essa
evidência `loaded` não é registrado e `loading` diz o motivo.
