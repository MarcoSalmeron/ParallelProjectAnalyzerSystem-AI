import asyncio
import uuid
import nest_asyncio
from agents.supervisor import team
from langgraph.checkpoint.memory import MemorySaver
from langchain_core.messages import HumanMessage
from analyzer_services.app.process.ConnectionManager import manager
from common.common_utl import get_embeddings_model
from analyzer_services.app.state import pending_responses
from schemas import ERPState
from langgraph.errors import GraphInterrupt  # 👈 importa la excepción

nest_asyncio.apply()

async def ejecutar_agencia():
    memory = MemorySaver()
    get_embeddings_model()
    oracle_graph = team.compile(checkpointer=memory)

    thread_id = f"oracle_project_{uuid.uuid4().hex[:8]}"
    config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": 100
    }

    # Mensaje inicial
    inputs = {"messages": [HumanMessage(content="Analiza los impactos de la versión 24D de Oracle Cloud.")]}
    print(f"🚀 Iniciando Oracle Cloud Analyzer [Thread: {thread_id}]")

    while True:
        try:
            # Ejecutamos hasta el próximo punto de interrupción o final
            async for event in oracle_graph.astream(inputs, config=config, stream_mode="values"):
                if "messages" in event:
                    last_msg = event["messages"][-1]
                    if hasattr(last_msg, 'name') and last_msg.name:
                        print(f"[{last_msg.name.upper()}]: {last_msg.content[:100]}...")

            # Verificación de estado tras astream
            state = await oracle_graph.aget_state(config)

            # Caso 1: flujo terminado
            if not state.next:
                print("\n✅ [PROCESO FINALIZADO]")
                final_messages = state.values.get("messages", [])
                if final_messages:
                    print(f"\nResumen Final:\n{final_messages[-1].content}")
                break

            # Caso 2: interrupción detectada en el estado
            if state.next == "interrupt":
                pregunta = state.values.get("messages", [])[-1].content
                print(f"\n🤖 Interrupción: {pregunta}")

                await manager.send_update(thread_id, {
                    "type": "interrupt",
                    "agent": "system",
                    "content": pregunta
                })

                while thread_id not in pending_responses:
                    await asyncio.sleep(0.5)

                respuesta = pending_responses.pop(thread_id)

                # Actualizar el estado del grafo con el módulo ERP
                await oracle_graph.update_state(config, {"erp_module": respuesta})

                # Reinyectar el mensaje humano
                inputs = {"messages": [HumanMessage(content=respuesta)]}
                continue


        except GraphInterrupt as gi:
            # 👈 Capturamos la excepción directamente
            pregunta = gi.args[0].value
            print(f"\n🤖 Interrupción capturada: {pregunta}")

            await manager.send_update(thread_id, {
                "type": "interrupt",
                "agent": "system",
                "content": pregunta
            })

            while thread_id not in pending_responses:
                await asyncio.sleep(0.5)

            respuesta = pending_responses.pop(thread_id)

            # Actualizar el estado del grafo con el módulo ERP
            await oracle_graph.update_state(config, {"erp_module": respuesta})

            # Reinyectar el mensaje humano
            inputs = {"messages": [HumanMessage(content=respuesta)]}
            continue


if __name__ == "__main__":
    try:
        asyncio.run(ejecutar_agencia())
    except KeyboardInterrupt:
        print("\nTerminado por el usuario.")
