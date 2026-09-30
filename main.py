import io
import json
import math
import fitz

from fastapi import FastAPI, File, Form, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response

app = FastAPI(title="PDF Editor Pro Backend", version="2.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)

FONTES = {
    ("Arial", "normal", "normal"): "helv",
    ("Arial", "bold", "normal"): "hebo",
    ("Arial", "normal", "italic"): "heit",
    ("Arial", "bold", "italic"): "hebi",

    ("Times New Roman", "normal", "normal"): "tiro",
    ("Times New Roman", "bold", "normal"): "tibo",
    ("Times New Roman", "normal", "italic"): "tiit",
    ("Times New Roman", "bold", "italic"): "tibi",

    ("Courier New", "normal", "normal"): "cour",
    ("Courier New", "bold", "normal"): "cobo",
    ("Courier New", "normal", "italic"): "coit",
    ("Courier New", "bold", "italic"): "cobi",
}

def converter_cor(valor):
    valor = (valor or "#000000").replace("#", "")
    if len(valor) != 6:
        return (0, 0, 0)
    return (
        int(valor[0:2], 16) / 255,
        int(valor[2:4], 16) / 255,
        int(valor[4:6], 16) / 255,
    )

def escolher_fonte(edicao):
    familia = edicao.get("fontFamily") or "Arial"
    peso = "bold" if edicao.get("fontWeight") == "bold" else "normal"
    estilo = "italic" if edicao.get("fontStyle") == "italic" else "normal"
    return FONTES.get((familia, peso, estilo), "helv")

def rect_normalizado(pagina, area):
    rect = pagina.rect
    x = rect.x0 + float(area.get("x", 0)) * rect.width
    y = rect.y0 + float(area.get("y", 0)) * rect.height
    w = max(0.5, float(area.get("w", 0)) * rect.width)
    h = max(0.5, float(area.get("h", 0)) * rect.height)
    return fitz.Rect(x, y, x + w, y + h)

def posicao_texto(pagina, edicao):
    rect = pagina.rect
    x = rect.x0 + float(edicao.get("left", 0)) * rect.width
    y = rect.y0 + float(edicao.get("top", 0)) * rect.height
    tamanho = max(4.0, float(edicao.get("fontSize", 0.02)) * rect.height)
    return x, y, tamanho

def centro(rect):
    return ((rect.x0 + rect.x1) / 2.0, (rect.y0 + rect.y1) / 2.0)

def distancia2(a, b):
    return (a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2

def encontrar_area_texto(pagina, alteracao):
    """
    Tenta localizar o texto ORIGINAL pelo conteúdo real do PDF.
    Se houver múltiplas ocorrências, usa a mais próxima da região clicada.
    Se a busca falhar, cai para a área visual enviada pelo editor.
    """
    original_text = str(alteracao.get("originalText") or "").strip()
    original_rect_data = alteracao.get("originalRect")

    alvo_visual = None
    if original_rect_data:
        alvo_visual = rect_normalizado(pagina, original_rect_data)

    if original_text:
        try:
            encontrados = pagina.search_for(original_text)
        except Exception:
            encontrados = []

        if encontrados:
            if alvo_visual is None:
                return encontrados[0]

            c_alvo = centro(alvo_visual)
            return min(encontrados, key=lambda r: distancia2(centro(r), c_alvo))

    return alvo_visual

@app.get("/")
def health():
    return {
        "ok": True,
        "servico": "PDF Editor Pro",
        "versao": "2.0.0",
    }

@app.post("/edit")
async def editar_pdf(
    file: UploadFile = File(...),
    edits: str = Form(...),
):
    try:
        alteracoes = json.loads(edits)
        if not isinstance(alteracoes, list):
            raise ValueError("As alterações precisam ser uma lista.")
    except Exception as erro:
        raise HTTPException(
            status_code=400,
            detail=f"Alterações inválidas: {erro}",
        )

    pdf = await file.read()
    if not pdf:
        raise HTTPException(status_code=400, detail="O PDF está vazio.")

    try:
        documento = fitz.open(stream=pdf, filetype="pdf")
    except Exception as erro:
        raise HTTPException(
            status_code=400,
            detail=f"Não foi possível abrir o PDF: {erro}",
        )

    # 1) Remove o texto original com base no texto REAL do PDF.
    substituicoes = {}

    for alteracao in alteracoes:
        if alteracao.get("kind") != "replacement":
            continue

        numero_pagina = int(alteracao.get("page", 1)) - 1
        if numero_pagina < 0 or numero_pagina >= len(documento):
            continue

        substituicoes.setdefault(numero_pagina, []).append(alteracao)

    for numero_pagina, lista in substituicoes.items():
        pagina = documento[numero_pagina]
        areas = []

        for alteracao in lista:
            area = encontrar_area_texto(pagina, alteracao)
            if area is None:
                continue

            # Margem mínima para garantir remoção completa das bordas dos glifos.
            area = fitz.Rect(
                max(pagina.rect.x0, area.x0 - 0.8),
                max(pagina.rect.y0, area.y0 - 0.8),
                min(pagina.rect.x1, area.x1 + 0.8),
                min(pagina.rect.y1, area.y1 + 0.8),
            )
            areas.append(area)

            # fill=None = sem retângulo branco.
            pagina.add_redact_annot(
                area,
                fill=None,
                cross_out=False,
            )

        if areas:
            # Remove texto, preserva imagens e gráficos/vetores.
            pagina.apply_redactions(
                images=0,
                graphics=0,
                text=0,
            )

    # 2) Insere o novo texto.
    for alteracao in alteracoes:
        if alteracao.get("kind") not in ("text", "replacement"):
            continue

        numero_pagina = int(alteracao.get("page", 1)) - 1
        if numero_pagina < 0 or numero_pagina >= len(documento):
            continue

        pagina = documento[numero_pagina]
        x, top, tamanho = posicao_texto(pagina, alteracao)

        cor = converter_cor(alteracao.get("fill", "#000000"))
        fonte = escolher_fonte(alteracao)
        texto = str(alteracao.get("text", ""))

        # Fabric usa top; PyMuPDF insere na baseline.
        baseline = top + tamanho * 0.86
        altura_linha = tamanho * 1.18
        linhas = texto.splitlines() or [""]

        for indice, linha in enumerate(linhas):
            pagina.insert_text(
                fitz.Point(
                    x,
                    baseline + indice * altura_linha,
                ),
                linha,
                fontsize=tamanho,
                fontname=fonte,
                color=cor,
                overlay=True,
            )

    saida = io.BytesIO()
    documento.save(
        saida,
        garbage=4,
        deflate=True,
        clean=True,
    )
    documento.close()

    return Response(
        content=saida.getvalue(),
        media_type="application/pdf",
        headers={
            "Content-Disposition": 'attachment; filename="PDF_EDITADO.pdf"'
        },
    )
