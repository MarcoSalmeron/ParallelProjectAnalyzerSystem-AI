import asyncio
from datetime import datetime
from time import time
from langchain_core.tools import tool
import logging

import json
import random
import psycopg2
from typing import List, Dict, Annotated
from psycopg2.extras import execute_values
from schemas.schemas import ReporteInvestigacion
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
import os
from common.common_utl import extract_feature_summary, extract_deprecated, retry_with_backoff, get_embedding, get_conn,get_embeddings_model, count_impacts
from common.ReporteCorporativo import ReporteCorporativo, AZUL_CONDOR, GRIS_FILA, GRIS_TEXTO
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import  ParagraphStyle
from reportlab.platypus import  Paragraph, Spacer, Table, TableStyle, PageBreak
from reportlab.lib.units import inch
from reportlab.lib.enums import TA_CENTER,TA_LEFT
from langchain_core.runnables import RunnableConfig
from concurrent.futures import ThreadPoolExecutor, as_completed



# ===============================
# LOGGING
# ===============================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

logger = logging.getLogger(__name__)
MAX_CONCURRENT_PAGES = 12

# Configuración de embdedings
EMBEDDING_MODEL = "multilingual-e5-large"


# --- CONFIGURACIÓN DE RUTAS ---
BASE_DOMAIN = "https://docs.oracle.com/en/cloud/saas/readiness/"
INDEX_URLS = [
     ("Financials", "https://docs.oracle.com/en/cloud/saas/readiness/erp-all.html"),
   ("Supply Chain and Manufacturing", "https://docs.oracle.com/en/cloud/saas/readiness/scm-all.html"),
    ("Human Capital Management", "https://docs.oracle.com/en/cloud/saas/readiness/hcm-all.html")
]
BASE_URLS = [
    ("Supply Chain and Manufacturing", "https://docs.oracle.com/en/cloud/saas/supply-chain-and-manufacturing/{version}/fasrp/index.html"),  
    ("Financials", "https://docs.oracle.com/en/cloud/saas/financials/{version}/farfa/index.html"),
    ("Human Capital Management", "https://docs.oracle.com/en/cloud/saas/human-resources/farws/index.html")
]

# --- CONFIGURACIÓN DE COLORES CORPORATIVOS ---
AZUL_CONDOR = colors.HexColor("#004A99")
ROJO_ORACLE = colors.HexColor("#FF0000")
GRIS_FONDO = colors.HexColor("#F4F7F9")


# ===============================
# HERRAMIENTAS
# ===============================

@tool
def tool_obtener_modulos_disponibles() -> List[str]:
    """Retorna la lista de módulos ERP disponibles"""
    return [modulo for modulo, _ in INDEX_URLS]

@tool
async def tool_investigar_version(version: str):
    """
    Ejecuta toda la investigación de una versión:
    1. Extrae impactos
    2. Extrae APIs deprecadas
    3. Guarda resultados en pgvector
    """

    logger.info(f"Iniciando investigación para versión {version}")

    impactos = await tool_descubrir_url_modulo(version)
    apis = await tool_extraer_apis_deprecadas(version)

    reporte = ReporteInvestigacion(
        impactos=impactos,
        apis_deprecadas=apis,
        plan_accion= [],
        proximos_pasos= [],
        servicios_soporte= []
    )

    result = tool_guardar_en_pgvector(version, reporte)

    return result

async def tool_descubrir_url_modulo(version: str):
    """
    Busca la URL específica de un módulo para una versión (ej: '26A') 
    navegando por los índices de Oracle Readiness.
    """
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PAGES)
    
    logger.info(f"--- [Paso 1] Buscando URL para {version} ---")

    # Usamos el gestor nativo para evitar errores de 'await'
    async with async_playwright() as p:
        # Lanzamos el navegador (headless=False para modo visual)
        browser = await p.chromium.launch(headless=True, args=[ "--no-sandbox", "--disable-dev-shm-usage","--lang=en-US"])
        page = await browser.new_page()
        
        
        data_results = []
        try:
            
            
            all_links = []
            for name, url in INDEX_URLS:
                logger.info(f"Navegando a índice: {url} {name}")
                # Navegación rápida esperando solo el DOM
                await page.goto(url, timeout=60000)
                await page.wait_for_load_state("domcontentloaded")
                
                links_locator = page.locator("a", has_text=f"What's New {version}")
                count = await links_locator.count()

                results = []
                for i in range(count):
                    link = links_locator.nth(i)
                    title = (await link.inner_text()).strip()
                    href = await link.get_attribute("href")

                    if href:
                        full_url = href if href.startswith("http") else f"{BASE_DOMAIN}{href}"
                        results.append((name, title.replace(f"What's New {version}", "").strip(), full_url))
                all_links.extend(results)    
                
            if all_links:
                tasks = []
                for producto,modulo, url in all_links:
                    task = retry_with_backoff(
                        extract_feature_summary,
                        browser,
                        semaphore,
                        producto,
                        modulo,
                        url
                    )
                    tasks.append(task)

                data_results = await asyncio.gather(*tasks)     
                data = [item for sublist in data_results for item in sublist]
               
                
        except Exception as e:
            logger.error(f"Error en {producto}: {e}")
        finally:
            await browser.close()
           # data = data[:200]
            return data
            
async def tool_extraer_apis_deprecadas(version: str): 
    """
    Navega a las URLs de Oracle Readiness para todos los módulos configurados 
    y extrae las tablas de 'Deprecated REST Resources' para la versión indicada.
    """

    async with async_playwright() as p:

        browser = await p.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-dev-shm-usage","--lang=en-US"]
        )

        tasks = []

        for producto, url_template in BASE_URLS:

            tasks.append(
                extract_deprecated(browser, producto, url_template, version)
            )

        results = await asyncio.gather(*tasks)

        flat = [item for sublist in results for item in sublist]

        
        await browser.close()
        print(f"Fin de la funcion deprecated {len(flat)}")
        #flat = flat[:100]
        return flat
@tool
def tool_marcar_error_version(version: str):
    """Si el investigador falla, liberamos la versión para reintentar después."""
    logger.info(f"[Investigador]---> Marcando versión {version} como FAILED debido a error en investigación.")
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute("UPDATE oracle_versions SET status = 'failed' WHERE version_id = %s", (version,))
        conn.commit()
    conn.close()

'''
def tool_guardar_en_pgvector(version: str, reporte: ReporteInvestigacion) -> str:
    """
    Persiste el reporte de investigación en pgvector. 
    Argumentos:
        version: El código de la versión de Oracle (ej: '26A').
        reporte: El objeto estructurado con impactos y apis_deprecadas.
    """
    logger.info(f"[DB]---> Guardando datos para la versión {version}")
    
    try:
        conn = get_conn()
        cur = conn.cursor()
        embeddings_model = get_embeddings_model()
        
        all_records = []
        
        logger.info(f"[DB]---> Guardando datos de impactos {len(reporte.impactos)} para la versión {version}")
        i=1
        # Como 'reporte' ya es un objeto ReporteInvestigacion, accedemos directo:
        for imp in reporte.impactos:
            # Generamos texto para E5
            txt = f"passage: Módulo: {imp.Module}. Feature: {imp.Feature}. Impacto: {imp.Impact_to_Existing_Processes}"
            vector = embeddings_model.embed_query(txt)
            print(f"Impacto {i}")
            # Guardamos imp.model_dump_json() para mantener la integridad en JSONB
            all_records.append((version, 'impacto', imp.Module, txt, imp.model_dump_json(), vector))
            i=i+1
        
        logger.info(f"[DB]---> Guardando datos de apis_deprecadas {len(reporte.impacapis_deprecadastos)} para la versión {version}")
        
        for api in reporte.apis_deprecadas:
            txt = f"passage: API Deprecada en {api.Module}: {api.Deprecated_Resource}. Reemplazo: {api.Replacement_Resource}"
            vector = embeddings_model.embed_query(txt)
            all_records.append((version, 'api', api.Module, txt, api.model_dump_json(), vector))

        logger.info(f"[DB]---> Insertando datos en la tabla oracle_knowledge_vectors")
        # Inserción masiva
        execute_values(cur, """
            INSERT INTO oracle_knowledge_vectors (version_id, tipo_dato, modulo, content_text, full_json, embedding)
            VALUES %s""", all_records)

        logger.info(f"[DB]---> Actualizando estado de la versión {version}")
        # Actualizamos estado de la versión
        cur.execute("UPDATE oracle_versions SET status = 'COMPLETED' WHERE version_id = %s", (version,))
        conn.commit()
        
        return f"Éxito: Se han persistido {len(all_records)} registros para la versión {version}."

    except Exception as e:
        conn.rollback()
        logger.error(f"Error en persistencia: {str(e)}")
        return f"Error crítico: {str(e)}"
    finally:
        cur.close()
        conn.close()
'''

def tool_guardar_en_pgvector(version: str, reporte: ReporteInvestigacion) -> str:
    logger.info(f"[DB]---> Guardando datos para la versión {version}")

    try:
        embeddings_model = get_embeddings_model()
        all_records = []

        # Generar embeddings concurrentemente
        tareas = []
        with ThreadPoolExecutor(max_workers=20) as executor:
            for imp in reporte.impactos:
                txt = f"passage: Módulo: {imp.Module}. Feature: {imp.Feature}. Impacto: {imp.Impact_to_Existing_Processes}"
                tareas.append(executor.submit(
                    lambda i=imp, t=txt: (
                        version, "impacto", i.Module, t, i.model_dump_json(), embeddings_model.embed_query(t)
                    )
                ))

            for api in reporte.apis_deprecadas:
                txt = f"passage: API Deprecada en {api.Module}: {api.Deprecated_Resource}. Reemplazo: {api.Replacement_Resource}"
                tareas.append(executor.submit(
                    lambda a=api, t=txt: (
                        version, "api", a.Module, t, a.model_dump_json(), embeddings_model.embed_query(t)
                    )
                ))

            all_records = [future.result() for future in as_completed(tareas)]

        # Inserción en DB
        with get_conn() as conn:
            with conn.cursor() as cur:
                execute_values(cur, """
                    INSERT INTO oracle_knowledge_vectors 
                    (version_id, tipo_dato, modulo, content_text, full_json, embedding)
                    VALUES %s""", all_records)

                cur.execute(
                    "UPDATE oracle_versions SET status = 'COMPLETED' WHERE version_id = %s",
                    (version,)
                )
            conn.commit()

        return f"Éxito: Se han persistido {len(all_records)} registros para la versión {version}."

    except Exception as e:
        logger.error(f"Error en persistencia: {str(e)}")
        return f"Error crítico: {str(e)}"


def tool_obtener_datos_completos(version: str) -> dict:
    """
    Recupera todos los registros de una versión desde pgvector para el Redactor.
    """

    logger.info("[Redactor] ---> Recuperando datos desde pgvector")

    conn = get_conn()
    cur = conn.cursor()

    impactos = []
    apis = []
    impactos_por_modulo = {}

    try:

        logger.info(f"[Redactor] ---> Buscando registros de la versión {version}")

        cur.execute("""
            SELECT tipo_dato, full_json
            FROM oracle_knowledge_vectors
            WHERE version_id = %s
        """, (version,))

        # iteración directa sobre el cursor (más eficiente que fetchall)
        logger.info(f"[Redactor] ---> Inicio de la iteración")
        for tipo, json_data in cur:

            if tipo == "impacto":

                impactos.append(json_data)

                # conteo por módulo (si existe)
                modulo = json_data.get("modulo", "General")
                impactos_por_modulo[modulo] = impactos_por_modulo.get(modulo, 0) + 1

            else:

                apis.append(json_data)

        resultado = {
            
            "impactos": impactos,
            "apis_deprecadas": apis,
            # campos requeridos por ReporteInvestigacion
            "plan_accion": [],
            "proximos_pasos": [],
            "servicios_soporte": []
        }

        logger.info(f"[Redactor] ---> Registros recuperados: {len(impactos)} impactos / {len(apis)} APIs")

        return resultado

    except Exception as e:import json


@tool
def tool_verificar_y_esperar_version(version: str) -> str:
    """
    Verifica disponibilidad de la versión en pgvector. 
    Retorna: 'SOLICITAR_INVESTIGACION', 'DATA_LISTA' o 'ESPERAR_COLA'.
    """
    version_id = version.upper().strip()
    conn = get_conn()
    cur = conn.cursor()
    logger.info(f"[Analista]---> Verificar y esperar la versión {version_id}")
    try:
        # 1. Consultar estado actual
        cur.execute("SELECT status FROM oracle_versions WHERE version_id = %s", (version_id,))
        row = cur.fetchone()
        
        # CASO 1: No existe en la DB -> Bloqueamos y pedimos investigar
        if not row:
            cur.execute(
                "INSERT INTO oracle_versions (version_id, status) VALUES (%s, 'PENDING')", 
                (version_id,)
            )
            conn.commit()
            return "SOLICITAR_INVESTIGACION"
        
        # CASO 2: Existe. Evaluamos el estado guardado en la primera columna
        status = row[0] 
        print(f"ℹ️ Version {version_id} status: {status}")
        if status == 'COMPLETED':
            return "DATA_LISTA"
            
        if status == 'PENDING':
            return "ESPERAR_COLA"
        
            
        # Si está en 'failed' o cualquier otro, permitimos reintentar
        cur.execute("UPDATE oracle_versions SET status = 'PENDING' WHERE version_id = %s", (version_id,))
        conn.commit()
        return "SOLICITAR_INVESTIGACION"

    except Exception as e:
        conn.rollback()
        logger.error(f"[Analista]---> Error técnico en tool_verificar_y_esperar_version: {str(e)}")
        return f"ERROR_TECNICO: {str(e)}"
    finally:
        cur.close()
        conn.close()


@tool
def tool_generar_pdf_ejecutivo(version: str, config: Annotated[RunnableConfig, "config"]) -> str:
    """
    Genera el PDF segmentado por páginas. 
    FRAGMENTACIÓN FORZADA: Divide celdas gigantes en filas múltiples para evitar LayoutErrors.
    """
    logger.info(f"[Redactor] --->Obteniendo datos para la versión {version}")
    datos = tool_obtener_datos_completos(version)
    reporte = ReporteInvestigacion(**datos)
    
    logger.info(f"[Redactor] ---> Generando PDF para la versión {version}")
    
    # 1. Extraer el thread_id de la configuración de la sesión
    thread_id = config["configurable"].get("thread_id", "default_session")
    ruta_pdf = f"./reports/reporte_{thread_id}.pdf"
    os.makedirs("./reports", exist_ok=True)

    doc = ReporteCorporativo(ruta_pdf, version, pagesize=letter,
                            leftMargin=0.5*inch, rightMargin=0.5*inch,
                            topMargin=1.6*inch, bottomMargin=0.8*inch)
    elementos = []
    
    # --- ESTILOS ---
    style_h = ParagraphStyle('TableHeader', fontSize=8, fontName='Helvetica-Bold', textColor=colors.white, alignment=TA_CENTER)
    style_n = ParagraphStyle('TableText', fontSize=7, leading=8.5, textColor=GRIS_TEXTO, alignment=TA_LEFT)
    style_sub = ParagraphStyle('ST', fontSize=14, textColor=AZUL_CONDOR, fontName='Helvetica-Bold', spaceBefore=12, spaceAfter=10)

    # --- PÁGINA 1: PORTADA Y OBJETIVO ---
    elementos.append(Paragraph("RESUMEN EJECUTIVO", ParagraphStyle('T', fontSize=22, textColor=AZUL_CONDOR, fontName='Helvetica-Bold')))
    elementos.append(Paragraph(f"Actualización Oracle ERP Cloud {version}", ParagraphStyle('V', fontSize=14, textColor=colors.HexColor("#4472C4"), spaceAfter=20)))
    elementos.append(Paragraph("1. OBJETIVO DEL DOCUMENTO", style_sub))
    elementos.append(Paragraph("Análisis estratégico de impactos técnicos y funcionales.", style_n))
    elementos.append(PageBreak())

    # --- PÁGINA 2: APIs DEPRECADAS ---
    elementos.append(Paragraph("2. ACCIÓN REQUERIDA: APIs DEPRECADAS", style_sub))
    data_api = [[Paragraph("Módulo", style_h), Paragraph("Recurso", style_h), Paragraph("Reemplazo / Path", style_h), Paragraph("Prioridad", style_h)]]
    for api in reporte.apis_deprecadas:
        data_api.append([Paragraph(api.Module, style_n), Paragraph(api.Deprecated_Resource, style_n), Paragraph(f"{api.Replacement_Resource}<br/>{api.Replacement_Resource_Paths}", style_n), Paragraph("ALTA", style_n)])
    
    t_api = Table(data_api, colWidths=[1*inch, 2*inch, 3.5*inch, 1*inch], repeatRows=1)
    t_api.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), AZUL_CONDOR), ('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    elementos.append(t_api)
    elementos.append(PageBreak())

    # --- PÁGINA 3 EN ADELANTE: IMPACTO (SOLUCIÓN AL ERROR 726 PTS) ---
    elementos.append(Paragraph("3. ANÁLISIS DE IMPACTO FUNCIONAL", style_sub))
    data_imp = [[Paragraph("Módulo", style_h), Paragraph("Funcionalidad", style_h), Paragraph("Impacto", style_h), Paragraph("Acción", style_h)]]

    # MOTOR DE FRAGMENTACIÓN: Si el texto > 450 chars, creamos filas nuevas para el mismo impacto
    LIMITE = 450 
    for imp in reporte.impactos:
        texto = imp.Impact_to_Existing_Processes
        if len(texto) > LIMITE:
            fragmentos = [texto[i:i+LIMITE] for i in range(0, len(texto), LIMITE)]
            for i, parte in enumerate(fragmentos):
                data_imp.append([
                    Paragraph(imp.Module if i==0 else "", style_n),
                    Paragraph(imp.Feature if i==0 else "(Cont.)", style_n),
                    Paragraph(parte, style_n),
                    Paragraph(imp.Action_to_Enable if i==0 else "", style_n)
                ])
        else:
            data_imp.append([Paragraph(imp.Module, style_n), Paragraph(imp.Feature, style_n), Paragraph(imp.Impact_to_Existing_Processes, style_n), Paragraph(imp.Action_to_Enable, style_n)])

    t_imp = Table(data_imp, colWidths=[1*inch, 1.4*inch, 3.5*inch, 1.6*inch], repeatRows=1)
    t_imp.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), AZUL_CONDOR), ('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('VALIGN', (0,0), (-1,-1), 'TOP'), ('ROWBACKGROUNDS', (0, 1), (-1, -1), [colors.white, GRIS_FILA])]))
    t_imp.splitByRow = True # Permite que la tabla fluya entre múltiples páginas
    elementos.append(t_imp)

    # --- SECCIONES FINALES (PLAN, PASOS, SOPORTE) ---
    elementos.append(PageBreak())
    
     # --- 4. RESUMEN NUMÉRICO DE IMPACTO (Los números grandes de la imagen) ---
    elementos.append(Spacer(1, 0.4*inch))
    impactos_lista = reporte.impactos
    total_items = len(impactos_lista) if len(impactos_lista) > 0 else 1
    
    sin_total = count_impacts(["None", "Sin Impacto", "No impact"], impactos_lista)
    peq_total = count_impacts(["Small scale", "Impacto Pequeño", "Escala Pequeña"], impactos_lista)
    may_total = count_impacts(["Large scale", "Impacto Mayor", "Impacto Crítico"], impactos_lista)

    # Validación para evitar división por cero si la lista está vacía
    divisor = total_items if total_items > 0 else 1
    sin_pct = round((sin_total / divisor) * 100)
    peq_pct = round((peq_total / divisor) * 100)
    may_pct = round((may_total / divisor) * 100)

    # Simulamos el diseño de números grandes de la imagen
    ##style_num = ParagraphStyle('Num', fontSize=28, alignment=TA_CENTER, fontName='Helvetica-Bold')
    ##style_label = ParagraphStyle('Label', fontSize=9, alignment=TA_CENTER, textColor=GRIS_TEXTO)  

    
    style_num = ParagraphStyle(
        'Num', 
        fontSize=32,          # Tamaño grande para los números
        leading=38,           # Espacio vertical para que no se encime el texto de abajo
        alignment=TA_CENTER, 
        fontName='Helvetica-Bold'
    )

    style_label = ParagraphStyle(
        'Label', 
        fontSize=9, 
        leading=11, 
        alignment=TA_CENTER, 
        textColor=GRIS_TEXTO
    )

    """data_impactos = [
        [Paragraph("<font color='#4D4D4D'>210</font>", style_num), 
         Paragraph("<font color='#FFB300'>145</font>", style_num), 
         Paragraph("<font color='#FF0000'>21</font>", style_num)],
        [Paragraph("Sin Impacto (56%)", style_label), 
         Paragraph("Impacto Pequeño (38%)", style_label), 
         Paragraph("Impacto Mayor (6%)", style_label)]
    ]
    t_impactos = Table(data_impactos, colWidths=[2.5*inch]*3)
    elementos.append(t_impactos)
    """
    
    data_impactos = [
    [Paragraph(f"<font color='#4D4D4D'>{sin_total}</font>", style_num), 
     Paragraph(f"<font color='#FFB300'>{peq_total}</font>", style_num), 
     Paragraph(f"<font color='#FF0000'>{may_total}</font>", style_num)],
    [Paragraph(f"Sin Impacto ({sin_pct}%)", style_label), 
     Paragraph(f"Impacto Pequeño ({peq_pct}%)", style_label), 
     Paragraph(f"Impacto Mayor ({may_pct}%)", style_label)]
    ]
    t_impactos = Table(data_impactos, colWidths=[2.5*inch]*3)

    t_impactos.setStyle(TableStyle([
        ('VALIGN', (0, 0), (-1, -1), 'TOP'),      # Alinea todo al tope de la celda
        ('BOTTOMPADDING', (0, 0), (-1, 0), 10),   # Empuja el texto de abajo 10 pts hacia abajo
        ('TOPPADDING', (0, 1), (-1, 1), 0),       # Quita espacio extra en la fila de etiquetas
        ('ALIGN', (0, 0), (-1, -1), 'CENTER'),
    ]))
    
    elementos.append(t_impactos)
    
    # --- 5. PLAN DE ACCIÓN PROPUESTO (Pág 4) ---
    elementos.append(Paragraph("5. PLAN DE ACCIÓN PROPUESTO", style_sub))
    data_plan = [[Paragraph("Fase", style_h), Paragraph("Periodo", style_h), Paragraph("Actividades Clave", style_h), Paragraph("Responsable", style_h)]]
    
    fases = [
        ["Fase 1", "Semanas 1-2", "Auditoría de APIs deprecadas", "Arquitecto de Integraciones"],
        ["Fase 2", "Semanas 3-6", "Plan de migración y priorización", "Líder de Proyecto"],
        ["Fase 3", "Semanas 7-14", "Implementación features de alto valor", "Equipo Funcional"],
        ["Fase 4", "Semanas 15-30", "Migración completa y adopción", "Equipo Técnico"]
    ]
    for f in fases:
        data_plan.append([Paragraph(cell, style_n) for cell in f])

    t_plan = Table(data_plan, colWidths=[0.7*inch, 1.1*inch, 3.4*inch, 2.3*inch])
    t_plan.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), AZUL_CONDOR), ('GRID', (0,0), (-1,-1), 0.5, colors.grey), ('VALIGN', (0,0), (-1,-1), 'TOP')]))
    elementos.append(t_plan)

    # --- 6. PRÓXIMOS PASOS (Cuadro Azul Claro de la imagen) ---
    elementos.append(Spacer(1, 0.5*inch))
    data_box = [[Paragraph("<b>Próximos Pasos Inmediatos</b>", ParagraphStyle('P', textColor=AZUL_CONDOR, fontSize=11))]]
    pasos = ["Realizar inventario completo de integraciones", "Programar taller de análisis de impacto", "Establecer gobierno del cambio"]
    for p in pasos:
        data_box.append([Paragraph(f"• {p}", style_n)])
    
    t_box = Table(data_box, colWidths=[7.5*inch])
    t_box.setStyle(TableStyle([
        ('BACKGROUND', (0,0), (-1,-1), colors.HexColor("#F0F7FF")),
        ('BOX', (0,0), (-1,-1), 0.5, colors.HexColor("#D0E4F5")),
        ('LEFTPADDING', (0,0), (-1,-1), 15),
        ('TOPPADDING', (0,0), (-1,-1), 10),
        ('BOTTOMPADDING', (0,0), (-1,-1), 10)
    ]))
    elementos.append(t_box)

    # --- 7. SOPORTE ESPECIALIZADO ---
    elementos.append(Paragraph("7. SOPORTE ESPECIALIZADO", style_sub))
    data_sop = [[Paragraph("Servicio", style_h), Paragraph("Descripción", style_h)]]
    for s in reporte.servicios_soporte:
        data_sop.append([Paragraph(s.servicio, style_n), Paragraph(s.descripcion, style_n)])
    
    t_sop = Table(data_sop, colWidths=[2*inch, 5.5*inch])
    t_sop.setStyle(TableStyle([('BACKGROUND', (0,0), (-1,0), AZUL_CONDOR), ('GRID', (0,0), (-1,-1), 0.5, colors.grey)]))
    elementos.append(t_sop)

    doc.build(elementos)
    return f"Reporte ejecutivo de Oracle Cloud {version} generado correctamente en {ruta_pdf}"   