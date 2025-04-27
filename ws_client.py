import asyncio
import websockets
import json
import sys

async def connect_and_analyze():
    uri = "ws://localhost:8000/ws/analyze" # Asegúrate que el puerto (8000) coincide con el servidor
    
    try:
        async with websockets.connect(uri) as websocket:
            print(f"Conectado a {uri}")
            
            # Pedir término de búsqueda al usuario
            search_term = input("Ingresa el término de búsqueda para Reddit: ")
            if not search_term:
                print("Término de búsqueda vacío. Saliendo.")
                return

            # Enviar mensaje inicial
            start_message = json.dumps({"action": "start", "term": search_term})
            print(f"---> Enviando: {start_message}")
            await websocket.send(start_message)

            # Escuchar respuestas
            print("\n<--- Esperando respuestas del servidor...")
            while True:
                try:
                    message_str = await websocket.recv()
                    message = json.loads(message_str)
                    
                    msg_type = message.get("type")
                    payload = message.get("payload")

                    if msg_type == "status":
                        print(f"[ESTADO]: {payload}")
                    elif msg_type == "ai_chunk":
                        # Imprimir fragmentos de IA directamente sin salto de línea
                        print(payload, end='', flush=True)
                    elif msg_type == "final_data":
                        print("\n[DATOS FINALES]:")
                        # Imprimir de forma más legible (ej: número de comentarios)
                        comments = payload.get('comments', [])
                        print(f"  - Recibidos {len(comments)} comentarios.")
                        # Podrías imprimir algunos detalles si quieres, ej:
                        # for i, comment in enumerate(comments[:3]):
                        #     print(f"    {i+1}. {comment['comment_body'][:50]}... (Score: {comment['comment_score']})")
                        # Considerar guardar payload['comments'] a un archivo aquí si es necesario
                        print("\nProceso completado en el servidor.")
                        break # Terminar después de recibir datos finales
                    elif msg_type == "error":
                        print(f"\n[ERROR DEL SERVIDOR]: {payload}")
                        break # Terminar si hay error
                    else:
                        print(f"[MENSAJE DESCONOCIDO]: {message}")
                        
                except websockets.exceptions.ConnectionClosedOK:
                    print("\nConexión cerrada limpiamente por el servidor.")
                    break
                except websockets.exceptions.ConnectionClosedError as e:
                    print(f"\nConexión cerrada con error: {e}")
                    break
                except json.JSONDecodeError:
                    print(f"\n<--- Recibido mensaje no JSON: {message_str}")
                except Exception as e:
                    print(f"\nError procesando mensaje: {e}")
                    break # Salir en caso de error inesperado
            
            print("\nSaliendo del bucle de recepción.")

    except websockets.exceptions.InvalidURI:
        print(f"Error: URI inválida - {uri}")
    except ConnectionRefusedError:
        print(f"Error: No se pudo conectar a {uri}. ¿Está el servidor FastAPI corriendo?")
    except Exception as e:
        print(f"Error inesperado en la conexión: {e}")

if __name__ == "__main__":
    # Ejecutar el cliente asyncio
    # En Windows, puede ser necesario ajustar la política de eventos si hay problemas
    # if sys.platform == "win32":
    #     asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    try:
        asyncio.run(connect_and_analyze())
    except KeyboardInterrupt:
        print("\nCliente interrumpido.") 
