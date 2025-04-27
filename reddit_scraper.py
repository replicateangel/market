import asyncio
import json
import logging
import os
from datetime import datetime

import praw
import prawcore
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from openai import OpenAI
import uvicorn # Solo para el if __name__ == '__main__' local

# --- Configuración --- 
# Credenciales (Lee desde variables de entorno/secrets)
CLIENT_ID = os.environ.get('REDDIT_CLIENT_ID')
CLIENT_SECRET = os.environ.get('REDDIT_CLIENT_SECRET')
USER_AGENT = os.environ.get('REDDIT_USER_AGENT', 'taratriia_api v1.0') 
OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")

# Parámetros de Búsqueda y Extracción (Podrían ser parámetros de la API/WebSocket en el futuro)
SUBREDDIT_TO_SEARCH = 'all'
SEARCH_LIMIT_POSTS = 25
SORT_POSTS_BY = 'comments'
COMMENT_LIMIT_PER_POST = 100 
REPLACE_MORE_LIMIT = 0 # No expandir comentarios anidados para velocidad
TOTAL_COMMENTS_TARGET = 100 
MAX_COMMENTS_PER_POST_TARGET = 10

# Filtros de Comentarios
MIN_COMMENT_WORDS = 10
MIN_COMMENT_SCORE = 1

# Configuración de OpenRouter
AI_MODEL_NAME = "google/gemini-flash-1.5"
MAX_INPUT_CHARS_AI = 15000 

# Configuración de Logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

# --- Clientes (Inicialización asíncrona opcional si se necesita) ---
# Nota: PRAW no es nativamente async, OpenAI sí puede serlo pero lo usamos sync aquí
reddit_instance = None
openrouter_client = None

# --- Funciones Core (Sincrónicas, llamadas desde el generador async) ---

def initialize_praw_sync(client_id, client_secret, user_agent):
    global reddit_instance
    if reddit_instance is None:
        if not client_id or not client_secret:
            logging.error("Secrets de Reddit no encontrados.")
            return None
        try:
            logging.info("Inicializando conexión PRAW...")
            reddit_instance = praw.Reddit(
                client_id=client_id, client_secret=client_secret, user_agent=user_agent
            )
            logging.info(f"Conectado a Reddit como: {reddit_instance.user.me()}")
        except Exception as e:
            logging.error(f"Error inicializando PRAW: {e}")
            reddit_instance = None
    return reddit_instance

def initialize_openrouter_client_sync(api_key):
    global openrouter_client
    if openrouter_client is None:
        if not api_key:
            logging.error("OPENROUTER_API_KEY no encontrada.")
            return None
        try:
            logging.info("Inicializando cliente OpenRouter...")
            openrouter_client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=api_key)
            # Podríamos añadir una llamada de prueba aquí si quisiéramos
            logging.info("Cliente OpenRouter inicializado.")
        except Exception as e:
            logging.error(f"Error inicializando cliente OpenRouter: {e}")
            openrouter_client = None
    return openrouter_client

def is_comment_valid(comment, min_words, min_score):
    """Verifica si un comentario cumple con los criterios de filtro."""
    return (
        comment.body and
        comment.body != '[deleted]' and
        comment.body != '[removed]' and
        len(comment.body.split()) >= min_words and
        comment.score >= min_score
    )

# --- Generador Asíncrono para WebSocket ---

async def run_analysis_stream(websocket: WebSocket, search_term: str):
    """Realiza el análisis y envía actualizaciones/resultados vía WebSocket."""
    global reddit_instance, openrouter_client # Usa las instancias globales
    
    # Función auxiliar para enviar mensajes JSON por WebSocket
    async def send_json(data_type: str, payload: any):
        try:
            await websocket.send_json({"type": data_type, "payload": payload})
        except WebSocketDisconnect:
            logging.warning("WebSocket desconectado por el cliente.")
            raise # Re-lanzar para detener el generador
        except Exception as e:
            logging.error(f"Error enviando mensaje WebSocket: {e}")
            raise

    await send_json("status", "Inicializando...")

    # Inicializar clientes (bloqueante, pero hecho una vez por instancia)
    # En una app más compleja, se podrían inicializar al arrancar FastAPI
    reddit = initialize_praw_sync(CLIENT_ID, CLIENT_SECRET, USER_AGENT)
    if not reddit:
        await send_json("error", "Fallo al conectar con Reddit. Verifica las secrets.")
        return
        
    client_ai = initialize_openrouter_client_sync(OPENROUTER_API_KEY)
    if not client_ai:
        await send_json("error", "Fallo al conectar con OpenRouter. Verifica la API Key.")
        return

    # --- Búsqueda y Recolección Reddit --- 
    collected_comments_data = []
    collected_comment_ids = set()
    posts_procesados = 0
    await send_json("status", f"Buscando posts para '{search_term}'...")

    try:
        # Nota: praw.search no es async. Se ejecuta de forma bloqueante aquí.
        # Para mayor rendimiento, se podría ejecutar en un thread pool con asyncio.to_thread
        subreddit = reddit.subreddit(SUBREDDIT_TO_SEARCH)
        search_results = list(subreddit.search(search_term, sort=SORT_POSTS_BY, limit=SEARCH_LIMIT_POSTS))
        
        await send_json("status", f"{len(search_results)} posts encontrados inicialmente.")

        for i, submission in enumerate(search_results):
            if len(collected_comments_data) >= TOTAL_COMMENTS_TARGET:
                await send_json("status", f"Límite total de {TOTAL_COMMENTS_TARGET} comentarios alcanzado.")
                break
            
            posts_procesados += 1
            status_msg = f"Procesando Post {posts_procesados}/{len(search_results)}: '{submission.title[:50]}...'"
            await send_json("status", status_msg)
            logging.info(status_msg) # Loggear también
            
            # Procesamiento de comentarios (también bloqueante)
            comments_added_from_post = 0
            # Poner replace_more a 0 o manejarlo en thread si se necesita
            try:
                 # Iterar sobre comentarios de nivel superior
                for comment in submission.comments.list(): 
                    if len(collected_comments_data) >= TOTAL_COMMENTS_TARGET:
                        break 
                    if comments_added_from_post >= MAX_COMMENTS_PER_POST_TARGET:
                        break

                    if isinstance(comment, praw.models.Comment) and comment.id not in collected_comment_ids:
                        if is_comment_valid(comment, MIN_COMMENT_WORDS, MIN_COMMENT_SCORE):
                            collected_comments_data.append({
                                'post_title': submission.title,
                                'post_id': submission.id,
                                'comment_id': comment.id,
                                'comment_body': comment.body,
                                'comment_score': comment.score,
                                'comment_utc_date': datetime.utcfromtimestamp(comment.created_utc).strftime('%Y-%m-%d %H:%M:%S UTC')
                            })
                            collected_comment_ids.add(comment.id)
                            comments_added_from_post += 1
            except Exception as comment_error:
                 logging.warning(f"Error procesando comentarios del post {submission.id}: {comment_error}")

            await asyncio.sleep(0.05) # Pequeña pausa para permitir que otros eventos ocurran

    except WebSocketDisconnect:
        return # Salir limpiamente si el cliente se desconecta
    except Exception as e:
        logging.error(f"Error durante búsqueda/extracción de Reddit: {e}", exc_info=True)
        await send_json("error", f"Error en Reddit: {str(e)}")
        return

    # --- Enviar Datos Recolectados (Opcional, si se quieren antes del análisis) ---
    await send_json("status", f"Recolección finalizada. {len(collected_comments_data)} comentarios válidos encontrados.")
    # Podríamos enviar la lista completa aquí si el cliente la necesita antes del análisis
    # await send_json("reddit_data", collected_comments_data)

    # --- Llamada a IA con Streaming --- 
    if not collected_comments_data:
        await send_json("status", "No se encontraron comentarios válidos para análisis.")
        return

    comments_text_for_ai = "\n\n".join([c['comment_body'] for c in collected_comments_data])
    if len(comments_text_for_ai) > MAX_INPUT_CHARS_AI:
        logging.warning(f"Truncando texto para IA ({len(comments_text_for_ai)} > {MAX_INPUT_CHARS_AI})")
        comments_text_for_ai = comments_text_for_ai[:MAX_INPUT_CHARS_AI] + "... [TRUNCADO]"
        
    system_prompt = "Eres un asistente de análisis de mercado experto. Analiza los siguientes comentarios de Reddit sobre un tema específico. Tu objetivo es extraer información valiosa para entender al público."
    user_prompt = f"Aquí tienes una colección de comentarios de Reddit sobre el tema '{search_term}':\n\n---\n{comments_text_for_ai}\n---\n\nPor favor, realiza un análisis conciso e identifica:\n1.  **Términos Clave y Temas Recurrentes:** Palabras o conceptos que aparecen frecuentemente.\n2.  **Situaciones, Problemas o Necesidades Comunes:** ¿Qué circunstancias o dificultades mencionan los usuarios relacionadas con el tema?\n3.  **Sentimientos Generales:** ¿Hay tendencias claras de opiniones positivas, negativas o neutrales? Menciona ejemplos si es posible.\n4.  **Posibles Insights:** ¿Alguna observación interesante o conclusión que se pueda sacar sobre este público o mercado basada en los comentarios?\n\nFormatea tu respuesta usando Markdown para claridad."
    
    await send_json("status", f"Llamando a IA ({AI_MODEL_NAME})...")
    
    try:
        # Nota: La llamada a OpenAI no es async por defecto si el cliente no se inicializó async
        # Idealmente, usaríamos un cliente async y `await client_ai.chat.completions.create`
        # o ejecutar esto en un thread pool.
        stream = client_ai.chat.completions.create(
            model=AI_MODEL_NAME,
            messages=[{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
            temperature=0.5,
            max_tokens=1024,
            stream=True
        )
        for chunk in stream:
            content = chunk.choices[0].delta.content
            if content:
                await send_json("ai_chunk", content)
                await asyncio.sleep(0.01) # Pequeña pausa 
        
        await send_json("status", "Análisis de IA completado.")

    except WebSocketDisconnect:
        return # Salir si el cliente se desconecta durante el stream de IA
    except Exception as e:
        logging.error(f"Error llamando a OpenRouter API: {e}", exc_info=True)
        await send_json("error", f"Error en análisis IA: {str(e)}")
    
    # Enviar la lista completa de comentarios al final (opcional)
    await send_json("final_data", {"comments": collected_comments_data})
    logging.info("Proceso completo para WebSocket.")


# --- Aplicación FastAPI --- 

app = FastAPI(title="Reddit Market Analyzer API")

@app.websocket("/ws/analyze")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    logging.info("WebSocket conectado.")
    search_term = ""
    try:
        # Esperar el término de búsqueda inicial del cliente
        data = await websocket.receive_text()
        message = json.loads(data) # Asumir que el cliente envía {"action": "start", "term": "..."}
        if message.get("action") == "start" and "term" in message:
            search_term = message["term"]
            logging.info(f"Iniciando análisis para: '{search_term}'")
            # Iniciar el generador
            await run_analysis_stream(websocket, search_term)
        else:
            await websocket.send_json({"type": "error", "payload": "Mensaje inicial inválido. Envía {'action': 'start', 'term': 'tu_termino'}."})

    except WebSocketDisconnect:
        logging.info(f"WebSocket desconectado (Término: '{search_term}').")
    except json.JSONDecodeError:
        logging.error("Error decodificando JSON inicial del WebSocket.")
        await websocket.send_json({"type": "error", "payload": "Mensaje inicial debe ser JSON válido."}) 
    except Exception as e:
        logging.error(f"Error inesperado en WebSocket: {e}", exc_info=True)
        try:
            await websocket.send_json({"type": "error", "payload": f"Error interno del servidor: {str(e)}"}) 
        except: # Ignorar errores si el socket ya está cerrado
            pass
    finally:
        logging.info("Cerrando conexión WebSocket.")
        # No cerramos explícitamente aquí, FastAPI maneja la desconexión

@app.get("/")
async def root():
    return {"message": "API de Análisis de Mercado Reddit. Conéctate al endpoint /ws/analyze vía WebSocket."}

# --- Ejecución Local (para desarrollo) ---
if __name__ == "__main__":
    # Leer puerto para Uvicorn, default a 8000
    port = int(os.getenv('PORT', 8000))
    logging.info(f"Iniciando servidor Uvicorn en http://localhost:{port}")
    # Nota: host="0.0.0.0" es importante para despliegues, no solo localhost
    uvicorn.run("reddit_scraper:app", host="0.0.0.0", port=port, reload=False) # reload=True para desarrollo 