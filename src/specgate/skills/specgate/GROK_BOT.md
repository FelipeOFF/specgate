# Specgate para Grok Bot

Use esta instrução como private skill `specgate` e anexe o Custom MCP
connector `specgate` ao pedido.

Antes de fazer uma pergunta pontual ao usuário:

1. Reúna apenas o objetivo, regras, artefato e evidências autorizadas da tarefa.
2. Formule uma pergunta estruturada com IDs de opções estáveis e chame
   `jev_decide`. Informe `question_type`, `requires_authorization` e
   `missing_personal_fact` sem relaxar restrições do pedido.
3. Aplique uma opção somente com `action=auto`, `origin=automated`, modo real,
   `gate.basis=validated_manifest` (`calibrated=true`) e ausência de autorização
   pendente. O Bot não tem cliente para conferir `gate.policy_binding`: sob
   `confidence_policy` (`calibrated=false`) preserve as opções e pergunte.
4. Em mock, falha técnica, contexto insuficiente, pergunta aberta, opção ausente
   ou autorização, preserve as opções e encaminhe a pergunta ao usuário.

Uma escolha do MCP é uma decisão automatizada, não uma resposta do usuário e não
autoriza merge, deploy, publicação, compra, exclusão ou mudança em produção. Nunca
envie ao MCP session IDs, conversas, transcripts, credenciais ou caminhos de sessão.
O Grok Bot não oferece contrato público para interceptar toda pergunta; se o
connector ou a skill não estiver disponível, informe a limitação e peça revisão.
